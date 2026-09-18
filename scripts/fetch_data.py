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
TWSE_PRICE_FALLBACK_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"  # 備援：本益比表也含收盤價
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

OUTPUT_DIR = "data"
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")

ACCEL_MULTIPLIER = 1.3
MIN_CUM_YOY = 0
ACCEL_MAX_RATIO = 50.0   # 加速倍數超過50倍視為極端值排除（通常是累計年增率趨近0，分母太小造成倍數失真）

# 動能延續股：不看倍數關係，直接看兩個年增率是否都維持在高檔。
# 適合抓「已經在噴、還沒噴完」的中大型主流股（例如金居、德宏這類）；
# 加速股邏輯天生對這類公司不友善，因為它們的累計YoY會跟著當月一起墊高，
# 很難再打出1.3倍的差距。這組門檻先抓常見水準，之後可依實際分布再調整。
MOMENTUM_CUR_YOY_MIN = 50.0
MOMENTUM_CUM_YOY_MIN = 40.0

# 資料品質濾網：排除「去年同期基期太小」造成的百分比失真
# （例如業外一次性認列、資產處分，讓年增率衝到幾千%、幾萬%，這不是持續性動能）
MOMENTUM_MAX_YOY_CAP = 300.0      # 當月或累計YoY只要有一個超過300%，視為極端值直接排除
MOMENTUM_MIN_REVENUE_K = 100000   # 當月營收至少1億元（單位千元），濾掉規模太小、百分比容易被雜訊放大的公司


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

def latest_period_filter(rows: list, label: str):
    """
    只保留「該市場自己」已經申報到最新月份的公司，還沒申報的先排除
    （之後幾天重跑會自然補上）。

    這裡刻意只在單一市場內部判斷最新期別，不能跨市場一起算——上市、上櫃是
    兩個獨立的申報系統，進度本來就不同步。如果合併後才判斷，進度較快的市場
    會把進度較慢的市場「整批」判定成落後而排除掉，即使後者的資料完全正常，
    只是還沒輪到最新月份而已。
    """
    if not rows:
        return rows, None
    period_counts = {}
    for r in rows:
        k = ym_key(r["data_ym"])
        period_counts[k] = period_counts.get(k, 0) + 1
    current_key = max(period_counts.keys())
    data_ym = next(r["data_ym"] for r in rows if ym_key(r["data_ym"]) == current_key)
    stale = sum(c for k, c in period_counts.items() if k != current_key)
    print(f"     [{label}] 期別分布：{dict(sorted(period_counts.items(), key=lambda x:-x[1]))}")
    print(f"     [{label}] 採用最新期別 {data_ym}，排除尚未申報最新月份的公司 {stale} 家")
    return [r for r in rows if ym_key(r["data_ym"]) == current_key], data_ym


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
            "prev_revenue_k": to_float(pick(row, "營業收入-上月營收")),
            "lastyear_revenue_k": to_float(pick(row, "營業收入-去年當月營收")),
            "cum_revenue_k": to_float(pick(row, "累計營業收入-當月累計營收")),
            "cur_yoy": cur_yoy,
            "cum_yoy": cum_yoy,
        })
    print(f"     -> {market} 可用 {len(out)} 家")
    return out


def screen_accelerating(rows: list) -> list:
    """
    加速股：累計YoY>0 且 當月YoY > 1.3×累計YoY。
    加一道濾網：倍數超過 ACCEL_MAX_RATIO 直接排除——這種案例通常是累計YoY
    趨近於0（例如百和累計只有+0.03%），分母太小讓倍數在數學上噴出幾百倍，
    不是真的「加速」，是統計假象。
    """
    result = []
    excluded_extreme = 0
    for r in rows:
        if r["cum_yoy"] > MIN_CUM_YOY and r["cur_yoy"] > ACCEL_MULTIPLIER * r["cum_yoy"]:
            ratio = round(r["cur_yoy"] / r["cum_yoy"], 2)
            if ratio > ACCEL_MAX_RATIO:
                excluded_extreme += 1
                continue
            r2 = dict(r)
            r2["accel_ratio"] = ratio
            result.append(r2)
    result.sort(key=lambda x: x["accel_ratio"], reverse=True)
    print(f"     濾除：倍數>{ACCEL_MAX_RATIO:.0f}倍的極端值 {excluded_extreme} 家")
    return result


