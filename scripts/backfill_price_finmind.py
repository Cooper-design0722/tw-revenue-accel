"""
股價歷史回補（FinMind，一次性）—— 讓 60/120/240 日與今年以來漲幅可用

做兩件事：
  1. 回補約 400 個日曆天（≈270 個交易日）的收盤價 → data/price_history.json
  2. 取「去年最後一個交易日」的收盤價            → data/price_base.json（今年以來漲幅的基準）

為什麼改用 FinMind、不再用交易所的「依日期查詢」：
  交易所網站的日期查詢，櫃買端點疑似不認日期參數（回傳最新一天），
  導致歷史價格全部相同、漲幅全是 0%。FinMind 上市、上櫃都有，且逐檔、逐日的資料可驗證。

執行方式（與營收回補相同）：
  1. 先試「整批」：一次請求取得某一天全市場。免費額度通常不支援，付費 token 才可能成功。
     成功的話約 285 次請求、30 分鐘左右完成。
  2. 不行就「逐檔」：每檔一次請求取得整年日線。免費額度每小時 600 次 → 每檔間隔 6.5 秒，
     全市場約 3.6 小時。
  3. 可中斷續跑：進度存在 data/price_backfill_state.json，重跑會跳過已完成的。
  4. 跑滿 300 分鐘自動存檔收工（GitHub Actions 單次上限 6 小時），再按一次 Run workflow 即可接續。

資料處理原則：
  - 收盤價為交易所原始價，未還原除權息（與每日排程累積的資料同一基準）。
  - 只保留現有上市櫃公司（以交易所月營收名單為準），排除 ETF、權證等。
  - 每日排程寫的日子（src=daily）一律保留；舊版交易所回補寫的日子（src=backfill 或無標記）
    可能含重複資料，整批捨棄；本腳本寫的標記為 src=finmind。

需要環境變數 FINMIND_TOKEN（營收回補時已設在 GitHub Secrets）
"""

import requests
import json
import os
import time
import datetime

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

OUTPUT_DIR = "data"
PRICE_HISTORY_PATH = os.path.join(OUTPUT_DIR, "price_history.json")
PRICE_BASE_PATH = os.path.join(OUTPUT_DIR, "price_base.json")
STATE_PATH = os.path.join(OUTPUT_DIR, "price_backfill_state.json")

LOOKBACK_CALENDAR_DAYS = 400     # 約 270 個交易日，足夠算 240 日漲幅（需 241 個交易日）
KEEP_PRICE_DAYS = 280            # 與 fetch_valuation.py 的 KEEP_PRICE_DAYS 一致
RATE_LIMIT_SLEEP = 6.5
TIME_BUDGET_MIN = 300
SAVE_EVERY = 50
TRUSTED_SRC = {"daily", "finmind"}
BATCH_MIN_STOCKS = 500           # 單日回傳超過這個家數才算「整批可用」
RETURN_WINDOWS = [1, 5, 10, 20, 60, 120, 240]


class RateLimited(Exception):
    pass


def to_float(s):
    try:
        v = float(str(s).replace(",", "").strip())
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def fetch(params):
    params = dict(params)
    params["token"] = FINMIND_TOKEN
    r = requests.get(FINMIND_URL, params=params, timeout=60)
    if r.status_code in (402, 429):
        raise RateLimited(f"HTTP {r.status_code}")
    r.raise_for_status()
    d = r.json()
    st = d.get("status")
    if st == 402:
        raise RateLimited(d.get("msg"))
    if st not in (200, None):
        raise RuntimeError(f"status={st} {d.get('msg')}")
    return d.get("data", [])


def call(params, retries=3):
    """流量上限 → 休息 10 分鐘後重試同一筆（不跳過）；其他錯誤重試 retries 次"""
    attempt = 0
    while True:
        try:
            return fetch(params)
        except RateLimited as e:
            print(f"  觸及流量上限（{e}），休息 10 分鐘後重試")
            time.sleep(600)
        except Exception as e:
            attempt += 1
            if attempt >= retries:
                raise
            time.sleep(10)


def get_universe():
    """目前上市櫃公司代號（以交易所月營收名單為準）"""
    codes = set()
    for url in (TWSE_REVENUE_URL, TPEX_REVENUE_URL):
        try:
            r = requests.get(url, timeout=40)
            r.raise_for_status()
            for row in r.json():
                c = row.get("公司代號") or row.get("SecuritiesCompanyCode")
                if c:
                    codes.add(str(c).strip())
        except Exception as e:
            print(f"[WARN] 取得公司名單失敗 {url}：{e}")
    return codes


# ==================== 歷史檔讀寫 ====================

