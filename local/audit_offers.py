# -*- coding: utf-8 -*-
"""拿页面存档核对解析器: 渠道认全了没有, 最低价对不对。

CLAUDE.md 那条"报价之前先核对页面"的可执行版本。判据是 Google 自己在酒店
地址行下面印的那个头条最低价 —— 它是这家酒店当时的实际最低, 拿它跟解析出的
最低价逐张对账。

    python audit_offers.py              # 全量核对, 打印汇总
    python audit_offers.py --json out.json

## 归档本身的偏差(读结论前先知道这个)

`hotel_fast.ARCHIVE_BELOW = 100`: **只有出现过低于 NZ$100 报价的页面才会存档**。
所以 Copthorne(最低也要 NZ$103)一张都没有, 其余各店存的也偏向便宜的日子。
核对结论只覆盖存下来的这部分, 不能推广到全量。
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import offer_parse  # noqa: E402

BASE_DIR = HERE / "data"
SNAP_DIR = BASE_DIR / "snapshots"
ARCHIVE = SNAP_DIR / "raw_archive.tgz"

DROP = {("VR Auckland City", "Official site")}

# 归档文件名前 12 个字符 -> 酒店全名
PREFIX = {
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

FNAME_RE = re.compile(r"(.{12})_(2026-\d\d-\d\d)_(\d{4})-(\d{6})\.txt")
ADDR_RE = re.compile(r"Auckland (?:CBD|Central|1010|1011)")
HEAD_RE = re.compile(r"^\$(\d[\d,]*)$")


def headline(lines):
    """Google 印在地址行下面的头条最低价。注意它**含**被我们剔除的渠道 ——
    VR Auckland City 的头条价就是机场店那一条, 所以对 VR 要单独看。"""
    for i, ln in enumerate(lines):
        if ADDR_RE.search(ln) and "•" in ln:
            for j in range(i + 1, min(i + 4, len(lines))):
                m = HEAD_RE.match(lines[j])
                if m:
                    return float(m.group(1).replace(",", ""))
            return None
    return None


def newest_snapshot():
    c = sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv"))
    return c[-1] if c else None


def run_audit(archive=ARCHIVE, snapshot=None):
    """核对一遍, 返回结果 dict。看板和命令行都走这个函数。"""
    if not archive or not Path(archive).exists():
        return None

    # CSV 里同一时刻的读数, 用来对比新旧解析器
    old = collections.defaultdict(dict)
    snap = snapshot or newest_snapshot()
    if snap:
        with open(snap, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["price_nzd"] and (r["hotel"], r["provider"]) not in DROP:
                    old[(r["hotel"], r["checkin"])].setdefault(r["timestamp"], {})[
                        r["provider"]] = float(r["price_nzd"])

    def ts(s):
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

    stat = collections.defaultdict(lambda: collections.Counter())
    inpanel = collections.Counter()      # 渠道在本店报价区出现过多少张页面
    cheapest = collections.Counter()     # 剔除后谁最便宜
    more_ge = more_lt = more_na = 0
    pages = 0

    with tempfile.TemporaryDirectory(prefix="audit_raw_") as tmp:
        with tarfile.open(archive, "r:gz") as tf:
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:
                tf.extractall(tmp)

        for f in glob.glob(os.path.join(tmp, "**", "*.txt"), recursive=True):
            m = FNAME_RE.match(os.path.basename(f))
            if not m:
                continue
            pre, ci, md, hms = m.groups()
            hotel = PREFIX.get(pre)
            if not hotel:
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            head = headline(lines)
            if head is None:
                continue
            pages += 1

            res = offer_parse.parse_offers(text, hotel)
            full = res["offers"]
            for p in full:
                inpanel[p] += 1
            if not full:
                stat[hotel]["新解析未找到报价"] += 1
                continue

            # "View more options from $X": 没渲染出来的渠道都不便宜于 X 吗?
            if res["more_from"] is None:
                more_na += 1
            elif res["more_from"] >= min(full.values()):
                more_ge += 1
            else:
                more_lt += 1

            stat[hotel]["页数"] += 1
            if abs(min(full.values()) - head) < 0.5:
                stat[hotel]["新解析==头条"] += 1

            kept = {k: v for k, v in full.items() if (hotel, k) not in DROP}
            if kept:
                cheapest[min(kept, key=kept.get)] += 1

            # 老解析器(CSV)同一时刻的最低价
            ft = ts(f"2026-{md[:2]}-{md[2:]} {hms[:2]}:{hms[2:4]}:{hms[4:]}")
            by = old.get((hotel, ci))
            if by:
                t = min(by, key=lambda x: abs((ts(x) - ft).total_seconds()))
                if abs((ts(t) - ft).total_seconds()) <= 180:
                    stat[hotel]["可比"] += 1
                    if abs(min(by[t].values()) - head) < 0.5:
                        stat[hotel]["老解析==头条"] += 1

    return {
        "pages": pages,
        "per_hotel": {h: dict(c) for h, c in stat.items()},
        "inpanel": dict(inpanel),
        "cheapest": dict(cheapest),
        "more_from": {"ge": more_ge, "lt": more_lt, "absent": more_na},
    }


def report(a):
    pages = a["pages"]
    stat = {h: collections.Counter(c) for h, c in a["per_hotel"].items()}
    inpanel = collections.Counter(a["inpanel"])
    cheapest = collections.Counter(a["cheapest"])
    more_ge, more_lt, more_na = (a["more_from"][k] for k in ("ge", "lt", "absent"))
    print(f"页面存档 {pages} 张 (只含出现过 <NZ${100} 报价的页面; Copthorne 因此一张没有)\n")
    print(f"{'酒店':<38}{'页数':>6}{'新==头条':>10}{'可比':>6}{'老==头条':>10}")
    tot = collections.Counter()
    for h in sorted(stat):
        c = stat[h]
        tot.update(c)
        print(f"{h:<38}{c['页数']:>6}{c['新解析==头条']:>10}{c['可比']:>6}{c['老解析==头条']:>10}")
    print(f"{'合计':<38}{tot['页数']:>6}{tot['新解析==头条']:>10}"
          f"{tot['可比']:>6}{tot['老解析==头条']:>10}")
    if tot["页数"]:
        print(f"\n新解析一致率 {100*tot['新解析==头条']/tot['页数']:.1f}%"
              f"   老解析一致率 {100*tot['老解析==头条']/max(tot['可比'],1):.1f}%")
    print("\n注: VR Auckland City 的头条价就是被我们剔除的机场店那一条, 所以它"
          "「新==头条」高是正常的; 真正要用的是剔除之后的价。")

    print(f"\n渠道在本店报价区出现过的页数 (共 {pages} 张):")
    for p, c in inpanel.most_common():
        print(f"  {c:>5}  {p}")

    print("\n剔除后「当页最便宜的渠道」:")
    for p, c in cheapest.most_common():
        print(f"  {c:>5}  {p}")

    print(f"\n\"View more options from $X\": X >= 页面已显示最低价 {more_ge} 张, "
          f"X < 最低价 {more_lt} 张, 没有这一行 {more_na} 张")
    print("  -> 前者成立就说明: 没渲染出来的渠道都不会更便宜, "
          "渠道清单不完整但最低价完整。")



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把结果写成 JSON")
    args = ap.parse_args()
    a = run_audit()
    if a is None:
        sys.exit(f"找不到页面存档: {ARCHIVE}")
    report(a)
    if args.json:
        Path(args.json).write_text(json.dumps(a, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n-> {args.json}")


if __name__ == "__main__":
    main()
