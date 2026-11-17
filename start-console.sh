#!/usr/bin/env bash
# PROTO 控制台启动脚本——你的完整实例（3 个 agent 的舰队都在 ./proto_data）
# 用法：bash start-console.sh   （或 chmod +x 后 ./start-console.sh）
set -euo pipefail
cd "$(dirname "$0")"

# 防双开：已有实例在监听就直接打开页面，不再起第二个进程
PORT=8431
TOKEN="${PROTO_WEB_TOKEN:-niI72Mesx1eRYKrTpLn1bA}"
if lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "✓ 控制台已在运行（端口 ${PORT}）——直接打开页面"
  open "http://127.0.0.1:$PORT/#token=$TOKEN"
  exit 0
fi

echo "PROTO 控制台启动中…"
echo "  数据   : ./proto_data（你的 agent 舰队）"
echo "  地址   : http://127.0.0.1:${PORT}/#token=${TOKEN}"
echo "  停止   : Ctrl+C"
echo
open "http://127.0.0.1:${PORT}/#token=${TOKEN}" 2>/dev/null || true
exec python3 -u -m proto.web --data-dir ./proto_data --port ${PORT} --token "${TOKEN}"
