# auckland-hotel-watch

Yuan 的个人项目：奥克兰酒店比价监测。

## 路径约定

所有脚本使用相对自身位置的路径，**不得写死绝对路径**：

```python
HERE     = Path(__file__).resolve().parent     # local/
BASE_DIR = HERE / "data"                        # local/data/
```

写死绝对路径会让整个目录搬不动 —— 这堆脚本原先散在 `Downloads` 里就是这样。
新增脚本一律遵循这个约定。

## 结构

```
collect.py                  云端采集器 (DIDA 官方 API), 由 GitHub Actions 每 30 分钟跑
.github/workflows/collect.yml   调度; DIDA_KEY 来自仓库 secret, 不入库
data/<UTC时间戳>.csv         云端每轮一个文件 (单文件追加会导致 git 冲突)

local/                      本地采集 (抓 Google), 依赖开机
  hotel_fast.py             抓取与解析
  overnight_runner.py       调度, 每 30 分钟一轮
  check_alerts.py           低价提醒 (带页面原文复核)
  curate.py                 数据清洗, 产出 hotel_prices_curated.csv
  export_timeline.py        看板数据导出 + 模板注入
  data/                     数据、日志、页面存档
  dashboards/               看板模板与产物
```

## 两套采集的分工

| | 本地 (Google) | 云端 (DIDA) |
|---|---|---|
| 价格性质 | **零售价，可据此下单** | 批发价，比零售贵约 31%，**不可用于比价决策** |
| 用途 | 决定订哪家、哪一晚 | 只用于观察价格随时间（尤其小时）的变动 |
| 依赖开机 | 是 | 否 |

不要把两者的价格混在同一个统计里。
