"""
台股族群成交量能追蹤 - 每個交易日盤後執行

核心指標設計（為什麼不是直接看成交金額）：
  單看族群成交金額會被大盤整體氣氛帶著走——大盤爆量時所有族群都放量，
  看不出誰真的強。真正有訊號的是「佔比」：該族群成交金額佔全市場的比重。

  佔比變化 = 近5日佔比 − 近20日佔比（單位：百分點）
    > 0 代表資金正在往這個族群集中，這是相對強弱，已自動排除大盤因素
  放量倍數 = 近5日日均成交金額 ÷ 近20日日均成交金額
    > 1 代表近期在放量，配合佔比上升才是有效訊號

  兩者要一起看：
    佔比↑ + 放量↑ → 資金明確流入，族群正在成為市場焦點
    佔比↑ + 縮量   → 只是別的族群跌更兇，相對抗跌，不代表主動流入
    佔比↓ + 放量↑ → 放量但資金分散，可能是換手或出貨

資料源（皆免費、免金鑰，與月營收腳本共用）：
  上市行情  TWSE /v1/exchangeReport/STOCK_DAY_ALL      （含 TradeValue 成交金額）
  上櫃行情  TPEx /openapi/v1/tpex_mainboard_daily_close_quotes
  產業別    上市 t187ap03_L、上櫃 mopsfin_t187ap05_O

輸出：
  data/volume_history.json   滾動保留最近 N 個交易日的族群成交金額（單一檔案，不會檔案爆炸）
  data/volume_latest.json    前端讀取用，含各族群的佔比、佔比變化、放量倍數
"""

import requests
import json
import os
import re
import datetime

# ---------- 資料源 ----------
TWSE_PRICE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
# 產業別一律從「月營收」端點取，兩個市場都是中文產業名，可直接對齊。
# ⚠️ 不要改用 t187ap03_L（公司基本資料）：該端點的「產業別」欄位存的是數字代碼
#    （例如 2330=24、2884=17），跟上櫃端點的中文名稱兜不起來，會讓族群清單
#    出現「24」「17」與「半導體業」混雜的狀況。
TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

OUTPUT_DIR = "data"
HISTORY_PATH = os.path.join(OUTPUT_DIR, "volume_history.json")
LATEST_PATH = os.path.join(OUTPUT_DIR, "volume_latest.json")

KEEP_DAYS = 120     # 滾動保留天數，約半年交易日，足夠算20日指標又不會讓檔案無限膨脹
SHORT_WIN = 5       # 短期窗格
LONG_WIN = 20       # 長期窗格


# ==================== 共用工具 ====================

def fetch_json(url: str, label: str):
    try:
        resp = requests.get(url, timeout=40, headers={"User-Agent": "sector-volume-tracker/1.0"})
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        if isinstance(data, dict):
            data = [data]
        print(f"[OK] {label}: {len(data)} 筆")
        if data:
            print(f"     欄位範例: {list(data[0].keys())[:10]}")
        return data
    except Exception as e:
        print(f"[WARN] {label} 抓取失敗：{e}")
        return []


def pick(row: dict, *candidates):
    for c in candidates:
        if c in row and row[c] not in (None, "", "-", "--"):
            return row[c]
    norm = {re.sub(r"\s+", "", str(k)): v for k, v in row.items()}
    for c in candidates:
        key = re.sub(r"\s+", "", c)
        if key in norm and norm[key] not in (None, "", "-", "--"):
            return norm[key]
    return None


