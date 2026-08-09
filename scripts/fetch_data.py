"""
台股加速股篩選 - 每月資料抓取腳本

篩選邏輯：累計YoY > 0 且 當月YoY > 1.3 × 累計YoY → 判定為「加速股」
連續加速：往回追歷史存檔，計算該公司連續幾個月都在加速名單內

輸出：
  data/latest.json           前端讀取用（最新一期）
  data/history/{年月}.json   歷史累積，連續加速月數靠這批檔案回推

資料源（皆免費、免金鑰）：
  上市月營收   TWSE  /v1/opendata/t187ap05_L
  上櫃月營收   TPEx  /openapi/v1/mopsfin_t187ap05_O
  上市股本     TWSE  /v1/opendata/t187ap03_L
  上市收盤價   TWSE  /v1/exchangeReport/STOCK_DAY_ALL
  上櫃行情     TPEx  /openapi/v1/tpex_mainboard_daily_close_quotes（收盤價與發行股數同一支）
"""

import requests
import json
import datetime
import os
import re
import glob

# ---------- 資料源 ----------
TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
TWSE_CAPITAL_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_PRICE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

OUTPUT_DIR = "data"
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")

ACCEL_MULTIPLIER = 1.3
MIN_CUM_YOY = 0


# ==================== 共用工具 ====================

def fetch_json(url: str, label: str):
    try:
        resp = requests.get(url, timeout=40, headers={"User-Agent": "revenue-accel-scanner/1.0"})
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        if isinstance(data, dict):
            data = [data]
        print(f"[OK] {label}: {len(data)} 筆")
        if data:
            print(f"     欄位範例: {list(data[0].keys())[:8]}")
        return data
    except Exception as e:
        print(f"[WARN] {label} 抓取失敗：{e}")
        return []


def pick(row: dict, *candidates):
    """
    欄位名稱容錯：依序嘗試多個候選欄位名，找到就回傳。
    交易所偶爾會改欄名或夾帶空白，這層可以避免整支腳本掛掉。
    """
    for c in candidates:
        if c in row and row[c] not in (None, "", "-"):
            return row[c]
    # 再做一次寬鬆比對（去空白）
    norm = {re.sub(r"\s+", "", str(k)): v for k, v in row.items()}
    for c in candidates:
        key = re.sub(r"\s+", "", c)
        if key in norm and norm[key] not in (None, "", "-"):
            return norm[key]
    return None


def to_float(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_par_value(s):
    """
    解析每股面額。TWSE 回傳格式類似「新台幣 10.0000元」，取出數字部分。
    無面額股回傳 None，該公司改為跳過而非硬套 10 元。
    """
    if not s:
        return None
    txt = str(s)
    if "無面額" in txt:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", txt)
    if not m:
        return None
    val = float(m.group(1))
    return val if val > 0 else None


def ym_key(data_ym) -> str:
    """統一成6碼可排序字串。'11507' -> '011507'"""
    digits = re.sub(r"\D", "", str(data_ym or ""))
    return digits.zfill(6)


def prev_ym(key: str) -> str:
    y, m = int(key[:4]), int(key[4:])
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f"{y:04d}{m:02d}"


# ==================== 月營收 ====================

def normalize_revenue(raw: list, market: str) -> list:
    """上市與上櫃的欄位命名可能不同，統一在這裡吸收差異"""
    out = []
    for row in raw:
        code = pick(row, "公司代號", "SecuritiesCompanyCode", "CompanyCode")
        cur_yoy = to_float(pick(row, "營業收入-去年同月增減(%)", "營業收入-去年同月增減％"))
        cum_yoy = to_float(pick(row, "累計營業收入-前期比較增減(%)", "累計營業收入-前期比較增減％"))
        if code is None or cur_yoy is None or cum_yoy is None:
            continue
        out.append({
            "code": str(code).strip(),
            "name": pick(row, "公司名稱", "CompanyName"),
            "market": market,
            "industry": pick(row, "產業別", "Industry") or "未分類",
            "data_ym": pick(row, "資料年月", "DataYearMonth"),
            "cur_revenue_k": to_float(pick(row, "營業收入-當月營收")),
            "cum_revenue_k": to_float(pick(row, "累計營業收入-當月累計營收")),
