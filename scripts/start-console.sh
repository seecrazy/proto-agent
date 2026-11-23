#!/usr/bin/env bash
# PROTO 控制台启动脚本——你的完整实例（3 个 agent 的舰队都在 ./proto_data）
# 用法：bash scripts/start-console.sh   （或 chmod +x 后 ./scripts/start-console.sh）
set -euo pipefail
cd "$(dirname "$0")"

# 防双开：已有实例在监听就不再起第二个进程；令牌由 proto.env 或 proto.web 运行时生成。
PORT=8431
if lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "✓ 控制台已在运行（端口 ${PORT}）——请从启动日志复制带令牌的 URL"
  echo "  本次未启动新进程。若已更新代码，请先在原启动终端按 Ctrl+C 正常退出，再运行本脚本。"
  exit 0
fi

echo "PROTO 控制台启动中…"
echo "  数据   : ./proto_data（你的 agent 舰队）"
echo "  停止   : Ctrl+C"
echo
exec python3 -u -m proto.web --data-dir ./proto_data --port ${PORT}
