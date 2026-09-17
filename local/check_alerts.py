# -*- coding: utf-8 -*-
"""低价提醒检查器 —— 只在价格真的跌破阈值时报，且必须能用页面原文复核。

设计要点(都是被 2026-09-16 那次假 NZ$32 逼出来的):

  1. 任何候选提醒都要回到 raw_archive/ 里那次抓取的页面原文，
     确认这个金额确实以"整行就是一个金额"的形式出现过。核不上就不报。
  2. 去重按状态机: 跌破阈值报一次, 继续下探再报, 回到阈值以上才重新武装。
     不会同一个价格每轮响一次。
  3. 只看每个(酒店,入住日)的最新一轮观测, 不拿历史最低价冒充当前价。

输出: 标准输出一段给人看的摘要; 有命中时 exit 0 并在第一行写 ALERT,
      无命中时第一行写 QUIET。状态存 alert_state.json。
"""
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent
CSV_PATH = BASE / "hotel_prices_v3.csv"
ARCHIVE = BASE / "raw_archive"
STATE = BASE / "alert_state.json"
LOG = BASE / "alerts.log"

THRESHOLD = 80
STALE_AFTER_MIN = 90        # 数据超过这么久没更新就认为采集挂了
STALE_COOLDOWN_MIN = 360    # 停更提醒的冷却, 免得每半小时喊一次
PRICE_LINE_RE = re.compile(r"^(?:NZ)?\$\s*([\d,]+)(?:\.\d+)?$")
WD = "一二三四五六日"


def booking_url(hotel, checkin):
    """该酒店该入住日的 Google Travel 比价页链接。

    页面上 "Visit site" 按钮背后是 google.com/aclk?...&gclid=... 的广告跳转,
    带一次性追踪令牌, 存下来几小时后多半失效 —— 所以不存那个。
    比价页 URL 由 (酒店, 日期) 确定性算出, 不会过期, 过去的提醒也能补算。
    """
    try:
        sys.path.insert(0, str(HERE))
        import hotel_fast as hf
        return hf.build_url(hotel, date.fromisoformat(checkin))
    except Exception:
        return ""


def _blocked():
    """已知归属错误的 (酒店, 渠道) —— 定义在 hotel_fast.PROVIDER_BLOCK。

    2026-09-17 之前抓的数据里还留着这些行(拦截是之后才加的), 所以读的时候也要滤,
    否则看板和提醒会继续拿 VR Auckland Airport 的价当 VR Auckland City 的价。
    """
    try:
        sys.path.insert(0, str(HERE))
        import hotel_fast as hf
        return hf.PROVIDER_BLOCK
    except Exception:
        return {}


def load_rows():
    rows = []
    if not CSV_PATH.exists():
        return rows
    block = _blocked()
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                r["price_nzd"] = int(r["price_nzd"])
                r["_t"] = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError, TypeError):
                continue
            if r["provider"] in block.get(r["hotel"], ()):
                continue
            rows.append(r)
    return rows


def latest_min(rows):
    """每个(酒店,入住日)最新一轮的最低报价。"""
    newest = {}
    for r in rows:
        k = (r["hotel"], r["checkin"])
        if k not in newest or r["_t"] > newest[k]:
            newest[k] = r["_t"]
    best = {}
    for r in rows:
        k = (r["hotel"], r["checkin"])
        if r["_t"] != newest[k]:
            continue
        if k not in best or r["price_nzd"] < best[k]["price_nzd"]:
            best[k] = r
    return best


def verify(hotel, checkin, price, provider):
    """回到页面原文复核这条报价。

    不只是"页面里有这个数" —— 那样的话别的渠道区块里的金额也会放行。
    这里拿解析器把存档原文重跑一遍, 要求它吐出同一个(渠道, 价格)。
    找不到归档文件算"未复核", 一样不报。
    """
    if not ARCHIVE.exists():
        return False, "没有 raw_archive 目录"
    stem = f"{hotel[:12].replace(' ', '_')}_{checkin}_"
    files = sorted(ARCHIVE.glob(stem + "*.txt"))
    if not files:
        return False, "没有这次抓取的页面存档"
    latest = files[-1]
    try:
        text = latest.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"读存档失败 {e.__class__.__name__}"

    # 金额必须独立成行(排掉 "Save $33 if you stay ..." 这类句中金额)
    standalone = {int(m.group(1).replace(",", ""))
                  for ln in text.splitlines()
                  if (m := PRICE_LINE_RE.match(ln.strip()))}
    if price not in standalone:
        return False, f"{latest.name} 里没有独立成行的 ${price}"

    # 且解析器在这份原文上必须把这个价归给同一个渠道
    try:
        sys.path.insert(0, str(HERE))
        import hotel_fast as hf
        offers = hf.parse_offers(text, with_rooms=True)
    except Exception as e:
        return False, f"重跑解析器失败 {e.__class__.__name__}"
    got = offers.get(provider)
    if got is None:
        return False, f"{latest.name} 里 {provider} 没有报价"
    if got[0] != price:
        return False, f"{latest.name} 里 {provider} 是 ${got[0]} 不是 ${price}"
    return True, latest.name