def to_float(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def norm_date(s):
    """
    日期正規化成 YYYY-MM-DD。
    TWSE 可能給民國年(1150811)或西元(20260811)，TPEx 可能給 115/08/11。
    """
    digits = re.sub(r"\D", "", str(s or ""))
    if len(digits) == 8:                      # 西元 YYYYMMDD
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 7:                      # 民國 1150811
        y = int(digits[:3]) + 1911
        return f"{y}-{digits[3:5]}-{digits[5:]}"
    return None


# ==================== 產業別對照 ====================

def build_industry_map() -> dict:
    """
    建立 公司代號 -> 產業別 的對照表。
    上市、上櫃都取自各自的月營收端點，兩邊都是中文產業名稱，可以直接合併。
    """
    imap = {}
    for url, label in [(TWSE_REVENUE_URL, "TWSE 上市月營收(產業別)"),
                       (TPEX_REVENUE_URL, "TPEx 上櫃月營收(產業別)")]:
        for row in fetch_json(url, label):
            code = pick(row, "公司代號", "SecuritiesCompanyCode")
            ind = pick(row, "產業別", "Industry")
            if code and ind:
                imap.setdefault(str(code).strip(), str(ind).strip())

    # 健檢：產業別應該是中文。若出現大量純數字，代表資料源換成代碼制了，
    # 此時族群名稱會變成無意義的數字，必須先修對照來源再繼續。
    numeric = [v for v in imap.values() if re.fullmatch(r"\d+", v)]
    sample = sorted(set(imap.values()))[:8]
    print(f"     -> 產業別對照表 {len(imap)} 檔，產業種類 {len(set(imap.values()))} 種")
    print(f"     -> 產業別範例：{sample}")
    if numeric:
        print(f"     [警告] 有 {len(numeric)} 筆產業別是純數字代碼，族群名稱會顯示為數字。")
        print(f"            請檢查月營收端點的『產業別』欄位是否已改為代碼制。")
    return imap


# ==================== 當日成交金額 ====================

def fetch_today_values(imap: dict):
    """
    回傳 (交易日期, {產業別: 成交金額合計}, 診斷資訊)
    成交金額單位為元。
    """
    by_industry = {}
    dates = {}
    matched, unmatched = 0, 0

    # ---- 上市 ----
    for row in fetch_json(TWSE_PRICE_URL, "TWSE 上市每日行情"):
        code = pick(row, "Code", "證券代號")
        value = to_float(pick(row, "TradeValue", "成交金額"))
        d = norm_date(pick(row, "Date", "日期"))
        if not code or value is None:
            continue
        code = str(code).strip()
        if d:
            dates[d] = dates.get(d, 0) + 1
        ind = imap.get(code)
        if ind:
            by_industry[ind] = by_industry.get(ind, 0.0) + value
            matched += 1
        else:
            unmatched += 1

    # ---- 上櫃 ----
    otc_rows = fetch_json(TPEX_QUOTES_URL, "TPEx 上櫃每日行情")
    # 這支端點可能同時含多個交易日，只取最新那天
    otc_latest = {}
    for row in otc_rows:
        code = pick(row, "SecuritiesCompanyCode", "證券代號", "代號")
        d = norm_date(pick(row, "Date", "資料日期"))
        if not code:
            continue
        code = str(code).strip()
        key = (code, d or "")
        if code not in otc_latest or (d or "") > otc_latest[code][0]:
            otc_latest[code] = (d or "", row)

    for code, (d, row) in otc_latest.items():
        value = to_float(pick(row, "TradingValue", "TransactionAmount", "TradeValue",
                              "Amount", "成交金額", "成交值"))
        if value is None:
            continue
        if d:
            dates[d] = dates.get(d, 0) + 1
        ind = imap.get(code)
        if ind:
            by_industry[ind] = by_industry.get(ind, 0.0) + value
            matched += 1
        else:
            unmatched += 1

    trade_date = max(dates, key=dates.get) if dates else None
    print(f"     -> 交易日 {trade_date}，成功歸類 {matched} 檔、無產業別 {unmatched} 檔")
    if not by_industry:
        print("     [WARN] 沒有任何族群成交金額，請檢查上面的欄位範例確認成交金額欄名")
    return trade_date, by_industry


# ==================== 歷史累積 ====================

def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("days", [])
    except Exception as e:
        print(f"[WARN] 歷史檔讀取失敗：{e}")
        return []


def upsert_day(history: list, trade_date: str, by_industry: dict) -> list:
    """
    以交易日為 key 寫入。遇到假日重跑時，API 會回傳前一交易日的資料，
    此時日期已存在，直接覆蓋（內容相同）而不會重複累積，所以假日執行是安全的。
    """
    history = [d for d in history if d.get("date") != trade_date]
    history.append({"date": trade_date, "values": {k: round(v, 0) for k, v in by_industry.items()}})
    history.sort(key=lambda d: d["date"])
    if len(history) > KEEP_DAYS:
        history = history[-KEEP_DAYS:]
    return history


# ==================== 指標計算 ====================

def compute_metrics(history: list) -> list:
    """
    以「佔比」為核心：先把每日各族群金額換算成佔全市場的比重，再比較短長期窗格。
    這樣做的好處是自動排除大盤整體放量/縮量的影響，看到的是相對強弱。
    """
    if not history:
        return []

    industries = sorted({ind for d in history for ind in d["values"]})
    recent_s = history[-SHORT_WIN:]
    recent_l = history[-LONG_WIN:]

    def window_share(days, ind):
        tot = sum(sum(d["values"].values()) for d in days)
        val = sum(d["values"].get(ind, 0) for d in days)
        return (val / tot * 100) if tot > 0 else None

    def window_avg(days, ind):
        return sum(d["values"].get(ind, 0) for d in days) / len(days) if days else 0

    today = history[-1]
    today_total = sum(today["values"].values()) or 1

    out = []
    for ind in industries:
        s5 = window_share(recent_s, ind)
        s20 = window_share(recent_l, ind)
        avg5 = window_avg(recent_s, ind)
        avg20 = window_avg(recent_l, ind)
        out.append({
            "industry": ind,
            "today_value": round(today["values"].get(ind, 0), 0),
            "today_share": round(today["values"].get(ind, 0) / today_total * 100, 2),
            "share_5d": round(s5, 2) if s5 is not None else None,
            "share_20d": round(s20, 2) if s20 is not None else None,
            # 佔比變化：正值代表資金正往此族群集中（單位：百分點）
            "share_delta": round(s5 - s20, 2) if (s5 is not None and s20 is not None) else None,
            # 放量倍數：>1 代表近期成交金額高於20日均量
            "vol_ratio": round(avg5 / avg20, 2) if avg20 > 0 else None,
            "value_20d": round(sum(d["values"].get(ind, 0) for d in recent_l), 0),
        })

    out.sort(key=lambda x: (x["share_delta"] if x["share_delta"] is not None else -999), reverse=True)
    return out


# ==================== 主流程 ====================

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=== 建立產業別對照 ===")
    imap = build_industry_map()
    if not imap:
        print("[ERROR] 產業別對照表為空，中止（不覆蓋既有檔案）")
        return

    print("\n=== 抓取當日成交金額 ===")
    trade_date, by_industry = fetch_today_values(imap)
    if not trade_date or not by_industry:
        print("[ERROR] 未取得有效當日資料，中止（不覆蓋既有檔案）")
        return

    history = load_history()
    known = {d["date"] for d in history}
    is_new = trade_date not in known
    history = upsert_day(history, trade_date, by_industry)
    print(f"     -> {'新增' if is_new else '更新'}交易日 {trade_date}，"
          f"歷史累積 {len(history)} 個交易日")

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.date.today().isoformat(), "days": history},
                  f, ensure_ascii=False)

    print("\n=== 計算量能指標 ===")
    metrics = compute_metrics(history)
    ready = len(history) >= LONG_WIN

    payload = {
        "trade_date": trade_date,
        "updated_at": datetime.date.today().isoformat(),
        "history_days": len(history),
        "window_short": SHORT_WIN,
        "window_long": LONG_WIN,
        # 未滿20個交易日時指標仍會算，但長期窗格不完整，前端要提示尚在累積
        "ready": ready,
        "market_total_today": round(sum(by_industry.values()), 0),
        "industries": metrics,
    }
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if not ready:
        print(f"     [提示] 目前累積 {len(history)}/{LONG_WIN} 個交易日，"
              f"20日指標尚未穩定，需再跑 {LONG_WIN - len(history)} 個交易日")
    top = [f"{m['industry']} {m['share_delta']:+.2f}pp" for m in metrics[:3] if m["share_delta"] is not None]
    if top:
        print(f"     資金流入前三：{'、'.join(top)}")
    print(f"\n完成：交易日 {trade_date}，族群 {len(metrics)} 個，"
          f"全市場成交 {sum(by_industry.values())/1e8:,.0f} 億元")


if __name__ == "__main__":
    main()
