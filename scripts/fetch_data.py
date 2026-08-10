"""
台股營收選股掃描 - 每月資料抓取腳本

兩種互斥的篩選邏輯，分別輸出兩份清單：

1. 加速股（動能轉折）
   累計YoY > 0 且 當月YoY > 1.3 × 累計YoY
   抓「由弱轉強、剛開始噴」的公司。基期低的小型股容易入選；
   已經維持高成長很多個月的公司，累計YoY會跟著當月一起墊高，
   很難再打出1.3倍差距，所以這組邏輯結構性地篩不到「已經很強、還在強」的公司。

2. 動能延續股（已強且持續強）
   當月YoY > 50% 且 累計YoY > 40%（絕對門檻，不看倍數關係）
   抓「已經在噴、還沒噴完」的中大型主流股，例如金居、德宏這類。
   跟加速股邏輯互斥，兩份名單刻意分開看，不合併。

連續月數：兩份清單各自往回追歷史存檔，計算該公司連續幾個月都在自己那份名單內。

輸出：
  data/latest.json           前端讀取用（最新一期，含兩份清單）
  data/history/{年月}.json   歷史累積，連續月數靠這批檔案回推

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

# 動能延續股：不看倍數關係，直接看兩個年增率是否都維持在高檔。
# 適合抓「已經在噴、還沒噴完」的中大型主流股（例如金居、德宏這類）；
# 加速股邏輯天生對這類公司不友善，因為它們的累計YoY會跟著當月一起墊高，
# 很難再打出1.3倍的差距。這組門檻先抓常見水準，之後可依實際分布再調整。
MOMENTUM_CUR_YOY_MIN = 50.0
MOMENTUM_CUM_YOY_MIN = 40.0


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
            "cur_yoy": cur_yoy,
            "cum_yoy": cum_yoy,
        })
    print(f"     -> {market} 可用 {len(out)} 家")
    return out


def screen_accelerating(rows: list) -> list:
    result = []
    for r in rows:
        if r["cum_yoy"] > MIN_CUM_YOY and r["cur_yoy"] > ACCEL_MULTIPLIER * r["cum_yoy"]:
            r2 = dict(r)
            r2["accel_ratio"] = round(r["cur_yoy"] / r["cum_yoy"], 2)
            result.append(r2)
    result.sort(key=lambda x: x["accel_ratio"], reverse=True)
    return result


def screen_momentum(rows: list) -> list:
    """
    動能延續股：當月YoY與累計YoY都超過絕對門檻，不看兩者的倍數關係。
    依累計YoY排序（越高代表強勢維持越久），當月YoY當第二排序依據。
    另外算一個 momentum_score 給前端排序/顯示用，等權重平均兩個年增率。
    """
    result = []
    for r in rows:
        if r["cur_yoy"] > MOMENTUM_CUR_YOY_MIN and r["cum_yoy"] > MOMENTUM_CUM_YOY_MIN:
            r2 = dict(r)
            r2["momentum_score"] = round((r["cur_yoy"] + r["cum_yoy"]) / 2, 1)
            result.append(r2)
    result.sort(key=lambda x: (x["cum_yoy"], x["cur_yoy"]), reverse=True)
    return result


# ==================== 連續加速月數 ====================

def load_history_lists(category: str) -> dict:
    """
    掃描 data/history/ 所有歷史檔，取出指定類別（'accelerating' 或 'momentum'）
    在各期的公司代號集合。舊的歷史檔如果沒有 momentum 這個欄位（回補當時還沒有
    這個功能），該期就當作空集合處理，不會噴錯，只是那幾期算不出連續月數。
    """
    index = {}
    for path in glob.glob(os.path.join(HISTORY_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            key = ym_key(payload.get("data_ym"))
            if not key or key == "000000":
                continue
            index[key] = {r.get("code") for r in payload.get(category, []) if r.get("code")}
        except Exception as e:
            print(f"[WARN] 歷史檔讀取失敗 {path}：{e}")
    return index


def compute_streaks(accelerating: list, current_key: str, history: dict) -> None:
    for r in accelerating:
        streak = 1
        key = prev_ym(current_key)
        while key in history and r["code"] in history[key]:
            streak += 1
            key = prev_ym(key)
        r["streak"] = streak
    max_streak = max((r["streak"] for r in accelerating), default=0)
    multi = sum(1 for r in accelerating if r["streak"] >= 2)
    print(f"     連續加速：最長 {max_streak} 個月，連2個月以上共 {multi} 家")


# ==================== 市值 ====================

def build_market_cap_map() -> dict:
    """
    市值(千元) = 已發行股數 × 收盤價 / 1000
    上市：股本與收盤價分屬兩支端點，需要合併
    上櫃：行情端點同時含 Close 與 Capitals，一支搞定
    """
    cap_map = {}

    # ---- 上市 ----
    capital_raw = fetch_json(TWSE_CAPITAL_URL, "TWSE 上市公司基本資料(股本)")
    price_raw = fetch_json(TWSE_PRICE_URL, "TWSE 上市每日收盤行情")

    # 診斷用：把基本資料的完整欄位印出來，方便日後交易所改欄名時對照
    if capital_raw:
        print(f"     [診斷] 基本資料完整欄位: {list(capital_raw[0].keys())}")

    shares_map = {}
    from_direct, from_capital = 0, 0
    for row in capital_raw:
        code = pick(row, "公司代號", "SecuritiesCompanyCode")
        if not code:
            continue
        code = str(code).strip()

        # 路徑一：直接有已發行股數欄位
        shares = to_float(pick(row, "已發行普通股數或TDR原股發行股數", "已發行普通股數或TDR原發行股數", "已發行普通股數", "發行股數"))
        if shares:
            shares_map[code] = shares
            from_direct += 1
            continue

        # 路徑二：用實收資本額 ÷ 每股面額 推算股數
        paid_in = to_float(pick(row, "實收資本額", "資本額"))
        par = parse_par_value(pick(row, "普通股每股面額", "每股面額"))
        if paid_in and par:
            shares_map[code] = paid_in / par
            from_capital += 1

    print(f"     [診斷] 股數來源：直接欄位 {from_direct} 家、資本額推算 {from_capital} 家")

    price_map = {}
    for row in price_raw:
        code = pick(row, "Code", "證券代號")
        close = to_float(pick(row, "ClosingPrice", "收盤價", "Close"))
        if code and close:
            price_map[str(code).strip()] = close
    print(f"     [診斷] 上市收盤價可用 {len(price_map)} 檔")

    twse_hits = 0
    for code, shares in shares_map.items():
        close = price_map.get(code)
        if close:
            cap_map[code] = round(shares * close / 1000, 0)
            twse_hits += 1
    print(f"     -> 上市市值計算成功 {twse_hits} 家")

    # ---- 上櫃 ----
    # 這支端點會回傳多個交易日，同一檔要取最新日期那筆，否則可能用到舊價
    otc_raw = fetch_json(TPEX_QUOTES_URL, "TPEx 上櫃股票行情(含發行股數)")
    latest_by_code = {}
    for row in otc_raw:
        code = pick(row, "SecuritiesCompanyCode", "證券代號", "代號")
        date = str(pick(row, "Date", "資料日期") or "")
        if not code:
            continue
        code = str(code).strip()
        if code not in latest_by_code or date > latest_by_code[code][0]:
            latest_by_code[code] = (date, row)

    otc_hits = 0
    for code, (date, row) in latest_by_code.items():
        close = to_float(pick(row, "Close", "收盤"))
        shares = to_float(pick(row, "Capitals", "發行股數"))
        if close and shares:
            cap_map[code] = round(shares * close / 1000, 0)
            otc_hits += 1
    print(f"     -> 上櫃市值計算成功 {otc_hits} 家（去重後共 {len(latest_by_code)} 檔）")

    return cap_map


def size_tier(market_cap_k):
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

    print("=== 抓取月營收 ===")
    twse_rows = normalize_revenue(fetch_json(TWSE_REVENUE_URL, "TWSE 上市月營收"), "上市")
    tpex_rows = normalize_revenue(fetch_json(TPEX_REVENUE_URL, "TPEx 上櫃月營收"), "上櫃")
    rows = twse_rows + tpex_rows

    if not rows:
        print("[ERROR] 沒有抓到任何營收資料，中止本次執行（不覆蓋既有檔案）")
        return
    if not tpex_rows:
        print("[WARN] 上櫃資料為空，本次只會有上市公司。請把上面的欄位範例貼出來檢查。")

    data_ym = rows[0]["data_ym"]
    current_key = ym_key(data_ym)

    print("\n=== 計算市值 ===")
    cap_map = build_market_cap_map()
    matched = 0
    for r in rows:
        r["market_cap_k"] = cap_map.get(r["code"])
        r["size_tier"] = size_tier(r["market_cap_k"])
        if r["market_cap_k"]:
            matched += 1
    print(f"     -> 營收名單中成功對到市值 {matched}/{len(rows)} 家")

    print("\n=== 篩選加速股（動能轉折） ===")
    accelerating = screen_accelerating(rows)
    hist_accel = load_history_lists("accelerating")
    hist_accel.pop(current_key, None)
    compute_streaks(accelerating, current_key, hist_accel)

    print("\n=== 篩選動能延續股（已強且持續強） ===")
    momentum = screen_momentum(rows)
    hist_momentum = load_history_lists("momentum")
    hist_momentum.pop(current_key, None)
    compute_streaks(momentum, current_key, hist_momentum)

    payload = {
        "data_ym": data_ym,
        "ym_key": current_key,
        "updated_at": datetime.date.today().isoformat(),
        "count_all": len(rows),
        "count_twse": len(twse_rows),
        "count_tpex": len(tpex_rows),
        "count_accelerating": len(accelerating),
        "count_accel_streak_2plus": sum(1 for r in accelerating if r.get("streak", 1) >= 2),
        "count_momentum": len(momentum),
        "count_momentum_streak_2plus": sum(1 for r in momentum if r.get("streak", 1) >= 2),
        "history_months": len(hist_accel) + 1,
        "accelerating": accelerating,
        "momentum": momentum,
    }

    with open(os.path.join(HISTORY_DIR, f"{current_key}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUTPUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n完成：資料年月 {data_ym}")
    print(f"      上市 {len(twse_rows)} 家 + 上櫃 {len(tpex_rows)} 家 = {len(rows)} 家")
    print(f"      加速股 {len(accelerating)} 家　動能延續股 {len(momentum)} 家")


if __name__ == "__main__":
    main()