def main():
    rows = load_rows()
    if not rows:
        print("STALE")
        print("采集数据文件是空的，监测没有在工作。")
        return

    # 采集进程死了的话, 下面的逻辑会一直安静地回 QUIET —— 用户会以为"没有降价",
    # 实际是"没有数据"。2026-09-16 就这么丢了一整夜: runner 15:16 被待机杀掉,
    # 提醒任务照常跑, 照常静默。所以先查数据新不新。
    newest = max(r["_t"] for r in rows)
    age_min = (datetime.now() - newest).total_seconds() / 60
    if age_min > STALE_AFTER_MIN:
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        meta = state.setdefault("__meta__", {})
        last = meta.get("stale_notified_at")
        quiet_until = None
        if last:
            try:
                quiet_until = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                quiet_until = None
        # 别每 30 分钟重复喊, 冷却 STALE_COOLDOWN_MIN 分钟
        if quiet_until and (datetime.now() - quiet_until).total_seconds() / 60 < STALE_COOLDOWN_MIN:
            print("QUIET")
            print(f"数据已停更 {age_min/60:.1f} 小时，但刚提醒过，本轮不重复。")
            return
        meta["stale_notified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        print("STALE")
        print(f"价格监测已停更 {age_min/60:.1f} 小时（最后一条 "
              f"{newest:%m-%d %H:%M}），采集进程可能已经死了。")
        return

    best = latest_min(rows)
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    fired, unverified = [], []
    for (hotel, checkin), r in sorted(best.items(), key=lambda kv: kv[0][1]):
        key = f"{hotel}|{checkin}"
        st = state.setdefault(key, {"armed": True, "last": None})
        price = r["price_nzd"]

        if price >= THRESHOLD:
            st["armed"] = True
            st["last"] = None
            continue

        trigger = st["armed"] or (st["last"] is not None and price < st["last"])
        if not trigger:
            continue

        ok, why = verify(hotel, checkin, price, r["provider"])
        item = {"hotel": hotel, "checkin": checkin, "price": price,
                "prov": r["provider"], "ts": r["timestamp"],
                "wd": WD[date.fromisoformat(checkin).weekday()],
                "note": why, "new_low": not st["armed"],
                "url": booking_url(hotel, checkin)}
        if ok:
            fired.append(item)
            st["armed"] = False
            st["last"] = price
        else:
            # 复核不过 —— 不报, 也不改状态, 下轮还会再试
            unverified.append(item)

    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if fired:
        with open(LOG, "a", encoding="utf-8") as f:
            for i in fired:
                f.write(f"{stamp}\t{i['checkin']}\t{i['hotel']}\t"
                        f"NZ${i['price']}\t{i['prov']}\t{i['note']}\t{i['url']}\n")

    print("ALERT" if fired else "QUIET")
    if fired:
        cheapest = min(fired, key=lambda i: i["price"])
        # 推送上限 200 字符, 链接就占 120 —— 日期去掉年份留出余量
        print(f"{cheapest['checkin'][5:]} 周{cheapest['wd']} "
              f"{cheapest['hotel']} NZ${cheapest['price']} {cheapest['prov']}"
              f"{f'，另有 {len(fired) - 1} 个日期' if len(fired) > 1 else ''} "
              f"{cheapest['url']}")
        print()
        for i in fired:
            tag = "再创新低" if i["new_low"] else "跌破"
            print(f"  {i['checkin']} 周{i['wd']}  NZ${i['price']:<4} "
                  f"{i['prov']:<14} {i['hotel']:<26} {tag} · 已复核 {i['note']}")
            print(f"        {i['url']}")
    else:
        under = sum(1 for r in best.values() if r["price_nzd"] < THRESHOLD)
        print(f"没有新的跌破。当前低于 NZ${THRESHOLD} 的入住日 {under} 个"
              f"（都已提醒过，价格没有继续下探）。")
    if unverified:
        print()
        print(f"另有 {len(unverified)} 条低价没能用页面原文复核，已压下不报：")
        for i in unverified[:5]:
            print(f"  {i['checkin']} {i['hotel']} NZ${i['price']} — {i['note']}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