def load_days():
    """回傳 ({date: day}, 捨棄的天數)。只保留可信來源的日子。"""
    if not os.path.exists(PRICE_HISTORY_PATH):
        return {}, 0
    try:
        with open(PRICE_HISTORY_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f).get("days", [])
    except Exception as e:
        print(f"[WARN] 歷史檔讀取失敗：{e}")
        return {}, 0
    kept = {d["date"]: d for d in raw if d.get("src") in TRUSTED_SRC}
    return kept, len(raw) - len(kept)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"mode": None, "done_codes": [], "done_dates": []}


def merge(days, by_date):
    """FinMind 的值只補缺漏，不覆蓋每日排程（交易所官方值）已寫入的價格"""
    for date, closes in by_date.items():
        d = days.setdefault(date, {"date": date, "src": "finmind", "close": {}})
        for code, p in closes.items():
            d["close"].setdefault(code, p)


def build_base(days, today):
    """
    今年以來漲幅的基準 = 去年最後一個交易日的收盤價。
    歷史必須涵蓋去年底才寫（最後一天須在 12/20 之後），避免用到殘缺資料。
    """
    year = today.year
    prev = [d for d in days if d < f"{year}-01-01"]
    if not prev:
        return None
    last = max(prev)
    if last < f"{year - 1}-12-20":
        return None
    return {"year": str(year), "date": last, "close": dict(days[last]["close"])}


def save_all(days, state, today):
    ordered = sorted(days.values(), key=lambda x: x["date"])
    base = build_base(days, today)          # 趕在截斷前，才找得到去年底那天
    ordered = ordered[-KEEP_PRICE_DAYS:]
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(PRICE_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated_at": today.isoformat(), "days": ordered}, f,
                  ensure_ascii=False, separators=(",", ":"))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    if base:
        old = {}
        if os.path.exists(PRICE_BASE_PATH):
            try:
                with open(PRICE_BASE_PATH, "r", encoding="utf-8") as f:
                    old = json.load(f)
            except Exception:
                old = {}
        # 同一年、同一天的基準，只在家數變多時才覆蓋（逐檔回補是越跑越完整）
        same = old.get("year") == base["year"] and old.get("date") == base["date"]
        if not same or len(base["close"]) > len(old.get("close", {})):
            with open(PRICE_BASE_PATH, "w", encoding="utf-8") as f:
                json.dump(base, f, ensure_ascii=False, separators=(",", ":"))


def add_rows(by_date, rows, universe):
    n = 0
    for r in rows:
        code = str(r.get("stock_id", "")).strip()
        if not code or (universe and code not in universe):
            continue
        close = to_float(r.get("close"))     # 無成交日 FinMind 給 0 → 略過
        date = str(r.get("date", ""))[:10]
        if close and len(date) == 10:
            by_date.setdefault(date, {})[code] = close
            n += 1
    return n


# ==================== 兩種回補模式 ====================

def probe_batch(today):
    """找最近一個有資料的交易日，試著不帶 data_id 取全市場。"""
    d = today
    for _ in range(10):
        if d.weekday() < 5:
            try:
                rows = call({"dataset": "TaiwanStockPrice",
                             "start_date": d.isoformat(), "end_date": d.isoformat()}, retries=1)
            except Exception as e:
                print(f"[INFO] 整批取得不可用（{e}）")
                return False
            finally:
                time.sleep(RATE_LIMIT_SLEEP)
            if rows:
                ids = {str(r.get("stock_id")) for r in rows}
                ok = len(ids) > BATCH_MIN_STOCKS
                print(f"[{'OK' if ok else 'INFO'}] 試抓 {d}：回傳 {len(ids)} 檔 → "
                      f"{'整批可用' if ok else '免費額度不支援整批，改逐檔'}")
                return ok
        d -= datetime.timedelta(days=1)
    return False


def run_batch(days, state, universe, today, t0):
    start = today - datetime.timedelta(days=LOOKBACK_CALENDAR_DAYS)
    done = set(state.get("done_dates", []))
    by_date, n_req = {}, 0
    d = today
    while d >= start:
        if (time.time() - t0) / 60 > TIME_BUDGET_MIN:
            print(f"\n[時間上限] 已跑滿 {TIME_BUDGET_MIN} 分鐘，存檔收工")
            break
        if d.weekday() < 5 and d.isoformat() not in done:
            rows = call({"dataset": "TaiwanStockPrice",
                         "start_date": d.isoformat(), "end_date": d.isoformat()})
            add_rows(by_date, rows, universe)
            done.add(d.isoformat())      # 休市日（空）也記錄，續跑時不重複請求
            n_req += 1
            if n_req % SAVE_EVERY == 0:
                merge(days, by_date); by_date = {}
                state["done_dates"] = sorted(done)
                save_all(days, state, today)
                print(f"  進度：已處理 {n_req} 個日期，目前 {d}，已用 {(time.time() - t0) / 60:.0f} 分鐘")
            time.sleep(RATE_LIMIT_SLEEP)
        d -= datetime.timedelta(days=1)
    merge(days, by_date)
    state["done_dates"] = sorted(done)
    left = [x for x in (start + datetime.timedelta(days=i) for i in range((today - start).days + 1))
            if x.weekday() < 5 and x.isoformat() not in done]
    return len(left)


