# -*- coding: utf-8 -*-
"""Google Travel 报价区解析 —— 渠道名不再靠白名单。

## 为什么要换掉白名单

原来的 `hotel_fast.parse_offers` 用一份写死的 PROVIDERS 列表认渠道。
页面上出现列表之外的渠道时(Hotelscombined.co.nz / 6E Hotels / Amimir.com …),
那一行既不被认成渠道、也不被认成房型, 于是 `cur_prov` **保持不变**, 紧跟着的
价格就被记到了**上一个**渠道头上。后果有两个:

1. 这些渠道从数据里彻底消失;
2. 真实渠道的价格被冲掉 —— 2026-09-30 Ascotia 记的是 "Expedia $64",
   页面上 Expedia 其实是 $76, $64 是 Hotelscombined 的。

价格本身没错(整格最低价仍等于 Google 自己印的头条价), 错的是"这个价是谁家的"。
要按渠道下单就会订错地方。

## 换成什么

结构判断。报价区的形状是固定的:

    <渠道>
    [宣传语]            <- 可有可无, 如 "Book Direct" / "Free Wi-Fi"
    <价格>
    Visit site
    [<房型> / <房型细节> / <价格> / Visit site] * N   <- 同一渠道的其它房型

所以规则是: **价格归属于它前面最近的那个"渠道行"**, 而"渠道行"用排除法认 ——
不是价格、不是房型、不是房型细节、不是宣传语, 剩下的就是渠道名。同一个价格
之前出现多个候选时后者胜(页面上是 "Albion Hotel" / "Official Site" / 价格,
该算 Official Site)。

排除法比白名单可靠, 因为房型词和宣传语是有限且稳定的, 而渠道名是开放集合。

## 一个查过的边界: "View more options from $80"

页面底部这一行说明还有没渲染出来的渠道。抓不到它们, 但**不影响最低价** ——
Google 按价格升序排, 这个 "from" 值实测总是 >= 页面上已显示的最低价。
所以: 渠道清单不完整, 最低价完整。见 `audit_offers.py` 的核对。
"""
import re

# 整行就是一个金额才算价格。嵌在句子里的("Save $33 if you stay Sep 20 - 21.")
# 是促销文案, 曾经造成过一个根本不存在的 NZ$32。
PRICE_LINE_RE = re.compile(r"^(?:NZ)?\$\s*([\d,]+)(?:\.\d+)?$")

# 报价区的起点。**必须锚在 "Featured options"/"All options" 上**:
# 页面顶部的导航条里有一个 "Prices" 标签, 用它当起点会把酒店头部(评分、
# 评价数、地址、头条价)也圈进报价区 —— 于是 "(896)" 这种评价数会被当成
# 渠道名, 头条价记到它名下。只有两个锚都找不到时才退回 "Prices"。
SECTION_START = re.compile(r"featured options|all options|比价|报价", re.I)
SECTION_START_FALLBACK = re.compile(r"^prices$", re.I)

HARD_STOP = re.compile(
    r"^track this hotel|^get emails|price range|prices are currently"
    r"|top things to know|similar hotels|nearby places|^show price",
    re.I)

# 还有多少渠道没渲染出来: "View more options from $80"
MORE_RE = re.compile(r"view more options from\s*(?:NZ)?\$\s*([\d,]+)", re.I)

# ---- 排除法用的三类行 ------------------------------------------------

# 房型
ROOM_RE = re.compile(
    r"\b(room|suite|apartment|studio|bed|king|queen|twin|double|single|deluxe"
    r"|superior|standard|executive|penthouse|villa|dorm|bunk|ensuite)\b"
    r"|房|套房|床", re.I)

# 房型细节: 以数字/间隔点开头, 或含 guests / 取消政策 / 早餐 / 阳台
DETAIL_RE = re.compile(
    r"^\s*[\d·•]|guests?\b|free cancellation|breakfast|balcony|non-?refundable"
    r"|pay at|includes?\b|·", re.I)

# 宣传语 / 按钮 / 结构行
CHROME_RE = re.compile(
    r"^visit\b|^book now|^get (price|a lower price)|^see (price|deal)"
    r"|^view\b|^sponsored|^featured options|^all options|^prices$|^more options"
    r"|free wi-?fi|book direct|member (discount|price|rate)|save (with|on|time)"
    r"|easy booking|instant confirmation|read real guest reviews"
    r"|earn rewards|free (membership|enrolment)|price match|no booking fee"
    r"|^official$|^deal\b|^\W*$"
    # 评价数 "(896)" / "(1.4K)"、星级、纯数字行 —— 页面头部的东西, 不是渠道
    r"|^\(\s*[\d.,]+\s*[KkMm]?\s*\)$|^[\d.]+$|star hotel|^\d+\s*(★|stars?)",
    re.I)


