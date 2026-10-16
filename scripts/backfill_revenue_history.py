"""
月營收歷史回補（一次性）—— 供「三年平均、營收創新高、連續成長月數」使用

抓每檔公司 2021/01 至今的逐月營收，寫入 data/revenue_history.json（單位千元）。
之後每月 fetch_data.py 會用官方值接上新月份，不需要再跑這支。

為什麼從 2021 年開始：
  2026/09 的「單月YoY前三年平均」要 2023~2025 三年的 9 月 YoY，
  而 2023 年的 YoY 需要 2022 年做基期 → 至少要 2022 年起的資料。
  多抓一年讓「營收創新高」可以往回比較到 5 年以上。

執行特性：
  1. 先嘗試一次抓全市場（付費 token 才可能成功，1 次請求搞定）
  2. 失敗就逐檔抓，免費額度每小時 600 次 → 每檔間隔 6.5 秒，全市場約 3.5 小時
  3. 可中斷續跑：已有 2021 年資料的公司會跳過，重跑只補剩下的
  4. 時間上限：跑滿 300 分鐘自動存檔收工（GitHub Actions 單次上限 6 小時），
     log 會印出剩餘家數，再按一次 Run workflow 即可接續
  5. 官方值優先：已存在的月份（來自交易所官方）不覆蓋，只補缺漏

需要環境變數 FINMIND_TOKEN（你之前回補時已設在 GitHub Secrets）
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
HISTORY_PATH = os.path.join(OUTPUT_DIR, "revenue_history.json")

START_DATE = "2021-01-01"
DONE_IF_EARLIEST_BEFORE = "2021-04"   # 已有 2021/03 以前資料就視為回補完成
RATE_LIMIT_SLEEP = 6.5
TIME_BUDGET_MIN = 300
SAVE_EVERY = 50


def to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def load_history():
    """回傳 (逐月營收, 已回補代號集合)"""
    if not os.path.exists(HISTORY_PATH):
        return {}, set()
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("stocks", {}), set(d.get("backfilled", []))


def save_history(hist, backfilled):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.date.today().isoformat(), "unit": "千元",
                   "backfilled": sorted(backfilled), "stocks": hist},
                  f, ensure_ascii=False, separators=(",", ":"))


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
    return sorted(codes)


def merge_rows(hist, rows):
    """FinMind revenue 單位為元 → 轉千元；只補缺漏月份，不覆蓋官方值"""
    added = 0
    for r in rows:
        code = str(r.get("stock_id", "")).strip()
        y, mo = r.get("revenue_year"), r.get("revenue_month")
        rev = to_float(r.get("revenue"))
        if not code or not y or not mo or rev is None:
            continue
        key = f"{int(y)}-{int(mo):02d}"
        s = hist.setdefault(code, {})
        if key not in s:
            s[key] = round(rev / 1000, 0)
            added += 1
    return added


def fetch(params):
    params = dict(params)
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    r = requests.get(FINMIND_URL, params=params, timeout=60)
    r.raise_for_status()
    d = r.json()
    if d.get("status") not in (200, None):
        raise RuntimeError(d.get("msg"))
    return d.get("data", [])


def main():
    if not FINMIND_TOKEN:
        print("[ERROR] 找不到 FINMIND_TOKEN，請確認 GitHub Secrets")
        raise SystemExit(1)

    t0 = time.time()
    end_date = datetime.date.today().isoformat()
    hist, backfilled = load_history()
    universe = get_universe()
    print(f"公司名單 {len(universe)} 家，既有歷史 {len(hist)} 家，已回補 {len(backfilled)} 家")

    def done(code):
        # 已回補過（含 2021 年後才上市、資料本來就較短的公司），或已有足夠早的資料
        if code in backfilled:
            return True
        s = hist.get(code)
        return bool(s) and min(s.keys()) < DONE_IF_EARLIEST_BEFORE

    todo = [c for c in universe if not done(c)]
    print(f"需回補 {len(todo)} 家（已完成 {len(universe) - len(todo)} 家會跳過）")
    if not todo:
        print("全部已回補完成，不需執行")
        return

    # 1) 嘗試整批（付費 token 才會成功）
    try:
        rows = fetch({"dataset": "TaiwanStockMonthRevenue",
                      "start_date": START_DATE, "end_date": end_date})
        ids = {r.get("stock_id") for r in rows}
        if len(ids) > 500:
            added = merge_rows(hist, rows)
            backfilled.update(c for c in ids if c)
            save_history(hist, backfilled)
            print(f"[OK] 整批抓取成功：{len(ids)} 家、新增 {added} 個月份資料")
            todo = [c for c in universe if not done(c)]
            print(f"整批後仍需逐檔補 {len(todo)} 家")
        else:
            print(f"[INFO] 整批只回傳 {len(ids)} 家（免費額度通常不支援），改逐檔抓取")
    except Exception as e:
        print(f"[INFO] 整批抓取不可用（{e}），改逐檔抓取")

    # 2) 逐檔
    est = len(todo) * RATE_LIMIT_SLEEP / 60
    print(f"逐檔抓取 {len(todo)} 家，預估 {est:.0f} 分鐘（上限 {TIME_BUDGET_MIN} 分鐘）")
    fetched, empty, failed = 0, 0, 0
    for i, code in enumerate(todo, 1):
        if (time.time() - t0) / 60 > TIME_BUDGET_MIN:
            print(f"\n[時間上限] 已跑滿 {TIME_BUDGET_MIN} 分鐘，存檔收工")
            break
        try:
            rows = fetch({"dataset": "TaiwanStockMonthRevenue", "data_id": code,
                          "start_date": START_DATE, "end_date": end_date})
            if rows:
                merge_rows(hist, rows)
                fetched += 1
            else:
                empty += 1   # FinMind 無此代號，仍標記已處理，避免續跑時重複請求
            backfilled.add(code)
        except Exception as e:
            failed += 1
            print(f"  [{code}] 失敗：{e}")
            if "limit" in str(e).lower() or "429" in str(e):
                print("  觸及流量上限，休息 10 分鐘")
                time.sleep(600)
        if i % SAVE_EVERY == 0:
            save_history(hist, backfilled)
            print(f"  進度 {i}/{len(todo)}　成功 {fetched}　無資料 {empty}　失敗 {failed}　"
                  f"已用 {(time.time() - t0) / 60:.0f} 分鐘")
        time.sleep(RATE_LIMIT_SLEEP)

    save_history(hist, backfilled)

    remaining = [c for c in universe if not done(c)]
    print(f"\n完成本輪：成功 {fetched}　無資料 {empty}　失敗 {failed}")
    print(f"歷史檔共 {len(hist)} 家，尚未回補 {len(remaining)} 家")
    if remaining:
        print("⚠️ 尚未全部完成 → 請再按一次 Run workflow 接續（已完成的會自動跳過）")
    else:
        print("✅ 回補完成。接著手動執行一次 Monthly Revenue，三年平均等欄位就會出現")
    for code in ["1101", "2330", "3661", "3037"]:
        s = hist.get(code, {})
        if s:
            ks = sorted(s)
            print(f"  抽樣 {code}：{len(ks)} 個月（{ks[0]} ~ {ks[-1]}）")


if __name__ == "__main__":
    main()
