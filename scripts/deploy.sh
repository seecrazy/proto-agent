#!/usr/bin/env bash
# PROTO 一键部署引导：venv → 安装 → 部署向导
# 用法：bash scripts/deploy.sh [--install-service ...其余参数原样传给 proto.setup]
set -euo pipefail
cd "$(dirname "$0")/.."

# ---- 颜色（NO_COLOR / 非终端自动降级）----
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_OFF=""
fi
ok()   { printf "%s✓%s %s\n" "$C_GREEN" "$C_OFF" "$1"; }
warn() { printf "%s⚠%s %s\n" "$C_YELLOW" "$C_OFF" "$1"; }
die()  { printf "%s✗ %s%s\n" "$C_RED" "$1" "$C_OFF" >&2; exit 1; }
step() { printf "%s▸ [%s/4]%s %s\n" "$C_BOLD$C_CYAN" "$1" "$C_OFF" "$2"; }

printf "%s" "$C_BOLD$C_CYAN"
cat <<'BANNER'
╔══════════════════════════════════════════╗
║   PROTO · 可控成长型 Agent · 部署引导   ║
╚══════════════════════════════════════════╝
BANNER
printf "%s" "$C_OFF"

# 1) Python
command -v python3 >/dev/null || die "未找到 python3，请先安装 Python 3.9+"
python3 - <<'PY' || exit 1
import sys
if sys.version_info < (3, 9):
    print(f"需要 Python >= 3.9，当前 {sys.version_info[:3]}")
    sys.exit(1)
PY
PYVER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
step 1 "Python $PYVER 检查"
ok "Python $PYVER（≥3.9）"

# 2) venv
if [ ! -x ".venv/bin/python" ]; then
  step 2 "创建独立虚拟环境 .venv（不污染系统）"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
else
  step 2 "虚拟环境"
  ok ".venv 已存在（复用）"
fi

# 3) 安装
step 3 "安装 proto-agent（零运行时依赖）"
.venv/bin/pip install -q -e .
ok "已安装：proto-run / proto-web / proto-backup"

# 4) 向导
step 4 "部署向导（LLM 配置 / 自检 / 令牌 / 人设 / 可选开机自启）"
printf "%s  向导支持：任意步 < 回上一步；确认页数字跳步重选%s\n" "$C_DIM" "$C_OFF"
exec .venv/bin/python -m proto.setup "$@"
