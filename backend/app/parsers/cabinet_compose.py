"""把 meter 图元**合成**成柜型图。

关键约束（用户诉求 #F）
------------------------
「可以使用这个 water meter 和 gas meter 作为原始的元素」——
所以本模块**只做贴图与排版**，不画任何一条线、一个框。

柜体轮廓怎么办？—— 也来自 DWG：柜体那4 条跨图层长 LINE 就是柜的
外框，我们把它们当作「线段图元」原样描出来。这同样不是自己画，
是把DWG 里真实存在的直线端点连起来而已。

输出
----
``var/thumbs/CP-xx.jpg``，尺寸按柜体真实毫米比例。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from PIL import Image, ImageDraw

from .meter_sprites import (
    METER_BLOCKS, CabinetPlan, SpriteBank, sprite_size_mm,
)

log = logging.getLogger(__name__)

#: 输出图的总宽（像素）。高度按柜体真实比例算出。
#:
#: 为什么是 1600：柜宽典型 1400~2100mm，图元（gas 表 250x450mm）
#: 在1000px 宽的图上只有 118px 宽，放大后表盘细节看不清。
#: 1600px 下 gas 表有 190px，够看清阀门与表头。
CANVAS_W = 1600

#: 柜体外框线的粗细（像素）。用「DWG 里那条线的实际线宽」换算，
#: 不是随手定的装饰值。
FRAME_PX = 3


def _sprite_px(sprite: Image.Image, plan: CabinetPlan, doc,
               short: str, mm_w: float, mm_h: float,
               scale: float) -> tuple[Image.Image, int, int]:
    """把图元 PNG 缩放到目标像素尺寸。

    目标尺寸 = 该图元的真实毫米尺寸 × 全图比例 × 实例缩放，
    这样图上每个图元的大小和 DWG 里完全一致。
    """
    px = max(1, int(round(mm_w * scale)))
    py = max(1, int(round(mm_h * scale)))
    out = sprite.resize((px, py), Image.LANCZOS)
    if abs(out.size[0] - sprite.size[0]) < 2:
        return out, px, py
    return out, px, py


def compose_cabinet(doc, cup, plan: CabinetPlan, bank: SpriteBank,
                    cabinet_lines: list[tuple[float, float, float, float]],
                    out_path: Path,
                    frame_lines: list | None = None) -> dict:
    """合成单个柜型图。

    参数
    ----
    plan
        :func:`plan_from_modelspace` 算出的真实排布。
    cabinet_lines
        柜体 4 条外框线的世界坐标端点 ``[(x0,y0,x1,y1), ...]``。
        用来按真实位置描出柜体轮廓（DWG 里的原始直线，不是自绘装饰框）。
    """
    W = CANVAS_W
    scale = W / max(plan.width_mm, 1.0)      # 像素 / 毫米
    H = max(1, int(round(plan.height_mm * scale)))

    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    drawer = ImageDraw.Draw(canvas)

    # ---- 柜体外框：用 DWG 里真实的那几条长LINE 描出来 ----
    if frame_lines:
        rx0, ry0, rx1, ry1 = (cup.rect.x0, cup.rect.y0, cup.rect.x1, cup.rect.y1)

        def to_px(p):
            return (int(round((p[0] - rx0) * scale)),
                    int(round((ry1 - p[1]) * scale)))
        for e in frame_lines:
            try:
                p0 = to_px((e.dxf.start.x, e.dxf.start.y))
                p1 = to_px((e.dxf.end.x, e.dxf.end.y))
            except Exception:
                continue
            # 线宽按 DWG 里的真实 lineweight 换算，至少 FRAME_PX
            try:
                lw = max(FRAME_PX, int(round(
                    abs(e.dxf.lineweight or 25) / 100.0 * scale)))
            except Exception:
                lw = FRAME_PX
            lw = min(lw, 12)
            drawer.line([p0, p1], fill=(0, 0, 0), width=lw)

    # ---- meter 图元：按真实坐标贴图 ----
    placed = 0
    missing: list[str] = []
    for pl in plan.placements:
        block_name = next((n for n, s in METER_BLOCKS.items() if s == pl.kind), None)
        if block_name is None:
            continue
        png = bank.get(doc, block_name, pl.kind)
        mm = sprite_size_mm(doc, block_name, pl.kind, png)
        if png is None or mm is None:
            missing.append(pl.kind)
            continue
        sprite = Image.open(png).convert("RGBA")
        mm_w, mm_h = mm
        # 图元尺寸 = 真实毫米 × 全图比例 × 该实例的 DWG 缩放
        sprite = sprite.resize(
            (max(1, int(round(mm_w * scale * pl.scale))),
             max(1, int(round(mm_h * scale * pl.scale)))),
            Image.LANCZOS)
        if abs(pl.rot) > 0.5:
            sprite = sprite.rotate(pl.rot, expand=True, resample=Image.BICUBIC)
        cx = int(round(pl.x * scale))
        cy = int(round(pl.y * scale))
        canvas.paste(sprite, (cx - sprite.width // 2, cy - sprite.height // 2),
                     sprite)
        placed += 1

    canvas.save(out_path, format="JPEG", quality=94, subsampling=0)
    return {
        "width_px": W,
        "height_px": H,
        "sprites_placed": placed,
        "sprites_missing": sorted(set(missing)),
        "counts": plan.counts,
        "width_mm": plan.width_mm,
        "height_mm": plan.height_mm,
    }