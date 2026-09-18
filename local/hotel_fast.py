"""
Google Hotels 比价 — 提速版
---------------------------
相比 hotel_price_watch.py 的四项优化(均不涉及并发):

  1. 拦截 image/media/font 请求 —— Google Travel 图片极多, 只取文字不需要它们
  2. 6 秒死等 -> 轮询直到"页面上的价格数量不再增长"就立刻走
  3. REQUEST_GAP 5s -> 2s
  4. consent 弹窗只在第一次处理(cookie 会留在 context 里)

用法:
    python hotel_fast.py --bench     先跑对照基准, 比较 快/慢 两套配置的
                                     单次耗时 和 抓到的渠道数(证明没抓漏)
    python hotel_fast.py             按提速配置正式扫一遍
"""

import argparse
import base64
import csv
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

import offer_parse as _offer_parse

# ---------------------------------------------------------------- 配置区

BASE_DIR = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent

HOTELS = [
    "VR Queen Street Auckland",
    "VR Auckland City",
    "Shakespeare Hotel Auckland",
    "Auckland City Hotel",
    # 2026-09-18 核实加入: 20 Wyndham Street, Auckland CBD, 3 星。
    # 四个抽样日 87/79/145/104, 三个落在 NZ$50-110 段内。
    # 搜索结果页的房型设施写明 "Double Studio: ... 1 bedroom, 1 bathroom" —— 独立卫浴。
    "ibis budget Auckland Central",
    # 窗口缩短后容量富余, 补上这两家: 都在 CBD, 抽样日里出现过价位段内的报价,
    # 但波动大(Quadrant 96-301, Copthorne 111-579), 值不值得订要看实际分布。
    "The Quadrant Hotel & Suites Auckland",
    "Copthorne Hotel Auckland City",
    # 2026-09-18 用户提名并核实通过, 两家都在 CBD 且价位段内:
    #   Albion Hotel        119 Hobson Street  3星  09-22: 81, 10-06: 76
    #   Ascotia Off Queen   5 Scotia Place     3星  09-22: 64, 10-06: 79  <- 目前见过的最低
    # 卫浴情况未知 —— 那是订房前打电话确认的事, 不是不监测的理由。
    "Albion Hotel Auckland",
    "Ascotia Off Queen Auckland",
]

# Google 把 VR Auckland Airport (bookings.vrhotels.co.nz hotelID=116052) 的订房引擎
# 挂在了 VR Auckland City 页面的 "Official site" 位上。2026-09-17 跟着 lodging/clk
# 链接验证过 09-22 和 10-06 两个日期, 都落到 "VR Auckland Airport" 的 Property Details。
# 市区店在 188 Hobson Street, 机场店在 Mangere —— 那个价不是这家酒店的, 必须丢弃。
# (对照: VR Queen Street 的 Official site 落到 hotelID=110754, 是对的, 不受影响。)
PROVIDER_BLOCK = {
    "VR Auckland City": {"Official site"},
}

DATE_FROM = date(2026, 9, 16)
# 2026-09-18: 用户 10-16 之后不再每天进城, 位置约束只在这个窗口内成立。
# 窗口从 61 天收到 29 天, 省出的采集容量用来多盯几家 CBD 酒店。
DATE_TO = date(2026, 10, 16)

NIGHTS = 1
CURRENCY = "NZD"

PRICE_THRESHOLD = 100
CSV_PATH = BASE_DIR / "hotel_prices_v3.csv"   # v3: 解析器修好之后的干净数据
# 低于这个价的报价一律留存原始页面文本, 以后任何"捡漏"都能回溯到页面原文。
ARCHIVE_DIR = BASE_DIR / "raw_archive"
ARCHIVE_BELOW = 100
HEADLESS = True
PAGE_TIMEOUT = 45_000

