"""
Google Hotels 比价 — 自动轮转版 (供 Windows 任务计划程序每 30 分钟调用一次)
---------------------------------------------------------------------------
与 hotel_price_watch.py 的区别:

  * 跑一轮就退出, 不自己 sleep 循环 —— 由任务计划程序负责定时
  * 任务列表按 "日期 x 两家酒店交错" 排列, 任何一轮被掐断, 两家酒店覆盖度都均等
  * 时间预算 (BUDGET_MINUTES) 到了就停, 游标存盘, 下一轮接着跑, 跑完一圈自动回头
  * 文件锁: 上一轮没跑完时这一轮直接跳过, 绝不会两个 Chromium 同时开
  * 每次抓取失败自动重试一次
  * 全部输出同时写 hotel_watch.log, 后台跑也看得见

手动跑一轮:  python hotel_watch_auto.py
查看日志:    type %USERPROFILE%\\Downloads\\hotel_watch.log
"""

import base64
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- 配置区

BASE_DIR = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent

HOTELS = [
    "VR Queen Street Auckland",
    "VR Auckland City",
]

# TARGET = "range" 扫 DATE_FROM..DATE_TO 全区间(自动分片轮转)
# TARGET = "watch" 只盯 WATCH_DATES 那几天(每轮跑满整圈)
TARGET = "range"

DATE_FROM = date(2026, 9, 16)
DATE_TO = date(2026, 11, 17)

WATCH_DATES = [
    date(2026, 10, 16),
    date(2026, 11, 1),
    date(2026, 11, 10),
]

NIGHTS = 1
CURRENCY = "NZD"

BUDGET_MINUTES = 25      # 每轮最多跑这么久, 给 30 分钟的触发间隔留 5 分钟余量
PRICE_THRESHOLD = 100    # 低于这个价(NZD/晚)记一条提醒
REQUEST_GAP = 5          # 每次请求之间停几秒
RETRIES = 1              # 每个 (日期,酒店) 失败后额外重试几次
HEADLESS = True
PAGE_TIMEOUT = 45_000
STALE_LOCK_MINUTES = 40  # 锁文件超过这个时间视为上一轮崩了, 强行接管

CSV_PATH = BASE_DIR / "hotel_prices.csv"
LOG_PATH = BASE_DIR / "hotel_watch.log"
ALERT_PATH = BASE_DIR / "hotel_alerts.log"
STATE_PATH = BASE_DIR / ".hotel_watch_state.json"
LOCK_PATH = BASE_DIR / ".hotel_watch.lock"

PROVIDERS = [
    "Booking.com", "Agoda", "Expedia", "Hotels.com", "Trip.com", "trivago",
    "Wotif", "Vio.com", "Bluepillow", "Priceline", "Stayforlong", "Travelup",
    "loveholidays", "Hotelopia", "eDreams", "官方网站", "Official site",
]

PRICE_RE = re.compile(r"NZ\$\s*([\d,]+)")
PRICE_RE_FALLBACK = re.compile(r"(?<!NZ)\$\s*([\d,]+)")

# ---------------------------------------------------------------- 日志


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
    rx = PRICE_RE if PRICE_RE.search(text) else PRICE_RE_FALLBACK
    offers = {}
    for i, line in enumerate(lines):
        m = rx.search(line)
        if not m:
            continue
        price = int(m.group(1).replace(",", ""))
        if not (20 <= price <= 3000):
            continue
        for j in range(i, max(-1, i - 4), -1):
            hit = next((p for p in PROVIDERS if p.lower() in lines[j].lower()), None)
            if hit:
                # 同一家渠道出现多次时保留最低价
                if hit not in offers or price < offers[hit]:
                    offers[hit] = price
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
    new_file = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "hotel", "checkin", "provider", "price_nzd"])
        for provider, price in sorted(offers.items(), key=lambda kv: kv[1]):
            w.writerow([stamp, hotel, checkin.isoformat(), provider, price])


