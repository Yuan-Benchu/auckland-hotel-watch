"""验证 SETTLE_MIN_MS 下限能不能把赞助块(Booking.com/Hotels.com)等回来。"""
import sys, time
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding='utf-8')
import hotel_fast as hf
from datetime import date
from playwright.sync_api import sync_playwright

COMBOS = [("VR Queen Street Auckland", date(2026,9,20)),
          ("VR Auckland City",         date(2026,9,20)),
          ("VR Queen Street Auckland", date(2026,10,16)),
          ("VR Auckland City",         date(2026,10,16)),
          ("VR Queen Street Auckland", date(2026,11,7)),
          ("VR Auckland City",         date(2026,11,7))]

def run(label, min_ms):
    hf.SETTLE_MIN_MS = min_ms
    tot_n, tot_t = [], []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(locale="en-NZ", timezone_id="Pacific/Auckland",
                            viewport={"width":1400,"height":1000})
        sc = hf.Scraper(ctx, block=True); pg = ctx.new_page()
        print(f"\n===== {label} (SETTLE_MIN_MS={min_ms}) =====")
        for hotel, d in COMBOS:
            t0 = time.monotonic()
            try:
                o = sc.fetch(pg, hotel, d)
            except Exception as e:
                print(f"  {d} {hotel[:22]:22} 失败 {type(e).__name__}"); continue
            dt = time.monotonic()-t0
            tot_n.append(len(o)); tot_t.append(dt)
            lo = min(o.values()) if o else 0
            print(f"  {d} {hotel[:22]:22} {dt:4.1f}s {len(o)}家 最低{lo:4}  "
                  f"{','.join(sorted(o))}")
            time.sleep(2)
        b.close()
    return sum(tot_n)/len(tot_n), sum(tot_t)/len(tot_t)

a = run("修改前(无下限)", 0)
b = run("修改后(2.6s下限)", 2600)
print(f"\n{'='*58}")
print(f"无下限   : 平均 {a[0]:.2f} 个渠道, {a[1]:.1f}s")
print(f"2.6s下限 : 平均 {b[0]:.2f} 个渠道, {b[1]:.1f}s")
print(f"渠道数 {b[0]-a[0]:+.2f}, 每次多花 {b[1]-a[1]:+.1f}s")