# --- 提速参数 -------------------------------------------------------
BLOCK_RESOURCES = {"image", "media", "font"}
REQUEST_GAP = 2          # 原来 5
SETTLE_POLL_MS = 400     # 轮询间隔
SETTLE_STABLE_ROUNDS = 2 # 价格数量连续几轮不变就认为渲染完了
# "Sponsored·Featured options"(Booking.com/Hotels.com 在这块)是广告位, 渲染比
# "All options"(官网/Super.com)晚。只看"价格数稳定"会在赞助块到达前就退出,
# 结果每次只抓到 2 个渠道。这里给一个最短等待下限, 让赞助块有时间到。
SETTLE_MIN_MS = 0        # 实测只多 0.17 个渠道却多花 2.1s, 不划算
PRICE_WAIT_TIMEOUT = 15  # 秒, 轮询总上限

# --- 基准测试用的"原始配置", 用来做对照 -----------------------------
SLOW_GAP = 5
SLOW_BLIND_WAIT_MS = 6000

PROVIDERS = [
    "Booking.com", "Agoda", "Expedia", "Hotels.com", "Trip.com", "trivago",
    "Wotif", "Vio.com", "Bluepillow", "Priceline", "Stayforlong", "Travelup",
    "loveholidays", "Hotelopia", "eDreams", "Super.com", "Hotelplanner",
    "Algotels", "getaroom", "ZenHotels", "官方网站", "Official site",
]

PRICE_RE = re.compile(r"NZ\$\s*([\d,]+)")
# 目标酒店的报价区里既有 "NZ$102" 也有裸 "$451", 两种都要认
PRICE_ANY_RE = re.compile(r"(?:NZ)?\$\s*([\d,]+)")

# 页面在这些标题之后列的是"相似酒店/附近推荐"——那是别家酒店的价格, 必须截断。
# 不截断的话, 解析器会把 Rendezvous/Quest Takapuna 之类的报价算成目标酒店的。
SECTION_END = re.compile(
    r"similar hotels|nearby places|you might also like|other options nearby"
    r"|people also search|相似酒店|附近", re.I)
# 报价区从这里开始, 之前是导航/评分/地址
SECTION_START = re.compile(r"featured options|all options|prices|比价|报价", re.I)


# ---------------------------------------------------------------- URL 构造
#
# Google 旅行页把日期和货币编码在 ts= 参数里(protobuf + base64url)。


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


# ---------------------------------------------------------------- 解析


def scope_to_hotel(lines):
    """只保留目标酒店自己的报价区。

    Google 的酒店页在 "Similar hotels" 之后会列一串别家酒店的报价,
    格式和目标酒店的报价几乎一样。整页扫描会把它们混进来 —— 这是
    原脚本一直存在的问题, 会让最低价严重偏低(抓到的是隔壁更便宜的酒店)。
    """
    start = 0
    for i, ln in enumerate(lines):
        if SECTION_START.search(ln):
            start = i
            break
    end = len(lines)
    for i in range(start, len(lines)):
        if SECTION_END.search(lines[i]):
            end = i
            break
    return lines[start:end]


# 真报价永远独占一行: "$145" / "NZ$126"。假的都嵌在句子里:
#   "Save $33 if you stay Sep 20 - 21."   促销省钱额, 不是房价 <- 造成 NZ$32 假低价
#   "View more options from $121"          起价广告
#   "Stay 1 extra night for an avg ... $202"  两晚均摊价
# 所以只认"整行就是一个金额"的行。这比维护黑名单可靠得多。
PRICE_LINE_RE = re.compile(r"^(?:NZ)?\$\s*([\d,]+)(?:\.\d+)?$")

# 报价区的硬结尾。不截断的话解析器会一路吃到页面底部的促销文案和相似酒店。
HARD_STOP = re.compile(
    r"^track this hotel|^get emails|price range|prices are currently"
    r"|top things to know|similar hotels|nearby places|^show price",
    re.I)

ROOM_HINT = re.compile(
    r"room|suite|apartment|studio|bed|king|queen|twin|double|single|deluxe"
    r"|superior|standard|executive|penthouse|villa|房|套房|床", re.I)