def screen_momentum(rows: list) -> list:
    """
    動能延續股：當月YoY與累計YoY都超過絕對門檻，不看兩者的倍數關係。
    依累計YoY排序（越高代表強勢維持越久），當月YoY當第二排序依據。

    另外加兩道資料品質濾網，排除基期過低造成的百分比失真：
      1. YoY 超過 MOMENTUM_MAX_YOY_CAP（例如去年同期業外一次性認列、幾乎沒營收）
      2. 當月營收規模低於 MOMENTUM_MIN_REVENUE_K（規模太小，百分比容易被雜訊放大）
    這兩種公司即使數字符合門檻，代表的多半不是「持續性動能」，是統計假象。
    """
    result = []
    excluded_extreme, excluded_small = 0, 0
    for r in rows:
        if not (r["cur_yoy"] > MOMENTUM_CUR_YOY_MIN and r["cum_yoy"] > MOMENTUM_CUM_YOY_MIN):
            continue
        if r["cur_yoy"] > MOMENTUM_MAX_YOY_CAP or r["cum_yoy"] > MOMENTUM_MAX_YOY_CAP:
            excluded_extreme += 1
            continue
        if not r["cur_revenue_k"] or r["cur_revenue_k"] < MOMENTUM_MIN_REVENUE_K:
            excluded_small += 1
            continue
        r2 = dict(r)
        r2["momentum_score"] = round((r["cur_yoy"] + r["cum_yoy"]) / 2, 1)
        result.append(r2)
    result.sort(key=lambda x: (x["cum_yoy"], x["cur_yoy"]), reverse=True)
    print(f"     濾除：極端值(YoY>{MOMENTUM_MAX_YOY_CAP:.0f}%) {excluded_extreme} 家、"
          f"規模過小(<{MOMENTUM_MIN_REVENUE_K/10000:.0f}千萬) {excluded_small} 家")
    return result


# ==================== 族群（產業別）營收統計 ====================

def build_industry_stats(rows: list) -> list:
    """
    把個股營收加總到產業別，算出族群層級的規模與動能。

    為什麼要看族群總額而不是族群內平均年增率：
      平均年增率會被小公司的極端百分比拉歪（一家小廠營收翻10倍，
      對整個族群的實際景氣沒什麼代表性）。用「總額」加總後再算增減，
      等於自動以營收規模加權，大廠的權重自然較高，更貼近該族群的真實景氣。

    MoM（月增）看的是短期動能轉折，YoY（年增）看的是排除季節性後的趨勢，
    兩個都算出來，前端可以切換。
    """
    agg = {}
    for r in rows:
        ind = r.get("industry") or "未分類"
        a = agg.setdefault(ind, {
            "industry": ind, "count": 0,
            "cur": 0.0, "prev": 0.0, "lastyear": 0.0,
            "has_prev": 0, "has_lastyear": 0,
        })
        a["count"] += 1
        if r.get("cur_revenue_k"):
            a["cur"] += r["cur_revenue_k"]
        if r.get("prev_revenue_k"):
            a["prev"] += r["prev_revenue_k"]
            a["has_prev"] += 1
        if r.get("lastyear_revenue_k"):
            a["lastyear"] += r["lastyear_revenue_k"]
            a["has_lastyear"] += 1

    out = []
    for a in agg.values():
        mom = round((a["cur"] / a["prev"] - 1) * 100, 2) if a["prev"] > 0 else None
        yoy = round((a["cur"] / a["lastyear"] - 1) * 100, 2) if a["lastyear"] > 0 else None
        out.append({
            "industry": a["industry"],
            "count": a["count"],
            "cur_revenue_k": round(a["cur"], 0),
            "prev_revenue_k": round(a["prev"], 0),
            "lastyear_revenue_k": round(a["lastyear"], 0),
            "mom": mom,
            "yoy": yoy,
        })
    out.sort(key=lambda x: (x["mom"] if x["mom"] is not None else -9999), reverse=True)

    total = sum(x["cur_revenue_k"] for x in out)
    print(f"     族群數 {len(out)} 個，全市場當月營收合計 {total/100000:,.0f} 億元")
    top = [f"{x['industry']} {x['mom']:+.1f}%" for x in out[:3] if x["mom"] is not None]
    bottom = [f"{x['industry']} {x['mom']:+.1f}%" for x in out[-3:] if x["mom"] is not None]
    print(f"     MoM 最強：{'、'.join(top)}")
    print(f"     MoM 最弱：{'、'.join(bottom)}")
    return out


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