def alert(hotel, checkin, provider, price):
    msg = (f"*** {hotel} {checkin}: {provider} NZ${price} "
           f"— 低于 {PRICE_THRESHOLD} ***")
    log("  " + msg)
    try:
        with open(ALERT_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def run_one(page, hotel, checkin, prefix=""):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    offers = None
    for attempt in range(RETRIES + 1):
        try:
            offers = scrape(page, hotel, checkin)
            break
        except Exception as e:
            if attempt < RETRIES:
                log(f"{prefix}{checkin}  {hotel}: {type(e).__name__}, 重试中")
                time.sleep(REQUEST_GAP)
            else:
                log(f"{prefix}{checkin}  {hotel}: 失败 — {type(e).__name__}")
                return
    if not offers:
        log(f"{prefix}{checkin}  {hotel}: 无报价(可能已售罄)")
        return
    write_rows(stamp, hotel, checkin, offers)
    prov, price = min(offers.items(), key=lambda kv: kv[1])
    weekday = "一二三四五六日"[checkin.weekday()]
    log(f"{prefix}{checkin} 周{weekday}  {hotel}: {prov} NZ${price} "
        f"({len(offers)} 家)")
    if price < PRICE_THRESHOLD:
        alert(hotel, checkin, prov, price)


# ---------------------------------------------------------------- 任务列表与游标


def target_dates():
    today = date.today()
    if TARGET == "watch":
        return [d for d in WATCH_DATES if d >= today]
    out, d = [], max(DATE_FROM, today)     # 过去的日期没有报价
    while d <= DATE_TO:
        out.append(d)
        d += timedelta(days=1)
    return out


def build_tasks():
    """按日期交错: (d1,酒店A) (d1,酒店B) (d2,酒店A) (d2,酒店B) ...
    这样任何一轮被时间预算掐断, 两家酒店的覆盖天数都相同。"""
    return [(d, h) for d in target_dates() for h in HOTELS]


def load_cursor(signature):
    try:
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if st.get("signature") == signature:
            return int(st.get("cursor", 0))
    except Exception:
        pass
    return 0      # 配置变了(或首次运行)就从头来


def save_cursor(signature, cursor):
    try:
        STATE_PATH.write_text(
            json.dumps({"signature": signature, "cursor": cursor,
                        "updated": datetime.now().isoformat(timespec="seconds")},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 文件锁


def acquire_lock():
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < STALE_LOCK_MINUTES * 60:
            log(f"上一轮还在跑({age / 60:.0f} 分钟前启动), 本轮跳过")
            return False
        log(f"发现 {age / 60:.0f} 分钟前的残留锁, 强行接管")
    LOCK_PATH.write_text(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}",
                         encoding="utf-8")
    return True


def release_lock():
    try:
        LOCK_PATH.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程


def cycle(page, tasks, signature):
    cursor = load_cursor(signature)
    total = len(tasks)
    deadline = time.monotonic() + BUDGET_MINUTES * 60
    log(f"本轮开始: 共 {total} 个目标, 从第 {cursor + 1} 个接着跑, "
        f"预算 {BUDGET_MINUTES} 分钟")

    done = 0
    while done < total:
        if time.monotonic() > deadline:
            log(f"时间预算用完: 本轮跑了 {done} 个, 下轮从第 {cursor + 1} 个继续")
            break
        d, hotel = tasks[cursor]
        run_one(page, hotel, d, prefix=f"[{done + 1}/{total}] ")
        cursor = (cursor + 1) % total
        done += 1
        save_cursor(signature, cursor)
        if done < total:
            time.sleep(REQUEST_GAP)
    else:
        log(f"本轮跑完整整一圈 ({total} 个)")

    save_cursor(signature, cursor)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not acquire_lock():
        return

    try:
        tasks = build_tasks()
        if not tasks:
            log("没有可抓的日期(区间已全部过期), 退出")
            return

        signature = json.dumps(
            {"target": TARGET, "hotels": HOTELS, "n": len(tasks),
             "first": tasks[0][0].isoformat(), "last": tasks[-1][0].isoformat()},
            ensure_ascii=False, sort_keys=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=HEADLESS)
            ctx = browser.new_context(
                locale="en-NZ",
                timezone_id="Pacific/Auckland",
                viewport={"width": 1400, "height": 1000},
            )
            page = ctx.new_page()
            try:
                cycle(page, tasks, signature)
            finally:
                browser.close()
    except KeyboardInterrupt:
        log("手动中断")
    except Exception as e:
        log(f"本轮异常退出: {type(e).__name__}: {e}")
    finally:
        release_lock()
        log(f"本轮结束, 数据在 {CSV_PATH}")


if __name__ == "__main__":
    main()
