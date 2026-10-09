#!/usr/bin/env bash
# 一键环境自检 + 启动。
#
# 存在理由：实测踩坑 —— 用户在 backend/ 目录里执行 `cd au-cupboards`
# 与 `cd backend`，两次都报 No such file or directory（因为已经在里面了），
# 虚拟环境因此建在 backend/backend，导致后续所有路径都错位。
#
# 本脚本不依赖当前工作目录，会自动定位仓库根目录。
set -euo pipefail

# ---- 定位仓库根目录（本文件所在的上级）----
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"

echo "════════════════════════════════════════════════"
echo " AU Meter Cupboard Scheduling System"
echo " 仓库根目录: $ROOT"
echo "════════════════════════════════════════════════"

# ---- Python 版本检查 ----
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ 找不到 $PY —— 请先安装 Python 3.11+"
  exit 1
fi

PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PYMAJOR="$("$PY" -c 'import sys; print(sys.version_info[0])')"
PYMINOR="$("$PY" -c 'import sys; print(sys.version_info[1])')"

echo "解释器    : $PY ($PYVER)"
if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 11 ]; }; then
  echo "✗ 需要 Python 3.11+，当前 $PYVER"
  echo "  macOS 用户注意：系统自带的是 2.7，请用 python3"
  exit 1
fi

# ---- 虚拟环境 ----
if [ ! -d ".venv" ]; then
  echo "创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "虚拟环境  : $(command -v python)"

# ---- 依赖 ----
echo "检查依赖 ..."
# 逐个 import 校验，而不是只看 uvicorn 在不在 —— 曾出现过"清单漏了
# python-multipart / httpx，但开发机全局环境恰好有，测试全绿而干净环境
# 装完启动即崩"的情况。
if ! python - <<'PY' 2>/dev/null
import importlib, sys
mods = ["uvicorn", "fastapi", "multipart", "fitz", "ezdxf", "PIL",
        "docx", "reportlab", "openpyxl", "httpx"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        missing.append(f"{m} ({type(e).__name__})")
if missing:
    print("缺失: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
then
  echo "安装依赖（首次约需 1-2 分钟）..."
  python -m pip install --upgrade pip --quiet
  python -m pip install -r requirements.txt
else
  echo "依赖      : 已就绪（核心 10 项校验通过）"
fi

# ---- CLI 自检 ----
echo ""
echo "────────── 自检 ──────────"
( cd backend && python -m app.cli health )
echo ""

# ---- 启动 ----
echo "启动服务：http://127.0.0.1:$PORT"
echo "按 Ctrl+C 停止"
echo ""
cd backend
exec python -m uvicorn app.api.server:app --host 0.0.0.0 --port "$PORT"
