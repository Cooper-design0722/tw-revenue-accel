"""
每日 CSV 匯出並寄送 Email

把全市場個股的月營收 + 估值資料合併成一份 CSV，
透過 Gmail SMTP 寄到指定信箱。

執行時機：Daily Scan 排程的最後一步（量能、估值抓完之後才寄）
所需環境變數（存在 GitHub Secrets）：
    GMAIL_APP_PASSWORD   Gmail 應用程式密碼（16 碼）

寄件帳號與收件信箱寫死在下面，不需要存 Secret：
    SENDER_EMAIL = "as0981014778@gmail.com"
    RECIPIENT_EMAIL = "as0981014778@gmail.com"
"""

import json
import os
import csv
import io
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

SENDER_EMAIL = "as0981014778@gmail.com"
RECIPIENT_EMAIL = "as0981014778@gmail.com"

DATA_DIR = "data"
REVENUE_PATH = os.path.join(DATA_DIR, "latest.json")
VALUATION_PATH = os.path.join(DATA_DIR, "valuation_latest.json")


def load_json(path, label):
    if not os.path.exists(path):
        print(f"[WARN] 找不到 {path}，{label} 欄位將為空")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 讀取 {path} 失敗：{e}")
        return {}


def build_csv():
    """合併月營收與估值資料，產出 CSV 字串（UTF-8 with BOM，Excel 直接開不亂碼）"""
    rev = load_json(REVENUE_PATH, "月營收")
    val = load_json(VALUATION_PATH, "估值")

    stocks = rev.get("all_stocks", [])
    val_stocks = val.get("stocks", {})
    data_ym = rev.get("data_ym", "")
    trade_date = val.get("trade_date", "")

    if not stocks:
        print("[ERROR] 月營收資料為空，無法產出 CSV")
        return None, None, None

    # 欄位順序
    headers = [
        "股號", "公司名稱", "市場", "產業別", "市值規模", "市值(億)",
        "當月營收(千元)", "累計營收(千元)",
        "當月年增率(%)", "累計年增率(%)", "月增率(%)",
        "收盤價", "今年累計EPS", "財報年", "財報季",
        "本益比(累計EPS)", "年化本益比", "近四季EPS"
    ]

    def esc(v):
        if v is None:
            return ""
        s = str(v)
        return f'"{s.replace(chr(34), chr(34)+chr(34))}"' if (',' in s or '"' in s or '\n' in s) else s

    rows = []
    for r in stocks:
        code = r.get("code", "")
        v = val_stocks.get(code, {})
        cap_yi = f"{r['market_cap_k']/100000:.1f}" if r.get("market_cap_k") else ""
        row = [
            code,
            r.get("name", ""),
            r.get("market", ""),
            r.get("industry", ""),
            r.get("size_tier", ""),
            cap_yi,
            r.get("cur_revenue_k", ""),
            r.get("cum_revenue_k", ""),
            r.get("cur_yoy", ""),
            r.get("cum_yoy", ""),
            r.get("mom", ""),
            v.get("close", ""),
            v.get("eps_ytd", ""),
            v.get("eps_year", ""),
            v.get("eps_quarter", ""),
            v.get("pe", ""),
            v.get("pe_annualized", ""),
            v.get("eps_ttm", ""),
        ]
        rows.append([esc(c) for c in row])

    # UTF-8 BOM 讓 Excel 直接開中文不亂碼
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM
    buf.write(",".join(headers) + "\r\n")
    for row in rows:
        buf.write(",".join(row) + "\r\n")

    csv_content = buf.getvalue()
    print(f"CSV 產出：{len(rows)} 筆，{len(csv_content)//1024} KB")
    return csv_content, data_ym, trade_date


def send_email(csv_content, data_ym, trade_date):
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not password:
        print("[ERROR] 找不到 GMAIL_APP_PASSWORD，請確認 GitHub Secrets 已設定")
        return False

    today = datetime.date.today().strftime("%Y-%m-%d")
    filename = f"台股月營收_{data_ym}_{trade_date or today}.csv"

    # 組信件
    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg["Subject"] = f"台股月營收每日匯出 {today}（資料年月 {data_ym}，股價 {trade_date or '—'}）"

    body = f"""每日自動匯出，請見附件。

資料年月：{data_ym}
股價日期：{trade_date or "（估值資料尚未產生）"}
個股筆數：{csv_content.count(chr(10)) - 1} 檔
匯出時間：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")} (UTC)

欄位說明：
  本益比(累計EPS) = 當天收盤價 ÷ 今年累計EPS
  年化本益比       = 收盤價 ÷ (累計EPS ÷ 已過季數 × 4)，可跨季比較
  近四季EPS       = 官方本益比反推，與交易所口徑一致，供參考

注意：今年累計EPS 一季更新一次（財報申報後），
     其餘欄位每個交易日更新。

---
此信由 GitHub Actions 自動寄出
"""
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # 附件
    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_content.encode("utf-8-sig"))
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    # 寄信
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(SENDER_EMAIL, password)
            smtp.send_message(msg)
        print(f"[OK] 已寄出 → {RECIPIENT_EMAIL}（附件：{filename}）")
        return True
    except smtplib.SMTPAuthenticationError:
        print("[ERROR] Gmail 認證失敗 —— 請確認：")
        print("        1. 兩步驟驗證已開啟")
        print("        2. 用的是「應用程式密碼」，不是 Gmail 登入密碼")
        print("        3. GitHub Secret 名稱是 GMAIL_APP_PASSWORD（全大寫）")
        return False
    except Exception as e:
        print(f"[ERROR] 寄信失敗：{e}")
        return False


def main():
    print("=== 產出 CSV ===")
    csv_content, data_ym, trade_date = build_csv()
    if csv_content is None:
        return

    print("\n=== 寄送 Email ===")
    ok = send_email(csv_content, data_ym, trade_date)
    if not ok:
        # 寄信失敗時讓 GitHub Actions 標記失敗，讓你知道有問題
        raise SystemExit(1)


if __name__ == "__main__":
    main()
