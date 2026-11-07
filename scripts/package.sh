#!/usr/bin/env bash
# 生成代码包：只打包 git 跟踪的文件（天然排除 .env、data/、node_modules、.next），
# 打包前扫描密钥与数据库文件，发现任何一项就中止。
# 用法：bash scripts/package.sh   →  dist/datapilot-<版本>.tar.gz
# 用 tar.gz 而不是 zip：macOS 自带的 unzip 6.00 解压中文文件名会失败（问题日志 P12）
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有未提交的改动，先提交再打包" >&2; exit 1
fi

VERSION="$(git describe --tags --always)"
OUT="dist/datapilot-${VERSION}.tar.gz"
FILES="$(git ls-files)"

echo "[package] 检查文件清单"
# 兜底检查（正常情况下 .gitignore 已经挡住）：data/ 只指仓库根目录，web/src/data 是看板数据
BAD_FILES="$(echo "$FILES" | grep -E '(^|/)\.env$|\.sqlite$|\.db$|^data/|node_modules/|\.next/|\.zip$' || true)"
if [ -n "$BAD_FILES" ]; then
  echo "发现不该打包的文件：" >&2; echo "$BAD_FILES" >&2; exit 1
fi

echo "[package] 扫描密钥"
# Anthropic / OpenAI / AWS / 通用私钥格式；.env.example 里只有空值
# 排除本脚本自身：它的正则里本来就写着这些模式
SECRETS="$(echo "$FILES" | grep -v '^scripts/package.sh$' | tr '\n' '\0' | xargs -0 grep -nIE \
  'sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{32,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Z_]*(API_KEY|SECRET|TOKEN)=[^[:space:]]+' \
  2>/dev/null || true)"
if [ -n "$SECRETS" ]; then
  echo "疑似密钥：" >&2; echo "$SECRETS" | cut -c1-120 >&2; exit 1
fi

mkdir -p dist
rm -f "$OUT"
git archive --format=tar.gz --prefix="datapilot/" -o "$OUT" HEAD
# 自检：解压到临时目录，确认中文文件名完好
CHECK="$(mktemp -d)"
tar -xzf "$OUT" -C "$CHECK"
test -f "$CHECK/datapilot/docs/问题日志.md" || { echo "解压自检失败：中文文件名丢失" >&2; exit 1; }
rm -rf "$CHECK"
echo "[package] 完成：$OUT（$(du -h "$OUT" | cut -f1)，$(echo "$FILES" | wc -l | tr -d ' ') 个文件）"