def build_market_cap_map():
    """
    市值(千元) = 已發行股數 × 收盤價 / 1000
    上市：股本與收盤價分屬兩支端點，需要合併；收盤價另外有備援來源補停牌股缺口
    上櫃：行情端點同時含 Close 與 Capitals，一支搞定

    回傳 (cap_map, shares_map, price_map)：後兩者是合併上市+上櫃後的原始資料，
    給主流程用來診斷「查無股數」還是「查無股價」，區分兩種不同的資料缺口原因。
    """
    cap_map = {}
    all_shares_map = {}
    all_price_map = {}

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

    # 備援：STOCK_DAY_ALL 對停牌股/當日無成交的公司可能沒有資料，
    # 用本益比表(BWIBBU_ALL)的收盤價補上缺口，只補主要來源沒有的代號
    fallback_raw = fetch_json(TWSE_PRICE_FALLBACK_URL, "TWSE 收盤價備援(本益比表)")
    fallback_added = 0
    for row in fallback_raw:
        code = pick(row, "Code", "證券代號")
        close = to_float(pick(row, "ClosingPrice", "收盤價"))
        if code and close:
            code = str(code).strip()
            if code not in price_map:
                price_map[code] = close
                fallback_added += 1
    if fallback_added:
        print(f"     [診斷] 備援來源補上 {fallback_added} 檔收盤價")

    twse_hits = 0
    for code, shares in shares_map.items():
        close = price_map.get(code)
        if close:
            cap_map[code] = round(shares * close / 1000, 0)
            twse_hits += 1
    print(f"     -> 上市市值計算成功 {twse_hits} 家")
    all_shares_map.update(shares_map)
    all_price_map.update(price_map)

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
        if close:
            all_price_map[code] = close
        if shares:
            all_shares_map[code] = shares
        if close and shares:
            cap_map[code] = round(shares * close / 1000, 0)
            otc_hits += 1
    print(f"     -> 上櫃市值計算成功 {otc_hits} 家（去重後共 {len(latest_by_code)} 檔）")

    return cap_map, all_shares_map, all_price_map


def size_tier(market_cap_k):
    """
    市值規模分三級：
      小規模　0-100億
      中規模　100-300億
      大規模　300億以上
    """
    if market_cap_k is None:
        return "未知"
    cap_yi = market_cap_k / 100000  # 千元 -> 億元
    if cap_yi >= 300:
        return "大規模(300億以上)"
    elif cap_yi >= 100:
        return "中規模(100-300億)"
    else:
        return "小規模(0-100億)"


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

    print("=== 期別檢查（上市、上櫃分開判斷，避免互相排擠） ===")
    twse_rows, data_ym_twse = latest_period_filter(twse_rows, "上市")
    tpex_rows, data_ym_tpex = latest_period_filter(tpex_rows, "上櫃")
    rows = twse_rows + tpex_rows

    if not rows:
        print("[ERROR] 過濾後沒有任何可用資料，中止本次執行（不覆蓋既有檔案）")
        return

    # 兩個市場的最新期別通常會一致；不一致時（例如上市已到7月、上櫃還在6月）
    # 直接把兩個都標在資料年月欄位，讓網頁上看得出來目前是混合期別，不會誤導成單一期別
    if data_ym_twse and data_ym_tpex and ym_key(data_ym_twse) != ym_key(data_ym_tpex):
        data_ym = f"上市{data_ym_twse}／上櫃{data_ym_tpex}"
        current_key = max(ym_key(data_ym_twse), ym_key(data_ym_tpex))
        print(f"     [注意] 兩市場期別不同步：上市 {data_ym_twse}、上櫃 {data_ym_tpex}")
    else:
        data_ym = data_ym_twse or data_ym_tpex
        current_key = ym_key(data_ym)

    print("\n=== 計算市值 ===")
    cap_map, shares_map, price_map = build_market_cap_map()
    matched = 0
    unmatched = []
    for r in rows:
        r["market_cap_k"] = cap_map.get(r["code"])
        r["size_tier"] = size_tier(r["market_cap_k"])
        if r["market_cap_k"]:
            matched += 1
        else:
            has_shares = r["code"] in shares_map
            has_price = r["code"] in price_map
            if not has_shares and not has_price:
                reason = "無股數也無股價"
            elif not has_shares:
                reason = "查無股數(可能無面額股或非普通股)"
            else:
                reason = "查無股價(可能當日停牌/新股尚無交易)"
            unmatched.append(f"{r['code']}{r['name']}({reason})")
    print(f"     -> 營收名單中成功對到市值 {matched}/{len(rows)} 家")
    if unmatched:
        preview = "、".join(unmatched[:15])
        more = f" ...等共{len(unmatched)}家" if len(unmatched) > 15 else ""
        print(f"     [診斷] 未配對到市值的公司：{preview}{more}")

    print("\n=== 族群營收統計 ===")
    industries = build_industry_stats(rows)

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
        "industries": industries,
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
