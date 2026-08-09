"""
一次性歷史回補腳本 —— 補齊民國115年（2026）1月起的歷史檔

背景：
  TWSE / TPEx OpenAPI 只提供「最新一期」快照，歷史無法回補，
  所以要讓「連續加速月數」馬上有意義，必須另外找有歷史深度的資料源。

做法：
  用 FinMind 的 TaiwanStockMonthRevenue 取得原始月營收（含歷史），
  自行計算當月年增率與累計年增率，再套用同一套加速股篩選邏輯，
  產出 data/history/011501.json ~ 011507.json。
  之後每月的 fetch_data.py 就會接續往下累積。

FinMind 只回傳原始營收金額，年增率是本腳本自己算的：
  當月YoY = 本年當月營收 / 去年同月營收 - 1
  累計YoY = 本年1~M月營收合計 / 去年1~M月營收合計 - 1

使用前置作業：
  1. 到 https://finmindtrade.com/ 免費註冊、驗證信箱，取得 API token
  2. 設定環境變數：export FINMIND_TOKEN="你的token"
  3. pip install requests
  4. python backfill_history.py

⚠️ 三個必須知道的限制（詳見 README）：
  1. 回補的年增率是本腳本自行計算，可能與 MOPS 公告數字有小數點級距差異
     （公司若曾追溯調整或併購重編，官方會用重編後基期，原始資料看不出來）
  2. 回補月份的「市值 / 規模級距」用的是今天的股價，不是當月的
  3. FinMind 免費額度為每小時 600 次請求，逐檔抓取時腳本會自動節流
"""

import requests
import json
import os
import time
import datetime

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"   # 上櫃月營收（已查證）

OUTPUT_DIR = "data"
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")

# 回補範圍：西元年月。BASE_YEAR 是計算年增率需要的去年基期
TARGET_YEAR = 2026          # 民國115年
BASE_YEAR = TARGET_YEAR - 1
TARGET_MONTHS = range(1, 13)   # 1~12月，資料不足的月份會自動跳過

ACCEL_MULTIPLIER = 1.3
MIN_CUM_YOY = 0

RATE_LIMIT_SLEEP = 6.5   # 秒/次，換算約 550 次/小時，留安全邊際


# ==================== 共用 ====================

