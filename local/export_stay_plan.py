# -*- coding: utf-8 -*-
"""把 v3 零售观测导出成「9/20-10/9 连住怎么订」看板。

只吃 Google 零售价 (可直接下单)。云端 DIDA 那套是 USD 批发价, 贵约 31%,
按 CLAUDE.md 的规矩不进这张图。

两个跟以往不同的处理, 都是踩过才加的:

1. **每格取「最后一次完整抓取」内的跨渠道最低**, 不是「各渠道各自的最新读数」。
   后者会把早已从页面上消失的渠道当成现价: VR Auckland City 9/22 的 Wotif 73
   只在 9/18 出现过 5 次, 而当天页面上的实际最低是 Booking.com 112。
   一次抓取 = 同一个 timestamp, 所以按 timestamp 分组取 max 即可。

2. **拿 raw_archive 里的页面原文复核**。Google 自己在地址行下面印了一个头条
   最低价, 拿它跟我们解析出的最低价逐次对账 —— 这是 CLAUDE.md 那条「报价之前
   先核对页面」的可执行版本。
"""
import argparse
import base64
import collections
import csv
import glob
import json
import os
import re
import statistics
import sys
import tarfile
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_DIR = HERE / "data"
SNAP_DIR = BASE_DIR / "snapshots"
TPL = HERE / "dashboards" / "stay_plan_template.html"
OUT = HERE / "dashboards" / "stay_plan.html"

# 住 9/20 入住, 10/9 退房 => 住 19 晚 (9/20 ... 10/8)。
# 10/9 那一格仍然算出来并画在图上, 但只作为「退房日别住」的对照。
CHECKIN = date(2026, 9, 20)
CHECKOUT = date(2026, 10, 9)
WD = "一二三四五六日"

# Google 把 VR Auckland Airport (hotelID=116052) 的订房引擎挂在了
# VR Auckland City 页面的 "Official site" 位上。市区店在 188 Hobson Street,
# 机场店在 Mangere —— 那个价不属于本监测对象, 任何统计前先剔掉。
DROP = {("VR Auckland City", "Official site")}

# 剔掉上面那一对之后, 这家的解析最低价跟页面头条价仍只有 50% 对得上
# (把机场价加回去也只解释一半, 剩下的没查清)。还画在热力图里, 但
# 不参与「订哪家」的任何计算 —— 没核实的数字不拿来做决定。
UNTRUSTED = {"VR Auckland City"}

# 归档文件名前 12 个字符 -> 酒店全名
ARCHIVE_PREFIX = {
    "Ascotia_Off_": "Ascotia Off Queen Auckland",
    "ibis_budget_": "ibis budget Auckland Central",
    "Albion_Hotel": "Albion Hotel Auckland",
    "Shakespeare_": "Shakespeare Hotel Auckland",
    "Auckland_Cit": "Auckland City Hotel",
    "Copthorne_Ho": "Copthorne Hotel Auckland City",
    "The_Quadrant": "The Quadrant Hotel & Suites Auckland",
    "VR_Queen_Str": "VR Queen Street Auckland",
    "VR_Auckland_": "VR Auckland City",
}

ADDR_RE = re.compile(r"Auckland (?:CBD|Central|1010|1011)")
HEAD_RE = re.compile(r"^\$(\d[\d,]*)$")
FNAME_RE = re.compile(r"(.{12})_(2026-\d\d-\d\d)_(\d{4})-(\d{6})\.txt")

# ---------------------------------------------------------------- Google URL
# 跟 hotel_fast.build_url 同一套 protobuf/base64 构造, 在这里重抄一遍是为了
# 不把 playwright 拖进这个纯分析脚本的依赖里 (导入 hotel_fast 会连带 import
# playwright, 云端/无浏览器环境直接炸)。改动必须与 hotel_fast 保持一致。
CURRENCY = "NZD"


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


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


def build_url(hotel, checkin, checkout=None):
    from urllib.parse import quote
    checkout = checkout or checkin + timedelta(days=1)
    return (f"https://www.google.com/travel/search?q={quote(hotel)}"
            f"&ts={build_ts(checkin, checkout)}")


# ---------------------------------------------------------------- 读取

def newest_snapshot():
    cands = sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv"))
    if not cands:
        sys.exit(f"找不到 v3 快照: {SNAP_DIR}/hotel_prices_v3_*.csv")
    return cands[-1]


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        raw = list(csv.DictReader(f))
    rows = []
    for r in raw:
        if not r.get("price_nzd"):
            continue
        if (r["hotel"], r["provider"]) in DROP:
            continue
        try:
            r["price"] = float(r["price_nzd"])
        except ValueError:
            continue
        rows.append(r)
    return raw, rows


# ---------------------------------------------------------------- 归约

