# -*- coding: utf-8 -*-
"""验证 DIDA/RollingGo 的 Hotel MCP 能不能替代抓 Google。

用法:
    set DIDA_KEY=mcp_xxxxxxxx          (或写进同目录的 dida_key.txt)
    python dida_probe.py

它只读不写, 不下单。回答三个问题:

  1. 奥克兰在不在库里 —— searchHotels 能否返回 CBD 的房源
  2. 我们盯的那 9 家在不在库里 —— 逐家 getHotelDetail
  3. 它的价格和我们从 Google 抓到的是不是一个量级
     (DIDA 给的是 B2B 批发价, 预期会更低; 差多少要看数字, 不能猜)
"""
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent
ENDPOINT = "https://mcp.rollinggo.ai/mcp"
CHECKIN = date(2026, 10, 6)          # 一个普通工作日, 和我们本地数据能对上
NIGHTS = 1

HOTELS = [
    "VR Queen Street Auckland", "VR Auckland City", "Shakespeare Hotel Auckland",
    "Auckland City Hotel", "ibis budget Auckland Central",
    "The Quadrant Hotel & Suites Auckland", "Copthorne Hotel Auckland City",
    "Albion Hotel Auckland", "Ascotia Off Queen Auckland",
]


def key():
    k = os.environ.get("DIDA_KEY", "").strip()
    if not k and (BASE / "dida_key.txt").exists():
        k = (BASE / "dida_key.txt").read_text(encoding="utf-8").strip()
    if not k:
        sys.exit("没有 API key。设环境变量 DIDA_KEY, 或把 key 写进 dida_key.txt")
    return k


_id = [0]


def rpc(method, params=None, sid=None):
    # JSON-RPC 的 notification 不带 id, 带了就变成普通请求, 服务端会 500
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
            sid_out = r.headers.get("Mcp-Session-Id")
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # notification 常见 202/无响应体; 其它错误照实抛出内容供诊断
        raw = e.read().decode("utf-8", "replace")
        if notify:
            return {}, sid
        return {"error": {"http": e.code, "body": raw[:400]}}, sid
    if notify and not raw.strip():
        return {}, sid_out or sid
    # streamable-http 可能是 SSE 格式, 也可能是纯 JSON
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line), sid_out
            except json.JSONDecodeError:
                continue
    return {"_raw": raw[:500]}, sid_out


def text_of(result):
    """把 MCP 的 content 数组压成可读文本。"""
    c = (result or {}).get("result", {}).get("content") or []
    out = []
    for item in c:
        if item.get("type") == "text":
            out.append(item.get("text", ""))
    return "\n".join(out)


def main():
    key()          # 没有 key 就立刻退出, 别先打一堆抬头再报错
    print(f"端点 {ENDPOINT}")
    print(f"入住 {CHECKIN}  {NIGHTS} 晚  货币 NZD\n")

    init, sid = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "hotel-watch-probe", "version": "1.0"}})
    if "error" in init:
        sys.exit(f"initialize 失败: {init['error']}")
    print(f"握手成功, 会话 {sid}")
    rpc("notifications/initialized", {}, sid)

    tools, _ = rpc("tools/list", {}, sid)
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    print(f"可用工具: {names}\n")

    print("=" * 70)
    print("一、奥克兰 CBD 有没有房源")
    print("=" * 70)
    r, _ = rpc("tools/call", {"name": "searchHotels", "arguments": {
        "originQuery": "hotels in Auckland CBD New Zealand",
        "place": "Auckland", "placeType": "City", "countryCode": "NZ", "size": 20,
        "checkInParam": {"adultCount": 1, "checkInDate": CHECKIN.isoformat(),
                         "stayNights": NIGHTS}}}, sid)
    print(text_of(r)[:2500] or json.dumps(r, ensure_ascii=False)[:1200])

    print("\n" + "=" * 70)
    print("二、我们盯的 9 家在不在库里")
    print("=" * 70)
    for h in HOTELS:
        r, _ = rpc("tools/call", {"name": "getHotelDetail", "arguments": {
            "name": h,
            "dateParam": {"checkInDate": CHECKIN.isoformat(),
                          "checkOutDate": (CHECKIN.replace(day=CHECKIN.day + NIGHTS)).isoformat()},
            "occupancyParam": {"adultCount": 1, "roomCount": 1},
            "localeParam": {"countryCode": "NZ", "currency": "NZD"}}}, sid)
        t = text_of(r)
        if "error" in r:
            print(f"  ✗ {h:<40} 报错 {str(r['error'])[:60]}")
        elif not t.strip():
            print(f"  ? {h:<40} 空响应")
        else:
            print(f"  ✓ {h:<40} {t[:150].replace(chr(10), ' | ')}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
