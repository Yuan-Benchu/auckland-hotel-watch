"""
诊断: 提速配置到底漏没漏渠道, 漏在哪一步
------------------------------------------
对同一批 (酒店, 入住日) 跑 5 种配置, 比较抓到的渠道集合:

  A1  原配置(不拦资源, 6s 死等 + 价格等待)   <- 基线
  A2  原配置, 再跑一遍                       <- 对照组: 量 Google 自身的自然波动
  B   拦资源 + settle(3)                     <- 当前提速配置
  C   拦资源 + settle(8)                     <- 更有耐心
  D   不拦资源 + settle(8)                   <- 单独隔离"拦资源"的影响

没有 A2 这个对照组就分不清 "配置漏抓" 和 "Google 本来就每次不一样"。
"""

import sys
from pathlib import Path
import time
from datetime import date
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hotel_fast as hf
from playwright.sync_api import sync_playwright

COMBOS = [
    ("VR Queen Street Auckland", date(2026, 9, 26)),
    ("VR Auckland City",         date(2026, 9, 26)),
    ("VR Queen Street Auckland", date(2026, 10, 16)),
    ("VR Auckland City",         date(2026, 10, 16)),
    ("VR Queen Street Auckland", date(2026, 11, 7)),
    ("VR Auckland City",         date(2026, 11, 7)),
]

# (标签, 是否拦资源, 是否用原始死等, settle 轮数)
CONFIGS = [
    ("A1 原配置",          False, True,  None),
    ("A2 原配置(对照)",    False, True,  None),
    ("B  拦+settle3",      True,  False, 3),
    ("C  拦+settle8",      True,  False, 8),
    ("D  不拦+settle8",    False, False, 8),
]

GAP = 2


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    results = defaultdict(dict)   # results[(hotel,date)][label] = (providers, secs)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # 拦资源是装在 context 上的, 所以两种 context 各建一个
        ctxs, scrapers, pages = {}, {}, {}
        for block in (True, False):
            c = browser.new_context(locale="en-NZ",
                                    timezone_id="Pacific/Auckland",
                                    viewport={"width": 1400, "height": 1000})
            ctxs[block] = c
            scrapers[block] = hf.Scraper(c, block=block)
            pages[block] = c.new_page()

        for hotel, d in COMBOS:
            print(f"\n{'=' * 66}\n{d}  {hotel}\n{'=' * 66}", flush=True)
            for label, block, slow, rounds in CONFIGS:
                if rounds:
                    hf.SETTLE_STABLE_ROUNDS = rounds
                t0 = time.monotonic()
                try:
                    offers = scrapers[block].fetch(pages[block], hotel, d, slow=slow)
                except Exception as e:
                    print(f"  {label:18} 失败 {type(e).__name__}", flush=True)
                    continue
                dt = time.monotonic() - t0
                results[(hotel, d)][label] = (set(offers), dt)
                names = ",".join(sorted(offers))
                print(f"  {label:18} {dt:5.1f}s  {len(offers)} 家  {names}", flush=True)
                time.sleep(GAP)

        browser.close()

    # ---------------- 汇总 ----------------
    print(f"\n\n{'#' * 70}\n# 汇总: 各配置相对基线 A1 的渠道集合差异\n{'#' * 70}\n")
    print(f"{'配置':18} {'平均渠道数':>10} {'平均耗时':>9} "
          f"{'比A1少的次数':>13} {'累计漏掉':>9}")
    print("-" * 70)

    base_label = CONFIGS[0][0]
    for label, *_ in CONFIGS:
        counts, times, fewer, missing_total = [], [], 0, 0
        for k, per in results.items():
            if label not in per or base_label not in per:
                continue
            got, dt = per[label]
            base, _ = per[base_label]
            counts.append(len(got))
            times.append(dt)
            missing = base - got
            if len(got) < len(base):
                fewer += 1
            missing_total += len(missing)
        if not counts:
            continue
        print(f"{label:18} {sum(counts) / len(counts):10.2f} "
              f"{sum(times) / len(times):8.1f}s {fewer:13} {missing_total:9}")

    print("\n漏掉的具体渠道 (相对 A1):")
    for label, *_ in CONFIGS[1:]:
        miss = defaultdict(int)
        for k, per in results.items():
            if label not in per or base_label not in per:
                continue
            for p in per[base_label][0] - per[label][0]:
                miss[p] += 1
        if miss:
            print(f"  {label:18} " +
                  ", ".join(f"{k}×{v}" for k, v in sorted(miss.items(),
                                                          key=lambda kv: -kv[1])))
        else:
            print(f"  {label:18} (无)")

    print("\n注: A2 用的配置和 A1 完全一样。A2 这一行的数字就是 Google 自身的")
    print("    自然波动下限 —— 任何配置的差异要大于它, 才谈得上是配置问题。")


if __name__ == "__main__":
    main()
