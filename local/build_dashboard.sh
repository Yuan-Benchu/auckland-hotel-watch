#!/usr/bin/env bash
# 重新生成看板: 合并 CSV -> 导出 JSON -> 注入模板 -> 输出到 artifact 源文件
#
# 用 Python 做注入会撞上 Windows 260 字符路径上限(scratchpad 路径很长),
# 所以这里用 head/tail 按字节拼接绕开。
set -euo pipefail

# 相对脚本自身定位, 不写死绝对路径
cd "$(dirname "$0")"

PY="C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe"
TPL="dashboard_template.html"
OUT="_built.html"
# 两个位置都要写:
#   DEST_LOCAL  —— 本地随时双击打开
#   DEST_PUB    —— Claude 的 scratchpad, Artifact 只能从这里发布
#                  (工作目录或 scratchpad 之外的路径会被拒绝)
DEST_LOCAL="dashboards/auckland_rates.html"
DEST_PUB="C:/Users/Administrator/AppData/Local/Temp/claude/C--Users-Administrator-AppData-Roaming-Claude-scratch-workspaces-6650fb6f-404b-4df1-b693-9eb51a9f91c4-97ac2ef6-f490-4690-8521-a3303d3b2ee5-scratch-2026-09-15-78c0c8/c32c7eaf-bb68-4220-a27d-d348f989b774/scratchpad/auckland_rates.html"

echo "── 合并数据 ──"
"$PY" merge_and_export.py | sed 's/^/  /'

echo "── 注入模板 ──"
OFF=$(grep -abo '/\*__DATA__\*/' "$TPL" | head -1 | cut -d: -f1)
[ -n "$OFF" ] || { echo "模板里找不到 placeholder"; exit 1; }

head -c "$OFF" "$TPL" > _a
tr -d '\n' < dashboard_data.json > _d
tail -c +$((OFF + 13)) "$TPL" > _b
cat _a _d _b > "$OUT"
rm -f _a _b _d

grep -q '__DATA__' "$OUT" && { echo "placeholder 没被替换掉"; exit 1; }
cp "$OUT" "$DEST_LOCAL"
cp "$OUT" "$DEST_PUB"
echo "  生成 $(stat -c %s "$OUT") bytes -> 本地 + scratchpad 各一份"
echo
echo "── 时间轴看板 ──"
"$PY" export_timeline.py | sed 's/^/  /'

echo "现在让 Claude 用 Artifact 重新发布 auckland_rates.html 即可(URL 不变)。"