def run_per_stock(days, state, universe, today, t0):
    start = (today - datetime.timedelta(days=LOOKBACK_CALENDAR_DAYS)).isoformat()
    done = set(state.get("done_codes", []))
    todo = sorted(c for c in universe if c not in done)
    print(f"逐檔回補：待處理 {len(todo)} 家（已完成 {len(done)} 家），"
          f"預估 {len(todo) * RATE_LIMIT_SLEEP / 60:.0f} 分鐘（上限 {TIME_BUDGET_MIN} 分鐘）")
    by_date, got, empty, failed = {}, 0, 0, 0
    for i, code in enumerate(todo, 1):
        if (time.time() - t0) / 60 > TIME_BUDGET_MIN:
            print(f"\n[時間上限] 已跑滿 {TIME_BUDGET_MIN} 分鐘，存檔收工")
            break
        try:
            rows = call({"dataset": "TaiwanStockPrice", "data_id": code,
                         "start_date": start, "end_date": today.isoformat()})
            if rows:
                add_rows(by_date, rows, None)
                got += 1
            else:
                empty += 1
            done.add(code)           # 無資料也記錄，續跑時不重複請求
        except Exception as e:
            failed += 1
            print(f"  [{code}] 失敗：{e}")
        if i % SAVE_EVERY == 0:
            merge(days, by_date); by_date = {}
            state["done_codes"] = sorted(done)
            save_all(days, state, today)
            print(f"  進度 {i}/{len(todo)}　成功 {got}　無資料 {empty}　失敗 {failed}　"
                  f"已用 {(time.time() - t0) / 60:.0f} 分鐘")
        time.sleep(RATE_LIMIT_SLEEP)
    merge(days, by_date)
    state["done_codes"] = sorted(done)
    print(f"\n本輪：成功 {got}　無資料 {empty}　失敗 {failed}")
    return len([c for c in universe if c not in done])


# ==================== 主流程 ====================

def report(days, universe, today):
    ordered = sorted(days)
    if not ordered:
        print("歷史檔為空")
        return
    print(f"\n股價歷史：{len(ordered)} 個交易日（{ordered[0]} ~ {ordered[-1]}）")
    last = days[ordered[-1]]["close"]
    cover = []
    for n in RETURN_WINDOWS:
        if len(ordered) > n:
            past = days[ordered[-1 - n]]["close"]
            cover.append(f"{n}日={sum(1 for c in last if c in past)}")
        else:
            cover.append(f"{n}日=0（歷史不足 {n + 1} 天）")
    print(f"可算漲幅家數：{'　'.join(cover)}")
    base = build_base(days, today)
    print(f"去年底基準價：{base['date'] if base else '尚未取得'}"
          f"{'（' + str(len(base['close'])) + ' 檔）' if base else ''}")
    for code in ["1101", "2330", "3661", "3037"]:
        a = days[ordered[0]]["close"].get(code)
        b = days[ordered[-1]]["close"].get(code)
        print(f"  抽樣 {code}：{ordered[0]} 收 {a}　→　{ordered[-1]} 收 {b}")


def main():
    if not FINMIND_TOKEN:
        print("[ERROR] 找不到 FINMIND_TOKEN，請確認 GitHub Secrets")
        raise SystemExit(1)

    t0 = time.time()
    today = datetime.date.today()
    universe = get_universe()
    if len(universe) < 1000:
        print(f"[ERROR] 公司名單只有 {len(universe)} 家，疑似取得失敗，中止（不覆蓋既有檔案）")
        raise SystemExit(1)

    days, dropped = load_days()
    state = load_state()
    print(f"公司名單 {len(universe)} 家；歷史檔保留可信日子 {len(days)} 天，"
          f"捨棄舊版交易所回補 {dropped} 天")

    if state.get("mode") is None:
        state["mode"] = "batch" if probe_batch(today) else "per_stock"
    print(f"回補模式：{'整批（一天一次請求）' if state['mode'] == 'batch' else '逐檔（一檔一次請求）'}")

    if state["mode"] == "batch":
        remaining = run_batch(days, state, universe, today, t0)
        unit = "個日期"
    else:
        remaining = run_per_stock(days, state, universe, today, t0)
        unit = "家公司"

    save_all(days, state, today)
    report(days, universe, today)

    if remaining:
        print(f"\n⚠️ 尚未全部完成（還剩 {remaining} {unit}）→ 請再按一次 Run workflow 接續，"
              f"已完成的會自動跳過")
    else:
        print("\n✅ 股價回補完成。接著手動執行一次 Daily Scan，60/120/240 日與今年以來漲幅就會出現")


if __name__ == "__main__":
    main()
