# -*- coding: utf-8 -*-
"""VR(以及任意一家)酒店的**渠道级**价格监测。

## 为什么要单独一个脚本

`watch_status.py` 盯的是"某家某晚的最低价动没动", 答不了"哪个渠道在卖、
贵多少、换没换人"。而换渠道恰恰能省钱: 9/21 那晚 Ascotia 的最低价在
HotelsCombined, Super.com 要贵 NZ$2; VR Queen Street 则是 Super.com 最低、
官网贵 NZ$21。这个差价只有拆到渠道才看得见。

## 数据从哪来 —— 只有一个来源

**云端 DIDA 没有渠道**。它的字段是 ts_utc/hotel/checkin/room/price/currency/
cancelable/meal/n_plans —— 那是批发商的房型方案, 不是 OTA 渠道。所以渠道级
监测**只能**建立在本地 Google 抓取上, 也就跟着它一起依赖 Yuan 开机。

本地这边又有两份, 可信度不一样:

* `data/snapshots/raw_archive.tgz` —— 页面原文。用 `offer_parse` 重新解析,
  渠道名**可信**。代价是只存了"出现过 NZ$100 以下报价"的页面(ARCHIVE_BELOW),
  贵的日子没有存档。
* `data/snapshots/hotel_prices_v3_*.csv` —— 有 provider 列, 覆盖全, 但
  **2026-09-19 之前采的渠道名一律不可信**(旧白名单解析器会把名单外渠道的
  价格记到上一个渠道头上)。CSV 里 VR 两家有 460 行记在 Wotif 名下, 而 5181
  张存档里 Wotif 真正出现在本店报价区的只有 1 张 —— 差距就是这么来的。

所以本脚本**只用存档算渠道**, CSV 只用来做一件事: 统计两边对不上的比例,
也就是下面那张"CSV 渠道名有多不可信"的表。

## 溢价怎么算

渠道的绝对价格跨日期不可比(9/25 全城翻倍)。所以每页先取该页最低价做基准,
渠道溢价 = 该渠道价 / 本页最低价 - 1。这样"Super.com 通常最低""官网通常贵
两成"这类结论才跨日期成立。
"""
import argparse
import collections
import json
import os
import re
import statistics
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SNAP_DIR = HERE / "data" / "snapshots"
STATE = HERE / "data" / ".channel_state.json"

sys.path.insert(0, str(HERE))
import offer_parse  # noqa: E402

NZ = ZoneInfo("Pacific/Auckland")

# 这条报价路由到 bookings.vrhotels.co.nz/116052 = VR Auckland **Airport**,
# 不是市区店。任何统计前先剔, 见 CLAUDE.md。
DROP = {("VR Auckland City", "Official site")}

# 存档文件名是 hotel[:12].replace(" ", "_"), 会撞车, 所以反查要用完整名单
ARCHIVE_NAME_LEN = 12


def find_archive():
    t = SNAP_DIR / "raw_archive.tgz"
    return t if t.exists() else None


def hotel_lookup():
    """从最新快照的 hotel 列反推"文件名前缀 -> 完整酒店名"。"""
    import csv
    snaps = sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv"))
    if not snaps:
        return {}, None
    with open(snaps[-1], encoding="utf-8-sig") as fh:
        names = sorted({r["hotel"] for r in csv.DictReader(fh)})
    return {h[:ARCHIVE_NAME_LEN].replace(" ", "_"): h for h in names}, snaps[-1]


FN_RE = re.compile(r"(.+?)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{6})\.txt$")


def scan_archive(tgz, pre2hotel, want):
    """-> {(酒店, 入住日, 抓取戳): {渠道: 价格}}，只保留 want 匹配的酒店。"""
    out = {}
    with tarfile.open(tgz) as tf, tempfile.TemporaryDirectory() as tmp:
        try:
            tf.extractall(tmp, filter="data")
        except TypeError:          # Python < 3.12 没有 filter=
            tf.extractall(tmp)
        for path in Path(tmp).rglob("*.txt"):
            m = FN_RE.match(path.name)
            if not m:
                continue
            pre, checkin, stamp = m.groups()
            hotel = pre2hotel.get(pre)
            if hotel is None or not want(hotel):
                continue
            res = offer_parse.parse_offers(
                path.read_text(encoding="utf-8", errors="replace"), hotel)
            offers = {k: v for k, v in res["offers"].items() if (hotel, k) not in DROP}
            if offers:
                out[(hotel, checkin, stamp)] = offers
    return out


