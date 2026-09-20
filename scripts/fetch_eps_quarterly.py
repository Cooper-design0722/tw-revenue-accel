"""
季報 EPS 抓取腳本 —— 取得「今年累計每股盈餘」

執行時機（一年四次，財報申報截止後幾天跑即可）：
    Q1  5/20 左右   （一般公司 5/15 前申報、金控 5/30，故建議 6/1 再補跑一次）
    Q2  8/20 左右   （8/14 前）
    Q3  11/20 左右  （11/14 前）
    Q4  隔年 4/5 左右（3/31 前）

為什麼取到的就是「今年累計」：
    台灣的綜合損益表本身是累計制——Q2 報表的每股盈餘就是上半年累計，
    Q3 是前三季累計。所以直接取「基本每股盈餘」欄位即可，不需自行加總。

⚠️ 端點探測：
    TWSE 的綜合損益表依產業切成多個端點變體（一般業、金控、銀行、證券、保險…），
    各變體互斥，一家公司只會出現在其中一支。變體的確切命名我無法事先確定，
    所以腳本會逐一探測下面的候選清單，把「成功」與「失敗」都印出來。
    跑完看 log：若某些產業的公司（例如金控股 2881、2882）抓不到，
    就是對應變體的路徑名稱不對，把 log 貼出來即可修正。

輸出：data/eps_quarterly.json
"""

import requests
import json
import os
import re
import datetime

OUTPUT_DIR = "data"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "eps_quarterly.json")

# 綜合損益表端點候選（依產業切分的變體），逐一探測
TWSE_BASE = "https://openapi.twse.com.tw/v1/opendata/"
TWSE_IS_VARIANTS = [
    "t187ap06_L_ci",    # 一般業
    "t187ap06_L_basi",  # 金融保險業(舊稱)
    "t187ap06_L_bd",    # 銀行業
    "t187ap06_L_fh",    # 金控業
    "t187ap06_L_ins",   # 保險業
    "t187ap06_L_mim",   # 證券業
    "t187ap06_L_otc",   # 其他
]
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1/"
TPEX_IS_VARIANTS = [
    "mopsfin_t187ap06_O_ci",
    "mopsfin_t187ap06_O_bd",
    "mopsfin_t187ap06_O_fh",
    "mopsfin_t187ap06_O_ins",
    "mopsfin_t187ap06_O_mim",
]

# 抽樣檢查用：跑完會印出這幾檔，方便你立刻核對
SPOT_CHECK = ["2330", "2881", "3661", "3037", "2884"]


def fetch_json(url, label, quiet=False):
    try:
        r = requests.get(url, timeout=40, headers={"User-Agent": "eps-quarterly/1.0"})
        if r.status_code == 404:
            if not quiet:
                print(f"  [404] {label} —— 此變體不存在")
            return None
        r.raise_for_status()
        r.encoding = "utf-8"
        d = r.json()
        if isinstance(d, dict):
            d = [d]
        return d
    except Exception as e:
        if not quiet:
            print(f"  [ERR] {label}：{e}")
        return None


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


def harvest(base, variants, market):
    """逐一探測變體，把有抓到的資料合併起來"""
    result = {}
    ok_variants, empty_variants = [], []

    for v in variants:
        url = base + v
        rows = fetch_json(url, v)
        if not rows:
            empty_variants.append(v)
            continue

        print(f"  [OK ] {v}：{len(rows)} 筆")
        print(f"        欄位：{list(rows[0].keys())[:12]}")

        got = 0
        for row in rows:
            code = pick(row, "公司代號", "SecuritiesCompanyCode", "CompanyCode")
            eps = to_float(pick(row, "基本每股盈餘（元）", "基本每股盈餘(元)",
                                "基本每股盈餘", "每股盈餘", "EPS"))
            year = pick(row, "年度", "Year")
            quarter = pick(row, "季別", "Quarter", "季")
            if code is None or eps is None:
                continue
            code = str(code).strip()
            # 同一家公司若在多個變體出現（理論上不會），保留季別較新的
            prev = result.get(code)
            if prev and (str(prev.get("quarter") or "") >= str(quarter or "")) \
                     and (str(prev.get("year") or "") >= str(year or "")):
                continue
            result[code] = {
                "eps_ytd": eps,
                "year": str(year) if year is not None else None,
                "quarter": str(quarter) if quarter is not None else None,
                "market": market,
                "source": v,
            }
            got += 1
        print(f"        -> 取得 {got} 家的每股盈餘")
        ok_variants.append(v)

    return result, ok_variants, empty_variants


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 70)
    print("探測 TWSE 上市綜合損益表變體")
    print("=" * 70)
    twse, ok_t, empty_t = harvest(TWSE_BASE, TWSE_IS_VARIANTS, "上市")

    print()
    print("=" * 70)
    print("探測 TPEx 上櫃綜合損益表變體")
    print("=" * 70)
    tpex, ok_p, empty_p = harvest(TPEX_BASE, TPEX_IS_VARIANTS, "上櫃")

    merged = {}
    merged.update(twse)
    for k, v in tpex.items():
        merged.setdefault(k, v)

    if not merged:
        print("\n[ERROR] 所有變體都沒抓到資料，中止（不覆蓋既有檔案）")
        print("        請把上面的 404／錯誤訊息貼出來，以便修正端點路徑")
        return

    # 季別分布：正常情況下絕大多數公司應該落在同一季
    dist = {}
    for v in merged.values():
        key = f"{v['year']}Q{v['quarter']}"
        dist[key] = dist.get(key, 0) + 1

    print()
    print("=" * 70)
    print("結果")
    print("=" * 70)
    print(f"成功變體：上市 {ok_t}，上櫃 {ok_p}")
    print(f"無資料　：上市 {empty_t}，上櫃 {empty_p}")
    print(f"合計取得 {len(merged)} 家公司的今年累計EPS")
    print(f"季別分布：{dict(sorted(dist.items(), key=lambda x: -x[1]))}")

    print()
    print("抽樣核對（拿去跟 Yahoo／Goodinfo 的累計EPS對照）：")
    for code in SPOT_CHECK:
        if code in merged:
            m = merged[code]
            print(f"  {code}：今年累計EPS {m['eps_ytd']}　"
                  f"（{m['year']} 年第 {m['quarter']} 季，{m['market']}，來源 {m['source']}）")
        else:
            print(f"  {code}：查無 —— 可能該產業變體未成功抓取")

    payload = {
        "updated_at": datetime.date.today().isoformat(),
        "quarter_distribution": dist,
        "ok_variants": {"twse": ok_t, "tpex": ok_p},
        "stocks": merged,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n已寫入 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