NOT_ROOM = re.compile(
    r"^(visit site|get price|free wi-?fi|visit site for more|book now"
    r"|official site|track this hotel|get emails|save with|check )", re.I)
ROOM_DETAIL = re.compile(r"^\s*\d|^\s*·|guests?", re.I)


def parse_offers(text, with_rooms=False):
    """**已弃用**, 采集走的是 offer_parse.parse_offers。这里留作对照基准。

    两个已核实的缺陷见调用点的注释: 渠道白名单造成渠道名错位;
    报价区起点匹配 "prices" 会把搜索结果列表里别家酒店的报价圈进来。
    价格数值不受影响(整格最低价仍等于 Google 自己印的头条价), 错的是归属。
    """
    lines = scope_to_hotel([ln.strip() for ln in text.splitlines() if ln.strip()])
    best = {}
    cur_prov, cur_room = None, ""

    for line in lines:
        if HARD_STOP.search(line):
            break

        hit = next((p for p in PROVIDERS if p.lower() in line.lower()), None)
        if hit:
            cur_prov, cur_room = hit, ""
            continue

        m = PRICE_LINE_RE.match(line)
        if m:
            if cur_prov:
                _record(best, cur_prov, cur_room, m.group(1))
            continue

        if "$" in line:          # 嵌在句子里的金额: 直接丢弃
            continue

        if (cur_prov and not NOT_ROOM.match(line)
                and not ROOM_DETAIL.search(line) and ROOM_HINT.search(line)):
            cur_room = line[:60]

    if with_rooms:
        return best
    return {k: v[0] for k, v in best.items()}


def _record(best, prov, room, raw):
    try:
        price = int(raw.replace(",", ""))
    except ValueError:
        return
    if not (20 <= price <= 3000):
        return
    if prov not in best or price < best[prov][0]:
        best[prov] = (price, room)
    elif price == best[prov][0] and not best[prov][1] and room:
        best[prov] = (price, room)


# ---------------------------------------------------------------- 抓取


class Scraper:
    """consent 只处理一次; 资源拦截在 context 层一次性装好。"""

    def __init__(self, ctx, block=True):
        self.ctx = ctx
        self.consent_done = False
        if block:
            ctx.route("**/*", self._route)

    @staticmethod
    def _route(route):
        try:
            if route.request.resource_type in BLOCK_RESOURCES:
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass

    def _dismiss_consent(self, page):
        if self.consent_done:
            return
        for label in ["Reject all", "拒绝全部", "全部拒绝", "Accept all", "全部接受"]:
            try:
                btn = page.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    self.consent_done = True
                    return
            except Exception:
                pass
        # 没弹 consent 也算处理过了, 后面不用再试
        self.consent_done = True

    @staticmethod
    def _wait_settled(page):
        """轮询到价格数量连续 N 轮不变就走, 取代 6 秒死等。"""
        t0 = time.monotonic()
        deadline = t0 + PRICE_WAIT_TIMEOUT
        floor = t0 + SETTLE_MIN_MS / 1000
        last, stable = -1, 0
        text = ""
        while time.monotonic() < deadline:
            text = page.inner_text("body")
            n = len(PRICE_ANY_RE.findall(text))
            if n > 0 and n == last:
                stable += 1
                if stable >= SETTLE_STABLE_ROUNDS and time.monotonic() >= floor:
                    return text
            else:
                stable = 0
            last = n
            page.wait_for_timeout(SETTLE_POLL_MS)
        return text or page.inner_text("body")

    def fetch(self, page, hotel, checkin, slow=False):
        page.goto(build_url(hotel, checkin), timeout=PAGE_TIMEOUT,
                  wait_until="domcontentloaded")
        self._dismiss_consent(page)
        if slow:
            page.wait_for_timeout(SLOW_BLIND_WAIT_MS)
            try:
                page.get_by_text(PRICE_RE).first.wait_for(timeout=12_000)
            except Exception:
                pass
            text = page.inner_text("body")
        else:
            text = self._wait_settled(page)
        # 2026-09-18: 换成 offer_parse 的结构化解析器。旧的 parse_offers 有两个
        # 已核实的毛病, 都会让"这个价是谁家的"变成假的(价格本身仍是对的):
        #   * 渠道靠白名单认。名单外的渠道(HotelsCombined / nz.KAYAK.com /
        #     klook / Amimir.com …)那一行既不算渠道也不算房型, cur_prov 保持
        #     不变, 它的价格就记到了上一个渠道头上。
        #   * 报价区起点匹配 "prices", 而搜索结果页顶部的 "View prices" 就含这个
        #     词 —— 于是整个搜索结果列表(别家酒店)都被圈进了本店报价区。
        #     Wotif 就是这么进来的: CSV 里 1857 行, 而在 5181 张归档页面里,
        #     它只在 1 张的本店报价区真正出现过。
        # 旧函数留着, diagnose.py 还在用它做对照。
        parsed = _offer_parse.parse_offers(text, hotel)
        offers = {k: (v, parsed["rooms"].get(k, ""))
                  for k, v in parsed["offers"].items()}
        # 便宜的报价最可能是解析错误(历史上 NZ$32 就是把 "Save $33" 当成了房价)。
        # 把原始页面留下来, 事后可以逐条核对, 不必再靠回忆。
        # 丢掉已知归属错误的渠道(见 PROVIDER_BLOCK)
        blocked = PROVIDER_BLOCK.get(hotel, ())
        if blocked:
            offers = {k: v for k, v in offers.items() if k not in blocked}
        if offers and min(v[0] for v in offers.values()) < ARCHIVE_BELOW:
            try:
                ARCHIVE_DIR.mkdir(exist_ok=True)
                tag = f"{hotel[:12].replace(' ', '_')}_{checkin}_{datetime.now():%m%d-%H%M%S}"
                (ARCHIVE_DIR / f"{tag}.txt").write_text(text, encoding="utf-8")
            except Exception:
                pass
        return offers


