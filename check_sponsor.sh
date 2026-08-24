#!/usr/bin/env bash
# gate 1a 核查脚本
# 用法: ./check_sponsor.sh "Employer Name"
# 退出码: 0 = 有命中（仍须人工看完整公司名！）, 1 = 零命中（不在注册表上）
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
CSV="$DIR/register.csv"

if [ ! -f "$CSV" ]; then
  echo "register.csv 不存在 — 先运行 GitHub Action (workflow_dispatch) 生成" >&2
  exit 2
fi

echo "=== Snapshot: $(cat "$DIR/SNAPSHOT_NAME.txt" 2>/dev/null || echo unknown) (updated $(cat "$DIR/LAST_UPDATED.txt" 2>/dev/null || echo '?')) ==="
echo "=== Query: $1 ==="
echo

# 列: Organisation Name, Town/City, County, Type & Rating, Route
if grep -i -- "$1" "$CSV"; then
  echo
  echo "⚠️  以上是子串命中。两条铁律："
  echo "   1. 必须核对完整公司名再下结论（搜 Pearson 会出 Dan Pearson Studio）"
  echo "   2. 广告提到 subsidiary / Enterprises Ltd / Support Services Ltd 时，单独再查子公司名"
  exit 0
else
  echo "❌ NO MATCH — 不在注册表上（gate 1a 不通过）"
  exit 1
fi
