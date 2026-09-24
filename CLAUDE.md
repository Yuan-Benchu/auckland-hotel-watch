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
- **渠道名靠白名单是错的，已换成结构判断。** 旧 `hotel_fast.parse_offers`
  用写死的 PROVIDERS 列表认渠道，名单外的（HotelsCombined / nz.KAYAK.com /
  klook / EaseMyTrip / Traveloka / Amimir…）那一行既不算渠道也不算房型，
  `cur_prov` 保持不变，它的价格就记到了**上一个**渠道头上。
  9/30 Ascotia 记的是 "Expedia $64"，页面上 Expedia 其实 $76，$64 是
  Hotelscombined 的。现在走 `local/offer_parse.py` 的排除法：不是价格、
  不是房型、不是房型细节、不是宣传语，剩下的就是渠道名。
  **2026-09-19 之前采的数据，渠道名一律不可信；价格可信。**

- **报价区起点不能匹配 `prices`。** 搜索结果页顶部的 "View prices" 就含这个
  词，用它当起点会把整个搜索结果列表（别家酒店）圈进本店报价区。
  Wotif 就是这么进来的：CSV 里 1857 行，而 5181 张归档页面里它真正出现在
  本店报价区的只有 **1 张**。起点只能锚在 `Featured options` / `All options`。

- **页面没显示的渠道不会更便宜。** 每页底部 "View more options from $X"
  说明还有渠道没渲染出来。5181 张存档里 **5181 张满足 X ≥ 页面已显示的
  最低价，0 例外** —— Google 按价格升序排。所以：渠道清单不完整，
  **最低价完整**。不必为了"抓全渠道"去点那个展开。

- **VR Auckland City 不是"数据存疑"**（2026-09-18 更正过一次错判）。
  它的 `Official site` 挂的是机场店、必须剔除，这条没变。但**不能**拿
  "剔过的最低价"去对"没剔的页面头条价" —— 795 张存档里 757 张的头条价就
  等于那条机场报价。剔掉之后最便宜的是 Super.com（741 张）。它的价格跟
  别家一样可信，真实限制只有一条：**这家没法用头条价做交叉校验**。

- **Copthorne 从来没被页面核对过。** `ARCHIVE_BELOW = 100` 只存"出现过
  NZ$100 以下报价"的页面，Copthorne 最低 NZ$103，所以一张存档都没有。
  它是最贵的一家，目前不影响结论，但别说它"核过了"。

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

## 核对与监测的两个工具

```
local/audit_offers.py     拿 raw_archive.tgz 逐张核对解析器: 最低价对不对、
                          渠道认全没有。新解析一致率 99.8%, 旧的 91.6%。
local/watch_status.py     巡检: 两套采集还活着吗、价格动了没有、结论要不要改。
local/channel_watch.py    渠道级监测: 哪个渠道在卖、贵多少、换没换人。
local/vr_alert.py         VR 两家: 零售跌破 NZ$80 提醒 + 云端批发新低早警。
```

`channel_watch.py` 只用 `raw_archive.tgz` 算渠道 —— **云端 DIDA 没有渠道字段**
(ts_utc/hotel/checkin/room/price/currency/cancelable/meal/n_plans 里没有),
渠道只能来自本地 Google 抓取, 所以它跟着本地采集一起依赖开机。CSV 的
`provider` 列覆盖全但名字不可信, 脚本只拿它统计"跟存档对不上的比例"。
状态文件 `.channel_state.json` 是全局的, 而 `--hotel` 可以只跑一部分 ——
比对时必须按本轮实际跑了哪些酒店/入住日限定范围, 否则"这轮没查"会被报成
"渠道消失"(这个假警报踩过)。

`vr_alert.py` 分两节, **各用各的判据, 永不相加**: 零售(NZD)跌破 NZ$80 才是
真提醒, 云端(USD 批发)只报"刷新了自身历史最低"。**NZ$80 这个门槛不能套到
USD 上** —— 币种和市场都不是一回事, 批发比零售贵约 31%, 套上去每一条都是假的。
零售快照过期(>36 小时)时只列当前值、**不触发提醒**: 拿几天前的价当成"现在
跌破了", 是假提醒里最骗人的一种。

判断"结论还成不成立"的逻辑（写在 `watch_status.py` 的模块说明里）：
有更新的零售快照 → 重跑 `export_stay_plan.py`；没有 → 看云端在两个数据集
重叠的入住日上动了多少。**动得少是"快照还能用"的弱证据，不是替代品** ——
批发和零售不是一回事，只是同一个市场。

## 当前状态（2026-09-19）

- 云端采集在跑，但调度不稳，偶有数小时空档
- 本地采集**需要手动装计划任务才能自愈**：
  `schtasks /Create /TN "AucklandHotelWatch" /TR "wscript.exe <仓库>\local\watchdog.vbs" /SC MINUTE /MO 10 /F`
  没装的时候崩了就一直崩着（已经发生过：9/18 17:42 崩，10 小时无人发现）
- 可视化发布在 Claude Artifact 上，数据来自 `export_viz.py` + `export_cloud.py`
- 连住看板（9/20 入住、10/9 退房，19 晚）：`local/export_stay_plan.py`
  结论是全程住 Ascotia Off Queen NZ$1549；理论下限 NZ$1427，
  **换店最多只省 NZ$122（7.9%），不值得搬**
- 本地零售名单 2026-09-18 补上 `Edit Auckland Central`：云端盯 11 家而本地
  只有 9 家，差的两家里 Abstract Hotel 确认 Google 不卖，Edit 只是从来没加
  过。它的 DIDA 批发价跟 Ascotia 在同一段。**位置与卫浴均未核实**
