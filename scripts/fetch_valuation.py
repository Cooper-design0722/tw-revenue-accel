"""
每日估值抓取腳本 —— 收盤價 + 今年累計EPS + 自算本益比

本益比計算方式（依需求指定）：
    本益比 = 當天收盤價 ÷ 今年累計EPS

⚠️ 這與交易所官方本益比的口徑不同，兩者不可混用：
    官方本益比的分母是「近四季EPS」（涵蓋完整12個月）。
    本腳本的分母是「今年累計EPS」，涵蓋月數隨季度變動：
        Q1 財報後 → 只含 3 個月獲利，算出的倍數約為年化值的 4 倍
        Q2 財報後 → 6 個月，約 2 倍
        Q3 財報後 → 9 個月，約 1.33 倍
        Q4/年報後 → 12 個月，此時才與年化值一致
    因此這個數字適合「同一季度內、跨股票」比較，
    不適合同一檔股票跨季度比較（會在財報更新當下出現跳動）。

    輸出欄位另附 pe_annualized（把累計EPS年化後再算的本益比），
    需要跨季比較時可以改用那一欄。年化採單純外推：
        年化EPS = 今年累計EPS ÷ 已涵蓋季數 × 4
    這假設各季獲利平均，對淡旺季明顯的公司會失真，僅供參考。

近四季EPS 為參考欄，由交易所官方本益比反推（收盤價 ÷ 官方本益比）。
若官方端點抓取失敗，此欄留空，不影響主要欄位。

資料來源與更新頻率：
    收盤價        每天變 → 本腳本每天抓
    今年累計EPS   一季變一次 → 由 fetch_eps_quarterly.py 產出，這裡只讀取併入

輸出：data/valuation_latest.json
"""

import requests
import json
import os
import re
import datetime

# 收盤價（主要來源，與量能腳本同一組端點）
TWSE_PRICE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
# 上市收盤價備援：STOCK_DAY_ALL 抓不到或太少時改用（同樣有 Code 與 ClosingPrice）
TWSE_PRICE_FALLBACK_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
# 官方本益比（僅用來反推「近四季EPS」參考欄，抓不到也不影響主要欄位）
TWSE_BWIBBU_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_PERATIO_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

OUTPUT_DIR = "data"
EPS_PATH = os.path.join(OUTPUT_DIR, "eps_quarterly.json")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "valuation_latest.json")

SPOT_CHECK = ["2330", "3661", "3037"]


def fetch_json(url, label, required=True):
    try:
        r = requests.get(url, timeout=40, headers={"User-Agent": "valuation-daily/1.0"})
        r.raise_for_status()
        r.encoding = "utf-8"
        d = r.json()
        if isinstance(d, dict):
            d = [d]
        print(f"[OK] {label}：{len(d)} 筆")
        if d:
            print(f"     欄位：{list(d[0].keys())[:10]}")
        return d
    except Exception as e:
        lvl = "WARN" if required else "INFO"
        print(f"[{lvl}] {label} 失敗：{e}")
        return []


def pick(row, *cands):
    for c in cands:
        if c in row and row[c] not in (None, "", "-", "--"):
            return row[c]
    norm = {re.sub(r"\s+", "", str(k)): v for k, v in row.items()}
    for c in cands:
        k = re.sub(r"\s+", "", c)
        if k in norm and norm[k] not in (None, "", "-", "--"):
            return norm[k]
    return None


