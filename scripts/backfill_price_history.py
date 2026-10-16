"""
股價歷史回補（一次性）—— 讓 5/10/20/40 日漲幅與今年以來漲幅馬上可用

抓兩樣東西：
  1. 最近 45 個交易日的全市場收盤價 → 合併進 data/price_history.json
  2. 今年第一個交易日的收盤價       → 寫入 data/price_base.json

資料源（交易所網站的「依日期查詢」，非 OpenAPI）：
  上市  TWSE 每日收盤行情 MI_INDEX（一天一次請求涵蓋全部上市股）
  上櫃  TPEx 上櫃股票每日收盤行情（同上）
  櫃買網站 2024 年改版過，新舊兩種網址都會嘗試，log 會印出實際成功的是哪一個。

約 65 個平日 × 2 個市場 ≈ 130 次請求，每次間隔 3.5 秒（交易所對連續請求有封鎖機制），
全程約 8 分鐘。

如果這支失敗也沒關係：每日排程本來就會逐日累積，
只是 40 日漲幅要等兩個月後才會有值。
"""

import requests
import json
import os
import re
import time
import datetime

OUTPUT_DIR = "data"
PRICE_HISTORY_PATH = os.path.join(OUTPUT_DIR, "price_history.json")
PRICE_BASE_PATH = os.path.join(OUTPUT_DIR, "price_base.json")

TARGET_DAYS = 45
MAX_CALENDAR_DAYS = 100
KEEP_PRICE_DAYS = 60
SLEEP = 3.5
HEADERS = {"User-Agent": "Mozilla/5.0 (price-backfill)"}

TWSE_URLS = [
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json",
    "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={ymd}&type=ALLBUT0999",
]
TPEX_URLS = [
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ?date={slash}&type=EW&response=json",
    "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={roc}&o=json",
]

ok_endpoint = {"twse": None, "tpex": None}


def to_float(s):
    try:
        v = float(str(s).replace(",", "").strip())
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_table(d, code_names, close_names):
    """
    在回傳 JSON 裡找出「含代號欄與收盤欄」的表格。
    兼容三種格式：tables:[{fields,data}]、fieldsN/dataN、aaData。
    """
    cands = []
    for t in d.get("tables", []) or []:
        if isinstance(t, dict):
            cands.append((t.get("fields") or [], t.get("data") or []))
    for k in d:
        m = re.fullmatch(r"fields(\d+)", k)
        if m:
            cands.append((d[k] or [], d.get(f"data{m.group(1)}") or []))
    for fields, data in cands:
        ci = next((i for i, f in enumerate(fields) if f in code_names), None)
        pi = next((i for i, f in enumerate(fields) if f in close_names), None)
        if ci is not None and pi is not None and data:
            return {str(row[ci]).strip(): to_float(row[pi]) for row in data if len(row) > max(ci, pi)}
    if d.get("aaData"):   # 櫃買舊版：第 0 欄代號、第 2 欄收盤
        return {str(row[0]).strip(): to_float(row[2]) for row in d["aaData"] if len(row) > 2}
    return {}


def fetch_twse(date):
    ymd = date.strftime("%Y%m%d")
    order = TWSE_URLS if not ok_endpoint["twse"] else [ok_endpoint["twse"]]
    for tpl in order:
        try:
            d = get_json(tpl.format(ymd=ymd))
            if str(d.get("stat", "OK")).upper() not in ("OK", ""):
                return {}          # 休市日
            prices = extract_table(d, ("證券代號",), ("收盤價",))
            if prices:
                ok_endpoint["twse"] = tpl
                return prices
        except Exception:
            continue
        finally:
            time.sleep(SLEEP)
    return {}


def fetch_tpex(date):
    slash = date.strftime("%Y/%m/%d")
    roc = f"{date.year - 1911}/{date.month:02d}/{date.day:02d}"
    order = TPEX_URLS if not ok_endpoint["tpex"] else [ok_endpoint["tpex"]]
    for tpl in order:
        try:
            d = get_json(tpl.format(slash=slash, roc=roc))
            prices = extract_table(d, ("代號", "股票代號", "證券代號"), ("收盤", "收盤價"))
            if prices:
                ok_endpoint["tpex"] = tpl
                return prices
        except Exception:
            continue
        finally:
            time.sleep(SLEEP)
    return {}


def fetch_day(date):
    """回傳該日全市場收盤；上市無資料視為休市，不再查上櫃以節省請求"""
    twse = fetch_twse(date)
    if not twse:
        return None
    tpex = fetch_tpex(date)
    merged = {c: p for c, p in twse.items() if p}
    merged.update({c: p for c, p in tpex.items() if p})
    return merged, len(twse), len(tpex)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today()

    print(f"=== 回補最近 {TARGET_DAYS} 個交易日 ===")
    got = {}
    d = today
    for _ in range(MAX_CALENDAR_DAYS):
        if len(got) >= TARGET_DAYS:
            break
        if d.weekday() < 5:
            res = fetch_day(d)
            if res:
                prices, nt, no = res
                got[d.isoformat()] = prices
                print(f"  {d}  上市 {nt:>4} 檔　上櫃 {no:>4} 檔　1101={prices.get('1101')}")
            else:
                print(f"  {d}  休市或無資料")
        d -= datetime.timedelta(days=1)

    print(f"\n成功端點：上市 {ok_endpoint['twse']}")
    print(f"　　　　　上櫃 {ok_endpoint['tpex']}")
    if not ok_endpoint["tpex"]:
        print("⚠️ 上櫃兩個網址都失敗 → 上櫃股票的漲幅只能靠每日排程逐日累積。請把這段 log 貼出來修正。")

    # 合併：每日排程已寫入的日期不覆蓋（官方 OpenAPI 值優先）
    hist = {"days": []}
    if os.path.exists(PRICE_HISTORY_PATH):
        with open(PRICE_HISTORY_PATH, "r", encoding="utf-8") as f:
            hist = json.load(f)
    existing = {x["date"]: x for x in hist.get("days", [])}
    added = 0
    for date, prices in got.items():
        if date not in existing:
            existing[date] = {"date": date, "close": prices}
            added += 1
    days = sorted(existing.values(), key=lambda x: x["date"])[-KEEP_PRICE_DAYS:]
    with open(PRICE_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated_at": today.isoformat(), "days": days}, f,
                  ensure_ascii=False, separators=(",", ":"))
    print(f"\n股價歷史：新增 {added} 天，共 {len(days)} 個交易日（{days[0]['date']} ~ {days[-1]['date']}）")

    print(f"\n=== 年初基準價（{today.year} 年第一個交易日） ===")
    d = datetime.date(today.year, 1, 2)
    for _ in range(15):
        if d.weekday() < 5:
            res = fetch_day(d)
            if res:
                prices, nt, no = res
                with open(PRICE_BASE_PATH, "w", encoding="utf-8") as f:
                    json.dump({"year": str(today.year), "date": d.isoformat(), "close": prices},
                              f, ensure_ascii=False, separators=(",", ":"))
                print(f"  基準日 {d}：上市 {nt} 檔、上櫃 {no} 檔，1101={prices.get('1101')}")
                break
        d += datetime.timedelta(days=1)
    else:
        print("  ⚠️ 找不到今年第一個交易日的資料，今年以來漲幅會改用每日排程第一次執行日為基準")

    print("\n完成。接著手動執行一次 Daily Scan，漲幅欄位就會出現。")


if __name__ == "__main__":
    main()
