# -*- coding: utf-8 -*-
"""VR 两家的低价提醒 —— 零售 NZ$80 门槛 + 云端批发新低早警。

## 为什么不是一个门槛管到底

Yuan 要的是"低于 80 纽币提醒我"。**NZ$80 是零售价的门槛**, 只能拿
Google 零售价(NZD)去比。云端 DIDA 是 **USD 批发价**, 实测比零售贵约
31% —— 拿 80 去比它, 既错币种又错市场, 报出来的每一条都是假的。
CLAUDE.md 的铁律就是这条: 两者不进同一个统计。

所以这里是**两节, 各用各的判据, 永不相加**:

  零售(NZD, 可下单)   跌破 NZ$80 → 真提醒, 附订房链接, 尽量拿页面原文核
  云端(USD, 不可下单)  刷新自身历史最低 → 早警, 说明"这家在松动",
                       但**不换算、不跟 80 比、不能据此下单**

## 为什么零售那节现在多半是哑的

零售价只有本地 Google 抓取能出, 它依赖 Yuan 开机。脚本优先读实时文件
`data/hotel_prices_v3.csv`, 没有就退回 `data/snapshots/` 里的快照, 并
在输出里写明数据有多旧。快照过期时**不会**把陈年旧价当成"现在跌破了"
来报 —— 那是最容易骗人的一种假提醒。

## 去重

状态机: 跌破报一次, 继续下探再报, 回到门槛以上才重新武装。云端新低同理,
只有真的比记录还低才报。状态存 data/.vr_alert_state.json。
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
LIVE_CSV = HERE / "data" / "hotel_prices_v3.csv"
SNAP_DIR = HERE / "data" / "snapshots"
CLOUD_DIR = REPO / "data"
STATE = HERE / "data" / ".vr_alert_state.json"

sys.path.insert(0, str(HERE))
import offer_parse  # noqa: E402

NZ = ZoneInfo("Pacific/Auckland")

HOTELS = ["VR Auckland City", "VR Queen Street Auckland"]
THRESHOLD_NZD = 80          # 只用于零售价。**不要**拿它比 USD。
RETAIL_STALE_H = 36         # 跟 watch_status.py 同一个口径
STAY_FROM, STAY_TO = date(2026, 9, 20), date(2026, 10, 9)

# 路由到 bookings.vrhotels.co.nz/116052 = VR Auckland **Airport**, 不是市区店
DROP = {("VR Auckland City", "Official site")}

CURRENCY = "NZD"


# ---------------------------------------------------------------- 订房链接
# 跟 hotel_fast.build_url 同一套 protobuf/base64 构造。在这里重抄是为了
# 不把 playwright 拖进依赖(导入 hotel_fast 会连带 import playwright,
# 云端/无浏览器环境直接炸)。改动必须与 hotel_fast 保持一致。
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _day(d):
    return (bytes([0x08]) + _varint(d.year)
            + bytes([0x10]) + _varint(d.month)
            + bytes([0x18]) + _varint(d.day))


def build_url(hotel, checkin, nights=1):
    import base64
    from urllib.parse import quote
    a = checkin
    b = checkin + timedelta(days=nights)
    inner = bytes([0x0A, len(_day(a))]) + _day(a) + bytes([0x12, len(_day(b))]) + _day(b)
    lvl2 = bytes([0x12, len(inner)]) + inner
    lvl1 = bytes([0x12, len(lvl2)]) + lvl2
    msg = bytes([0x08, 0x00, 0x1A, len(lvl1)]) + lvl1 + bytes([0x32, 0x02, 0x08, 0x00])
    c = CURRENCY.encode()
    msg += bytes([0x2A, len(c) + 4, 0x0A, len(c) + 2, 0x3A, len(c)]) + c
    ts = base64.urlsafe_b64encode(msg).decode().rstrip("=")
    return f"https://www.google.com/travel/search?q={quote(hotel)}&ts={ts}"


# ---------------------------------------------------------------- 零售
def retail_source():
    """优先实时文件, 没有就退回最新快照。-> (路径, 是不是快照)"""
    if LIVE_CSV.exists():
        return LIVE_CSV, False
    snaps = sorted(SNAP_DIR.glob("hotel_prices_v3_*.csv"))
    return (snaps[-1], True) if snaps else (None, False)


def retail_cells(path):
    """(酒店, 入住日) -> 最后一轮完整抓取里的跨渠道最低价。

    按"整轮"取而不是各渠道各取自己的最新值 —— 后者会把早就消失的渠道
    当成当前报价, 这个错误在 9/22 VR Auckland City 上出过一次。
    """
    import collections
    with open(path, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r["hotel"] in HOTELS]
    g = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        g[(r["hotel"], r["checkin"])][r["timestamp"]].append(r)
    out = {}
    for key, by in g.items():
        ts = max(by)
        cand = []
        for r in by[ts]:
            prov = (r.get("provider") or "").strip()
            if (key[0], prov) in DROP:
                continue
            try:
                cand.append((float(r["price_nzd"]), prov))
            except (TypeError, ValueError):
                continue
        if cand:
            p, prov = min(cand)
            out[key] = {"price": p, "prov": prov, "ts": ts}
    return out, (max(r["timestamp"] for r in rows) if rows else None)


def verify_in_archive(hotel, checkin, price):
    """这个金额在该 (酒店, 入住日) 的存档页面上以独占一行的形式出现过吗。

    没有存档 -> None(无法核), 不是 False。两者必须区分: 贵的日子本来
    就不存档(ARCHIVE_BELOW=100), 把"没法核"说成"核不过"会误导。
    """
    tgz = SNAP_DIR / "raw_archive.tgz"
    if not tgz.exists():
        return None
    pre = hotel[:12].replace(" ", "_")
    pat = re.compile(rf"^{re.escape(pre)}_{re.escape(checkin)}_\d{{4}}-\d{{6}}\.txt$")
    best = None
    with tarfile.open(tgz) as tf, tempfile.TemporaryDirectory() as tmp:
        members = [m for m in tf.getmembers() if pat.match(os.path.basename(m.name))]
        if not members:
            return None
        members.sort(key=lambda m: m.name)
        tf.extract(members[-1], tmp, set_attrs=False)
        text = (Path(tmp) / members[-1].name).read_text(encoding="utf-8", errors="replace")
    res = offer_parse.parse_offers(text, hotel)
    offers = {k: v for k, v in res["offers"].items() if (hotel, k) not in DROP}
    if not offers:
        return None
    best = min(offers.values())
    return abs(best - price) < 0.51


# ---------------------------------------------------------------- 云端
def cloud_floor():
    """(酒店, 入住日) -> (最新价, 整段历史最低, 最新轮时间)。USD 批发价。"""
    import collections
    rows = []
    for f in sorted(glob.glob(str(CLOUD_DIR / "*.csv"))):
        try:
            with open(f, encoding="utf-8-sig") as fh:
                rows.extend(r for r in csv.DictReader(fh) if r.get("hotel") in HOTELS)
        except OSError:
            continue
    per = collections.defaultdict(dict)
    for r in rows:
        try:
            p = float(r["price"])
        except (KeyError, TypeError, ValueError):
            continue
        k = (r["hotel"], r["checkin"])
        t = r["ts_utc"]
        per[k][t] = min(p, per[k].get(t, p))
    out = {}
    for k, s in per.items():
        ts = max(s)
        out[k] = {"now": s[ts], "floor": min(s.values()), "ts": ts, "n": len(s)}
    return out


# ---------------------------------------------------------------- 状态
def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"retail": {}, "cloud": {}}


def save_state(st):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="VR 两家低价提醒")
    ap.add_argument("--threshold", type=float, default=THRESHOLD_NZD,
                    help=f"零售 NZD 门槛, 默认 {THRESHOLD_NZD}")
    ap.add_argument("--no-state", action="store_true")
    args = ap.parse_args()

    st = load_state()
    hits = []

    # ======================= 零售 (NZD, 可下单) =======================
    print("═══ 零售价 (NZD, Google, 可下单) ═══")
    path, is_snap = retail_source()
    if path is None:
        print("  没有任何零售数据文件。")
    else:
        cells, last_ts = retail_cells(path)
        age = None
        if last_ts:
            t = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=NZ)
            age = (datetime.now(timezone.utc) - t).total_seconds() / 3600
        tag = "快照" if is_snap else "实时文件"
        print(f"  来源 {path.name} ({tag}), 最后一条读数 {last_ts} NZ"
              + (f", 距今 {age:.1f} 小时" if age is not None else ""))

        stale = age is not None and age > RETAIL_STALE_H
        if stale:
            print(f"  ** 数据已过期(>{RETAIL_STALE_H} 小时) —— 本节只列出当前值, ")
            print("     **不触发提醒**。拿几天前的价当成'现在跌破了'是假提醒里最骗人的一种。")

        under = []
        for (h, ci), c in sorted(cells.items()):
            d = date.fromisoformat(ci)
            if not (STAY_FROM <= d < STAY_TO):
                continue
            if c["price"] < args.threshold:
                under.append((h, ci, c))
        if not under:
            print(f"  住宿区间内没有低于 NZ${args.threshold:.0f} 的入住日。")
        for h, ci, c in under:
            ver = verify_in_archive(h, ci, c["price"])
            mark = {True: "存档核过", False: "**与存档不符**", None: "无存档可核"}[ver]
            key = f"{h}|{ci}"
            prev = st["retail"].get(key)
            fresh = prev is None or c["price"] < prev - 0.5
            flag = "新低" if fresh else "已报过"
            print(f"  NZ${c['price']:.0f}  {h}  {ci}  [{mark}] [{flag}]")
            print(f"      {build_url(h, date.fromisoformat(ci))}")
            if not stale and fresh and ver is not False:
                hits.append(f"零售 NZ${c['price']:.0f} · {h} · {ci}")
                if not args.no_state:
                    st["retail"][key] = c["price"]
        # 回到门槛之上就重新武装
        for key in list(st["retail"]):
            h, ci = key.split("|")
            cur = cells.get((h, ci))
            if cur and cur["price"] >= args.threshold:
                st["retail"].pop(key, None)

    # ================= 云端 (USD 批发, 不可下单) =================
    print()
    print("═══ 云端 DIDA (USD 批发价, **不可下单**, 只做早警) ═══")
    print(f"  这一节**不套用 NZ${args.threshold:.0f} 那个门槛** —— 币种和市场都不是一回事")
    print("  (实测批发比零售贵约 31%)。判据是'刷没刷新自己的历史最低'。")
    cf = cloud_floor()
    if not cf:
        print("  云端没有 VR 两家的数据。")
    else:
        newlow = []
        for (h, ci), c in sorted(cf.items()):
            key = f"{h}|{ci}"
            rec = st["cloud"].get(key)
            if rec is None:
                st["cloud"][key] = c["floor"]
            elif c["now"] < rec - 0.5:
                newlow.append((h, ci, c, rec))
                st["cloud"][key] = c["now"]
            elif c["floor"] < rec:
                st["cloud"][key] = c["floor"]
        print(f"  {len(cf)} 个 (酒店 x 入住日), 最新轮 {max(c['ts'] for c in cf.values())} UTC")
        for (h, ci), c in sorted(cf.items()):
            edge = " ← 就是历史最低" if c["now"] <= c["floor"] + 1e-9 else ""
            print(f"     {h:26s} {ci}  现 USD {c['now']:6.0f}   "
                  f"历史最低 {c['floor']:6.0f}  ({c['n']} 轮){edge}")
        for h, ci, c, rec in newlow:
            print(f"  ▼ 批发新低  {h} {ci}  USD {rec:.0f} → {c['now']:.0f}")
            hits.append(f"批发新低 USD {c['now']:.0f} · {h} · {ci}")

    if not args.no_state:
        save_state(st)

    print()
    if hits:
        print("ALERT  " + " | ".join(hits))
    else:
        print("QUIET  没有需要提醒的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
