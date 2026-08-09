"""
台股加速股篩選 - 每月資料抓取腳本
資料源：TWSE OpenAPI + TPEx OpenAPI（皆免費、免金鑰）

篩選邏輯：累計YoY > 0 且 當月YoY > 1.3 × 累計YoY → 判定為「加速股」
連續加速：往回追歷史存檔，計算該公司連續幾個月都在加速名單內

輸出：
  data/latest.json           前端讀取用（最新一期）
  data/history/{年月}.json   歷史累積，連續加速月數靠這批檔案回推

⚠️ 需要你動手確認/調整的地方（已用 ⚠️ 標註在對應位置）：
  1. TPEX_REVENUE_URL：目前用推測的端點命名慣例，請到
     https://www.tpex.org.tw/openapi/ 的 Swagger UI 搜尋「月營業收入」確認正確路徑
  2. 市值計算目前只做上市（TWSE），上櫃(TPEx)的股本/收盤價端點需要另外找對應路徑補上
"""

import requests
import json
import datetime
import os
import re
import glob

# ---------- 資料源設定 ----------
TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"       # 上市月營收（已確認可用）
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/opendata/t187ap05_O"   # ⚠️ 上櫃月營收，端點待驗證
TWSE_CAPITAL_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"       # 上市公司基本資料（含股本）
TWSE_PRICE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"  # 上市每日收盤行情

OUTPUT_DIR = "data"
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")

ACCEL_MULTIPLIER = 1.3   # 當月YoY需超過累計YoY的倍數
MIN_CUM_YOY = 0          # 只有累計YoY > 0 才適用倍數邏輯（負值的轉機股邏輯此版不納入）


# ==================== 共用工具 ====================

def fetch_json(url: str, label: str):
    """抓JSON，失敗不中斷整個流程，只記錄警告"""
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"[OK] {label}: {len(data)} 筆")
        return data
    except Exception as e:
        print(f"[WARN] {label} 抓取失敗：{e}")
        return []


