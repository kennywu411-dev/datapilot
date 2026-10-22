#!/bin/bash
# 生成本机启动器 ~/Applications/DataPilot.app（只在本机用，不进 git 和代码包）
# 用法：bash scripts/app/make_app.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APP="$HOME/Applications/DataPilot.app"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
sed "s#__ROOT__#$ROOT#" "$HERE/DataPilot.applescript" | osacompile -o "$APP"
# 换图标（icon.icns 由 icon.svg 生成：render_icon.swift 透明渲染 → sips 缩放 → iconutil）
cp "$HERE/icon.icns" "$APP/Contents/Resources/applet.icns"
touch "$APP"
echo "已生成 $APP"