# ---------------------------------------------------------------- 输出


def write_rows(stamp, hotel, checkin, offers):
    """offers 可以是 {provider: price} 或 {provider: (price, room_type)}。"""
    new_file = not CSV_PATH.exists()
    norm = {k: (v if isinstance(v, tuple) else (v, ""))
            for k, v in offers.items()}
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "hotel", "checkin", "provider",
                        "price_nzd", "room_type"])
        for provider, (price, room) in sorted(norm.items(), key=lambda kv: kv[1][0]):
            w.writerow([stamp, hotel, checkin.isoformat(), provider, price, room])


def date_range(start, end):
    d = max(start, date.today())
    while d <= end:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------- 基准测试


BENCH_DATES = [date(2026, 10, 16), date(2026, 10, 17),
               date(2026, 11, 1), date(2026, 11, 10)]


def bench():
    """同样的 (酒店,日期) 组合各跑一遍 慢/快 两套配置, 比较耗时和渠道数。
    先跑慢的再跑快的, 两者都在同一次运行里, 网络条件基本一致。"""
    combos = [(h, d) for d in BENCH_DATES for h in HOTELS]
    results = {}

    with sync_playwright() as pw:
        for mode, slow, block, gap in [("慢(原配置)", True, False, SLOW_GAP),
                                       ("快(提速后)", False, True, REQUEST_GAP)]:
            browser = pw.chromium.launch(headless=HEADLESS)
            ctx = browser.new_context(locale="en-NZ",
                                      timezone_id="Pacific/Auckland",
                                      viewport={"width": 1400, "height": 1000})
            sc = Scraper(ctx, block=block)
            page = ctx.new_page()
            times, counts = [], []
            print(f"\n===== {mode} =====", flush=True)
            for hotel, d in combos:
                t0 = time.monotonic()
                try:
                    offers = sc.fetch(page, hotel, d, slow=slow)
                except Exception as e:
                    print(f"  {d} {hotel}: 失败 {type(e).__name__}", flush=True)
                    continue
                dt = time.monotonic() - t0
                times.append(dt)
                counts.append(len(offers))
                cheapest = min(offers.values()) if offers else "-"
                print(f"  {d} {hotel[:24]:24} {dt:5.1f}s  "
                      f"{len(offers)} 家  最低 NZ${cheapest}", flush=True)
                time.sleep(gap)
            browser.close()
            results[mode] = (times, counts, gap)

    print("\n" + "=" * 62)
    print(f"{'配置':14} {'平均抓取':>9} {'+间隔':>7} {'单次合计':>9} "
          f"{'平均渠道数':>10}")
    print("-" * 62)
    summary = {}
    for mode, (times, counts, gap) in results.items():
        if not times:
            continue
        avg = statistics.mean(times)
        total = avg + gap
        avg_c = statistics.mean(counts)
        summary[mode] = (total, avg_c)
        print(f"{mode:14} {avg:8.1f}s {gap:6}s {total:8.1f}s {avg_c:10.2f}")
    print("-" * 62)

    if len(summary) == 2:
        (slow_t, slow_c), (fast_t, fast_c) = summary.values()
        n = len(list(date_range(DATE_FROM, DATE_TO))) * len(HOTELS)
        print(f"\n全量 {n} 次请求预计耗时:")
        print(f"  原配置: {n * slow_t / 60:5.1f} 分钟")
        print(f"  提速后: {n * fast_t / 60:5.1f} 分钟"
              f"   ({slow_t / fast_t:.1f}x)")
        if fast_c < slow_c - 0.01:
            print(f"\n⚠ 提速后平均渠道数 {fast_c:.2f} < 原来 {slow_c:.2f}, "
                  f"说明等得还不够久, 建议调大 SETTLE_STABLE_ROUNDS")
        else:
            print(f"\n✓ 渠道数没有下降 ({fast_c:.2f} vs {slow_c:.2f}), 提速没有代价")


