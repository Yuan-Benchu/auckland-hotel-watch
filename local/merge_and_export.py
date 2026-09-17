"""
合并所有抓取产出 -> 统一数据集 + 可视化用的 JSON
------------------------------------------------
输入(存在哪个读哪个):
    hotel_prices.csv        原脚本(慢配置)的产出
    hotel_prices_fast.csv   通宵 runner(提速配置)的产出

输出:
    hotel_prices_merged.csv  去重后的统一数据集
    dashboard_data.json      可视化用(已按 酒店/日期/渠道 聚合好)
"""

import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent
# 只读干净数据。
# hotel_prices.csv / hotel_prices_fast.csv 是用有 bug 的解析器抓的:
# 它会把页面 "Similar hotels" 区块里别家酒店的报价算成目标酒店的,
# 且污染是双向的(有时偏高有时偏低), 没法用系数修正, 只能弃用。
SOURCES = [
    ("v3", BASE / "hotel_prices_v3.csv"),
]
MERGED = BASE / "hotel_prices_merged.csv"
JSON_OUT = BASE / "dashboard_data.json"

WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


def load():
    rows = []
    for src, path in SOURCES:
        if not path.exists():
            print(f"  {path.name}: 不存在, 跳过")
            continue
        n = 0
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    r["price_nzd"] = int(r["price_nzd"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not r.get("hotel") or not r.get("checkin"):
                    continue
                r["source"] = src
                r.setdefault("room_type", "")
                rows.append(r)
                n += 1
        print(f"  {path.name}: {n} 行")
    return rows


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("读取来源:")
    rows = load()
    if not rows:
        print("没有任何数据, 退出")
        return

    # --- 去重: 同一 (时间戳, 酒店, 入住日, 渠道) 只留一条 ---
    seen, uniq = set(), []
    for r in rows:
        k = (r["timestamp"], r["hotel"], r["checkin"], r["provider"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    print(f"\n合并后 {len(uniq)} 行 (去掉 {len(rows) - len(uniq)} 条重复)")

    uniq.sort(key=lambda r: (r["hotel"], r["checkin"], r["timestamp"],
                             r["price_nzd"]))
    with open(MERGED, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "hotel", "checkin",
                                          "provider", "price_nzd", "room_type",
                                          "source"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(uniq)
    print(f"统一数据集 -> {MERGED.name}")

    hotels = sorted({r["hotel"] for r in uniq})
    providers = sorted({r["provider"] for r in uniq})
    checkins = sorted({r["checkin"] for r in uniq})
    stamps = sorted({r["timestamp"] for r in uniq})

    print(f"\n酒店 {len(hotels)} 家 | 渠道 {len(providers)} 个 | "
          f"入住日 {len(checkins)} 天 | 抓取时点 {len(stamps)} 个")
    print(f"时间跨度 {stamps[0]} ~ {stamps[-1]}")

    # --- 每个 (酒店, 入住日) 取最新一次快照 ---
    latest = {}
    for r in uniq:
        k = (r["hotel"], r["checkin"])
        if k not in latest or r["timestamp"] > latest[k]["ts"]:
            latest[k] = {"ts": r["timestamp"], "offers": {}}
        if r["timestamp"] == latest[k]["ts"]:
            p = latest[k]["offers"]
            if r["provider"] not in p or r["price_nzd"] < p[r["provider"]]:
                p[r["provider"]] = r["price_nzd"]

    # --- 历史最低价(跨所有快照), 用来判断"现在这个价算不算好" ---
    hist_min = defaultdict(lambda: 10 ** 9)
    for r in uniq:
        k = (r["hotel"], r["checkin"])
        hist_min[k] = min(hist_min[k], r["price_nzd"])

    series = []
    for hotel in hotels:
        pts = []
        for ci in checkins:
            k = (hotel, ci)
            if k not in latest:
                continue
            offers = latest[k]["offers"]
            if not offers:
                continue
            prov, price = min(offers.items(), key=lambda kv: kv[1])
            d = date.fromisoformat(ci)
            pts.append({
                "checkin": ci,
                "weekday": WEEKDAY[d.weekday()],
                "is_weekend": d.weekday() >= 4,       # 周五/六/日
                "min_price": price,
                "min_provider": prov,
                "max_price": max(offers.values()),
                "n_providers": len(offers),
                "offers": offers,
                "hist_min": hist_min[k],
                "ts": latest[k]["ts"],
            })
        if pts:
            series.append({"hotel": hotel, "points": pts})

    # --- 渠道层面: 每个渠道当最低价的次数 / 平均溢价 ---
    prov_stats = defaultdict(lambda: {"wins": 0, "appear": 0, "prem": []})
    for s in series:
        for p in s["points"]:
            lo = p["min_price"]
            for prov, price in p["offers"].items():
                prov_stats[prov]["appear"] += 1
                prov_stats[prov]["prem"].append((price - lo) / lo * 100)
                if price == lo:
                    prov_stats[prov]["wins"] += 1
    provider_summary = sorted(
        [{"provider": k,
          "wins": v["wins"],
          "appear": v["appear"],
          "avg_premium": round(statistics.mean(v["prem"]), 1) if v["prem"] else 0}
         for k, v in prov_stats.items()],
        key=lambda x: -x["wins"])

    # --- 每家酒店的汇总 ---
    summary = []
    for s in series:
        prices = [p["min_price"] for p in s["points"]]
        wk = [p["min_price"] for p in s["points"] if p["is_weekend"]]
        nw = [p["min_price"] for p in s["points"] if not p["is_weekend"]]
        cheapest = min(s["points"], key=lambda p: p["min_price"])
        summary.append({
            "hotel": s["hotel"],
            "days": len(prices),
            "median": round(statistics.median(prices)),
            "min": min(prices),
            "max": max(prices),
            "weekend_median": round(statistics.median(wk)) if wk else None,
            "weekday_median": round(statistics.median(nw)) if nw else None,
            "cheapest_date": cheapest["checkin"],
            "cheapest_provider": cheapest["min_provider"],
        })

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshots": len(stamps),
        "span": [stamps[0], stamps[-1]],
        "rows": len(uniq),
        "hotels": hotels,
        "providers": providers,
        "series": series,
        "provider_summary": provider_summary,
        "summary": summary,
    }
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"可视化数据 -> {JSON_OUT.name}")

    print("\n各酒店汇总:")
    for s in summary:
        print(f"  {s['hotel']}")
        print(f"    {s['days']} 天 | 中位 NZ${s['median']} | "
              f"区间 NZ${s['min']}–{s['max']}")
        print(f"    周末中位 NZ${s['weekend_median']} vs "
              f"平日中位 NZ${s['weekday_median']}")
        print(f"    最低 {s['cheapest_date']} NZ${s['min']} "
              f"({s['cheapest_provider']})")

    print("\n渠道表现 (当最低价次数 / 出现次数 / 平均溢价):")
    for p in provider_summary:
        print(f"  {p['provider']:16} {p['wins']:4} / {p['appear']:4}  "
              f"+{p['avg_premium']:.1f}%")


if __name__ == "__main__":
    main()
