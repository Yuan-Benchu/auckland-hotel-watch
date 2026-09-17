# -*- coding: utf-8 -*-
"""把历史观测清洗成一份可信数据集, 并留下可复核的处置记录。

分两类来源:

  可信源 (v3)   —— 解析器已改为只接受"整行就是一个金额"的价格行, 并在 6 个页面上
                   人工比对通过。整体采用, 只按 DROP_ROWS 逐行剔除。

  待检源 (更早) —— 解析器有已知缺陷, 但不等于整批不可信。逐格做交叉验证:
                   同一个 (酒店, 入住日, 渠道) 若满足两个条件, 就认为旧解析器
                   在这一格上没出错, 该格的所有行予以采用:

                     1. 在该文件内部, 这一格的价格自始至终是同一个数(没有跳动)
                     2. 这个数等于 v3 第一轮对同一格的读数

                   两个条件缺一不可。只看条件 1 会放过"系统性地一直错"的情况
                   (机场假价就是恒定的); 只看条件 2 会放过碰巧撞上的单点。

不做任何估算、插值或系数修正。不满足上述条件的行一律不进 curated 集。

产出:
  hotel_prices_curated.csv  —— 可信数据集, 带 source 列标明每行的来路
  CLEANING.md               —— 处置记录
"""
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent
OUT_CSV = BASE / "hotel_prices_curated.csv"
OUT_MD = BASE / "CLEANING.md"
FIELDS = ["timestamp", "hotel", "checkin", "provider", "price_nzd", "room_type", "source"]

TRUSTED = ("hotel_prices_v3.csv",
           "解析器只接受'整行就是一个金额'的价格行, 已在 6 个页面上人工比对通过, 0 条不一致。")

CANDIDATES = [
    ("hotel_prices_v2.csv",
     "状态机解析器但报价区未正确收尾, 会把页面底部的促销文案 "
     "'Save $33 if you stay Sep 20-21.' 当成房价, 制造了 112 条低于 NZ$60 的假低价。"),
    ("hotel_prices_fast.csv",
     "初代解析器: 未截断 Similar hotels 区块, 会把隔壁酒店的报价算成目标酒店。"),
    ("hotel_prices.csv",
     "与上同一代解析器, 同样的 Similar hotels 污染; 最高值 NZ$1518 明显失真。"),
]

# 交叉验证之后仍需逐行剔除的 (酒店, 渠道) —— 这是数据源本身的错, 不是解析器的错,
# 所以两代解析器会"一致地错", 交叉验证拦不住它, 必须单独列出来。
DROP_ROWS = {
    ("VR Auckland City", "Official site"):
        "Google 把 VR Auckland Airport (bookings.vrhotels.co.nz hotelID=116052) 的订房引擎 "
        "挂在了 VR Auckland City 页面的 Official site 位上。2026-09-17 跟随 lodging/clk 链接 "
        "验证过 09-22 和 10-06 两个入住日, 均落到 VR Auckland Airport 的 Property Details。"
        "市区店在 188 Hobson Street, 机场店在 Mangere —— 这个价不属于本监测对象。"
        "(对照: VR Queen Street 的 Official site 落到 hotelID=110754, 正确, 不受影响。)",
}

BASELINE_WINDOW_S = 1200   # v3 第一轮的时长, 用它的读数当交叉验证基准

SRC_LABEL = {
    "hotel_prices_v2.csv": "v2",
    "hotel_prices_fast.csv": "fast",
    "hotel_prices.csv": "初代",
}


def load(path):
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                r["price_nzd"] = int(r["price_nzd"])
                r["_t"] = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError, TypeError):
                continue
            r.setdefault("room_type", "")
            rows.append(r)
    return rows


def key(r):
    return (r["hotel"], r["checkin"], r["provider"])


def check_rules_agree():
    """DROP_ROWS 必须和采集端的 hotel_fast.PROVIDER_BLOCK 一致。

    两处各自维护同一条规则, 早晚会分叉: 采集端不再拦截而清洗端还在丢(或反之),
    结果就是有一批行既没被拦也没被洗掉。这里直接拦住这种情况。
    """
    sys.path.insert(0, str(HERE))
    import hotel_fast as hf
    live = {(h, p) for h, ps in getattr(hf, "PROVIDER_BLOCK", {}).items() for p in ps}
    if live != set(DROP_ROWS):
        raise SystemExit(
            "清洗规则和采集端拦截规则不一致, 已中止。\n"
            f"  hotel_fast.PROVIDER_BLOCK: {sorted(live)}\n"
            f"  curate.DROP_ROWS        : {sorted(DROP_ROWS)}\n"
            "改了一处就要同步另一处。")


