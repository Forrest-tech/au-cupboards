#!/usr/bin/env python3
"""校验柜型库里的图是不是**新的高清 DWG 原图切割**。

【为什么需要这个脚本】
渲染参数（``compose`` 默认 False、``DPI`` 110→300）是代码，
但 ``var/aucup.db`` 与 ``var/thumbs/*.jpg`` 被 ``.gitignore`` 排除 ——
``git pull`` 只更新代码，**不会**更新这两个。所以拉完代码后柜型库里
仍然是旧的黑图。这个脚本一眼就能看出「图到底换了没」。

判据
----
1. **分辨率**：新图宽度应≥ 1100px；旧的黑图只有 416px。
2. **不再查「黑像素占比」**：踩过坑 —— 14 套柜图里本来就有大量
   真实深色填充线条（暗像素15.6%），会被误判成旧黑图。
   「黑黢黢」的真正成因是分辨率不足，不是黑色填充。
3. **库一致性**：变体数 / units 合计 / 缺图数 / 一图多变体数。
4. **Ground Truth**：12 档 units 数是否吻合（权威口径）。

用法::

    python3 scripts/verify_cupboards.py
"""
from __future__ import annotations

import collections
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "var" / "aucup.db"
THUMBS = ROOT / "var" / "thumbs"

#: 用户给的权威口径：units 数 → 柜型数量
GROUND_TRUTH = {3: 2, 4: 1, 5: 2, 6: 2, 7: 3, 8: 1,
                9: 2, 10: 2, 11: 2, 12: 2, 13: 3, 14: 1}

MIN_WIDTH = 1100# 新图 ≥1100px；旧黑图 416px

GREEN, RED, YELLOW, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[0m")


def _load_rows() -> list[tuple]:
    if not DB.exists():
        print(f"{RED}找不到数据库 {DB}{RESET}")
        print("   柜型库是运行期数据（.gitignore），首次使用请先上传 DWG 入库。")
        sys.exit(1)
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select variant_code, positions_total, image_path, source, w, h "
        "from cupboard_variants order by positions_total, id"
    ).fetchall()
    con.close()
    return rows


def main() -> int:
    rows = _load_rows()
    if not rows:
        print(f"{RED}柜型库是空的（0 个变体）{RESET}")
        print("   → 在浏览器里走一遍：柜型库 → 上传图纸 → 渲染 → 确认入库")
        return 1

    print(f"库位置   {DB}")
    print(f"变体数   {len(rows)}")
    dwg = sum(1 for r in rows if r[3] == "dwg")
    units = sum(int(r[1] or 0) for r in rows)
    print(f"dwg 来源 {dwg}    units 合计 {units}")

    ok = True

    # ---- 1. Ground Truth ----
    have = collections.Counter(int(r[1]) for r in rows)
    diffs = [(u, n, have[u]) for u, n in sorted(GROUND_TRUTH.items())
             if have[u] != n]
    if diffs:
        ok = False
        print(f"\n{RED}[FAIL] Ground Truth 不吻合{RESET}")
        for u, want, got in diffs:
            print(f"       {u:>2} units：应为 {want}，实际 {got}")
    else:
        print(f"\n{GREEN}[PASS] Ground Truth 12 档全部吻合{RESET}")

    # ---- 2. 缺图 / 一图多变体 ----
    files = {p.name for p in THUMBS.glob("*.jpg")} if THUMBS.exists() else set()
    missing = [r[0] for r in rows if os.path.basename(r[2] or "") not in files]
    by_img: dict[str, list[str]] = collections.defaultdict(list)
    for r in rows:
        by_img[os.path.basename(r[2] or "")].append(r[0])
    shared = {k: v for k, v in by_img.items() if len(v) > 1}
    if missing:
        ok = False
        print(f"{RED}[FAIL] 缺图 {len(missing)} 个：{missing[:8]}{RESET}")
    else:
        print(f"{GREEN}[PASS] 缺图 0{RESET}")
    if shared:
        ok = False
        print(f"{RED}[FAIL] 一图多变体：{shared}{RESET}")
    else:
        print(f"{GREEN}[PASS] 一图多变体 0{RESET}")

    # ---- 3. 分辨率与黑块 ----
    try:
        from PIL import Image
    except ImportError:
        print(f"{YELLOW}未装 Pillow/numpy，跳过画质检查{RESET}"
              "（pip install pillow numpy）")
        return 0 if ok else 1

    widths = []
    for r in rows:
        p = ROOT / "var" / r[2]
        if not p.exists():
            continue
        with Image.open(p) as im:
            widths.append(int(im.size[0]))

    if not widths:
        print(f"{RED}[FAIL] 没有可读的图{RESET}")
        return 1

    #【判据只用分辨率，不用「黑像素占比」】
    #
    # 【踩坑记录】最初这里还查了「暗像素占比 < 12%」，结果误报：
    # 14 套柜（CP-14p-04）图里本来就有大量真实的深色填充线条，
    # 暗像素占比 15.6%，被判成「旧的黑图」—— 而它其实是正确的新图。
    #
    # 「黑黢黢」的真正成因是**分辨率不足**（110dpi → 416×641px），
    # 放大后细节糊成一片，而不是图真的有大面积黑色填充。
    # 所以判据收敛成唯一可靠的一条：宽度是否达到新图的量级。
    narrow = [w for w in widths if w < MIN_WIDTH]
    print(f"\n图宽度   最小 {min(widths)}px  最大 {max(widths)}px"
          f"  （新图应 ≥{MIN_WIDTH}px；旧黑图为416px）")

    if narrow:
        ok = False
        print(f"\n{RED}[FAIL] 有 {len(narrow)}/{len(widths)} 张图宽度 <"
              f" {MIN_WIDTH}px —— 还是旧的黑图！{RESET}")
        print("       var/ 被 .gitignore 排除，git pull 不会更新图片。")
        print("       请重新走一遍：柜型库 → 上传图纸 → 渲染 → 确认入库")
        print("       或直接运行：bash scripts/refresh_cupboards.sh")
    else:
        print(f"\n{GREEN}[PASS] 全部 {len(widths)} 张图分辨率 ≥{MIN_WIDTH}px"
              f"（已是 DWG 原图切割）{RESET}")

    print()
    print(f"{GREEN}全部通过 —— 柜型库已是高清 DWG 原图切割{RESET}"
          if ok else f"{RED}存在未通过项，见上{RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())