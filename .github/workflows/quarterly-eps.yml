# 放置路徑：.github/workflows/quarterly-eps.yml
#
# 季報 EPS（今年累計每股盈餘）抓取。一年只需要跑幾次。
#
# 財報申報期限（一般公司／金控·KY股）：
#   Q1    5/15  ／ 5/30
#   Q2    8/14  ／ 8/31
#   Q3    11/14 ／ 11/29
#   年報  3/31  ／ 3/31
#
# 排程刻意在每個期限後跑「兩次」：
#   第一次在一般公司期限後幾天，先把多數公司抓進來；
#   第二次在月底，補上金控與 KY 股（它們的期限晚兩週左右）。
# 每次都是整份覆蓋，重跑沒有副作用。
#
# cron '0 1 D M *' = 該月 D 日 UTC 01:00 = 台北時間 09:00

name: Quarterly EPS

on:
  schedule:
    - cron: '0 1 20 5 *'    # 5/20  Q1 第一輪
    - cron: '0 1 2 6 *'     # 6/02  Q1 補金控、KY
    - cron: '0 1 20 8 *'    # 8/20  Q2 第一輪
    - cron: '0 1 2 9 *'     # 9/02  Q2 補
    - cron: '0 1 20 11 *'   # 11/20 Q3 第一輪
    - cron: '0 1 2 12 *'    # 12/02 Q3 補
    - cron: '0 1 5 4 *'     # 4/05  年報
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: data-commit
  cancel-in-progress: false

jobs:
  eps:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests

      - name: Fetch quarterly EPS
        run: python scripts/fetch_eps_quarterly.py

      - name: Commit and push results
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git pull --rebase --autostash || true
          git add data/
          git diff --staged --quiet || git commit -m "chore: quarterly EPS $(date -u +%Y-%m-%d)"
          git push
