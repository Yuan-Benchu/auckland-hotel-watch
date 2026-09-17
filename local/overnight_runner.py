"""
通宵无人值守 runner
-------------------
串起整条链, 起好之后不需要人盯:

  阶段 0  等现有的 hotel_price_watch.py 收工
          —— 判据是 "hotel_prices.csv 连续 QUIET_SECONDS 没有新写入",
             不是 "python 进程消失"。因为现在有一个卡死的 python 进程
             (CPU 几乎为 0) 可能永远不退出, 等它会死等。
  阶段 1  每 CYCLE_MINUTES 跑一轮全量(两家酒店 x 全区间, 按日期交错)
          —— 失败率过高自动把间隔翻倍(防止被 Google 限流), 恢复后自动调回
  阶段 2  每轮结束刷新 overnight_report.md, 随时可看

停止:  在 local/data/ 建一个空文件 STOP_OVERNIGHT  (或直接结束进程)
日志:  overnight.log
数据:  hotel_prices_fast.csv
报告:  overnight_report.md
"""

import csv
import os
import subprocess
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hotel_fast as hf          # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

BASE_DIR = Path(__file__).resolve().parent / "data"
HERE = Path(__file__).resolve().parent

OLD_CSV = BASE_DIR / "hotel_prices.csv"          # 只用来判断旧 sweep 有没有收工
NEW_CSV = BASE_DIR / "hotel_prices_v3.csv"       # 解析器修好后的干净数据
LOG_PATH = BASE_DIR / "overnight.log"
REPORT_PATH = BASE_DIR / "overnight_report.md"
STOP_FLAG = BASE_DIR / "STOP_OVERNIGHT"
BASH_EXE = r"F:\ITsoftware\Git\Git\bin\bash.exe"
CYCLE_DONE_FLAG = BASE_DIR / ".cycle_done"   # 每轮重建完写一次, 供 Claude 监听

QUIET_SECONDS = 120        # 原 sweep 停写这么久就认为它收工了
MAX_WAIT_MINUTES = 45      # 等它收工的上限, 超时就直接开跑
CYCLE_MINUTES = 30         # 每轮间隔(用户要求)
MAX_HOURS = 72             # 安全上限(3天), 正常靠 STOP_OVERNIGHT 停
BAD_RATIO = 0.30           # 一轮里失败+无报价 超过这个比例 视为被限流
MAX_CYCLE_MINUTES = 240    # 退避上限

# 无人值守时对"提速"取保守档: 价格数量连续 3 轮不变才走, 宁可慢一点也别抓漏
hf.SETTLE_STABLE_ROUNDS = 3
hf.REQUEST_GAP = 2
# 赞助块渲染晚, 用 hotel_fast 里的 SETTLE_MIN_MS 下限保证渠道抓全
hf.CSV_PATH = NEW_CSV


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        # 后台无窗口启动时 sys.stdout 可能是 None, print 会直接抛异常
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 阶段 0


def wait_for_old_sweep():
    """等 hotel_prices.csv 不再增长。卡死的 python 进程不影响判断。"""
    if not OLD_CSV.exists():
        log("没找到 hotel_prices.csv, 直接开跑")
        return
    deadline = time.monotonic() + MAX_WAIT_MINUTES * 60
    log(f"阶段 0: 等现有 sweep 收工 (CSV 静默 {QUIET_SECONDS}s 视为结束, "
        f"最多等 {MAX_WAIT_MINUTES} 分钟)")
    while time.monotonic() < deadline:
        quiet = time.time() - OLD_CSV.stat().st_mtime
        if quiet >= QUIET_SECONDS:
            log(f"CSV 已静默 {quiet:.0f}s, 认定原 sweep 收工")
            return
        time.sleep(20)
    log("等待超时, 不再等了, 直接开跑")


# ---------------------------------------------------------------- 阶段 1


def build_tasks():
    """按日期交错, 保证两家酒店覆盖度始终一致。"""
    dates = list(hf.date_range(hf.DATE_FROM, hf.DATE_TO))
    return [(d, h) for d in dates for h in hf.HOTELS]