def build_cells(rows, dates):
    """(酒店, 入住日) -> 最后一次抓取内的跨渠道最低价 + 那一次的全部渠道。"""
    want = set(dates)
    scrape = collections.defaultdict(dict)
    for r in rows:
        if r["checkin"] in want:
            scrape[(r["hotel"], r["checkin"])].setdefault(r["timestamp"], {})[r["provider"]] = r["price"]

    cells = {}
    for (h, d), by_ts in scrape.items():
        ts = max(by_ts)
        quotes = by_ts[ts]
        cheapest = min(quotes, key=quotes.get)
        per_scrape = [min(v.values()) for v in by_ts.values() if v]
        cells[f"{h}|{d}"] = {
            "p": quotes[cheapest],
            "prov": cheapest,
            "n": len(quotes),
            "ts": ts,
            "all": sorted(quotes.items(), key=lambda kv: kv[1]),
            "lo": min(per_scrape),
            "hi": max(per_scrape),
            "obs": len(per_scrape),
        }
    return cells


def best_plan(grid, hotels, nights, penalty):
    """在「每换一次店罚 penalty 元」下的最省住法。动态规划, 状态 = 当晚住哪家。"""
    cost = {h: grid[h][nights[0]] for h in hotels}
    path = {h: [h] for h in hotels}
    for i in range(1, len(nights)):
        nc, np_ = {}, {}
        for h in hotels:
            prev = min(hotels, key=lambda g: cost[g] + (0 if g == h else penalty))
            nc[h] = cost[prev] + (0 if prev == h else penalty) + grid[h][nights[i]]
            np_[h] = path[prev] + [h]
        cost, path = nc, np_
    end = min(hotels, key=lambda g: cost[g])
    seq = path[end]
    return sum(grid[seq[i]][nights[i]] for i in range(len(nights))), seq


def to_legs(seq, nights, grid):
    legs, cur, start = [], seq[0], 0
    for i in range(1, len(seq) + 1):
        if i == len(seq) or seq[i] != cur:
            span = nights[start:i]
            legs.append({"hotel": cur, "from": span[0], "to": span[-1],
                         "nights": len(span),
                         "sum": sum(grid[cur][d] for d in span)})
            if i < len(seq):
                cur, start = seq[i], i
    return legs


# ---------------------------------------------------------------- 页面复核

