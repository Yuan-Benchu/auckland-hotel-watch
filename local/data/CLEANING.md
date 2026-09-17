# 数据清洗记录

生成于 2026-09-18 04:17:16

## 结果

- 可信数据集 `hotel_prices_curated.csv` — **6461 行**
- 时间跨度 2026-09-16 02:28:36 → 2026-09-18 04:16:13
- 覆盖酒店 9 家: Albion Hotel Auckland、Ascotia Off Queen Auckland、Auckland City Hotel、Copthorne Hotel Auckland City、Shakespeare Hotel Auckland、The Quadrant Hotel & Suites Auckland、VR Auckland City、VR Queen Street Auckland、ibis budget Auckland Central
- 去重丢弃 0 行

按来路:

- `v3` — 6323 行
- `已验证·v2` — 128 行
- `已验证·初代` — 5 行
- `已验证·fast` — 5 行

## 各来源处置

| 来源 | 原始 | 采用 | 未采用 | 处置方式 |
|---|---:|---:|---:|---|
| `hotel_prices_v3.csv` | 6781 | 6323 | 458 | 整体采用(可信源) |
| `hotel_prices_v2.csv` | 5996 | 128 | 5868 | 该格在文件内部有跳动 3516 行; v3 无对照读数 2014 行; 通过验证 |
| `hotel_prices_fast.csv` | 457 | 5 | 452 | v3 无对照读数 342 行; 与新解析器读数不符 72 行; 通过验证但属已知 |
| `hotel_prices.csv` | 506 | 5 | 501 | v3 无对照读数 399 行; 与新解析器读数不符 97 行; 通过验证但属已知 |

## 逐源说明

### `hotel_prices_v3.csv`

已知缺陷: 解析器只接受'整行就是一个金额'的价格行, 已在 6 个页面上人工比对通过, 0 条不一致。

采用 6323 行, 未采用 458 行。未采用的原因分布: 整体采用(可信源)

### `hotel_prices_v2.csv`

已知缺陷: 状态机解析器但报价区未正确收尾, 会把页面底部的促销文案 'Save $33 if you stay Sep 20-21.' 当成房价, 制造了 112 条低于 NZ$60 的假低价。

采用 128 行, 未采用 5868 行。未采用的原因分布: 该格在文件内部有跳动 3516 行; v3 无对照读数 2014 行; 通过验证但属已知错误渠道 296 行; 与新解析器读数不符 42 行

### `hotel_prices_fast.csv`

已知缺陷: 初代解析器: 未截断 Similar hotels 区块, 会把隔壁酒店的报价算成目标酒店。

采用 5 行, 未采用 452 行。未采用的原因分布: v3 无对照读数 342 行; 与新解析器读数不符 72 行; 通过验证但属已知错误渠道 38 行

### `hotel_prices.csv`

已知缺陷: 与上同一代解析器, 同样的 Similar hotels 污染; 最高值 NZ$1518 明显失真。

采用 5 行, 未采用 501 行。未采用的原因分布: v3 无对照读数 399 行; 与新解析器读数不符 97 行; 通过验证但属已知错误渠道 5 行

## 交叉验证规则

待检源的每个 (酒店, 入住日, 渠道) 格子, 同时满足下面两条才采用:

1. 在该文件内部, 这一格的价格自始至终是同一个数(没有跳动)
2. 这个数等于 v3 第一轮(20 分钟内)对同一格的读数

两条缺一不可。只看第 1 条会放过'系统性地一直错'的情况(机场假价就是恒定的);
只看第 2 条会放过碰巧撞上的单点。

## 逐行剔除规则

**VR Auckland City × Official site**

Google 把 VR Auckland Airport (bookings.vrhotels.co.nz hotelID=116052) 的订房引擎 挂在了 VR Auckland City 页面的 Official site 位上。2026-09-17 跟随 lodging/clk 链接 验证过 09-22 和 10-06 两个入住日, 均落到 VR Auckland Airport 的 Property Details。市区店在 188 Hobson Street, 机场店在 Mangere —— 这个价不属于本监测对象。(对照: VR Queen Street 的 Official site 落到 hotelID=110754, 正确, 不受影响。)

## 没有做的事

- 没有对任何价格做估算、插值或系数修正。
- 没有把未通过验证的数据以任何形式混入统计。原始文件保留在原处。
