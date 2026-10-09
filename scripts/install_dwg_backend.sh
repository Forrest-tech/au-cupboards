#!/usr/bin/env bash
# 在**不依赖 Homebrew** 的前提下装好 DWG 解析后端。
#
# 存在理由：用户在 macOS 11Intel 上 `brew install libredwg` 失败 ——
#   Error: undefined method `compatibility_version' for M4:Class
#   Error: undefined method `first' for nil:NilClass (cask_loader)
# 根因是 Homebrew 自身版本与 tap 结构不匹配（brew 本体太旧、tap 已更新到
# 需要新版 Homebrew 的格式），而 macOS 11 已不被 Homebrew 支持，
# `brew update` 只会越修越坏。这类环境**不要继续修 brew**，
# 直接源码编译 LibreDWG 反而更快更可控。
#
# 优先策略：
#   1. 已装 ODA File Converter（.app 里带 CLI）→ 直接用，什么都不用装
#   2. PATH 里已有 dwgread → 直接用
#   3. 从 GitHub Release 下官方 tarball 源码编译到 ~/.local/bin（无需 brew）
#
# 用法：
#   bash scripts/install_dwg_backend.sh
#
# 产物： ~/.local/bin/dwgread
set -euo pipefail

LIBREDWG_VER="${LIBREDWG_VER:-0.13.3}"
PREFIX="${PREFIX:-$HOME/.local}"
# WORK 固定而非 mktemp —— 编译失败重跑时要复用已下载的源码（20MB）

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

OS="$(uname -s)"
say "════════════════════════════════════════════════"
say " DWG 解析后端安装（免 brew 版）"
say " 系统: $OS  架构: $(uname -m)"
say " 目标: $PREFIX/bin"
say "════════════════════════════════════════════════"

# ---------- 1. 已有 ODA？----------
# ODA File Converter 的 CLI 在 .app 包内部，不在 PATH，
# 所以要直接查 /Applications，否则明明装了却报"未安装"。
step "检查现有后端"
ODA_FOUND=""
for cand in \
  /Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter \
  "$HOME/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter" \
  "/Applications/ODA File Converter.app/Contents/MacOS/ODAFileConverter" \
  /usr/local/bin/ODAFileConverter \
  /opt/homebrew/bin/ODAFileConverter
do
  [ -x "$cand" ] && { ODA_FOUND="$cand"; break; }
done
if [ -n "$ODA_FOUND" ]; then
  ok "发现 ODA File Converter"
  say "  $ODA_FOUND"
  say ""
  say "  把这行加进 ~/.zshrc，service 才能自动发现它："
  say "    export PATH=\"$(dirname "$ODA_FOUND"):\$PATH\""
  exit 0
fi
if command -v dwgread >/dev/null 2>&1; then
  ok "PATH 里已有 dwgread: $(command -v dwgread)"
  dwgread --version 2>&1 | head -1 || true
  exit 0
fi
say "  未找到 ODA / dwgread，开始编译 LibreDWG $LIBREDWG_VER"

# ---------- 2. 前置工具 ----------
step "检查编译工具链"
need_cc=1
if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
  need_cc=0
fi
if [ "$need_cc" -eq 0 ]; then
  die "找不到 C 编译器。执行：xcode-select --install  （选「安装」即可）"
fi
say "  编译器: $(command -v cc || command -v gcc)"

# tar / curl / make
for t in tar curl make; do
  command -v "$t" >/dev/null 2>&1 || die "缺少 $t —— macOS 自带，通常不会走到这里"
done
say "  tar/curl/make 就绪"

# autoconf 系列：官方 tarball 里已带 configure，理论上不需要；
# 万一没有才走这里，且不需要 brew —— 用 pip 装不了，只能给提示。
TARBALL="libredwg-$LIBREDWG_VER.tar.gz"
URL="https://github.com/LibreDWG/libredwg/releases/download/$LIBREDWG_VER/$TARBALL"

# ---------- 3. 下载 ----------
# 默认把工作目录固定下来：编译失败重跑时不必重下20MB。
# 传 WORK=/tmp/xxx 可用完即弃。
WORK="${WORK:-${TMPDIR:-/tmp}/libredwg-build}"
step "准备构建目录 $WORK"
mkdir -p "$WORK"
cd "$WORK"
if [ -s "$TARBALL" ]; then
  ok "复用已下载的 ${TARBALL}（$(du -h "$TARBALL" | cut -f1)）"
else
  say "  $URL"
  # GitHub release 资产走 release-assets.githubusercontent.com，
  # 慢是正常的。带断点续传，中断后重跑不用从头下。
  curl -fL -C - --connect-timeout 30 --retry 5 --retry-delay 3 \
       --progress-bar \
       -o "${TARBALL}.part" "$URL" || die "下载失败，检查网络后重试"
  mv "${TARBALL}.part" "$TARBALL"
  ok "已下载 $(du -h "$TARBALL" | cut -f1)"
fi

step "解包"
# 已解包且configure 在就别重复解
if [ ! -x "$WORK/libredwg-$LIBREDWG_VER/configure" ]; then
  tar xzf "$TARBALL"
else
  say "  已解包，跳过"
fi
SRC="$WORK/libredwg-$LIBREDWG_VER"
[ -d "$SRC" ] || die "解包异常，期望目录 $SRC 不存在"
ok "$SRC"

