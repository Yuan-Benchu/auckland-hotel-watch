"""价格随抓取时间的变化。关键是把"真降价"和"渠道覆盖变好导致的最低价下移"分开。"""
import csv, sys, statistics
from collections import defaultdict
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')

rows = []
with open('hotel_prices_clean.csv', encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        try: r['price_nzd'] = int(r['price_nzd'])
        except: continue
        r['t'] = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S')
        rows.append(r)

# 按时间间隔 >5 分钟切分成"轮次"
rows.sort(key=lambda r: r['t'])
cycles, cur, last = [], [], None
for r in rows:
    if last and (r['t'] - last).total_seconds() > 300:
        cycles.append(cur); cur = []
    cur.append(r); last = r['t']
if cur: cycles.append(cur)

print(f"共 {len(cycles)} 轮\n")
print(f"{'轮':>2} {'时间':>13} {'条数':>5} {'组合':>5} {'平均渠道':>8}  "
      f"{'City最低中位':>12} {'Queen最低中位':>13}  {'City官网':>9} {'Queen官网':>10}")
print('-'*94)

hist = []
for i, c in enumerate(cycles, 1):
    combos = defaultdict(dict)
    for r in c:
        k = (r['hotel'], r['checkin'])
        p = combos[k]
        if r['provider'] not in p or r['price_nzd'] < p[r['provider']]:
            p[r['provider']] = r['price_nzd']
    avg_prov = statistics.mean(len(v) for v in combos.values())
    out = {}
    for hotel in ('VR Auckland City', 'VR Queen Street Auckland'):
        mins = [min(v.values()) for k, v in combos.items() if k[0] == hotel]
        offi = [v['Official site'] for k, v in combos.items()
                if k[0] == hotel and 'Official site' in v]
        out[hotel] = (statistics.median(mins) if mins else None,
                      statistics.median(offi) if offi else None,
                      len(mins))
    a, q = out['VR Auckland City'], out['VR Queen Street Auckland']
    print(f"{i:2} {c[0]['t']:%H:%M}-{c[-1]['t']:%H:%M} {len(c):5} "
          f"{len(combos):5} {avg_prov:8.2f}  "
          f"{str(a[0]):>12} {str(q[0]):>13}  {str(a[1]):>9} {str(q[1]):>10}")
    hist.append((i, avg_prov, a, q))

print("\n注: 「最低中位」受渠道覆盖影响(渠道越多最低价越低);")
print("    「官网」只看 Official site 这一个渠道, 不受覆盖变化干扰 —— 看趋势要看这两列。\n")

# 逐组合配对比较: 第一轮 vs 最后一轮, 只用两轮都有官网报价的组合
first, last_c = cycles[0], cycles[-1]
def offi_map(c):
    m = {}
    for r in c:
        if r['provider'] != 'Official site': continue
        k = (r['hotel'], r['checkin'])
        if k not in m or r['price_nzd'] < m[k]: m[k] = r['price_nzd']
    return m
f_m, l_m = offi_map(first), offi_map(last_c)
shared = sorted(set(f_m) & set(l_m))
if shared:
    deltas = [l_m[k] - f_m[k] for k in shared]
    up = sum(1 for d in deltas if d > 0); dn = sum(1 for d in deltas if d < 0)
    print(f"配对比较(首轮 vs 末轮, 仅官网, {len(shared)} 个组合):")
    print(f"  涨 {up} / 跌 {dn} / 平 {len(deltas)-up-dn}")
    print(f"  中位变化 {statistics.median(deltas):+.0f}  平均 {statistics.mean(deltas):+.1f}")
    big = sorted(zip(deltas, shared))
    print(f"  跌最多: " + ", ".join(f"{k[1]}({k[0][:12]}) {d:+d}" for d,k in big[:3]))
    print(f"  涨最多: " + ", ".join(f"{k[1]}({k[0][:12]}) {d:+d}" for d,k in big[-3:]))
