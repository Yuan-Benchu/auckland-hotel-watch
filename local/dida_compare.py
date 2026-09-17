# -*- coding: utf-8 -*-
"""把 DIDA 的报价和我们从 Google 抓到的同日报价并排比较。只读。"""
import csv, json, re, sys
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, ".")
import dida_probe as dp
import check_alerts as ca

CI = date(2026, 10, 6); CO = date(2026, 10, 7)
HOTELS = dp.HOTELS

# 本地(Google)同日最新报价
rows = ca.load_rows()
newest = {}
for r in rows:
    k = (r["hotel"], r["checkin"])
    if k not in newest or r["_t"] > newest[k]: newest[k] = r["_t"]
local = {}
for r in rows:
    k = (r["hotel"], r["checkin"])
    if r["_t"] != newest[k] or r["checkin"] != CI.isoformat(): continue
    if k not in local or r["price_nzd"] < local[k]["price_nzd"]: local[k] = r

init, sid = dp.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "cmp", "version": "1"}})
dp.rpc("notifications/initialized", {}, sid)

print(f"入住 {CI} 退房 {CO}, 1 人 1 间\n")
print(f"{'酒店':<38}{'DIDA':>12}{'Google(NZD)':>13}{'差':>9}")
out=[]
for h in HOTELS:
    r, _ = dp.rpc("tools/call", {"name": "getHotelDetail", "arguments": {
        "name": h,
        "dateParam": {"checkInDate": CI.isoformat(), "checkOutDate": CO.isoformat()},
        "occupancyParam": {"adultCount": 1, "roomCount": 1},
        "localeParam": {"countryCode": "NZ", "currency": "NZD"}}}, sid)
    t = dp.text_of(r)
    try:
        j = json.loads(t)
    except Exception:
        j = {}
    # 从返回里找最低价和货币
    prices=[]
    def walk(o):
        if isinstance(o, dict):
            for k,v in o.items():
                if k in ("lowestPrice","totalPrice","price","amount") and isinstance(v,(int,float)):
                    prices.append(v)
                walk(v)
        elif isinstance(o, list):
            for x in o: walk(x)
    walk(j)
    cur = set(re.findall(r'"currency"\s*:\s*"([A-Z]{3})"', t)) or set(re.findall(r'\b(NZD|USD|CNY|EUR)\b', t))
    g = local.get((h, CI.isoformat()), {}).get("price_nzd")
    lo = min(prices) if prices else None
    cs = "/".join(sorted(cur)) if cur else "?"
    diff = f"{lo-g:+.0f}" if (lo and g and cs=="NZD") else ("口径不同" if lo and g else "-")
    print(f"{h[:37]:<38}{(f'{lo:.0f} {cs}' if lo else '无价'):>12}{(g if g else '-'):>13}{diff:>9}")
    out.append({"hotel":h,"dida":lo,"currency":cs,"google_nzd":g})
Path("dida_vs_google.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