def run_cycle(n, tasks):
    ok = bad = 0
    durations = []
    t_start = time.monotonic()
    log(f"===== 第 {n} 轮开始, 共 {len(tasks)} 个目标 =====")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=hf.HEADLESS)
        ctx = browser.new_context(locale="en-NZ",
                                  timezone_id="Pacific/Auckland",
                                  viewport={"width": 1400, "height": 1000})
        sc = hf.Scraper(ctx, block=True)
        page = ctx.new_page()
        try:
            for i, (d, hotel) in enumerate(tasks, 1):
                if STOP_FLAG.exists():
                    log("检测到 STOP_OVERNIGHT, 中止本轮")
                    break
                t0 = time.monotonic()
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    offers = sc.fetch(page, hotel, d)
                except Exception as e:
                    bad += 1
                    log(f"[{i}/{len(tasks)}] {d} {hotel}: 失败 {type(e).__name__}")
                    continue
                dt = time.monotonic() - t0
                durations.append(dt)
                if not offers:
                    bad += 1
                    log(f"[{i}/{len(tasks)}] {d} {hotel}: 无报价 ({dt:.1f}s)")
                    continue
                ok += 1
                hf.write_rows(stamp, hotel, d, offers)
                prov, (price, _room) = min(offers.items(),
                                           key=lambda kv: kv[1][0])
                wd = "一二三四五六日"[d.weekday()]
                flag = "  ***低价***" if price < hf.PRICE_THRESHOLD else ""
                log(f"[{i}/{len(tasks)}] {d} 周{wd} {hotel[:24]:24} "
                    f"{prov} NZ${price} ({len(offers)}家) {dt:4.1f}s{flag}")
                time.sleep(hf.REQUEST_GAP)
        finally:
            browser.close()

    mins = (time.monotonic() - t_start) / 60
    avg = statistics.mean(durations) if durations else 0
    log(f"===== 第 {n} 轮结束: 成功 {ok} / 异常 {bad}, "
        f"用时 {mins:.1f} 分钟, 平均抓取 {avg:.1f}s =====")
    return ok, bad, mins, avg


# ---------------------------------------------------------------- 阶段 2 报告


def load_csv(path):
    rows = []
    if not path.exists():
        return rows
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    r["price_nzd"] = int(r["price_nzd"])
                except (ValueError, KeyError, TypeError):
                    continue
                rows.append(r)
    except Exception:
        pass
    return rows


