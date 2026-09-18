# -*- coding: utf-8 -*-
"""监测现状一览: 两套采集还活着吗, 价格动了没有, 结论要不要改。

    python watch_status.py              # 打印
    python watch_status.py --json x.json

## 这个脚本能回答和不能回答的

两套采集的分工在 CLAUDE.md 里写死了, 这里照办:

* **云端 DIDA** (`data/*.csv`): USD 批发价, 比零售贵约 31% 且价差浮动,
  **不能**用来决定订哪家。但它不依赖任何人开机, 一直在跑 —— 所以它能回答
  "价格动了没有", 哪怕回答不了"现在多少钱"。
* **本地 Google** (`local/data/snapshots/hotel_prices_v3_*.csv`): 零售价,
  可直接下单, 看板用的就是它。但它依赖 Yuan 的电脑开着, 快照是静态文件。

所以判断"结论还成不成立"的逻辑是:
  1. 有没有更新的零售快照? 有 -> 重跑 export_stay_plan.py, 结论可能变。
  2. 没有的话, 看云端在两个数据集重叠的入住日上动了多少。**动得少 = 零售
     大概也没大动**; 动得多 = 快照可能过期了, 该催一次本地采集。
     这是弱证据, 不是替代品 —— 批发和零售不是一回事, 只是同一个市场。
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CLOUD_DIR = REPO / "data"
SNAP_DIR = HERE / "data" / "snapshots"

# 住宿区间 —— 跟 export_stay_plan.py 保持一致
CHECKIN, CHECKOUT = dt.date(2026, 9, 20), dt.date(2026, 10, 9)

# 采集节奏: 循环内部对齐到 :07/:22/:37/:52, 即 15 分钟一轮。
# 超过这个就算空档 —— 调度器丢触发时会出现几十分钟到几小时的洞。
EXPECTED_GAP_MIN = 15
GAP_ALERT_MIN = 40


def parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def load_cloud():
    rows = []
    for f in sorted(CLOUD_DIR.glob("*.csv")):
        try:
            with open(f, encoding="utf-8") as fh:
                rows.extend(csv.DictReader(fh))
        except OSError:
            continue
    for r in rows:
        try:
            r["_p"] = float(r["price"])
        except (KeyError, ValueError, TypeError):
            r["_p"] = None
    return [r for r in rows if r["_p"] is not None]


def cloud_health(rows, now=None):
    now = now or dt.datetime.utcnow()
    stamps = sorted({r["ts_utc"] for r in rows})
    if not stamps:
        return {"rounds": 0, "alive": False}
    ts = [parse_ts(s) for s in stamps]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:])]
    big = [{"from": a.strftime("%m-%d %H:%M"), "to": b.strftime("%m-%d %H:%M"),
            "minutes": round((b - a).total_seconds() / 60)}
           for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() / 60 > GAP_ALERT_MIN]
    age = (now - ts[-1]).total_seconds() / 60
    return {
        "rounds": len(ts),
        "first": ts[0].strftime("%Y-%m-%d %H:%M"),
        "last": ts[-1].strftime("%Y-%m-%d %H:%M"),
        "age_min": round(age),
        "alive": age < 90,          # 一次触发管近 6 小时, 90 分钟没动静就不正常
        "median_gap": round(statistics.median(gaps)) if gaps else None,
        "gaps": big,
        "uptime_pct": round(100 * sum(1 for g in gaps if g <= GAP_ALERT_MIN) / len(gaps), 1)
                      if gaps else None,
    }


def cloud_movement(rows):
    """云端在「住宿区间内」的入住日上, 每家酒店的最低价动了多少。

    单位是 USD 批发价, **不可与零售价比大小**, 只看动没动。
    """
    inwin = [r for r in rows
             if CHECKIN.isoformat() <= r["checkin"] < CHECKOUT.isoformat()]
    by = collections.defaultdict(dict)       # (hotel, checkin) -> ts -> price
    for r in inwin:
        k = (r["hotel"], r["checkin"])
        by[k][r["ts_utc"]] = min(r["_p"], by[k].get(r["ts_utc"], r["_p"]))

    out = []
    for (h, d), series in sorted(by.items()):
        stamps = sorted(series)
        if len(stamps) < 2:
            continue
        vals = [series[s] for s in stamps]
        first, last = vals[0], vals[-1]
        out.append({
            "hotel": h, "checkin": d, "rounds": len(stamps),
            "first": first, "last": last, "lo": min(vals), "hi": max(vals),
            "delta": round(last - first, 2),
            "spread": round(max(vals) - min(vals), 2),
            "currency": inwin[0].get("currency", "USD"),
        })
    return out


def retail_snapshots():
    out = []
    for f in sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        out.append({"file": f.name, "date": m.group(1) if m else None,
                    "bytes": f.stat().st_size})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()

    rows = load_cloud()
    health = cloud_health(rows)
    move = cloud_movement(rows)
    snaps = retail_snapshots()

    print("═══ 云端 DIDA 采集 (不依赖开机; USD 批发价, 只看动没动) ═══")
    if not health.get("rounds"):
        print("  没有任何数据")
    else:
        flag = "正常" if health["alive"] else "**已停**"
        print(f"  状态 {flag} —— 最后一轮 {health['last']} UTC, 距今 {health['age_min']} 分钟")
        print(f"  共 {health['rounds']} 轮, {health['first']} → {health['last']}, "
              f"间隔中位 {health['median_gap']} 分钟 (目标 {EXPECTED_GAP_MIN})")
        print(f"  按时率 {health['uptime_pct']}% (间隔 <= {GAP_ALERT_MIN} 分钟算按时)")
        if health["gaps"]:
            print(f"  空档 {len(health['gaps'])} 个 —— 调度器丢触发, 见 CLAUDE.md:")
            for g in health["gaps"][-5:]:
                print(f"     {g['from']} → {g['to']}   {g['minutes']} 分钟")

    print("\n═══ 住宿区间内, 云端看到的价格变动 ═══")
    if not move:
        print("  云端的固定入住日没有落在 9/20–10/8 之内的")
    else:
        cur = move[0]["currency"]
        print(f"  (单位 {cur} 批发价 —— **不能**跟看板上的 NZD 零售价比大小)")
        print(f"  {'酒店':<38}{'入住日':<12}{'首轮':>7}{'末轮':>7}{'变化':>7}{'极差':>7}{'轮次':>6}")
        for m in move:
            print(f"  {m['hotel']:<38}{m['checkin']:<12}{m['first']:>7.0f}{m['last']:>7.0f}"
                  f"{m['delta']:>+7.0f}{m['spread']:>7.0f}{m['rounds']:>6}")
        still = sum(1 for m in move if m["spread"] == 0)
        print(f"\n  {len(move)} 个(酒店×入住日)组合里, {still} 个整段观测一分钱没动。")
        if still == len(move):
            print("  -> 云端这边完全静止。弱证据: 零售那边大概也没大动, 快照还能用。")
        else:
            worst = max(move, key=lambda m: m["spread"])
            print(f"  -> 动得最多的是 {worst['hotel']} {worst['checkin']}, "
                  f"极差 {cur}{worst['spread']:.0f}。")
            print("     这只说明市场在动, 换算不出零售价 —— 要下单还得看零售快照。")

    print("\n═══ 本地零售快照 (看板的数据源, 依赖 Yuan 的电脑开着) ═══")
    for s in snaps:
        print(f"  {s['file']}   {s['bytes']//1024} KB")
    if not snaps:
        print("  没有快照 —— 看板无法生成")
    else:
        print(f"  最新快照日期 {snaps[-1]['date']}。要刷新看板, 先在本地跑一轮采集并")
        print("  更新 local/data/snapshots/, 再跑 python local/export_stay_plan.py")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"health": health, "movement": move, "snapshots": snaps,
             "generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {args.json}")


if __name__ == "__main__":
    main()