cd "$SRC"

# ---------- 4. configure ----------
step "configure"
# --disable-bindings  省去 python/perl 绑定，那堆依赖在老mac 上极难装
# --disable-shared     只要静态 lib，dwgread 是独立进程，不影响
# --disable-dependency-tracking  老mac 上 automake 版本差异容易踩坑
if [ ! -x ./configure ]; then
  warn "tarball 里没有 configure，需要 autotools"
  say ""
  say "  macOS 自带 clang 但不带 autoconf/automake，且你的 brew 已损坏。"
  say "  两个办法，选一个："
  say ""
  say "  A. 只用 PDF（推荐先这样）—— PDF 解析完全不受影响，"
  say "     DWG 上传会明确报'后端缺失'而不是静默失败。"
  say ""
  say "  B. 修 Homebrew："
  say "     rm -rf /usr/local/Homebrew && mkdir -p /usr/local/Homebrew"
  say "     /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  say "     然后： brew install autoconf automake libtool && 重新运行本脚本"
  exit 1
fi

# --disable-werror 是**必须的**：LibreDWG 默认开 -Werror，而 Apple clang
# 的 -Wformat 检查比 GCC 严格，会把
#   "format specifies type 'unsigned short' but the argument has type
#    'BITCODE_BL'"（print.c / dwg.spec 里的宏展开）
# 这类格式串警告升级成错误，5 个文件直接编不过。Linux 上 gcc 没这问题，
# 所以这是纯 macOS 专属坑。官方 configure 本身就提供该开关。
./configure \
  --prefix="$PREFIX" \
  --disable-bindings \
  --disable-shared \
  --disable-dependency-tracking \
  --disable-werror \
  --with-cxx=no \
  CFLAGS="-O2 -Wno-format -Wno-format-extra-args -Wno-implicit-function-declaration" \
  > "$WORK/configure.log" 2>&1 || {
    warn "configure 失败，日志尾部："
    tail -25 "$WORK/configure.log" >&2
    die "configure 未通过"
  }
ok "configure 通过（已关 -Werror 与 -Wformat）"

# ---------- 5. make ----------
step "编译（Intel Mac 大约 3-8 分钟，别中断）"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 2)"

# 兜底：即使 configure 已关 -Werror，Makefile 里也可能另有追加。
# 再传一次 CFLAGS 强制 -Wno-error，且放在命令行（优先级高于 Makefile 变量）。
if ! make -j"$JOBS" \
     CFLAGS="-O2 -Wno-error -Wno-format -Wno-format-extra-args" \
     > "$WORK/make.log" 2>&1; then
  # 再兜一次：清掉可能的 -Werror 硬编码
  say "  首次编译失败，尝试完全禁用警告后重试 ..."
  if make -j"$JOBS" CFLAGS="-O2 -Wno-error" \
       > "$WORK/make2.log" 2>&1; then
    warn "第二次编译通过（已关闭全部警告）—— 这通常是 Apple clang 过于严格所致"
  else
    warn "编译失败，日志尾部："
    tail -40 "$WORK/make.log" >&2
    [ -f "$WORK/make2.log" ] && { say "--- 第二次尝试 ---"; tail -25 "$WORK/make2.log" >&2; }
    say ""
    say "构建目录保留在 ${WORK}（改了参数可以直接重跑本脚本，不必重下源码）"
    die "make 未通过"
  fi
fi
ok "编译完成"

step "安装到 $PREFIX"
make install > "$WORK/install.log" 2>&1 || {
    tail -25 "$WORK/install.log" >&2
    die "make install 未通过"
}
ok "已安装"

# ---------- 6. 验证 ----------
step "验证"
DWGREAD="$PREFIX/bin/dwgread"
[ -x "$DWGREAD" ] || die "装完却找不到 $DWGREAD"
chmod +x "$DWGREAD" 2>/dev/null || true

VER="$("$DWGREAD" --version 2>&1 | head -1 || echo '（版本号读取失败，但二进制存在）')"
ok "dwgread 可执行：$DWGREAD"
say "  $VER"

# 真跑一次：--help 能出说明就说明动态库齐全
if "$DWGREAD" --help >/dev/null 2>&1; then
  ok "运行正常（--help 可用）"
else
  warn "二进制在但 --help 报错，可能是运行时库缺失："
  warn "  otool -L $DWGREAD | grep -i 'not found'"
  exit 1
fi

# ---------- 7. 告知PATH ----------
say ""
say "════════════════════════════════════════════════"
say " 安装完成，还差最后一步：让服务找到它"
say "════════════════════════════════════════════════"
say ""
say "执行这行（写进 ~/.zshrc，永久生效）："
say ""
say "    echo 'export PATH=\"$PREFIX/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
say ""
say "然后验证："
say ""
say "    which dwgread && dwgread --version"
say ""
say "最后重启服务，顶栏徽章应变成「DWG 就绪」："
say ""
say "    bash setup.sh"
say ""

# 顺手验证 PATH 是否已生效，省得用户以为没装好
if command -v dwgread >/dev/null 2>&1; then
  ok "PATH 里已经能找到 dwgread，无需再改配置"
else
  warn "当前 shell 还找不到 dwgread —— 上面那行 export 必须执行"
fi