def to_float(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def roc_key(year_ad: int, month: int) -> str:
    """西元年月 -> 6碼民國年月鍵值。2026/07 -> '011507'"""
    return f"{year_ad - 1911:04d}{month:02d}"


def fetch_json(url, label, **kwargs):
    try:
        resp = requests.get(url, timeout=40, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[WARN] {label} 失敗：{e}")
        return None


# ==================== 建立公司清單與產業別 ====================

def build_universe() -> dict:
    """
    從 TWSE/TPEx OpenAPI 當期快照取得公司代號、名稱、產業別、市場別。
    這些是「屬性」不是「時序資料」，用當期快照回填歷史是合理的。
    """
    universe = {}
    for url, market, label in [
        (TWSE_REVENUE_URL, "上市", "TWSE"),
        (TPEX_REVENUE_URL, "上櫃", "TPEx"),
    ]:
        raw = fetch_json(url, f"{label}公司清單") or []
        for row in raw:
            code = row.get("公司代號") or row.get("SecuritiesCompanyCode")
            if code:
                code = str(code).strip()
                universe[code] = {
                    "code": code,
                    "name": row.get("公司名稱") or row.get("CompanyName"),
                    "market": market,
                    "industry": row.get("產業別") or row.get("Industry") or "未分類",
                }
        print(f"[OK] {label}：{len(raw)} 家")
    return universe


# ==================== FinMind 月營收 ====================

def finmind_get(params: dict):
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
    try:
        resp = requests.get(FINMIND_URL, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("data", [])
    except Exception as e:
        print(f"[WARN] FinMind 請求失敗：{e}")
        return None


def fetch_revenue_bulk(start_date: str, end_date: str):
    """
    先試「不指定 data_id」的整批抓取。
    ⚠️ FinMind 部分資料集支援整批、部分必須逐檔，這裡試了才知道，
       失敗就自動退回逐檔模式。
    """
    print("嘗試整批抓取月營收…")
    data = finmind_get({
        "dataset": "TaiwanStockMonthRevenue",
        "start_date": start_date,
        "end_date": end_date,
    })
    if data:
        print(f"[OK] 整批抓取成功：{len(data)} 筆")
        return data
    print("[INFO] 整批抓取無資料，改用逐檔模式")
    return None


def fetch_revenue_per_stock(codes: list, start_date: str, end_date: str):
    """逐檔抓取，含節流。2000 檔約需 3.5 小時，建議在本機掛著跑。"""
    all_rows = []
    total = len(codes)
    print(f"逐檔抓取 {total} 檔，預估耗時 {total * RATE_LIMIT_SLEEP / 3600:.1f} 小時")
    for i, code in enumerate(codes, 1):
        data = finmind_get({
            "dataset": "TaiwanStockMonthRevenue",
            "data_id": code,
            "start_date": start_date,
            "end_date": end_date,
        })
        if data:
            all_rows.extend(data)
        if i % 50 == 0:
            print(f"  進度 {i}/{total}（已收集 {len(all_rows)} 筆）")
        time.sleep(RATE_LIMIT_SLEEP)
    return all_rows


def build_revenue_map(rows: list) -> dict:
    """
    整理成 {股票代號: {(年, 月): 營收}}
    FinMind 欄位：stock_id / revenue / revenue_year / revenue_month
    """
    m = {}
    for r in rows:
        code = r.get("stock_id")
        y = r.get("revenue_year")
        mo = r.get("revenue_month")
        rev = to_float(r.get("revenue"))
        if code and y and mo and rev is not None:
            m.setdefault(code, {})[(int(y), int(mo))] = rev
    return m


# ==================== 年增率計算與篩選 ====================

def compute_month_row(code: str, meta: dict, rev_map: dict, year: int, month: int):
    """算出某公司某月的當月YoY與累計YoY。基期資料不齊就回傳 None（不臆測補值）"""
    cur = rev_map.get((year, month))
    base = rev_map.get((BASE_YEAR, month))
    if cur is None or not base:
        return None

    cur_sum, base_sum = 0.0, 0.0
    for mo in range(1, month + 1):
        c = rev_map.get((year, mo))
        b = rev_map.get((BASE_YEAR, mo))
        if c is None or b is None:
            return None      # 累計期間有缺口，該月不計算
        cur_sum += c
        base_sum += b
    if base_sum == 0:
        return None

    return {
        "code": code,
        "name": meta.get("name"),
        "market": meta.get("market"),
        "industry": meta.get("industry"),
        "data_ym": f"{year - 1911}{month:02d}",
        "cur_revenue_k": round(cur / 1000, 0),      # FinMind 單位為元，轉千元對齊主腳本
        "cum_revenue_k": round(cur_sum / 1000, 0),
        "cur_yoy": round((cur / base - 1) * 100, 2),
        "cum_yoy": round((cur_sum / base_sum - 1) * 100, 2),
    }


def screen_accelerating(rows: list) -> list:
    result = []
    for r in rows:
        if r["cum_yoy"] > MIN_CUM_YOY and r["cur_yoy"] > ACCEL_MULTIPLIER * r["cum_yoy"]:
            r2 = dict(r)
            r2["accel_ratio"] = round(r["cur_yoy"] / r["cum_yoy"], 2)
            result.append(r2)
    result.sort(key=lambda x: x["accel_ratio"], reverse=True)
    return result


# ==================== 主流程 ====================

def main():
    if not FINMIND_TOKEN:
        print("[提醒] 未設定 FINMIND_TOKEN，將以匿名額度執行（每小時 300 次，容易中斷）")
        print("       建議先到 https://finmindtrade.com/ 免費註冊取得 token\n")

    os.makedirs(HISTORY_DIR, exist_ok=True)

    universe = build_universe()
    if not universe:
        print("[ERROR] 取不到公司清單，中止")
        return

    start_date = f"{BASE_YEAR}-01-01"
    end_date = f"{TARGET_YEAR}-12-31"

    rows = fetch_revenue_bulk(start_date, end_date)
    if rows is None:
        rows = fetch_revenue_per_stock(sorted(universe.keys()), start_date, end_date)
    if not rows:
        print("[ERROR] 取不到任何月營收資料，中止")
        return

    rev_by_code = build_revenue_map(rows)
    print(f"整理完成：{len(rev_by_code)} 檔有月營收資料\n")

    prev_codes = set()      # 上一個月的加速股名單，用來累計 streak
    prev_streaks = {}
    written = 0

    for month in TARGET_MONTHS:
        month_rows = []
        for code, meta in universe.items():
            rm = rev_by_code.get(code)
            if not rm:
                continue
            row = compute_month_row(code, meta, rm, TARGET_YEAR, month)
            if row:
                month_rows.append(row)

        if len(month_rows) < 100:
            print(f"{TARGET_YEAR}/{month:02d}：僅 {len(month_rows)} 家有完整資料，判定該月尚未公布，停止回補")
            break

        accelerating = screen_accelerating(month_rows)

        # 連續加速：前一月也在名單內就累加
        streaks = {}
        for r in accelerating:
            r["streak"] = prev_streaks.get(r["code"], 0) + 1 if r["code"] in prev_codes else 1
            streaks[r["code"]] = r["streak"]

        key = roc_key(TARGET_YEAR, month)
        payload = {
            "data_ym": f"{TARGET_YEAR - 1911}{month:02d}",
            "ym_key": key,
            "updated_at": datetime.date.today().isoformat(),
            "count_all": len(month_rows),
            "count_accelerating": len(accelerating),
            "count_streak_2plus": sum(1 for r in accelerating if r["streak"] >= 2),
            "history_months": month,
            "backfilled": True,      # 標記為回補資料，與正式抓取區分
            "accelerating": accelerating,
        }
        with open(os.path.join(HISTORY_DIR, f"{key}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"{TARGET_YEAR}/{month:02d}　全市場 {len(month_rows):>4} 家　"
              f"加速股 {len(accelerating):>3} 家　連2月以上 {payload['count_streak_2plus']:>3} 家")

        prev_codes = {r["code"] for r in accelerating}
        prev_streaks = streaks
        written += 1

    print(f"\n回補完成：共寫入 {written} 個月的歷史檔到 {HISTORY_DIR}/")
    print("注意：回補檔沒有市值欄位，前端市值/規模篩選對這幾期會顯示「未知」，")
    print("      正式排程從下一期開始就會帶入市值。")


if __name__ == "__main__":
    main()
