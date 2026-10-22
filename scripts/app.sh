#!/bin/bash
# DataPilot 启停（Docker 版）。DataPilot.app 调用它，也可以直接在终端用。
# 用法：bash scripts/app.sh start | stop | status
set -euo pipefail
# 从访达双击启动的应用拿不到终端里的 PATH，这里补上 Homebrew 路径
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")/.."

running() {
  curl -sf -m 2 http://localhost:8000/api/health >/dev/null &&
    curl -sf -o /dev/null -m 2 http://localhost:3000
}

case "${1:-}" in
  status)
    running
    ;;
  start)
    colima status >/dev/null 2>&1 || colima start
    docker-compose up -d
    for _ in $(seq 60); do
      running && exit 0
      sleep 1
    done
    echo "60 秒内服务没有就绪，查看日志：docker-compose logs" >&2
    exit 1
    ;;
  stop)
    if colima status >/dev/null 2>&1; then
      docker-compose down
      colima stop
    fi
    ;;
  *)
    echo "用法：bash scripts/app.sh start | stop | status" >&2
    exit 2
    ;;
esac
