# 数据快照

活动中的采集文件被 `.gitignore` 排除了——采集进程每几秒就往里追加一次，
跟踪它会让工作区永远是脏的，`rebase` / `filter-branch` 全都过不去（实际
挡过好几次）。所以活文件留在本地，**这里放的是它们的快照**，用于交接和复现。

## 可信度

| 文件 | 能不能直接用 |
|---|---|
| `hotel_prices_v3_*.csv` | ✅ **主力数据集**。解析器修好之后采的，可直接用 |
| `../hotel_prices_curated.csv` | ✅ 交叉验证后保留的旧数据，已跟踪在上一层 |
| `legacy/*.csv` | ⚠️ **有解析 bug，不要直接用**。见 `../CLEANING.md` |
| `logs/*` | 运行记录，排查用 |
| `raw_archive.tgz` | 页面原文存档，复核价格用 |

### legacy/ 为什么不可信

早期解析器会把页面上任何形如 `$xx` 的数字当成房价，包括促销语里的
`Save $33 if you stay Sep 20 – 21.`——于是采到过一个根本不存在的
NZ$32。修复办法是只认独占一行的价格（`PRICE_LINE_RE` + `HARD_STOP`），
见 `local/hotel_fast.py`。

`hotel_prices_curated.csv` 是从这批旧数据里捞出来的可信子集，判据两条：
1. 在该文件内部，这一格的价格自始至终没跳动过
2. 这个数等于修复后第一轮（20 分钟内）对同一格的读数

两条同时满足才保留，最后从几千行里只留下 138 行。

## 列的含义

`hotel_prices_v3_*.csv`：

```
timestamp   读到这个价的时刻（新西兰时间），不是入住时刻
hotel       酒店名
checkin     入住日（住一晚，退房日 = 入住日 + 1）
provider    渠道（Super.com / Official site / Booking.com …）
price_nzd   该渠道该晚的价格，NZD，可直接下单
room_type   房型，常为空（Google 不总是给）
```

一个 (hotel, checkin) 格子会被反复采样，所以同一格有很多行、时间戳不同。
要"当前价"就取该格 `timestamp` 最大的那行。

## 已知必须做的剔除

```python
DROP = {("VR Auckland City", "Official site")}
```

这家的"官网"报价路由到 `bookings.vrhotels.co.nz/116052`，那是
**VR Auckland Airport**，不是市区店。价格因此系统性偏低。已核实过，
任何统计都要先剔掉这一对。

## raw_archive.tgz

5173 个页面存档，解开 74 MB。文件名格式
`<酒店>_<入住日>_<抓取时刻>.txt`。解析器读到异常价格时，用它可以回到
当时的页面原文核对，而不是靠推测。

```bash
tar -xzf raw_archive.tgz
```
