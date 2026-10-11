#!/usr/bin/env bash
# 重新生成柜型库高清图（DWG 原图切割 + 300dpi）
#
# 【为什么必须重跑】
# 代码里的渲染参数（compose 默认 False、DPI=300）已修好，
# 但 `var/` 被 .gitignore 排除 —— git pull 只会更新代码，
# 不会更新 var/aucup.db 与 var/thumbs/*.jpg。
# 所以拉完代码后，柜型库里仍然是旧的黑图，必须重新入库。
#
# 用法（在仓库根目录执行）：
#   bash scripts/refresh_cupboards.sh
#
# 前提：后端已启动，且 samples/ 下有 DWG。

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
DWG="${DWG:-$(ls samples/*.dwg 2>/dev/null | head -1 || true)}"

echo "==> 仓库: $ROOT"
echo "==> 后端: $BASE"

if [ -z "${DWG}" ]; then
  echo "!! samples/ 下没有 .dwg，请把图纸放到 samples/ 再跑" >&2
  exit 1
fi
echo "==> 样本: $DWG"

if ! curl -sf -o /dev/null "${BASE}/api/health"; then
  echo "!! 后端没起来，请先在另一个终端启动：" >&2
  echo "   cd backend && python3 -m uvicorn app.api.server:app --host 0.0.0.0 --port ${PORT}" >&2
  exit 1
fi

echo "==> 旧图与旧库记录数："
ls var/thumbs/*.jpg 2>/dev/null | wc -l | sed 's/^/    图 /'
python3 - <<'PY' || true
import sqlite3, pathlib
p = pathlib.Path("var/aucup.db")
if p.exists():
    c = sqlite3.connect(p)
    print("    变体", c.execute("select count(*) from cupboard_variants").fetchone()[0])
PY

read -r -p "==> 清空柜型库并重新渲染入库？[y/N] " ans
case "$ans" in
  y|Y|yes|YES) ;;
  *) echo "已取消"; exit 0;;
esac

# 只删 dwg 来源的变体，保留 seed_poc 等种子数据
python3 - <<'PY'
import sqlite3, pathlib
p = pathlib.Path("var/aucup.db")
if p.exists():
    c = sqlite3.connect(p)
    n = c.execute("select count(*) from cupboard_variants").fetchone()[0]
    c.execute("delete from cupboard_variants")
    c.commit()
    print(f"    已清空 {n} 条变体")
PY
rm -f var/thumbs/*.jpg
echo "    已删除旧缩略图"

echo "==> 请在浏览器里完成上传（渲染 + 入库）："
echo "    1. 打开 ${BASE}/"
echo "    2. 点顶部「柜型库」→ 左上「上传图纸」"
echo "    3. 选择 ${DWG}"
echo "    4. 等渲染完成（23 个柜型，约 30 秒）→「确认入库」"
echo
echo "==> 完成后跑校验："
echo "    python3 scripts/verify_cupboards.py"