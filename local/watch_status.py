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
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CLOUD_DIR = REPO / "data"
SNAP_DIR = HERE / "data" / "snapshots"
# 上次巡检看到的最后一轮 + 当时的价格。用来回答"**这次**动了什么",
# 而不是"整段观测里动过什么" —— 后者是个累计量, 报过一次之后每轮都会再报,
# 巡检就变成了噪音。被 .gitignore 排除, 属于运行时状态。
STATE = HERE / "data" / ".watch_state.json"

# 住宿区间 —— 跟 export_stay_plan.py 保持一致
CHECKIN, CHECKOUT = dt.date(2026, 9, 20), dt.date(2026, 10, 9)

# 采集节奏: 循环内部对齐到 :07/:22/:37/:52, 即 15 分钟一轮。
# 超过这个就算空档 —— 调度器丢触发时会出现几十分钟到几小时的洞。
EXPECTED_GAP_MIN = 15
GAP_ALERT_MIN = 40

# 零售快照里的 timestamp 是**新西兰当地时间**(见 snapshots/README), 云端的
# ts_utc 是 UTC。两者直接相减会差 12-13 小时, 所以必须显式换算。
NZ = ZoneInfo("Pacific/Auckland")
# 本地采集依赖 Yuan 的电脑开着, 夜里关机十来个小时是常态, 不该报警。
# 超过一天半就不正常了 —— CLAUDE.md 记过一次崩了 10 小时没人发现。
RETAIL_STALE_H = 36


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


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(rows):
    """记下每个(酒店,入住日)在最后一轮的价格, 供下次巡检比对。"""
    last = {}
    for r in rows:
        k = f'{r["hotel"]}|{r["checkin"]}'
        cur = last.get(k)
        if cur is None or r["ts_utc"] > cur[0]:
            last[k] = (r["ts_utc"], r["_p"])
        elif r["ts_utc"] == cur[0]:
            last[k] = (cur[0], min(cur[1], r["_p"]))
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(
            {"seen_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
             "last": {k: v[1] for k, v in last.items()},
             "last_ts": max((v[0] for v in last.values()), default=None)},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def since_last(rows, state):
    """跟上次巡检相比动了多少。这才是每轮该看的量。"""
    prev = (state or {}).get("last") or {}
    if not prev:
        return None
    now = {}
    for r in rows:
        k = f'{r["hotel"]}|{r["checkin"]}'
        cur = now.get(k)
        if cur is None or r["ts_utc"] > cur[0]:
            now[k] = (r["ts_utc"], r["_p"])
        elif r["ts_utc"] == cur[0]:
            now[k] = (cur[0], min(cur[1], r["_p"]))
    out = []
    for k, (_, v) in now.items():
        if k in prev and abs(v - prev[k]) >= 1:
            h, d = k.split("|")
            out.append({"hotel": h, "checkin": d, "was": prev[k], "now": v,
                        "delta": round(v - prev[k], 2)})
    return sorted(out, key=lambda x: -abs(x["delta"]))


def retail_snapshots():
    out = []
    for f in sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        out.append({"file": f.name, "date": m.group(1) if m else None,
                    "bytes": f.stat().st_size})
    return out


def retail_freshness(snaps):
    """零售快照里最后一条读数离现在多久。

    这是**决定订哪家的那份数据**, 它陈旧了结论就跟着陈旧 —— 云端再准也替代
    不了。所以这一项单独报, 不跟云端的健康度混在一起。
    """
    if not snaps:
        return None
    f = SNAP_DIR / snaps[-1]["file"]
    try:
        with open(f, encoding="utf-8-sig") as fh:
            stamps = [r["timestamp"] for r in csv.DictReader(fh) if r.get("timestamp")]
    except OSError:
        return None
    if not stamps:
        return None
    last = dt.datetime.strptime(max(stamps), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NZ)
    now = dt.datetime.now(dt.timezone.utc)
    hours = (now - last).total_seconds() / 3600
    return {
        "file": snaps[-1]["file"],
        "last_nz": max(stamps),
        "last_utc": last.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "hours": round(hours, 1),
        "stale": hours > RETAIL_STALE_H,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()

    rows = load_cloud()
    health = cloud_health(rows)
    move = cloud_movement(rows)
    snaps = retail_snapshots()
    fresh = retail_freshness(snaps)
    state = load_state()
    inwin = [r for r in rows
             if CHECKIN.isoformat() <= r["checkin"] < CHECKOUT.isoformat()]
    delta = since_last(inwin, state)

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

    print("\n═══ 跟上次巡检相比 ═══")
    if delta is None:
        print("  没有上次巡检的记录 —— 这一轮只建基线, 下一轮起才能报'动了什么'")
    elif not delta:
        print(f"  上次巡检 {state.get('seen_at', '?')} UTC 以来, 住宿区间内没有任何变动。")
    else:
        print(f"  上次巡检 {state.get('seen_at', '?')} UTC 以来有 {len(delta)} 个组合变动:")
        for d in delta:
            print(f"     {d['hotel']:<38}{d['checkin']}  {d['was']:.0f} → {d['now']:.0f}"
                  f"  ({d['delta']:+.0f})")

    print("\n═══ 本地零售快照 (看板的数据源, 依赖 Yuan 的电脑开着) ═══")
    for s in snaps:
        print(f"  {s['file']}   {s['bytes']//1024} KB")
    if fresh:
        flag = "**过期**" if fresh["stale"] else "尚可"
        print(f"  最后一条读数 {fresh['last_nz']} NZ (= {fresh['last_utc']} UTC)")
        print(f"  距今 {fresh['hours']} 小时 —— {flag} (超过 {RETAIL_STALE_H} 小时算过期)")
        if fresh["stale"]:
            print("  -> 本地采集大概率停了。看门狗计划任务装了吗:")
            print('     schtasks /Create /TN "AucklandHotelWatch" /TR '
                  '"wscript.exe <仓库>\\local\\watchdog.vbs" /SC MINUTE /MO 10 /F')
    if not snaps:
        print("  没有快照 —— 看板无法生成")
    else:
        print(f"  最新快照日期 {snaps[-1]['date']}。要刷新看板, 先在本地跑一轮采集并")
        print("  更新 local/data/snapshots/, 再跑 python local/export_stay_plan.py")

    save_state(inwin)

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"health": health, "movement": move, "delta": delta,
             "snapshots": snaps, "retail_freshness": fresh,
             "generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {args.json}")


if __name__ == "__main__":
    main()