def profiles(pages):
    """每个渠道的画像: 出现、当最低、溢价、变价次数。"""
    seen = collections.Counter()
    cheapest = collections.Counter()
    prem = collections.defaultdict(list)
    series = collections.defaultdict(dict)      # (酒店,入住日,渠道) -> {戳: 价}
    for (h, ci, st), offers in pages.items():
        lo = min(offers.values())
        best = [k for k, v in offers.items() if v == lo]
        for k, v in offers.items():
            seen[k] += 1
            if k in best:
                cheapest[k] += 1
            if lo > 0:
                prem[k].append(v / lo - 1)
            series[(h, ci, k)][st] = v
    moves = collections.Counter()
    for (h, ci, k), s in series.items():
        vals = [s[t] for t in sorted(s)]
        moves[k] += sum(1 for a, b in zip(vals, vals[1:]) if a != b)
    return seen, cheapest, prem, moves, series


def latest_by_cell(pages):
    """每个 (酒店, 入住日) 的最后一张存档。"""
    last = {}
    for (h, ci, st), offers in pages.items():
        k = (h, ci)
        if k not in last or st > last[k][0]:
            last[k] = (st, offers)
    return last


def csv_disagreement(snapshot, pages, want):
    """CSV 的 provider 列跟存档对不上的比例 —— 证明为什么只信存档。"""
    import csv as _csv
    if snapshot is None:
        return None
    # 存档侧: (酒店, 入住日) -> 那张页上最低价对应的渠道集合
    arch = {}
    for (h, ci), (st, offers) in latest_by_cell(pages).items():
        lo = min(offers.values())
        arch[(h, ci)] = ({k for k, v in offers.items() if v == lo}, lo)
    agree = disagree = 0
    bad = collections.Counter()
    with open(snapshot, encoding="utf-8-sig") as fh:
        rows = [r for r in _csv.DictReader(fh) if want(r["hotel"])]
    cells = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        cells[(r["hotel"], r["checkin"])][r["timestamp"]].append(r)
    for key, by in cells.items():
        if key not in arch:
            continue
        names, lo = arch[key]
        ts = max(by)
        cand = []
        for r in by[ts]:
            p = (r.get("provider") or "").strip()
            if (key[0], p) in DROP:
                continue
            try:
                cand.append((float(r["price_nzd"]), p))
            except (TypeError, ValueError):
                continue
        if not cand:
            continue
        _, prov = min(cand)
        if prov in names:
            agree += 1
        else:
            disagree += 1
            bad[prov] += 1
    return agree, disagree, bad


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(cur):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="渠道级价格监测(只用页面存档)")
    ap.add_argument("--hotel", default="VR", help="酒店名子串, 默认 VR(两家都要)")
    ap.add_argument("--checkin", help="只看某个入住日")
    ap.add_argument("--no-state", action="store_true", help="不写状态文件")
    args = ap.parse_args()

    tgz = find_archive()
    if tgz is None:
        sys.exit(f"找不到 {SNAP_DIR/'raw_archive.tgz'} —— 渠道只能从页面存档算")
    pre2hotel, snapshot = hotel_lookup()
    if not pre2hotel:
        sys.exit(f"{SNAP_DIR} 里没有 hotel_prices_v3_*.csv, 无法反查酒店名")

    key = args.hotel.lower()
    want = lambda h: key in h.lower()                                  # noqa: E731
    pages = scan_archive(tgz, pre2hotel, want)
    if args.checkin:
        pages = {k: v for k, v in pages.items() if k[1] == args.checkin}
    if not pages:
        sys.exit(f"存档里没有匹配 “{args.hotel}” 的页面")

    hotels = sorted({k[0] for k in pages})
    stamps = sorted({k[2] for k in pages})
    print(f"═══ 渠道级监测 · {' + '.join(hotels)} ═══")
    print(f"  {len(pages)} 张页面存档, {len({k[1] for k in pages})} 个入住日, "
          f"抓取戳 {stamps[0]} → {stamps[-1]}")
    print("  来源: raw_archive.tgz 逐张重新解析。**云端 DIDA 没有渠道字段**, ")
    print("        渠道只能来自本地 Google 抓取 —— 它停了, 这里就不会更新。")
    if DROP & {(h, "Official site") for h in hotels}:
        print("  已剔除 VR Auckland City 的 Official site(路由到机场店, 不是市区店)")
    print()

    seen, cheapest, prem, moves, series = profiles(pages)
    n = len(pages)
    print("═══ 渠道画像 ═══")
    print(f"  {'渠道':22s} {'出现':>6s} {'占比':>6s} {'当最低':>7s} "
          f"{'溢价中位':>9s} {'溢价最大':>9s} {'变价':>5s}")
    for k, c in sorted(seen.items(), key=lambda kv: -kv[1]):
        pm = prem[k]
        print(f"  {k[:22]:22s} {c:6d} {100*c/n:5.1f}% {cheapest[k]:7d} "
              f"{100*statistics.median(pm):8.1f}% {100*max(pm):8.1f}% {moves[k]:5d}")
    print()
    print("  溢价 = 该渠道价 / 本页最低价 - 1。绝对价跨日期不可比(9/25 全城翻倍),")
    print("  除以本页最低价之后才比得了。溢价中位 0.0% = 它经常就是最低的那个。")
    print()

    print("═══ 每个入住日, 最后一张存档上的各渠道报价 (NZD) ═══")
    last = latest_by_cell(pages)
    for h in hotels:
        cis = sorted(ci for (hh, ci) in last if hh == h)
        if not cis:
            continue
        print(f"  ── {h}")
        for ci in cis:
            st, offers = last[(h, ci)]
            lo = min(offers.values())
            items = sorted(offers.items(), key=lambda kv: kv[1])
            txt = "  ".join(f"{'*' if v == lo else ' '}{k} {v:.0f}" for k, v in items)
            print(f"     {ci}  @{st}  {txt}")
        print()

    dis = csv_disagreement(snapshot, pages, want)
    if dis:
        agree, disagree, bad = dis
        tot = agree + disagree
        if tot:
            print("═══ CSV 的渠道名有多不可信 ═══")
            print(f"  拿 {snapshot.name} 的 provider 列跟存档对: "
                  f"对上 {agree}/{tot} ({100*agree/tot:.0f}%), 对不上 {disagree}")
            if bad:
                print("  CSV 说最便宜的是它、存档说不是 —— 出现次数:")
                for k, c in bad.most_common(8):
                    print(f"     {c:4d}  {k}")
            print("  所以渠道一律以存档为准。价格可信, 渠道名不可信。")
            print()

    # ---- 跟上次巡检相比 ----
    cur = {}
    for (h, ci), (st, offers) in last.items():
        for k, v in offers.items():
            cur[f"{h}|{ci}|{k}"] = v
    prev = load_state()
    if prev:
        # 状态文件是全局的, 但这一轮可能只跑了一部分酒店/入住日。
        # 不限定范围就会把"这轮没查"当成"渠道消失" —— `--hotel "VR Queen"`
        # 跑完会把 VR Auckland City 的每一条都报成消失, 全是假警报。
        scope = {k for k in prev
                 if k.split("|")[0] in {h for h in hotels}
                 and (not args.checkin or k.split("|")[1] == args.checkin)}
        moved, gone, new = [], [], []
        for k, v in cur.items():
            if k in prev and prev[k] != v:
                moved.append((abs(v - prev[k]), k, prev[k], v))
            elif k not in prev:
                new.append((k, v))
        for k in scope:
            if k not in cur:
                gone.append((k, prev[k]))
        print("═══ 跟上次巡检相比 ═══")
        if not (moved or gone or new):
            print("  渠道报价没有变动。")
        for _, k, a, b in sorted(moved, reverse=True):
            print(f"  变价  {k}  {a:.0f} → {b:.0f}  ({b-a:+.0f})")
        for k, v in sorted(new):
            print(f"  新出现  {k}  {v:.0f}")
        for k, v in sorted(gone):
            print(f"  消失    {k}  (上次 {v:.0f})")
    else:
        print("═══ 跟上次巡检相比 ═══")
        print("  首次运行, 没有上一轮状态可比。下一轮开始才有变动。")
    if not args.no_state:
        merged = dict(prev)
        merged.update(cur)
        save_state(merged)

    last_stamp = stamps[-1]
    print()
    print(f"  最后一张存档 {last_stamp} —— 存档随本地采集一起停了就不会更新。")
    print("  要刷新: 在本地跑一轮 hotel_fast.py, 更新 data/snapshots/。")


if __name__ == "__main__":
    main()
