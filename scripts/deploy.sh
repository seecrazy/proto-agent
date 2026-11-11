#!/usr/bin/env bash
# PROTO 一键部署引导：venv → 安装 → 部署向导
# 用法：bash scripts/deploy.sh [--install-service ...其余参数原样传给 proto.setup]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "PROTO 部署引导"
echo "=============="

# 1) Python 版本检查（>=3.9）
if ! command -v python3 >/dev/null; then
  echo "错误：未找到 python3，请先安装 Python 3.9+"; exit 1
fi
PYVER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    print(f"错误：需要 Python >= 3.9，当前 {sys.version_info[:3]}")
    sys.exit(1)
PY
echo "[1/4] Python $PYVER ✓"

# 2) 独立 venv（不污染系统环境）
if [ ! -x ".venv/bin/python" ]; then
  echo "[2/4] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
else
  echo "[2/4] 虚拟环境已存在 ✓"
fi

# 3) 安装 PROTO（零运行时依赖，秒装）
echo "[3/4] 安装 proto-agent ..."
.venv/bin/pip install -q -e .
echo "      proto-run / proto-web / proto-backup 命令就绪 ✓"

# 4) 部署向导（LLM 配置/自检/令牌/人设/可选开机自启）
echo "[4/4] 启动部署向导 ..."
exec .venv/bin/python -m proto.setup "$@"