def _is_vendor(line, hotel_name=None):
    """排除法: 不是价格/房型/细节/宣传语, 就当渠道名。"""
    if not line or "$" in line:
        return False
    if PRICE_LINE_RE.match(line):
        return False
    if CHROME_RE.search(line):
        return False
    if DETAIL_RE.search(line):
        return False
    if ROOM_RE.search(line):
        return False
    # 渠道名都很短且不带逗号。带逗号的是地址行("5 Scotia Place, Auckland CBD…")
    # 或民宿标题("Magnificent SkyTower, Sea, Habour View in CBD")。
    if len(line) > 40 or "," in line:
        return False
    if hotel_name and line.lower() == hotel_name.lower():
        return False
    return True


def scope_to_offers(lines):
    """截出目标酒店自己的报价区。

    "Similar hotels" 之后是别家酒店的报价, 格式几乎一样, 不截断会把隔壁的
    便宜价算成目标酒店的 —— 这是初代解析器的老毛病。
    """
    start = None
    for i, ln in enumerate(lines):
        if SECTION_START.search(ln):
            start = i
            break
    if start is None:
        for i, ln in enumerate(lines):
            if SECTION_START_FALLBACK.match(ln):
                start = i
                break
    if start is None:
        start = 0
    out = []
    for ln in lines[start:]:
        if HARD_STOP.search(ln):
            break
        out.append(ln)
    return out


def parse_offers(text, hotel_name=None):
    """-> {"offers": {渠道: 最低价}, "rooms": {渠道: 房型}, "more_from": int|None}

    `more_from` 是 "View more options from $X" 里的 X: 还有没显示出来的渠道,
    但它们都不便宜于 X。None 表示页面没有这一行(渠道已全部显示)。
    """
    raw = [ln.strip() for ln in text.splitlines() if ln.strip()]
    more_from = None
    for ln in raw:
        m = MORE_RE.search(ln)
        if m:
            try:
                more_from = int(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    lines = scope_to_offers(raw)
    offers, rooms = {}, {}
    cur_prov, cur_room = None, ""

    for line in lines:
        m = PRICE_LINE_RE.match(line)
        if m:
            if cur_prov:
                try:
                    price = int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if 20 <= price <= 3000:
                    if cur_prov not in offers or price < offers[cur_prov]:
                        offers[cur_prov] = price
                        rooms[cur_prov] = cur_room
            continue

        if _is_vendor(line, hotel_name):
            # 后者胜: 页面上是 "Albion Hotel" / "Official Site" / 价格,
            # 这个价该算 Official Site。
            cur_prov, cur_room = _canon(line), ""
        elif ROOM_RE.search(line) and not DETAIL_RE.search(line):
            cur_room = line[:60]

    return {"offers": offers, "rooms": rooms, "more_from": more_from}


# 同一个渠道页面上有几种写法, 归一到一个名字, 否则统计会把它们算成两家。
_CANON = [
    (re.compile(r"^official\s*site$", re.I), "Official site"),
    (re.compile(r"^官方网站$"), "Official site"),
    (re.compile(r"^expedia", re.I), "Expedia"),
    (re.compile(r"^hotels\.com", re.I), "Hotels.com"),
    (re.compile(r"^booking\.com", re.I), "Booking.com"),
    (re.compile(r"^agoda", re.I), "Agoda"),
    (re.compile(r"^trip\.com", re.I), "Trip.com"),
    (re.compile(r"^wotif", re.I), "Wotif"),
    (re.compile(r"^super\.com", re.I), "Super.com"),
    (re.compile(r"^priceline", re.I), "Priceline"),
    (re.compile(r"^hotelscombined", re.I), "HotelsCombined"),
    (re.compile(r"^trivago", re.I), "trivago"),
    (re.compile(r"^vio\.com", re.I), "Vio.com"),
    (re.compile(r"^bluepillow", re.I), "Bluepillow"),
]


def _canon(name):
    n = name.strip().strip("·•-–—").strip()
    for rx, out in _CANON:
        if rx.match(n):
            return out
    return n
