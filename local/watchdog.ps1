<#
    采集看门狗 —— 让本地 Google 采集不依赖任何会话或窗口。

    由 Windows 计划任务每 10 分钟调用一次。它自己不采集, 只负责判断
    "现在该不该起一个采集进程", 判断完就退出。

    最危险的失败是重复启动: 两个 Chromium 同时刷 Google 会被限流,
    而且两个进程往同一个 CSV 追加会写坏文件。所以这里用两道锁:

      1. PID 文件 —— 看门狗自己起的进程记在这里, 活着就跳过
      2. CSV 新鲜度 —— 抓不到 PID 时的兜底。采集进程每轮都在写
         hotel_prices_v3.csv, 轮间最长静默约 30 分钟, 所以 40 分钟内
         有写入就认为"有人在采", 不再起第二个。
         这一条专门用来保护"不是看门狗起的"那个进程 —— 比如现在这个。

    还认 STOP_OVERNIGHT: 那个文件在, 就什么都不做。否则你手动停掉采集
    之后, 看门狗会 10 分钟后又把它拉起来, 停不掉。

    参数:
      -Root    采集目录, 默认脚本自己所在目录; 测试时指向沙箱
      -Python  python.exe 路径
      -DryRun  只打印会做什么, 不真的启动进程

    路径一律相对脚本自身定位 —— 写死绝对路径正是这堆东西当初混进
    Downloads 又搬不走的原因, 见 CLAUDE.md。
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$Python = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# 不能在 param() 的默认值里用 $PSScriptRoot —— 那里它是空的, 会静默回退到
# 当前工作目录, 于是脚本跑去错的地方找文件而且一声不吭。实测踩过。
# $MyInvocation.MyCommand.Path 在脚本作用域里才是可靠的。
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($Root)) { throw "无法确定脚本所在目录, 请用 -Root 显式指定" }
$Script    = Join-Path $Root "overnight_runner.py"
$DataDir   = Join-Path $Root "data"
$PidFile   = Join-Path $DataDir "runner.pid"
$StopFlag  = Join-Path $DataDir "STOP_OVERNIGHT"
$Csv       = Join-Path $DataDir "hotel_prices_v3.csv"
$Log       = Join-Path $DataDir "watchdog.log"
$FreshMin  = 40      # CSV 这么久没动才认为采集真的停了

function Say([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    try { Add-Content -Path $Log -Value $line -Encoding utf8 } catch { }
}

# ── 0. 环境自检 ──────────────────────────────────────────────
if (-not (Test-Path $DataDir)) { Say "数据目录不存在, 放弃: $DataDir"; exit 1 }
if (-not (Test-Path $Script))  { Say "找不到采集脚本, 放弃: $Script";   exit 1 }
if (-not (Test-Path $Python))  { Say "找不到 python, 放弃: $Python";    exit 1 }

# ── 1. 手动停止优先 ──────────────────────────────────────────
if (Test-Path $StopFlag) { Say "STOP_OVERNIGHT 存在, 不启动"; exit 0 }

# ── 2. 第一道锁: 看门狗自己起的进程还活着吗 ──────────────────
if (Test-Path $PidFile) {
    $raw = (Get-Content $PidFile -Raw).Trim()
    $procId = 0
    if ([int]::TryParse($raw, [ref]$procId) -and $procId -gt 0) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -eq "python") {
            Say "采集进程 $procId 仍在运行, 跳过"
            exit 0
        }
        Say "PID $procId 已不存在, 视为已退出"
    } else {
        Say "PID 文件内容无法解析, 忽略"
    }
}

# ── 3. 第二道锁: 有没有别人在采 ──────────────────────────────
if (Test-Path $Csv) {
    $idle = [int]((Get-Date) - (Get-Item $Csv).LastWriteTime).TotalMinutes
    if ($idle -lt $FreshMin) {
        Say "CSV $idle 分钟前还有写入(阈值 $FreshMin), 判定有进程在采, 跳过"
        exit 0
    }
    Say "CSV 已静默 $idle 分钟, 判定采集已停"
} else {
    Say "还没有 CSV, 首次启动"
}

# ── 4. 启动 ──────────────────────────────────────────────────
if ($DryRun) { Say "[DryRun] 这里会启动采集, 但没有真的启动"; exit 0 }

$out = Join-Path $DataDir "overnight_stdout.log"
$err = Join-Path $DataDir "overnight_stderr.log"
$proc = Start-Process -FilePath $Python `
                      -ArgumentList "`"$Script`"" `
                      -WorkingDirectory $Root `
                      -WindowStyle Hidden `
                      -RedirectStandardOutput $out `
                      -RedirectStandardError  $err `
                      -PassThru
Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
Say "已启动采集, PID $($proc.Id)"
exit 0
