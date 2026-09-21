# -*- coding: utf-8 -*-
"""云端每小时采集器 —— 通过 DIDA 官方 API 取奥克兰酒店报价。

为什么是这套而不是抓 Google:
  - 抓 Google 违反其服务条款, 云端 agent 会拒绝执行
  - 本地抓取在用户关机时必然停摆, 已经因此丢了两整夜数据
  DIDA 是官方 API, 云端可以合法地每小时跑一次, 用户关机不受影响。

刻意的取舍:
  - 只取固定的 4 个入住日 x 11 家酒店 = 44 次调用, 一轮约 2.5 分钟。
    目的是攒"小时维度"的样本, 不是覆盖全部日期 —— 覆盖全日期由本地
    那套 Google 采集负责, 两边互补。
  - 价格一律记 USD 原值 + 货币字段。DIDA 的 localeParam.currency 是
    失效的(请求 NZD 也返回 USD), 所以不做任何换算, 换算留到分析时做,
    免得把汇率假设烧进数据里。

只读。不下单, 不调用任何 order 相关接口。
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 每轮写自己的文件, 不追加到同一个 CSV。
# 两轮同时跑时都往同一文件末尾追加, git rebase 必然冲突 —— 2026-09-17
# 实际撞过一次, 整轮数据白采。按轮次分文件从结构上杜绝这种冲突:
# 新文件之间永远能干净合并。分析时 glob data/*.csv 即可。
DATA_DIR = ROOT / "data"
ENDPOINT = "https://mcp.rollinggo.ai/mcp"

HOTELS = [
    "VR Queen Street Auckland", "VR Auckland City", "Shakespeare Hotel Auckland",
    "Auckland City Hotel", "ibis budget Auckland Central",
    "The Quadrant Hotel & Suites Auckland", "Copthorne Hotel Auckland City",
    "Albion Hotel Auckland", "Ascotia Off Queen Auckland",
    "Abstract Hotel",            # Google 不卖这家, 只有 DIDA 有
    "Edit Auckland Central",
]

# 固定采样日: 一个平日 / 一个周末 / 一个月中平日。
# 只取 2 个而不是更多, 是为了把单轮控制在 2 分钟内 ——
# 实测每次调用约 3.5 秒, 33 次要 116 秒, 加上 job 启动会超额。
# GitHub Actions 私有仓库每月 2000 分钟免费, 每小时一轮 = 720 轮/月,
# 单轮超过 2 分钟就会超额。小时维度需要的是"轮次多", 不是"每轮覆盖广",
# 全日期覆盖由本地那套 Google 采集负责。
# 两个周六 + 两个周二。取两组同星期几而不是各一个, 是为了让"小时规律"
# 这个结论不至于只建立在某一天上: 如果两个周二的小时曲线对不上,
# 那说明看到的是当天的偶然波动, 不是时段规律。
CHECKINS = ["2026-09-26", "2026-10-03",   # 周六 x2
            "2026-10-06", "2026-10-13"]   # 周二 x2

# 某几家额外多盯几天。上面那段"单轮 2 分钟"的预算是**旧模型**下写的
# (一次触发 = 一轮)。现在 workflow 是一次触发跑 5h45m 的长循环, 轮间
# sleep 到下一个 :07/:22/:37/:52 —— Actions 分钟数按墙钟计, 跟每轮调
# 几次 API 无关。真正的约束只剩一条: 单轮必须塞进 15 分钟的间隔。
# 现在 11 家 x 4 日 = 44 次 x ~3.5s ~= 154s, 加下面这几次也才 ~168s。
#
# 只给点名的酒店加, 不给所有家加: 全加就是 11 x 8 = 88 次 ~= 308s,
# 仍在 15 分钟内, 但数据量翻倍而多出来的日子只有一家用得上。
EXTRA_CHECKINS = {
    # Yuan 在盯这家的周一到周五那一段, 而本地 Google 采集依赖开机、
    # 已经停了几十小时。云端补上这几天, 至少"动没动"有人看着。
    # 注意这仍是 USD 批发价, 不能用来下单。
    "VR Auckland City": ["2026-09-21", "2026-09-22",
                         "2026-09-23", "2026-09-24"],
}

FIELDS = ["ts_utc", "hotel", "checkin", "room", "price", "currency",
          "cancelable", "meal", "n_plans"]


def key():
    k = os.environ.get("DIDA_KEY", "").strip()
    if not k and (ROOT / "key.txt").exists():
        k = (ROOT / "key.txt").read_text(encoding="utf-8").strip()
    if not k:
        sys.exit("缺少 DIDA_KEY (环境变量或 key.txt)")
    return k


_id = [0]


def rpc(method, params=None, sid=None):
    notify = method.startswith("notifications/")
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        _id[0] += 1
        body["id"] = _id[0]
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {key()}",
                 **({"Mcp-Session-Id": sid} if sid else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            sid_out = r.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as e:
        if notify:
            return {}, sid
        return {"error": {"http": e.code,
                          "body": e.read().decode("utf-8", "replace")[:200]}}, sid
    except Exception as e:
        return {"error": {"exc": type(e).__name__}}, sid
    if notify and not raw.strip():
        return {}, sid_out or sid
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line), sid_out
            except json.JSONDecodeError:
                continue
    return {"_raw": raw[:300]}, sid_out


def detail(name, ci, sid):
    co = (date.fromisoformat(ci) + timedelta(days=1)).isoformat()
    r, _ = rpc("tools/call", {"name": "getHotelDetail", "arguments": {
        "name": name,
        "dateParam": {"checkInDate": ci, "checkOutDate": co},
        "occupancyParam": {"adultCount": 1, "roomCount": 1},
        "localeParam": {"countryCode": "NZ", "currency": "NZD"}}}, sid)
    if "error" in r:
        return None, r["error"]
    content = (r.get("result") or {}).get("content") or []
    txt = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    try:
        return json.loads(txt), None
    except Exception:
        return None, {"parse": txt[:120]}


def main():
    key()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    init, sid = rpc("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "auckland-hotel-watch", "version": "1.0"}})
    if "error" in init:
        sys.exit(f"initialize 失败: {init['error']}")
    rpc("notifications/initialized", {}, sid)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M") + ".csv")
    rows, ok, bad = [], 0, 0
    for h in HOTELS:
        for ci in CHECKINS + EXTRA_CHECKINS.get(h, []):
            j, err = detail(h, ci, sid)
            plans = (j or {}).get("roomRatePlans") or []
            if not plans:
                bad += 1
                continue
            p = min(plans, key=lambda x: x.get("averagePrice", 10 ** 9))
            rows.append({
                "ts_utc": ts, "hotel": h, "checkin": ci,
                "room": (p.get("roomName") or "")[:60],
                "price": p.get("averagePrice"),
                "currency": p.get("currency") or "?",
                "cancelable": p.get("cancelable"),
                "meal": (p.get("mealTypeStr") or "")[:30],
                "n_plans": len(plans),
            })
            ok += 1
            time.sleep(0.25)

    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    files = sorted(DATA_DIR.glob("*.csv"))
    total = sum(sum(1 for _ in open(p, encoding="utf-8")) - 1 for p in files)
    print(f"{ts}  写入 {ok} 行 -> {out.name}, 无报价/失败 {bad}, "
          f"累计 {len(files)} 个文件 / {total} 行")
    if not rows:
        sys.exit("本轮一行都没拿到 —— 可能是 key 失效或接口变更")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
