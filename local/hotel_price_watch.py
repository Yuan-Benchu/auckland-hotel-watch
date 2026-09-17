"""
Google Hotels 比价监控 v3
-------------------------
两种模式:
  MODE = "sweep"  扫一遍 DATE_FROM..DATE_TO 之间每一天, 存 CSV, 跑完退出
  MODE = "watch"  只盯 WATCH_DATES 里的几个日期, 每 INTERVAL_MINUTES 循环一次

先跑 sweep 拿全景曲线, 再改成 watch 做日常监控。

安装(只做一次):
    python -m pip install playwright
    python -m playwright install chromium

运行:
    python hotel_price_watch.py

停止: Ctrl+C (sweep 中断后重跑会自动跳过当天已抓过的日期)
"""

import base64
import csv
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- 配置区

MODE = "sweep"                  # "sweep" 或 "watch"

HOTELS = [
    "VR Queen Street Auckland",
    "VR Auckland City",
]

# --- sweep 模式用: 扫描区间(含两端)。过去的日期没有报价, 会自动跳过。
DATE_FROM = date(2026, 9, 16)
DATE_TO = date(2026, 11, 17)

# --- watch 模式用: 只盯这几天
WATCH_DATES = [
    date(2026, 10, 16),
    date(2026, 11, 1),
    date(2026, 11, 10),
]

NIGHTS = 1
CURRENCY = "NZD"

INTERVAL_MINUTES = 60           # watch 模式每轮间隔
PRICE_THRESHOLD = 100           # 低于这个价(NZD/晚)就提醒
REQUEST_GAP = 5                 # 每次请求之间停几秒, 别调太小
CSV_PATH = "hotel_prices.csv"
HEADLESS = True
PAGE_TIMEOUT = 45_000

PROVIDERS = [
    "Booking.com", "Agoda", "Expedia", "Hotels.com", "Trip.com", "trivago",
    "Wotif", "Vio.com", "Bluepillow", "Priceline", "Stayforlong", "Travelup",
    "loveholidays", "Hotelopia", "eDreams", "官方网站", "Official site",
]

PRICE_RE = re.compile(r"NZ\$\s*([\d,]+)")

# ---------------------------------------------------------------- URL 构造
#
# Google 旅行页把日期和货币编码在 ts= 参数里(protobuf + base64url)。
# 直接传 checkin=/checkout= 会被忽略, 必须用 ts。


def _varint(n):
    out = b""
    while True:
        chunk = n & 0x7F
        n >>= 7
        out += bytes([chunk | (0x80 if n else 0)])
        if not n:
            return out


def _day(d):
    return (bytes([0x08]) + _varint(d.year)
            + bytes([0x10]) + _varint(d.month)
            + bytes([0x18]) + _varint(d.day))


def build_ts(checkin, checkout, currency=CURRENCY):
    a, b = _day(checkin), _day(checkout)
    inner = bytes([0x0A, len(a)]) + a + bytes([0x12, len(b)]) + b
    lvl2 = bytes([0x12, len(inner)]) + inner
    lvl1 = bytes([0x12, len(lvl2)]) + lvl2
    msg = bytes([0x08, 0x00, 0x1A, len(lvl1)]) + lvl1 + bytes([0x32, 0x02, 0x08, 0x00])
    c = currency.encode()
    msg += bytes([0x2A, len(c) + 4, 0x0A, len(c) + 2, 0x3A, len(c)]) + c
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def build_url(hotel, checkin):
    checkout = checkin + timedelta(days=NIGHTS)
    return (f"https://www.google.com/travel/search?q={quote(hotel)}"
            f"&ts={build_ts(checkin, checkout)}")


def date_range(start, end):
    today = date.today()
    d = max(start, today)                 # 过去的日期没有报价
    while d <= end:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------- 抓取


def dismiss_consent(page):
    for label in ["Reject all", "拒绝全部", "全部拒绝", "Accept all", "全部接受"]:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=3000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass


