# -*- coding: utf-8 -*-
"""把 v2(已作废) + v3(干净) 两份观测导出成时间轴看板用的 JSON。"""
import csv, json, statistics, sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent
V2, V3 = BASE / "hotel_prices_v2.csv", BASE / "hotel_prices_v3.csv"
OUT = BASE / "timeline_data.json"
EPOCH = datetime(2026, 9, 16)
WD = "一二三四五六日"


sys.path.insert(0, str(HERE))
import hotel_fast as _hf0                      # noqa: E402
# 已知归属错误的 (酒店, 渠道): Google 把 VR Auckland Airport 的订房引擎挂在了
# VR Auckland City 的 "Official site" 位上。拦截是 2026-09-17 才加的, 之前抓的
# 数据里还留着这些行, 所以读的时候必须一并滤掉。
BLOCK = getattr(_hf0, "PROVIDER_BLOCK", {})


def load(p, src):
    out = []
    if not p.exists():
        return out
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                r["price_nzd"] = int(r["price_nzd"])
                r["_m"] = int((datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                               - EPOCH).total_seconds() // 60)
            except (ValueError, KeyError, TypeError):
                continue
            if r["provider"] in BLOCK.get(r["hotel"], ()):
                continue
            r["_src"] = src
            out.append(r)
    return out


v2, v3 = load(V2, 2), load(V3, 3)
allr = v2 + v3

# 每渠道每(酒店,入住日)的时间序列
series = defaultdict(lambda: defaultdict(list))
for r in allr:
    series[f"{r['hotel']}|{r['checkin']}"][r["provider"]].append(
        [r["_m"], r["price_nzd"], r["_src"]])
for combo in series.values():
    for pts in combo.values():
        pts.sort()

# 每轮的"当日最低价"(只用 v3)
rounds = defaultdict(dict)
for r in v3:
    k = (r["hotel"], r["checkin"])
    cur = rounds[r["_m"] // 20 * 20].get(k)
    if cur is None or r["price_nzd"] < cur[0]:
        rounds[r["_m"] // 20 * 20][k] = (r["price_nzd"], r["provider"])

# 提前天数 vs 最新一轮最低价
best_latest = {}
for r in v3:
    k = (r["hotel"], r["checkin"])
    if k not in best_latest or r["price_nzd"] < best_latest[k]["price_nzd"]:
        best_latest[k] = r
lead = []
for (hotel, ci), r in sorted(best_latest.items(), key=lambda kv: kv[0][1]):
    d = date.fromisoformat(ci)
    lead.append({"hotel": hotel, "checkin": ci, "lead": (d - EPOCH.date()).days,
                 "wd": WD[d.weekday()], "we": d.weekday() >= 4,
                 "price": r["price_nzd"], "prov": r["provider"]})

combos = sorted({(r["hotel"], r["checkin"]) for r in allr})
prov_order = [p for p, _ in sorted(
    ((p, sum(1 for r in allr if r["provider"] == p))
     for p in {r["provider"] for r in allr}), key=lambda kv: -kv[1])]

# 每个(酒店,入住日)的 Google Travel 比价页链接。由日期确定性算出, 不会过期 ——
# 页面上 Visit site 背后是 google.com/aclk?...&gclid=... 的一次性广告跳转, 存不住。
sys.path.insert(0, str(HERE))
import hotel_fast as _hf          # noqa: E402
urls = {f"{h}|{ci}": _hf.build_url(h, date.fromisoformat(ci)) for h, ci in combos}

# 订房方案: 逐晚挑最便宜的那家。Google 上连住越长越贵也越难订
# (实测 1晚 81 / 2晚 87 / 3晚 90 / 7晚 110 / 30晚 98, 5·14·21 晚根本没有报价),
# 所以"碎订"就是最省的订法, 这里把每一晚的最优选择列出来。
stay_start = date(2026, 9, 17)
# 必须用"最新一轮的最低价", 不是"历史最低价" —— 历史低点早就订不到了,
# 把它填进方案表等于报一个不存在的价。
_newest = {}
for r in v3:
    k = (r["hotel"], r["checkin"])
    if k not in _newest or r["_m"] > _newest[k]:
        _newest[k] = r["_m"]
_latest = {}
for r in v3:
    k = (r["hotel"], r["checkin"])
    if r["_m"] != _newest[k]:
        continue
    if k not in _latest or r["price_nzd"] < _latest[k]["price_nzd"]:
        _latest[k] = r
plan = []
for ci in sorted({r["checkin"] for r in v3}):
    d = date.fromisoformat(ci)
    if d < stay_start:
        continue
    cands = [(_latest[(h, ci)]["price_nzd"], h, _latest[(h, ci)]["provider"])
             for h in sorted({k[0] for k in _latest}) if (h, ci) in _latest]
    if not cands:
        continue
    p, h, prov = min(cands)
    plan.append({"checkin": ci, "wd": WD[d.weekday()], "we": d.weekday() >= 4,
                 "price": p, "hotel": h, "prov": prov,
                 "url": urls.get(f"{h}|{ci}", "")})

suspect = [r for r in v2 if r["price_nzd"] < 60]
data = {
    "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "epoch": EPOCH.strftime("%Y-%m-%d"),
    "v2": {"rows": len(v2), "suspect": len(suspect),
           "span": [min((r["timestamp"] for r in v2), default=""),
                    max((r["timestamp"] for r in v2), default="")]},
    "v3": {"rows": len(v3), "rounds": len(rounds),
           "span": [min((r["timestamp"] for r in v3), default=""),
                    max((r["timestamp"] for r in v3), default="")]},
    "fix_at": None,
    "hotels": sorted({r["hotel"] for r in allr}),
    "checkins": sorted({r["checkin"] for r in allr}),
    "providers": prov_order,
    "series": {k: dict(v) for k, v in series.items()},
    "lead": lead,
    "urls": urls,
    "plan": plan,
}
if v3:
    data["fix_at"] = min(r["_m"] for r in v3)
OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
               encoding="utf-8")
print(f"v2 {len(v2)} 行 (可疑 {len(suspect)}) | v3 {len(v3)} 行 / {len(rounds)} 轮 "
      f"| {len(combos)} 组合 -> {OUT.name} {OUT.stat().st_size/1024:.0f}KB")


# ---- 注入模板 ----
# 模板与看板产物都在 local/dashboards/, 数据在 local/data/。
# 搬家时这两行还指着 data/, 而代码是 if TPL.exists() 才生成 ——
# 模板找不到就静默跳过, 不报错, 所以一直没被发现。
TPL = HERE / "dashboards" / "timeline_template.html"
HTML = HERE / "dashboards" / "auckland_timeline.html"
if TPL.exists():
    tpl = TPL.read_text(encoding="utf-8")
    assert "/*__DATA__*/" in tpl, "模板里找不到 placeholder"
    HTML.write_text(tpl.replace("/*__DATA__*/{}",
                                json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
                    encoding="utf-8")
    print(f"-> {HTML.name} {HTML.stat().st_size/1024:.0f}KB")