def main():
    check_rules_agree()
    kept, report = [], []

    # ---- 可信源 ----
    v3 = load(BASE / TRUSTED[0])
    if not v3:
        raise SystemExit(f"找不到可信源 {TRUSTED[0]}, 无法建立交叉验证基准。")
    dropped = sum(1 for r in v3 if key(r)[::2] in {(h, p) for h, p in DROP_ROWS})
    for r in v3:
        if (r["hotel"], r["provider"]) in DROP_ROWS:
            continue
        r["source"] = "v3"
        kept.append(r)
    report.append([TRUSTED[0], len(v3), len(v3) - dropped, dropped, TRUSTED[1],
                   "整体采用(可信源)"])

    # ---- 交叉验证基准: v3 第一轮每格的最早读数 ----
    t0 = min(r["_t"] for r in v3)
    baseline = {}
    for r in sorted(v3, key=lambda r: r["_t"]):
        if (r["_t"] - t0).total_seconds() > BASELINE_WINDOW_S:
            continue
        baseline.setdefault(key(r), r["price_nzd"])

    # ---- 待检源: 逐格交叉验证 ----
    for name, flaw in CANDIDATES:
        rows = load(BASE / name)
        if not rows:
            report.append([name, 0, 0, 0, flaw, "文件不存在或为空"])
            continue
        cells = defaultdict(list)
        for r in rows:
            cells[key(r)].append(r)
        ok_rows = 0
        reasons = defaultdict(int)
        for k, rs in cells.items():
            prices = {r["price_nzd"] for r in rs}
            base = baseline.get(k)
            if base is None:
                reasons["v3 无对照读数"] += len(rs); continue
            if len(prices) > 1:
                reasons["该格在文件内部有跳动"] += len(rs); continue
            if prices != {base}:
                reasons["与新解析器读数不符"] += len(rs); continue
            if (k[0], k[2]) in DROP_ROWS:
                reasons["通过验证但属已知错误渠道"] += len(rs); continue
            for r in rs:
                r["source"] = "已验证·" + SRC_LABEL[name]
                kept.append(r)
            ok_rows += len(rs)
        detail = "; ".join(f"{k} {v} 行" for k, v in
                           sorted(reasons.items(), key=lambda kv: -kv[1]))
        report.append([name, len(rows), ok_rows, len(rows) - ok_rows, flaw, detail])

    # ---- 去重并落盘 ----
    seen, final, dup = set(), [], 0
    for r in sorted(kept, key=lambda r: (r["timestamp"], r["hotel"], r["checkin"], r["provider"])):
        u = (r["timestamp"], r["hotel"], r["checkin"], r["provider"])
        if u in seen:
            dup += 1; continue
        seen.add(u); final.append(r)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(final)

    # ---- 处置记录 ----
    hotels = sorted({r["hotel"] for r in final})
    by_src = defaultdict(int)
    for r in final:
        by_src[r["source"]] += 1
    span = (final[0]["timestamp"], final[-1]["timestamp"]) if final else ("", "")
    L = ["# 数据清洗记录", "", f"生成于 {datetime.now():%Y-%m-%d %H:%M:%S}", "",
         "## 结果", "",
         f"- 可信数据集 `{OUT_CSV.name}` — **{len(final)} 行**",
         f"- 时间跨度 {span[0]} → {span[1]}",
         f"- 覆盖酒店 {len(hotels)} 家: " + "、".join(hotels),
         f"- 去重丢弃 {dup} 行", "",
         "按来路:", ""]
    for s, c in sorted(by_src.items(), key=lambda kv: -kv[1]):
        L.append(f"- `{s}` — {c} 行")
    L += ["", "## 各来源处置", "",
          "| 来源 | 原始 | 采用 | 未采用 | 处置方式 |", "|---|---:|---:|---:|---|"]
    for name, total, ok, no, _, how in report:
        L.append(f"| `{name}` | {total} | {ok} | {no} | {how[:40]} |")
    L += ["", "## 逐源说明", ""]
    for name, total, ok, no, flaw, how in report:
        L += [f"### `{name}`", "", f"已知缺陷: {flaw}", "",
              f"采用 {ok} 行, 未采用 {no} 行。未采用的原因分布: {how}", ""]
    L += ["## 交叉验证规则", "",
          "待检源的每个 (酒店, 入住日, 渠道) 格子, 同时满足下面两条才采用:", "",
          "1. 在该文件内部, 这一格的价格自始至终是同一个数(没有跳动)",
          f"2. 这个数等于 v3 第一轮({BASELINE_WINDOW_S // 60} 分钟内)对同一格的读数", "",
          "两条缺一不可。只看第 1 条会放过'系统性地一直错'的情况(机场假价就是恒定的);",
          "只看第 2 条会放过碰巧撞上的单点。", "",
          "## 逐行剔除规则", ""]
    for (hotel, prov), why in DROP_ROWS.items():
        L += [f"**{hotel} × {prov}**", "", why, ""]
    L += ["## 没有做的事", "",
          "- 没有对任何价格做估算、插值或系数修正。",
          "- 没有把未通过验证的数据以任何形式混入统计。原始文件保留在原处。", ""]
    OUT_MD.write_text("\n".join(L), encoding="utf-8")

    print(f"{OUT_CSV.name}: {len(final)} 行 ({span[0]} → {span[1]})")
    for s, c in sorted(by_src.items(), key=lambda kv: -kv[1]):
        print(f"   {s:<16}{c:>6} 行")
    print()
    for name, total, ok, no, _, _ in report:
        print(f"   {name:<26}{total:>6} 行 -> 采用 {ok:>5}, 未采用 {no:>5}")
    print(f"\n处置记录: {OUT_MD.name}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