def parse_offers(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    offers = {}
    for i, line in enumerate(lines):
        m = PRICE_RE.search(line)
        if not m:
            continue
        price = int(m.group(1).replace(",", ""))
        if not (20 <= price <= 3000):
            continue
        for j in range(i, max(-1, i - 4), -1):
            hit = next((p for p in PROVIDERS if p.lower() in lines[j].lower()), None)
            if hit:
                offers.setdefault(hit, price)
                break
    return offers


def scrape(page, hotel, checkin):
    page.goto(build_url(hotel, checkin), timeout=PAGE_TIMEOUT,
              wait_until="domcontentloaded")
    dismiss_consent(page)
    page.wait_for_timeout(6000)
    try:
        page.get_by_text(PRICE_RE).first.wait_for(timeout=12_000)
    except Exception:
        pass
    return parse_offers(page.inner_text("body"))


def write_rows(stamp, hotel, checkin, offers):
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "hotel", "checkin", "provider", "price_nzd"])
        for provider, price in sorted(offers.items(), key=lambda kv: kv[1]):
            w.writerow([stamp, hotel, checkin.isoformat(), provider, price])


def already_done_today():
    """返回今天已经抓过的 (hotel, checkin) 集合, 用于中断后续跑。"""
    done = set()
    if not os.path.exists(CSV_PATH):
        return done
    today = date.today().isoformat()
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["timestamp"].startswith(today):
                done.add((row["hotel"], row["checkin"]))
    return done


def alert(hotel, checkin, provider, price):
    print(f"  *** {hotel} {checkin}: {provider} NZ${price} "
          f"— 低于 {PRICE_THRESHOLD} ***")
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(880, 400)
        except Exception:
            pass


def run_one(page, hotel, checkin):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        offers = scrape(page, hotel, checkin)
    except Exception as e:
        print(f"{checkin}  {hotel}: 失败 — {type(e).__name__}")
        return
    if not offers:
        print(f"{checkin}  {hotel}: 无报价(可能已售罄)")
        return
    write_rows(stamp, hotel, checkin, offers)
    prov, price = min(offers.items(), key=lambda kv: kv[1])
    weekday = "一二三四五六日"[checkin.weekday()]
    print(f"{checkin} 周{weekday}  {hotel}: {prov} NZ${price} ({len(offers)} 家)")
    if price < PRICE_THRESHOLD:
        alert(hotel, checkin, prov, price)


# ---------------------------------------------------------------- 主流程


def sweep(page):
    dates = list(date_range(DATE_FROM, DATE_TO))
    done = already_done_today()
    todo = [(h, d) for h in HOTELS for d in dates
            if (h, d.isoformat()) not in done]
    total = len(todo)
    if not total:
        print("今天已经扫过一遍了。想重扫就改 CSV_PATH 或删掉 csv。")
        return
    eta = total * (REQUEST_GAP + 8) / 60
    print(f"共 {len(dates)} 天 × {len(HOTELS)} 家 = {total} 次请求, "
          f"预计 {eta:.0f} 分钟。中断后重跑会自动续上。\n")
    for n, (hotel, d) in enumerate(todo, 1):
        print(f"[{n}/{total}] ", end="")
        run_one(page, hotel, d)
        time.sleep(REQUEST_GAP)
    print(f"\n扫描完成。数据在 {os.path.abspath(CSV_PATH)}")


def watch(page):
    while True:
        print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===")
        for hotel in HOTELS:
            for d in WATCH_DATES:
                run_one(page, hotel, d)
                time.sleep(REQUEST_GAP)
        print(f"下轮: {INTERVAL_MINUTES} 分钟后")
        time.sleep(INTERVAL_MINUTES * 60)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            locale="en-NZ",
            timezone_id="Pacific/Auckland",
            viewport={"width": 1400, "height": 1000},
        )
        page = ctx.new_page()
        try:
            (sweep if MODE == "sweep" else watch)(page)
        except KeyboardInterrupt:
            print(f"\n已停止。数据在 {os.path.abspath(CSV_PATH)}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