def write_report(cycles):
    old, new = load_csv(OLD_CSV), load_csv(NEW_CSV)

    # --- 提速有没有代价: 同一个 (酒店,日期) 上比较抓到的渠道数 ---
    def prov_count(rows):
        agg = defaultdict(set)
        for r in rows:
            agg[(r["hotel"], r["checkin"])].add(r["provider"])
        return agg

    old_c, new_c = prov_count(old), prov_count(new)
    shared = sorted(set(old_c) & set(new_c))
    same = better = worse = 0
    for k in shared:
        a, b = len(old_c[k]), len(new_c[k])
        if b > a:
            better += 1
        elif b < a:
            worse += 1
        else:
            same += 1

    # --- 每个日期的最低价(用最新一轮的数据) ---
    best = {}
    for r in new:
        k = (r["checkin"], r["hotel"])
        if k not in best or r["price_nzd"] < best[k][0]:
            best[k] = (r["price_nzd"], r["provider"], r["timestamp"])

    lines = []
    lines.append("# 通宵抓取报告")
    lines.append("")
    lines.append(f"生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("")

    lines.append("## 运行概况")
    lines.append("")
    lines.append("| 轮次 | 成功 | 异常 | 用时(分) | 平均单次(秒) |")
    lines.append("|---|---|---|---|---|")
    for i, (ok, bad, mins, avg) in enumerate(cycles, 1):
        lines.append(f"| {i} | {ok} | {bad} | {mins:.1f} | {avg:.1f} |")
    lines.append("")

    if cycles:
        avg_all = statistics.mean([c[3] for c in cycles if c[3]] or [0])
        lines.append(f"提速后平均单次抓取 **{avg_all:.1f}s**"
                     f"(原配置约 8.3s 抓取 + 5s 间隔 = 13.3s/次)")
        lines.append("")

    lines.append("## 提速是否抓漏了数据")
    lines.append("")
    lines.append(f"在两套配置都覆盖到的 **{len(shared)}** 个(酒店,日期)组合上，"
                 f"比较抓到的渠道数量:")
    lines.append("")
    lines.append(f"- 渠道数持平: **{same}**")
    lines.append(f"- 提速后更多: **{better}**")
    lines.append(f"- 提速后更少: **{worse}**  ← 这个数越接近 0 越好")
    lines.append("")
    if worse > len(shared) * 0.1 if shared else False:
        lines.append("> ⚠ 提速后渠道数下降明显，建议把 `SETTLE_STABLE_ROUNDS` "
                     "调到 4 或把间隔调回 5s 重跑。")
    else:
        lines.append("> ✓ 没有明显的抓取缺失，提速没有以数据完整度为代价。")
    lines.append("")

    lines.append("## 各日期最低价")
    lines.append("")
    for hotel in hf.HOTELS:
        rows = sorted([(k[0], v) for k, v in best.items() if k[1] == hotel])
        if not rows:
            continue
        lines.append(f"### {hotel}")
        lines.append("")
        lines.append("| 入住日 | 星期 | 最低价 | 渠道 |")
        lines.append("|---|---|---|---|")
        for ci, (price, prov, _) in rows:
            try:
                wd = "一二三四五六日"[date.fromisoformat(ci).weekday()]
            except ValueError:
                wd = "?"
            mark = " 🔻" if price < hf.PRICE_THRESHOLD else ""
            lines.append(f"| {ci} | 周{wd} | NZ${price}{mark} | {prov} |")
        lines.append("")
        prices = [p for _, (p, _, _) in rows]
        if prices:
            cheapest = min(rows, key=lambda x: x[1][0])
            lines.append(f"最便宜: **{cheapest[0]} NZ${cheapest[1][0]}** "
                         f"({cheapest[1][1]}) · "
                         f"中位数 NZ${statistics.median(prices):.0f} · "
                         f"区间 NZ${min(prices)}–{max(prices)}")
            lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    log(f"报告已刷新: {REPORT_PATH}")


# ---------------------------------------------------------------- 主流程


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if STOP_FLAG.exists():
        STOP_FLAG.unlink()          # 清掉上次留下的停止标记

    log("#" * 60)
    log(f"通宵 runner 启动, PID {os.getpid()}")
    log(f"每 {CYCLE_MINUTES} 分钟一轮, 最多跑 {MAX_HOURS} 小时")
    log(f"停止方法: 在 local/data/ 建文件 {STOP_FLAG.name}")
    log("#" * 60)

    wait_for_old_sweep()

    tasks = build_tasks()
    log(f"任务列表: {len(tasks)} 个 "
        f"({len(tasks) // len(hf.HOTELS)} 天 x {len(hf.HOTELS)} 家, 按日期交错)")

    cycles = []
    interval = CYCLE_MINUTES
    hard_stop = time.monotonic() + MAX_HOURS * 3600
    n = 0

    while time.monotonic() < hard_stop:
        if STOP_FLAG.exists():
            log("检测到 STOP_OVERNIGHT, 退出")
            break
        n += 1
        try:
            ok, bad, mins, avg = run_cycle(n, tasks)
            cycles.append((ok, bad, mins, avg))
        except Exception as e:
            log(f"第 {n} 轮异常: {type(e).__name__}: {e}")
            cycles.append((0, len(tasks), 0, 0))
            ok, bad = 0, len(tasks)

        try:
            write_report(cycles)
        except Exception as e:
            log(f"写报告失败: {type(e).__name__}: {e}")

        # 每轮跑完立刻重建看板 HTML, 这样本地文件永远是最新的。
        # (发布到 Artifact 需要 Claude 的工具调用, 后台进程做不到 —— 
        #  会话活着时由 Claude 盯着 CYCLE_DONE_FLAG 发布。)
        try:
            # 注意: 必须用 cwd + 相对路径。传 Windows 绝对路径给 Git bash 会失败,
            # 因为反斜杠在 bash 里是转义符。
            # 必须写死 Git bash 的绝对路径。裸写 "bash" 会解析到
            # C:\Windows\System32ash.exe (WSL), 这台机器没装 WSL 发行版,
            # 它只会打印一句 aka.ms/wslstore 然后 exit 1。
            r = subprocess.run([BASH_EXE, "build_dashboard.sh"],
                               cwd=str(HERE),
                               capture_output=True, text=True, timeout=180,
                               # 中文 Windows 默认 GBK 解码, 脚本输出是 UTF-8,
                               # 不指定会在读取线程里抛 UnicodeDecodeError
                               encoding="utf-8", errors="replace")
            if r.returncode == 0:
                log("看板已重建: auckland_rates.html")
                CYCLE_DONE_FLAG.write_text(
                    f"{n}|{datetime.now():%Y-%m-%d %H:%M:%S}", encoding="utf-8")
            else:
                # bash 的报错可能走 stdout, 两个都记
                err = (r.stderr.strip() + " " + r.stdout.strip()).strip()
                log(f"看板重建失败(rc={r.returncode}): {err[-300:]}")
        except Exception as e:
            log(f"看板重建异常: {type(e).__name__}: {e}")

        # 自适应退避: 异常太多多半是被限流了, 把间隔翻倍
        total = ok + bad
        if total and bad / total > BAD_RATIO:
            interval = min(interval * 2, MAX_CYCLE_MINUTES)
            log(f"异常率 {bad / total:.0%} 偏高, 间隔放宽到 {interval} 分钟")
        elif interval > CYCLE_MINUTES and total and bad / total < 0.1:
            interval = max(CYCLE_MINUTES, interval // 2)
            log(f"已恢复正常, 间隔收回到 {interval} 分钟")

        wait_s = max(0, interval * 60 - mins * 60) if cycles else interval * 60
        if time.monotonic() + wait_s > hard_stop:
            log(f"达到 {MAX_HOURS} 小时上限, 收工")
            break
        log(f"休息 {wait_s / 60:.0f} 分钟, 下一轮约 "
            f"{(datetime.now() + timedelta(seconds=wait_s)):%H:%M}")
        slept = 0
        while slept < wait_s:
            if STOP_FLAG.exists():
                break
            time.sleep(min(20, wait_s - slept))
            slept += 20

    try:
        write_report(cycles)
    except Exception:
        pass
    log(f"通宵 runner 结束, 共 {len(cycles)} 轮。报告: {REPORT_PATH}")


if __name__ == "__main__":
    main()