def verify(rows, dates, archive):
    """逐次抓取比对: 我们解析的最低价 == Google 页面上印的头条最低价?

    对不上不一定是我们错 —— VR Auckland City 的头条价含被剔除的机场店报价,
    那是我们故意不要的。所以同时统计「加回 Official site 就对得上」的条数。
    """
    if not archive or not archive.exists():
        return None

    want = set(dates)
    kept = collections.defaultdict(dict)
    for r in rows:
        if r["checkin"] in want:
            kept[(r["hotel"], r["checkin"])].setdefault(r["timestamp"], {})[r["provider"]] = r["price"]

    def parse_ts(s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

    stat = collections.defaultdict(lambda: [0, 0])
    with tempfile.TemporaryDirectory(prefix="stayplan_raw_") as tmp:
        with tarfile.open(archive, "r:gz") as tf:
            # filter= 是 3.12 才有的参数, 旧版本没有但默认行为一致
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:
                tf.extractall(tmp)

        for f in glob.glob(os.path.join(tmp, "**", "*.txt"), recursive=True):
            m = FNAME_RE.match(os.path.basename(f))
            if not m:
                continue
            pre, ci, md, hms = m.groups()
            hotel = ARCHIVE_PREFIX.get(pre)
            if not hotel or ci not in want:
                continue
            ft = parse_ts(f"2026-{md[:2]}-{md[2:]} {hms[:2]}:{hms[2:4]}:{hms[4:]}")
            by_ts = kept.get((hotel, ci))
            if not by_ts:
                continue
            # 归档文件名的时刻和写 CSV 的时刻差几秒, 取最近的那一次抓取
            ts = min(by_ts, key=lambda t: abs((parse_ts(t) - ft).total_seconds()))
            if abs((parse_ts(ts) - ft).total_seconds()) > 180:
                continue

            head = None
            with open(f, encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh]
            for i, ln in enumerate(lines):
                if ADDR_RE.search(ln) and "•" in ln:
                    for j in range(i + 1, min(i + 4, len(lines))):
                        mm = HEAD_RE.match(lines[j])
                        if mm:
                            head = float(mm.group(1).replace(",", ""))
                            break
                    break
            if head is None:
                continue
            stat[hotel][1] += 1
            if abs(min(by_ts[ts].values()) - head) < 0.5:
                stat[hotel][0] += 1

    return dict(stat)


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true", help="跳过页面原文复核(慢)")
    args = ap.parse_args()

    snap = newest_snapshot()
    raw, rows = load(snap)

    span = (CHECKOUT - CHECKIN).days
    dates = [(CHECKIN + timedelta(days=i)).isoformat() for i in range(span + 1)]
    nights = dates[:span]

    cells = build_cells(rows, dates)
    hotels = sorted({k.split("|")[0] for k in cells})
    # 只保留整段每一晚都有报价的酒店 —— 缺一晚就没法算「连住总价」
    hotels = [h for h in hotels if all(f"{h}|{d}" in cells for d in dates)]
    grid = {h: {d: cells[f"{h}|{d}"]["p"] for d in dates} for h in hotels}

    singles = sorted((sum(grid[h][d] for d in nights), h) for h in hotels)

    # 「订哪家」只在可信的那几家里挑
    pick = [h for h in hotels if h not in UNTRUSTED]
    floor = sum(min(grid[h][d] for h in pick) for d in nights)

    # 换店次数 -> 该次数下最省的住法
    by_switch = {}
    for pen in (0, 3, 5, 8, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200, 300, 500, 1000, 5000):
        tot, seq = best_plan(grid, pick, nights, pen)
        sw = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
        if sw not in by_switch or tot < by_switch[sw]["total"]:
            by_switch[sw] = {"switches": sw, "total": tot, "legs": to_legs(seq, nights, grid)}
    frontier = [by_switch[k] for k in sorted(by_switch)]

    vol = {h: {"median": statistics.median([cells[f"{h}|{d}"]["hi"] - cells[f"{h}|{d}"]["lo"] for d in nights]),
               "max": max(cells[f"{h}|{d}"]["hi"] - cells[f"{h}|{d}"]["lo"] for d in nights)}
           for h in hotels}

    # 尖峰日的判据用 CLAUDE.md 那条: 看覆盖度, 一两家异常是漏抓, 整排一起动才是真涨价。
    # 具体化为「至少 2/3 的酒店当晚价格比它自己 19 晚的中位数高 30% 以上」。
    med = {h: statistics.median([grid[h][d] for d in nights]) for h in hotels}
    spikes = [d for d in dates
              if sum(1 for h in hotels if grid[h][d] >= med[h] * 1.3) >= len(hotels) * 2 / 3]

    ver = None if args.no_verify else verify(rows, dates, SNAP_DIR / "raw_archive.tgz")

    urls = {f"{h}|{d}": build_url(h, date.fromisoformat(d)) for h in hotels for d in dates}
    seg = {}
    for plan in frontier:
        for leg in plan["legs"]:
            a = date.fromisoformat(leg["from"])
            b = date.fromisoformat(leg["to"]) + timedelta(days=1)
            seg[f'{leg["hotel"]}|{leg["from"]}|{b.isoformat()}'] = build_url(leg["hotel"], a, b)

    payload = {
        "hotels": hotels,
        "dates": dates,
        "nights": nights,
        "weekday": {d: WD[date.fromisoformat(d).weekday()] for d in dates},
        "cells": cells,
        "grid": grid,
        "singles": [[t, h] for t, h in singles],
        "untrusted": sorted(UNTRUSTED & set(hotels)),
        "spikes": spikes,
        "floor": floor,
        "frontier": frontier,
        "vol": vol,
        "verify": ver,
        "urls": urls,
        "segurls": seg,
        "meta": {
            "snapshot": snap.name,
            "rows_total": len(raw),
            "rows_used": len(rows),
            "dropped": len(raw) - len(rows),
            "first_ts": min(r["timestamp"] for r in rows),
            "last_ts": max(r["timestamp"] for r in rows),
            "last_scrape_min": min(c["ts"] for c in cells.values()),
            "last_scrape_max": max(c["ts"] for c in cells.values()),
            "checkin": CHECKIN.isoformat(),
            "checkout": CHECKOUT.isoformat(),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }

    tpl = TPL.read_text(encoding="utf-8")
    assert "/*__DATA__*/{}" in tpl, "模板里找不到 placeholder"
    OUT.write_text(tpl.replace("/*__DATA__*/{}", json.dumps(payload, ensure_ascii=False)),
                   encoding="utf-8")

    print(f"快照      {snap.name}  ({len(rows)}/{len(raw)} 行可用, 剔除 {len(raw)-len(rows)})")
    print(f"观测窗口  {payload['meta']['first_ts']} → {payload['meta']['last_ts']}")
    print(f"整段 {len(nights)} 晚 ({nights[0]} ~ {nights[-1]}), 全程有报价的酒店 {len(hotels)} 家")
    print()
    for t, h in singles:
        print(f"  {h:<38} NZ${t:>7.0f}   均 {t/len(nights):>5.1f}/晚")
    cheapest = min(t for t, h in singles if h in pick)
    print(f"\n  不参与选店(数据存疑): {', '.join(sorted(UNTRUSTED & set(hotels))) or '无'}")
    print(f"  理论下限(每晚都换到最便宜) NZ${floor:.0f}")
    print(f"  换店最多能省             NZ${cheapest-floor:.0f}"
          f"  ({100*(cheapest-floor)/cheapest:.1f}%)")
    print(f"  整排一起涨的尖峰日: {', '.join(spikes) or '无'}")
    if ver:
        print("\n页面原文复核 (解析最低价 == Google 头条最低价):")
        for h, (ok, n) in sorted(ver.items(), key=lambda kv: -kv[1][0] / max(kv[1][1], 1)):
            print(f"  {h:<38} {ok:>4}/{n:<4} {100*ok/max(n,1):>5.1f}%")
    print(f"\n看板 -> {OUT}")


if __name__ == "__main__":
    main()