# ---------------------------------------------------------------- 正式扫描


def sweep():
    dates = list(date_range(DATE_FROM, DATE_TO))
    combos = [(h, d) for d in dates for h in HOTELS]
    total = len(combos)
    t_start = time.monotonic()
    print(f"共 {len(dates)} 天 x {len(HOTELS)} 家 = {total} 次请求\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(locale="en-NZ",
                                  timezone_id="Pacific/Auckland",
                                  viewport={"width": 1400, "height": 1000})
        sc = Scraper(ctx, block=True)
        page = ctx.new_page()
        try:
            for n, (hotel, d) in enumerate(combos, 1):
                t0 = time.monotonic()
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    offers = sc.fetch(page, hotel, d)
                except Exception as e:
                    print(f"[{n}/{total}] {d} {hotel}: 失败 {type(e).__name__}",
                          flush=True)
                    continue
                if not offers:
                    print(f"[{n}/{total}] {d} {hotel}: 无报价", flush=True)
                    continue
                write_rows(stamp, hotel, d, offers)
                prov, (price, _room) = min(offers.items(),
                                           key=lambda kv: kv[1][0])
                wd = "一二三四五六日"[d.weekday()]
                flag = "  ***低价***" if price < PRICE_THRESHOLD else ""
                eta = (time.monotonic() - t_start) / n * (total - n) / 60
                print(f"[{n}/{total}] {d} 周{wd} {hotel[:24]:24} "
                      f"{prov} NZ${price} ({len(offers)}家) "
                      f"{time.monotonic() - t0:4.1f}s 剩~{eta:.0f}分{flag}",
                      flush=True)
                time.sleep(REQUEST_GAP)
        except KeyboardInterrupt:
            print("\n已停止")
        finally:
            browser.close()
    print(f"\n用时 {(time.monotonic() - t_start) / 60:.1f} 分钟, "
          f"数据在 {CSV_PATH}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", action="store_true",
                    help="跑对照基准, 比较提速前后的耗时和抓取完整度")
    args = ap.parse_args()
    bench() if args.bench else sweep()


if __name__ == "__main__":
    main()
