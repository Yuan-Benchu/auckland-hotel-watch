# auckland-hotel-watch

Yuan 的个人项目：奥克兰 CBD 酒店比价监测。

目标是给一次 2026 年 9–10 月的长住找最省的订法。硬约束：住处必须在 CBD，
这个约束到 **10 月 16 日**为止（之后不必每天进城）。

## 路径约定

所有脚本使用相对自身位置的路径，**不得写死绝对路径**：

```python
HERE     = Path(__file__).resolve().parent     # local/
BASE_DIR = HERE / "data"                        # local/data/
```

写死绝对路径会让整个目录搬不动 —— 这堆脚本原先散在 `Downloads` 里就是这样。
新增脚本一律遵循这个约定。PowerShell 里不要在 `param()` 的默认值里用
`$PSScriptRoot`（那里它是空的，会静默回退到当前工作目录），用脚本作用域的
`$MyInvocation.MyCommand.Path`。

`.ps1` 一律存成 **UTF-8 with BOM** —— Windows PowerShell 5.1 把无 BOM 的
UTF-8 当 ANSI 读，中文全乱且直接语法报错。

## 结构

```
collect.py                      云端采集器 (DIDA 官方 API)
.github/workflows/collect.yml   调度; DIDA_KEY 来自仓库 secret, 不入库
data/<UTC时间戳>.csv             云端每轮一个文件 (单文件追加会导致 git 冲突)

local/                          本地采集 (抓 Google), 依赖开机
  hotel_fast.py                 抓取与解析
  overnight_runner.py           调度, 每 30 分钟一轮, 最多连跑 72 小时
  watchdog.ps1 / watchdog.vbs   看门狗: 采集掉了自动拉起来
  check_alerts.py               低价提醒 (带页面原文复核)
  curate.py                     数据清洗, 产出 hotel_prices_curated.csv
  export_timeline.py            看板数据导出 + 模板注入
  build_dashboard.sh            重建两个看板
  data/                         活动数据、日志、页面存档 (大部分被 gitignore)
  data/snapshots/               上面那些的快照, 用于交接与复现, 见其 README
  dashboards/                   看板模板与产物
```

## 两套采集的分工

| | 本地 (Google) | 云端 (DIDA) |
|---|---|---|
| 价格性质 | **零售价，可据此下单** | 批发价，比零售贵约 31%，**不可用于比价决策** |
| 币种 | NZD | **USD**（`localeParam.currency` 失效，请求 NZD 也返回 USD） |
| 用途 | 决定订哪家、哪一晚 | 只用于观察价格随时间的变动 |
| 依赖开机 | 是 | 否 |
| 价格会不会动 | 每天都在动，跨天可差 NZ$50 | 实测 16 轮里 4 个入住日有 3 个**一分钱没动** |

**不要把两者的价格混在同一个统计里、同一张图里。** 混了之后得出的任何
结论都是假的。DIDA 的价格一律记 USD 原值 + 货币字段，不做换算——换算留到
分析时做，免得把汇率假设烧进数据。

## 已经踩实的事实（不要重新试）

- **VR Auckland City 的"官网"报价是假的。** 它路由到
  `bookings.vrhotels.co.nz/116052`，那是 **VR Auckland Airport**，不是市区店。
  任何统计前先剔除 `("VR Auckland City", "Official site")`。
- **Google Travel 静默地把住宿上限截到 30 晚。** 查更长的区间时页面会渲染成
  另一个日期范围而不报错。要验证就读页面上 check-in/check-out 两个 `<input>`
  的实际值。
- **DIDA 不便宜。** 同一批酒店实测 NZ$1,096 vs Google 的 NZ$835，贵约 31%。
  已测过，不必再测。
- **"几点订更便宜"目前还答不了。** 小时与日期是**混淆**的：覆盖最密的那天
  (9/18, 13 小时) 三条线里两条极差只有 NZ$1 和 NZ$3，但跨天能差 NZ$50。
  前两天各只有 3 小时覆盖且中间断档，无法区分"凌晨更便宜"和"那天整体降价"。
  在拿到连续几天的完整小时覆盖之前，**不要下这个结论**。

## 铁律：报价之前先核对页面

**没有对着源页面核过的数字，不往外写。** 曾经把促销语
`Save $33 if you stay Sep 20 – 21.` 当成房价，报出过一个根本不存在的
NZ$32。修法是只认独占一行的价格（`PRICE_LINE_RE` + `HARD_STOP`）。

离群值在被证明之前一律当作解析 bug。判断办法是看**覆盖度**：如果某晚只有
一两家有报价而最低价异常，那是漏抓；如果 9 家**一起**翻倍，那才是真的
（10 月 9–10 日就是这种情况，已核实，是活动周末）。

**任何时候报价格，都要附上预订链接**（`hotel_fast.build_url(hotel, date)`
能确定性地重建 Google Travel 的 URL）。

## GitHub Actions 的坑

官方文档明说 `schedule` 在高负载时会被延迟、**负载够高时排队任务会被直接
丢弃**，并建议避开整点。实测 `cron: "5,35"`：6 小时 43 分内本该触发 13 次，
实际只到 2 次，另外 11 次无记录无报错。

所以采集节奏由**已经跑起来的那台机器**决定，不由调度器决定：一次触发 =
一个 5h45m 的循环，内部对齐到 `:07/:22/:37/:52` 各采一轮并分别提交。
但这**没有消除**丢触发——循环结束后仍需要至少一次触发把下一轮拉起来，
实测出现过 57 分钟和 2 小时 20 分的空档。

提交数据用 `git add data/*.csv`，**不要 `git add data/`**——后者会把任何
掉进那个目录的东西一起提交（看门狗日志曾因路径 bug 掉进去过）。

## 当前状态（2026-09-19）

- 云端采集在跑，但调度不稳，偶有数小时空档
- 本地采集**需要手动装计划任务才能自愈**：
  `schtasks /Create /TN "AucklandHotelWatch" /TR "wscript.exe <仓库>\local\watchdog.vbs" /SC MINUTE /MO 10 /F`
  没装的时候崩了就一直崩着（已经发生过：9/18 17:42 崩，10 小时无人发现）
- 可视化发布在 Claude Artifact 上，数据来自 `export_viz.py` + `export_cloud.py`
