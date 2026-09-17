#!/usr/bin/env bash
# 重新生成看板: 合并 CSV -> 导出 JSON -> 注入模板 -> 写出 HTML
#
# 用 Python 做注入会撞上 Windows 260 字符路径上限, 所以这里用 head/tail
# 按字节拼接绕开。
set -euo pipefail

# 相对脚本自身定位, 不写死绝对路径 —— 写死绝对路径正是这堆东西当初
# 混进 Downloads 又搬不走的原因。
cd "$(dirname "$0")"

PY="C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe"
TPL="dashboards/dashboard_template.html"
DATA="data/dashboard_data.json"
OUT="dashboards/_built.html"
DEST="dashboards/auckland_rates.html"

echo "── 合并数据 ──"
"$PY" merge_and_export.py | sed 's/^/  /'

echo "── 注入模板 ──"
OFF=$(grep -abo '/\*__DATA__\*/' "$TPL" | head -1 | cut -d: -f1)
[ -n "$OFF" ] || { echo "模板里找不到 placeholder"; exit 1; }

head -c "$OFF" "$TPL" > _a
tr -d '\n' < "$DATA" > _d
tail -c +$((OFF + 13)) "$TPL" > _b
cat _a _d _b > "$OUT"
rm -f _a _b _d

grep -q '__DATA__' "$OUT" && { echo "placeholder 没被替换掉"; exit 1; }
cp "$OUT" "$DEST"
echo "  生成 $(stat -c %s "$OUT") bytes -> $DEST"
echo

echo "── 时间轴看板 ──"
"$PY" export_timeline.py | sed 's/^/  /'

echo
echo "完成。两个看板都在 dashboards/ 下, Artifact 直接从那里发布。"