def to_float(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def ym_key(data_ym) -> str:
    """
    把各種年月寫法統一成 6 碼可排序字串（民國年補到4碼 + 月2碼）
    例：'11507' -> '011507'；'115/07' -> '011507'
    字串排序 = 時間排序，檔名也不會出現斜線
    """
    digits = re.sub(r"\D", "", str(data_ym or ""))
    return digits.zfill(6)


def prev_ym(key: str) -> str:
    """回推上一個月。'011507' -> '011506'，'011501' -> '011412'"""
    y, m = int(key[:4]), int(key[4:])
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f"{y:04d}{m:02d}"


# ==================== 資料整理 ====================

def normalize_revenue(raw: list, market: str) -> list:
    """把TWSE/TPEx月營收原始資料轉成統一欄位格式"""
    out = []
    for row in raw:
        code = row.get("公司代號")
        cur_yoy = to_float(row.get("營業收入-去年同月增減(%)"))
        cum_yoy = to_float(row.get("累計營業收入-前期比較增減(%)"))
        if code is None or cur_yoy is None or cum_yoy is None:
            continue
        out.append({
            "code": code,
            "name": row.get("公司名稱"),
            "market": market,
            "industry": row.get("產業別"),
            "data_ym": row.get("資料年月"),
            "cur_revenue_k": to_float(row.get("營業收入-當月營收")),        # 千元
            "cum_revenue_k": to_float(row.get("累計營業收入-當月累計營收")),  # 千元
            "cur_yoy": cur_yoy,
            "cum_yoy": cum_yoy,
        })
    return out


def screen_accelerating(rows: list) -> list:
    """加速股邏輯：累計YoY>0 且 當月YoY > 1.3 × 累計YoY，依倍數由高到低排序"""
    result = []
    for r in rows:
        if r["cum_yoy"] is not None and r["cum_yoy"] > MIN_CUM_YOY:
            if r["cur_yoy"] > ACCEL_MULTIPLIER * r["cum_yoy"]:
                r2 = dict(r)
                r2["accel_ratio"] = round(r["cur_yoy"] / r["cum_yoy"], 2) if r["cum_yoy"] else None
                result.append(r2)
    result.sort(key=lambda x: (x["accel_ratio"] or 0), reverse=True)
    return result


# ==================== 連續加速月數 ====================

def load_history_index() -> dict:
    """掃描 data/history/ 所有歷史檔，建成 {ym_key: set(公司代號)}"""
    index = {}
    for path in glob.glob(os.path.join(HISTORY_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            key = ym_key(payload.get("data_ym"))
            if not key or key == "000000":
                continue
            index[key] = {r.get("code") for r in payload.get("accelerating", []) if r.get("code")}
        except Exception as e:
            print(f"[WARN] 歷史檔讀取失敗 {path}：{e}")
    return index


def compute_streaks(accelerating: list, current_key: str, history: dict) -> None:
    """
    計算連續加速月數，寫回每筆的 streak 欄位。
    規則：本月算1，往前逐月檢查，只要該月歷史檔存在且公司在名單內就+1；
          某個月份檔案不存在就停止，避免把資料缺口誤判成連續。
    """
    for r in accelerating:
        streak = 1
        key = prev_ym(current_key)
        while key in history and r["code"] in history[key]:
            streak += 1
            key = prev_ym(key)
        r["streak"] = streak

    max_streak = max((r["streak"] for r in accelerating), default=0)
    multi = sum(1 for r in accelerating if r["streak"] >= 2)
    print(f"  連續加速：最長 {max_streak} 個月，連2個月以上共 {multi} 家")


# ==================== 市值 ====================

def build_market_cap_map() -> dict:
    """
    市值(千元) = 已發行股數 × 收盤價 / 1000
    ⚠️ 目前僅涵蓋上市(TWSE)。上櫃(TPEx)股本/收盤價端點待補，補上後比照此寫法擴充。
    """
    capital_raw = fetch_json(TWSE_CAPITAL_URL, "TWSE公司基本資料(股本)")
    price_raw = fetch_json(TWSE_PRICE_URL, "TWSE每日收盤行情")

    shares_map = {}
    for row in capital_raw:
        code = row.get("公司代號")
        shares = to_float(row.get("已發行普通股數或TDR原發行股數"))
        if code and shares:
            shares_map[code] = shares

    price_map = {}
    for row in price_raw:
        code = row.get("證券代號")
        close = to_float(row.get("收盤價"))
        if code and close:
            price_map[code] = close

    cap_map = {}
    for code, shares in shares_map.items():
        close = price_map.get(code)
        if close:
            cap_map[code] = round(shares * close / 1000, 0)  # 千元，與營收單位一致
    return cap_map


def size_tier(market_cap_k):
    """市值級距分類，前端直接用這欄位做篩選"""
    if market_cap_k is None:
        return "未知"
    cap_yi = market_cap_k / 100000  # 千元 -> 億元
    if cap_yi >= 1000:
        return "大型股(千億+)"
    elif cap_yi >= 100:
        return "中大型股(百億-千億)"
    elif cap_yi >= 30:
        return "中型股(30-100億)"
    else:
        return "小型股(<30億)"


# ==================== 主流程 ====================

def main():
    os.makedirs(HISTORY_DIR, exist_ok=True)

    twse_raw = fetch_json(TWSE_REVENUE_URL, "TWSE月營收(上市)")
    tpex_raw = fetch_json(TPEX_REVENUE_URL, "TPEx月營收(上櫃)")

    rows = normalize_revenue(twse_raw, "上市") + normalize_revenue(tpex_raw, "上櫃")
    if not rows:
        print("[ERROR] 沒有抓到任何營收資料，中止本次執行（不覆蓋既有檔案）")
        return

    data_ym = rows[0]["data_ym"]
    current_key = ym_key(data_ym)

    # 市值 + 規模分級
    cap_map = build_market_cap_map()
    for r in rows:
        r["market_cap_k"] = cap_map.get(r["code"])
        r["size_tier"] = size_tier(r["market_cap_k"])

    # 篩選加速股
    accelerating = screen_accelerating(rows)

    # 連續加速月數（先排除本月自己的檔案，避免自己算自己）
    history = load_history_index()
    history.pop(current_key, None)
    compute_streaks(accelerating, current_key, history)

    payload = {
        "data_ym": data_ym,
        "ym_key": current_key,
        "updated_at": datetime.date.today().isoformat(),
        "count_all": len(rows),
        "count_accelerating": len(accelerating),
        "count_streak_2plus": sum(1 for r in accelerating if r.get("streak", 1) >= 2),
        "history_months": len(history) + 1,
        "accelerating": accelerating,
    }

    with open(os.path.join(HISTORY_DIR, f"{current_key}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUTPUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"完成：資料年月 {data_ym}，全市場 {len(rows)} 家，加速股 {len(accelerating)} 家")


if __name__ == "__main__":
    main()
