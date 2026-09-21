# -*- coding: utf-8 -*-
"""小时级价格波动看板 —— 回答 CLAUDE.md 里那个一直悬着的问题:「几点订更便宜」。

## 为什么现在能问了

CLAUDE.md 记着这条:「小时与日期是**混淆**的 …… 在拿到连续几天的完整小时
覆盖之前, **不要下这个结论**」。写下那条的时候, 零售那边最密的一天只有
13 个小时, 前两天各只有 3 小时且中间断档。

云端 DIDA 这套就是冲着这个建的 —— 固定 11 家 × 4 个入住日反复观测, 15 分钟
一轮, 不依赖任何人开机。到 2026-09-19 已经攒了 115 轮, NZ 当地时间 0-23 点
全覆盖(两天合起来), 其中 7 个小时两天都有 —— 够做"两天对不对得上"这个检验了。

## 两套数据严格分开

按 CLAUDE.md 的铁律, 云端(USD 批发价)和本地(NZD 零售价)**不进同一张图**。
这里的分工是:

* 云端答"**什么时候动、动多少**"(相对自身的百分比, 不看绝对价);
* 本地答"**订哪家、多少钱**"(绝对 NZD, 可下单)。

## 归一化口径

每条(酒店 × 入住日)序列除以**它自己的中位数**, 只看相对偏离。这样 USD 的
绝对水平完全不参与比较, 也就不存在"拿批发价当零售价"的风险。
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import json
import math
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CLOUD_DIR = REPO / "data"
SNAP_DIR = HERE / "data" / "snapshots"
TPL = HERE / "dashboards" / "hourly_template.html"
OUT = HERE / "dashboards" / "hourly.html"

NZ = ZoneInfo("Pacific/Auckland")

# 住宿区间, 跟 export_stay_plan.py 一致
CHECKIN, CHECKOUT = dt.date(2026, 9, 20), dt.date(2026, 10, 9)

DROP = {("VR Auckland City", "Official site")}

# 极差超过这个比例的序列算"大动", 单独拎出来看, 免得少数几条把均值带跑
BIG_MOVER = 10.0


def utc(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)


def nz_naive(s):
    """零售快照的 timestamp 本来就是 NZ 当地时间(见 snapshots/README)。"""
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 云端

def load_cloud():
    rows = []
    for f in sorted(CLOUD_DIR.glob("*.csv")):
        try:
            with open(f, encoding="utf-8") as fh:
                rows.extend(csv.DictReader(fh))
        except OSError:
            continue
    out = []
    for r in rows:
        try:
            r["_p"] = float(r["price"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append(r)
    return out


def cloud_series(rows):
    """(酒店, 入住日) -> ({ts_utc: 当轮最低价}, {ts_utc: 该价对应的房型})。

    房型要一起记: 最低价换了房型还翻了几倍, 那是"便宜房型下架", 不是"涨价"。
    这两件事混在一张小时波动图里, 图就废了 —— 见 structural_breaks()。
    """
    ser = collections.defaultdict(dict)
    room = collections.defaultdict(dict)
    for r in rows:
        if not (CHECKIN.isoformat() <= r["checkin"] < CHECKOUT.isoformat()):
            continue
        k = (r["hotel"], r["checkin"])
        t = r["ts_utc"]
        if t not in ser[k] or r["_p"] < ser[k][t]:
            ser[k][t] = r["_p"]
            room[k][t] = (r.get("room") or "").strip()
    return ser, room


# 最低价档位跳动到这个倍数以上, 且换了房型, 就当结构性断裂
BREAK_RATIO = 2.0


def structural_breaks(ser, room):
    """认出"最便宜的房型整类没了"这种序列。

    2026-09-20 12:07 UTC 起, VR Queen Street 四个入住日的 Compact Single /
    Double Standard 同时从 DIDA 的返回里消失, 最低价从 USD 50 跳到 473 ——
    方案总数没掉, 就是那一档不卖了。这不是重新定价, 把它算进"几点订更便宜"
    会让相对偏离冲到 +400%, 一条序列就能把当天的小时剖面整个带偏。
    所以单独拎出来, 不进小时统计, 但要在页面上明说。
    """
    out = {}
    for k, s in ser.items():
        ts = sorted(s)
        for a, b in zip(ts, ts[1:]):
            if s[a] <= 0:
                continue
            ratio = max(s[a], s[b]) / min(s[a], s[b])
            if ratio >= BREAK_RATIO and room[k].get(a) != room[k].get(b):
                out[k] = {"hotel": k[0], "checkin": k[1], "at": b,
                          "at_nz": utc(b).astimezone(NZ).strftime("%m-%d %H:%M"),
                          "from": s[a], "to": s[b],
                          "room_from": room[k].get(a, ""), "room_to": room[k].get(b, ""),
                          "ratio": round(ratio, 1)}
                break
    return out


def rel_profile(ser, to_nz):
    """(日期, 小时) -> 相对各自中位数的偏离(%) 列表。"""
    rel = collections.defaultdict(list)
    for s in ser.values():
        vals = list(s.values())
        med = statistics.median(vals)
        if med <= 0:
            continue
        for t, v in s.items():
            tt = to_nz(t)
            rel[(tt.date().isoformat(), tt.hour)].append(100 * (v - med) / med)
    return rel


def pearson(x, y):
    if len(x) < 3:
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return round(num / den, 3) if den else None


def hour_table(rel):
    """按日期 × 小时汇总, 同时给均值和中位数 —— 两者差很多本身就是结论:
    中位数处处为 0 说明"多数格子根本没动", 均值的起伏是少数几条带的。"""
    days = sorted({d for d, _ in rel})
    tbl = {}
    for d in days:
        for h in range(24):
            v = rel.get((d, h))
            if v:
                tbl[f"{d}|{h}"] = {
                    "mean": round(statistics.mean(v), 2),
                    "median": round(statistics.median(v), 2),
                    "n": len(v),
                }
    return days, tbl


def reproducibility(rel, days):
    """两天在同一小时上对不对得上 —— CLAUDE.md 要求的那个检验。"""
    out = []
    for i in range(len(days)):
        for j in range(i + 1, len(days)):
            a, b = days[i], days[j]
            common = [h for h in range(24) if rel.get((a, h)) and rel.get((b, h))]
            if len(common) < 3:
                continue
            xa = [statistics.mean(rel[(a, h)]) for h in common]
            xb = [statistics.mean(rel[(b, h)]) for h in common]
            out.append({
                "a": a, "b": b, "hours": common,
                "r": pearson(xa, xb),
                "mean_abs_diff": round(statistics.mean(abs(p - q) for p, q in zip(xa, xb)), 2),
                "range_a": round(max(xa) - min(xa), 2),
                "range_b": round(max(xb) - min(xb), 2),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="另存一份数据")
    args = ap.parse_args()

    # ---- 云端 ----
    craw = load_cloud()
    ser, croom = cloud_series(craw)
    if not ser:
        sys.exit("云端没有落在住宿区间内的入住日")

    # 结构性断裂的序列不进小时统计 —— 它衡量的不是同一个东西。
    breaks = structural_breaks(ser, croom)
    ser_hours = {k: v for k, v in ser.items() if k not in breaks}

    stamps = sorted({t for s in ser.values() for t in s})
    idx = {t: i for i, t in enumerate(stamps)}
    times = [{"utc": t, "nz": utc(t).astimezone(NZ).strftime("%Y-%m-%d %H:%M")} for t in stamps]

    series = []
    for (h, d), s in sorted(ser.items()):
        vals = list(s.values())
        med = statistics.median(vals)
        spread = (max(vals) - min(vals)) / med * 100 if med else 0
        pts = [None] * len(stamps)
        for t, v in s.items():
            pts[idx[t]] = v
        series.append({
            "hotel": h, "checkin": d, "median": med,
            "spread_pct": round(spread, 2),
            "spread_abs": round(max(vals) - min(vals), 2),
            "lo": min(vals), "hi": max(vals),
            "n": len(vals),
            "big": spread >= BIG_MOVER,
            # 结构性断裂的序列照画(原始走势图要如实), 但标出来, 并且不进小时统计
            "brk": (h, d) in breaks,
            "v": pts,
        })

    # 变价事件
    events = []
    for (h, d), s in sorted(ser.items()):
        ts = sorted(s)
        vals = [s[t] for t in ts]
        for t, a, b in zip(ts[1:], vals, vals[1:]):
            if a != b:
                tt = utc(t).astimezone(NZ)
                events.append({"hotel": h, "checkin": d, "at": t,
                               "nz_hour": tt.hour, "nz": tt.strftime("%m-%d %H:%M"),
                               "from": a, "to": b, "d": round(b - a, 2)})

    # 变价间隔
    gaps = []
    for (h, d), s in ser.items():
        ts = sorted(s)
        vals = [s[t] for t in ts]
        last = None
        for t, a, b in zip(ts[1:], vals, vals[1:]):
            if a != b:
                tt = utc(t)
                if last:
                    gaps.append((tt - last).total_seconds() / 3600)
                last = tt

    crel = rel_profile(ser_hours, lambda t: utc(t).astimezone(NZ))
    cdays, ctbl = hour_table(crel)
    crepro = reproducibility(crel, cdays)

    # 同一天内 vs 跨天
    within, across = [], []
    for s in ser_hours.values():
        med = statistics.median(s.values())
        if med <= 0:
            continue
        byday = collections.defaultdict(list)
        for t, v in s.items():
            byday[utc(t).astimezone(NZ).date()].append(v)
        spans = [(max(v) - min(v)) / med * 100 for v in byday.values() if len(v) >= 8]
        if spans:
            within.append(max(spans))
        meds = [statistics.median(v) for v in byday.values() if len(v) >= 8]
        if len(meds) >= 2:
            across.append((max(meds) - min(meds)) / med * 100)

    # ---- 零售 ----
    snaps = sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv"))
    rser, rrel, rdays, rtbl, rrepro = {}, {}, [], {}, []
    rsnap = None
    if snaps:
        rsnap = snaps[-1]
        with open(rsnap, encoding="utf-8-sig") as fh:
            rrows = [r for r in csv.DictReader(fh)
                     if r["price_nzd"] and (r["hotel"], r["provider"]) not in DROP]
        scr = collections.defaultdict(dict)
        for r in rrows:
            if not (CHECKIN.isoformat() <= r["checkin"] < CHECKOUT.isoformat()):
                continue
            scr[(r["hotel"], r["checkin"])].setdefault(r["timestamp"], {})[
                r["provider"]] = float(r["price_nzd"])
        rser = {k: {t: min(v.values()) for t, v in by.items()} for k, by in scr.items()}
        rrel = rel_profile(rser, nz_naive)
        rdays, rtbl = hour_table(rrel)
        rrepro = reproducibility(rrel, rdays)

    # 零售序列的极差分布。注意跟"按小时的中位数偏离为 0"不是一回事:
    # 前者说的是整段(跨天)动没动, 后者说的是**一天之内**典型格子动没动。
    rspreads = []
    for s in rser.values():
        if len(s) < 5:
            continue
        med = statistics.median(s.values())
        if med > 0:
            rspreads.append((max(s.values()) - min(s.values())) / med * 100)
    rflat = sum(1 for x in rspreads if x == 0)
    # 各小时中位数偏离全为 0 吗 —— 两套数据都要单独核, 不能靠印象
    rzero = all(c["median"] == 0 for c in rtbl.values()) if rtbl else False

    def nonzero(tbl):
        """中位偏离不为 0 的格子: 个数 + 最大绝对值。"""
        bad = [c["median"] for c in tbl.values() if c["median"] != 0]
        return {"n": len(bad), "of": len(tbl),
                "max_abs": round(max((abs(x) for x in bad), default=0.0), 2)}

    payload = {
        "cloud": {
            "median_all_zero": all(c["median"] == 0 for c in ctbl.values()) if ctbl else False,
            "median_nonzero": nonzero(ctbl),
            "breaks": sorted(breaks.values(), key=lambda b: (b["hotel"], b["checkin"])),
            "n_hour_series": len(ser_hours),
            "times": times,
            "series": series,
            "events": events,
            "days": cdays,
            "hour": ctbl,
            "repro": crepro,
            "flat": sum(1 for s in series if s["spread_pct"] == 0),
            "n_series": len(series),
            "spread_median": round(statistics.median(s["spread_pct"] for s in series), 2),
            "within_median": round(statistics.median(within), 2) if within else None,
            "across_median": round(statistics.median(across), 2) if across else None,
            "gap_median": round(statistics.median(gaps), 2) if gaps else None,
            "gap_q1": round(sorted(gaps)[len(gaps) // 4], 2) if gaps else None,
            "gap_q3": round(sorted(gaps)[3 * len(gaps) // 4], 2) if gaps else None,
            "rounds": len(stamps),
            "first": times[0]["nz"], "last": times[-1]["nz"],
        },
        "retail": {
            "snapshot": rsnap.name if rsnap else None,
            "days": rdays, "hour": rtbl, "repro": rrepro,
            "n_series": len(rser),
            "flat": rflat,
            "spread_median": round(statistics.median(rspreads), 2) if rspreads else None,
            "lt2": sum(1 for x in rspreads if x < 2),
            "n_spread": len(rspreads),
            "median_all_zero": rzero,
            "median_nonzero": nonzero(rtbl),
        },
        "meta": {
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "checkin": CHECKIN.isoformat(), "checkout": CHECKOUT.isoformat(),
            "big_mover": BIG_MOVER,
        },
    }

    tpl = TPL.read_text(encoding="utf-8")
    assert "/*__DATA__*/{}" in tpl, "模板里找不到 placeholder"
    OUT.write_text(tpl.replace("/*__DATA__*/{}", json.dumps(payload, ensure_ascii=False)),
                   encoding="utf-8")

    c = payload["cloud"]
    print(f"云端 {c['rounds']} 轮, {c['first']} → {c['last']} (NZ)")
    print(f"  {c['n_series']} 条序列: 一分钱没动 {c['flat']} 条, 极差中位 {c['spread_median']}%")
    print(f"  变价 {len(events)} 次, 两次变价间隔中位 {c['gap_median']} 小时"
          f" (四分位 {c['gap_q1']}/{c['gap_q3']})")
    print(f"  同一天内最大极差 中位 {c['within_median']}% | 跨天中位数之差 中位 {c['across_median']}%")
    print("\n  小时剖面的两天一致性检验:")
    for r in crepro:
        print(f"    {r['a']} vs {r['b']}: 重叠 {len(r['hours'])} 小时, r={r['r']}, "
              f"同一小时平均差 {r['mean_abs_diff']}pp, 小时间幅度 {r['range_a']}/{r['range_b']}pp")
    print(f"\n零售 {payload['retail']['snapshot']}: {payload['retail']['n_series']} 条序列, "
          f"整段极差中位 {payload['retail']['spread_median']}%, <2% 的 {payload['retail']['lt2']} 条")
    print(f"  各小时中位数偏离全为 0: 云端 {payload['cloud']['median_all_zero']}, "
          f"零售 {payload['retail']['median_all_zero']}")
    for r in rrepro:
        print(f"    {r['a']} vs {r['b']}: 重叠 {len(r['hours'])} 小时, r={r['r']}")
    print(f"\n看板 -> {OUT}")

    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