def to_float(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def norm_date(s):
    digits = re.sub(r"\D", "", str(s or ""))
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 7:
        return f"{int(digits[:3]) + 1911}-{digits[3:5]}-{digits[5:]}"
    return None


def load_quarterly_eps():
    if not os.path.exists(EPS_PATH):
        print(f"[提示] 找不到 {EPS_PATH}，本次「今年累計EPS」與本益比都會是空的。")
        print(f"       請先執行 fetch_eps_quarterly.py（一季跑一次即可）。")
        return {}, None
    try:
        with open(EPS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        stocks = d.get("stocks", {})
        print(f"[OK] 讀入季報EPS：{len(stocks)} 家（產生於 {d.get('updated_at')}）")
        return stocks, d.get("updated_at")
    except Exception as e:
        print(f"[WARN] 季報EPS檔讀取失敗：{e}")
        return {}, None


def collect_prices():
    """收盤價：上市取 ClosingPrice、上櫃取 Close。回傳 {code: (price, market, date)}"""
    prices = {}
    dates = {}

    for row in fetch_json(TWSE_PRICE_URL, "TWSE 上市每日行情"):
        code = pick(row, "Code", "證券代號")
        close = to_float(pick(row, "ClosingPrice", "收盤價"))
        d = norm_date(pick(row, "Date", "日期"))
        if code and close:
            prices[str(code).strip()] = (close, "上市", d)
            if d:
                dates[d] = dates.get(d, 0) + 1

    # 上櫃端點可能含多個交易日，同一檔取日期最新的那筆
    otc_latest = {}
    for row in fetch_json(TPEX_QUOTES_URL, "TPEx 上櫃每日行情"):
        code = pick(row, "SecuritiesCompanyCode", "證券代號", "代號")
        d = norm_date(pick(row, "Date", "資料日期")) or ""
        if not code:
            continue
        code = str(code).strip()
        if code not in otc_latest or d > otc_latest[code][0]:
            otc_latest[code] = (d, row)
    for code, (d, row) in otc_latest.items():
        close = to_float(pick(row, "Close", "收盤"))
        if close:
            prices[code] = (close, "上櫃", d or None)
            if d:
                dates[d] = dates.get(d, 0) + 1

    n_twse = sum(1 for v in prices.values() if v[1] == "上市")
    # 上市太少（正常約 1,000 檔）→ 改用備援端點補，避免整批上市股沒有價格
    if n_twse < 500:
        print(f"     [警告] 上市收盤價只有 {n_twse} 檔，改用備援端點 STOCK_DAY_AVG_ALL 補齊")
        added = 0
        for row in fetch_json(TWSE_PRICE_FALLBACK_URL, "TWSE 上市收盤價備援"):
            code = pick(row, "Code", "證券代號")
            close = to_float(pick(row, "ClosingPrice", "收盤價"))
            if code and close and str(code).strip() not in prices:
                prices[str(code).strip()] = (close, "上市", None)
                added += 1
        print(f"     -> 備援補上 {added} 檔上市收盤價")

    n_twse = sum(1 for v in prices.values() if v[1] == "上市")
    n_tpex = sum(1 for v in prices.values() if v[1] == "上櫃")
    trade_date = max(dates, key=dates.get) if dates else None
    print(f"     -> 收盤價合計 {len(prices)} 檔（上市 {n_twse}、上櫃 {n_tpex}），交易日 {trade_date}")
    if n_tpex < 300:
        print(f"     [警告] 上櫃收盤價只有 {n_tpex} 檔（正常約 800 檔），請檢查上櫃端點欄位")
    return prices, trade_date


def collect_official_pe():
    """官方本益比，僅供反推近四季EPS 參考用。失敗不影響主流程。"""
    pe_map = {}
    for url, label in [(TWSE_BWIBBU_URL, "TWSE 官方本益比(參考)"),
                       (TPEX_PERATIO_URL, "TPEx 官方本益比(參考)")]:
        for row in fetch_json(url, label, required=False):
            code = pick(row, "Code", "SecuritiesCompanyCode", "證券代號", "股票代號")
            pe = to_float(pick(row, "PEratio", "PERatio", "本益比"))
            if code and pe and pe > 0:
                pe_map[str(code).strip()] = pe
    print(f"     -> 官方本益比 {len(pe_map)} 檔（用於反推近四季EPS）")
    return pe_map


def quarters_covered(q):
    """季別轉成已涵蓋季數，用於年化。無法判讀時回傳 None。"""
    try:
        n = int(re.sub(r"\D", "", str(q)))
        return n if 1 <= n <= 4 else None
    except (ValueError, TypeError):
        return None


# ==================== 股價歷史與漲幅 ====================
#
# data/price_history.json  滾動保留最近 280 個交易日的收盤價（只存現有上市櫃公司）
# data/price_base.json     去年最後一個交易日的收盤價（算「今年以來漲幅」）
#
# 歷史由 backfill_price_finmind.py 一次性回補（FinMind），之後本腳本每個交易日接上最新一天。
# 240 日漲幅需要 241 個交易日，280 天保留了約 40 天的緩衝。
#
# 漲幅一律用原始收盤價，未還原除權息：
#   N 日內若遇除息，會呈現為假跌（約等於殖利率），與還原價系統的數字不同。

PRICE_HISTORY_PATH = os.path.join(OUTPUT_DIR, "price_history.json")
PRICE_BASE_PATH = os.path.join(OUTPUT_DIR, "price_base.json")
KEEP_PRICE_DAYS = 280
RETURN_WINDOWS = [1, 5, 10, 20, 60, 120, 240]


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 讀取 {path} 失敗：{e}")
        return default


def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def load_universe() -> set:
    """現有上市櫃公司代號（取自月營收輸出）。歷史檔只存這些，排除 ETF、權證，檔案小約兩成。"""
    d = _load(os.path.join(OUTPUT_DIR, "latest.json"), {})
    return {r.get("code") for r in d.get("all_stocks", []) if r.get("code")}


def update_price_history(prices: dict, trade_date: str) -> list:
    """以交易日為 key 寫入今日收盤；假日重跑時日期相同，直接覆蓋不重複累積"""
    hist = _load(PRICE_HISTORY_PATH, {"days": []})
    days = [d for d in hist.get("days", []) if d.get("date") != trade_date]
    uni = load_universe()
    close = {c: p[0] for c, p in prices.items() if (not uni or c in uni)}
    days.append({"date": trade_date, "src": "daily", "close": close})
    days.sort(key=lambda d: d["date"])
    days = days[-KEEP_PRICE_DAYS:]
    _save(PRICE_HISTORY_PATH, {"updated_at": datetime.date.today().isoformat(), "days": days})
    return days


def update_price_base(prices: dict, trade_date: str, days: list) -> dict:
    """
    今年以來漲幅的基準 = 去年最後一個交易日的收盤價。
    平常由回補腳本寫好；跨年後檔案還是去年的，就從歷史檔找新一年的基準，
    歷史裡沒有去年底才退而求其次，用今年第一次執行當天的收盤價暫代。
    """
    base = _load(PRICE_BASE_PATH, {})
    year = trade_date[:4]
    if base.get("year") != year:
        prev = [d for d in days if d["date"] < f"{year}-01-01"]
        if prev:
            last = prev[-1]
            base = {"year": year, "date": last["date"], "close": dict(last["close"])}
            print(f"     -> 建立 {year} 年基準價：去年最後交易日 {last['date']}")
        else:
            base = {"year": year, "date": trade_date, "close": {c: p[0] for c, p in prices.items()}}
            print(f"     -> 建立 {year} 年基準價：歷史不足，暫以 {trade_date} 代替（今年以來漲幅會略偏低）")
        _save(PRICE_BASE_PATH, base)
    return base


def compute_returns(code: str, days: list, base: dict) -> dict:
    out = {}
    today = days[-1]["close"].get(code) if days else None
    for n in RETURN_WINDOWS:
        key = f"r{n}"
        if today is None or len(days) <= n:
            out[key] = None
            continue
        past = days[-1 - n]["close"].get(code)
        out[key] = round((today / past - 1) * 100, 2) if past else None
    b = (base.get("close") or {}).get(code)
    out["rytd"] = round((today / b - 1) * 100, 2) if (today and b) else None
    return out


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=== 讀取季報EPS ===")
    eps_map, eps_updated = load_quarterly_eps()

    print("\n=== 抓取收盤價 ===")
    prices, trade_date = collect_prices()
    if not prices:
        print("\n[ERROR] 沒抓到任何收盤價，中止（不覆蓋既有檔案）")
        return

    print("\n=== 股價歷史與漲幅 ===")
    days = update_price_history(prices, trade_date)
    base = update_price_base(prices, trade_date, days)
    print(f"     -> 股價歷史 {len(days)} 個交易日（{days[0]['date']} ~ {days[-1]['date']}），"
          f"年初基準 {base.get('date')}")
    if len(days) <= 240:
        print(f"     [提示] 只有 {len(days)} 個交易日，240 日漲幅需要 241 個；"
              f"若已執行股價回補仍不足，請檢查回補 log（Backfill price 的結尾統計）")

    print("\n=== 抓取官方本益比（參考欄用） ===")
    official_pe = collect_official_pe()

    stocks = {}
    n_pe, n_loss, n_no_eps = 0, 0, 0
    for code, (close, market, _) in prices.items():
        q = eps_map.get(code) or {}
        eps_ytd = q.get("eps_ytd")
        quarter = q.get("quarter")

        # 本益比 = 當天收盤價 ÷ 今年累計EPS
        # 累計EPS ≤ 0（虧損）時本益比無意義，留空
        if eps_ytd is None:
            pe = None
            n_no_eps += 1
        elif eps_ytd <= 0:
            pe = None
            n_loss += 1
        else:
            pe = round(close / eps_ytd, 2)
            n_pe += 1

        # 年化版本：把累計EPS外推成全年再算，供跨季比較參考
        qn = quarters_covered(quarter)
        pe_ann = None
        if eps_ytd and eps_ytd > 0 and qn:
            eps_ann = eps_ytd / qn * 4
            pe_ann = round(close / eps_ann, 2)

        # 近四季EPS：由官方本益比反推，純參考
        off_pe = official_pe.get(code)
        eps_ttm = round(close / off_pe, 2) if off_pe else None

        stocks[code] = {
            "close": close,
            "market": market,
            "eps_ytd": eps_ytd,
            "eps_year": q.get("year"),
            "eps_quarter": quarter,
            "pe": pe,                 # 收盤價 ÷ 今年累計EPS
            "pe_annualized": pe_ann,  # 收盤價 ÷ 年化後EPS
            "eps_ttm": eps_ttm,       # 近四季（參考）
            **compute_returns(code, days, base),   # r1/r5/r10/r20/r60/r120/r240/rytd（%）
        }

    # 防呆：某個週期幾乎所有股票漲幅都剛好是 0%，不是市場真的沒動，
    # 而是歷史價格是重複資料。這種情況寧可留空，也不要顯示誤導的 0。
    for key in ("r1", "r5", "r10", "r20", "r60", "r120", "r240", "rytd"):
        vals = [v[key] for v in stocks.values() if v.get(key) is not None]
        if len(vals) >= 200:
            zero_share = sum(1 for x in vals if x == 0) / len(vals)
            if zero_share > 0.5:
                print(f"     [警告] {key} 有 {zero_share:.0%} 的股票漲幅剛好是 0 → 判定為重複資料，整欄留空。"
                      f"請刪除 data/price_history.json、data/price_base.json、data/price_backfill_state.json 後重跑股價回補。")
                for v in stocks.values():
                    v[key] = None

    print(f"\n=== 統計 ===")
    print(f"     交易日：{trade_date}")
    print(f"     個股總數：{len(stocks)}")
    print(f"     可算本益比：{n_pe}")
    print(f"     累計EPS為負／零：{n_loss}（本益比留空，屬正常）")
    print(f"     查無累計EPS：{n_no_eps}（季報檔未涵蓋，需確認 fetch_eps_quarterly 是否抓齊）")
    cover = {k: sum(1 for v in stocks.values() if v.get(k) is not None) for k in ("r1", "r5", "r10", "r20", "r60", "r120", "r240", "rytd")}
    print(f"     可算漲幅家數：" + "　".join(f"{k}={n}" for k, n in cover.items()))
    n_noq = sum(1 for v in stocks.values() if v.get("eps_ytd") is not None and not v.get("eps_quarter"))
    if n_noq:
        print(f"     [警告] {n_noq} 檔有累計EPS但缺財報季別 → 年化本益比無法計算（見 fetch_eps_quarterly 的季別欄位）")

    print(f"\n=== 抽樣核對 ===")
    for code in SPOT_CHECK:
        s = stocks.get(code)
        if not s:
            print(f"  {code}：查無收盤價")
            continue
        print(f"  {code}（{s['market']}）收盤 {s['close']}")
        print(f"        今年累計EPS {s['eps_ytd']}（{s['eps_year']}Q{s['eps_quarter']}）"
              f"　→　本益比 {s['pe']}")
        print(f"        年化本益比 {s['pe_annualized']}　近四季EPS {s['eps_ttm']}（參考）")
        print(f"        漲幅 1日 {s['r1']}%　5日 {s['r5']}%　10日 {s['r10']}%　"
              f"20日 {s['r20']}%　60日 {s['r60']}%　120日 {s['r120']}%　240日 {s['r240']}%　今年以來 {s['rytd']}%")

    payload = {
        "trade_date": trade_date,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "eps_quarterly_updated_at": eps_updated,
        "pe_formula": "收盤價 ÷ 今年累計EPS",
        "count": len(stocks),
        "count_with_pe": n_pe,
        "stocks": stocks,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n已寫入 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
