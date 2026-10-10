"""柜型图合成 —— **只用 DWG 里的真实图元，绝不自己绘制图形**。

背景（用户诉求 #F）
--------------------
用户原话：「这个图，我之前的要求是需要使用 dwg 里面的原来的图，相当于从
dwg 导出的，可以使用这个 water meter 和 gas meter 作为原始的元素」。

早期实现是把柜体区域内的所有实体丢给 matplotlib 整柜渲染，结果：
  · 图里混进了建筑底图线（墙线、图框、标注引线），柜型不干净；
  · 用户想看的是「这个柜里有几个 water meter、几个 gas meter」，
    整柜渲染只能看到一堆糊在一起的线。

本模块的做法
------------
1. **抽图元**：把 DWG 里的 4 个 meter 块（``water meter v`` /
   ``water meter h`` / ``gas meter 1`` / ``h-meter``）各自渲染成一张
   透明底 PNG。这些 PNG 是DXF 矢量图元的直接光栅化，**一个线条都没画**。
2. **量排布**：在 modelspace 里统计每个柜体矩形内4 类meter 的 INSERT 实例
   坐标，得到真实的行列位置。
3. **合成**：按真实尺寸比例把图元 PNG 贴到对应位置，输出柜型图。

图元 PNG 有缓存（按源 DXF 的 mtime + 块名做 key），所以 23 个柜型
不会重复渲染同一个 meter。
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: DWG 里代表各仪表的块名 → 内部简称。
#: 注意大小写与空格都必须与 DXF 完全一致（实测就是这个写法）。
METER_BLOCKS = {
    "water meter v": "water_v",
    "water meter h": "water_h",
    "gas meter 1": "gas",
    "h-meter": "hot",
}

#: 图元 PNG 的缓存目录。
#:
#: 必须是**项目根**下的绝对路径，不能用相对路径 —— 这个值会在
#: uvicorn worker、pytest、CLI、fork 子进程里被用到，而它们的 CWD
#: 并不一致（实测从 backend/ 跑就落到了 backend/var/sprites）。
#: 项目根 = 本文件向上三层（app/parsers/x.py -> backend -> 仓库根）。
SPRITE_DIR = Path(__file__).resolve().parents[3] / "var" / "sprites"

#: 图元渲染 DPI。300 是为了让 meter 的表盘/阀门在放大后依然锐利；
#: 合成时会按真实毫米尺寸缩放，所以高 DPI 只影响清晰度、不影响布局。
SPRITE_DPI = 300


@dataclass
class MeterPlacement:
    """柜内一个 meter 实例的落位。

    坐标单位毫米，y 轴向下（与图像坐标系一致）。
    """

    kind: str                 #: water_v / water_h / gas / hot
    x: float                  #: 图元中心 x（相对柜体左下角）
    y: float                  #: 图元中心 y
    rot: float = 0.0          #: 绕自身中心的旋转角（度）
    scale: float = 1.0        #: DWG 里该实例的缩放（1.0 = 原尺寸）


@dataclass
class CabinetPlan:
    """一个柜型该画成什么样 —— 全部来自 DWG 实测，没有臆测。"""

    width_mm: float
    height_mm: float
    placements: list[MeterPlacement] = field(default_factory=list)
    #: 实测行数 / 列数（由表位坐标聚类得出，不是估的）
    rows: int = 0
    cols: int = 0

    @property
    def counts(self) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for p in self.placements:
            c[p.kind] += 1
        return dict(c)


class SpriteBank:
    """meter 图元 PNG 的按需渲染 + 磁盘缓存。"""

    def __init__(self, sprite_dir: Path | None = None):
        self.dir = Path(sprite_dir or SPRITE_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Path] = {}

    def key(self, doc, short: str) -> str:
        """缓存键 = 源 DXF 内容摘要 + 图元名。
        用内容摘要而不是 mtime，避免同一文件改时间戳就重新渲染。"""
        h = hashlib.sha1()
        h.update(doc.dxfversion.encode())
        h.update(doc.header.get("$LASTSAVEDBY", "").encode())
        for name in METER_BLOCKS:
            blk = doc.blocks.get(name)
            if blk is None:
                continue
            h.update(name.encode())
            h.update(str(len(blk)).encode())
            for e in blk:
                h.update(e.dxftype().encode())
                h.update(str(e.dxf.handle).encode())
        return h.hexdigest()[:16]

    def get(self, doc, block_name: str, short: str) -> Path | None:
        """取图元 PNG；块不存在则返回 None（调用方降级，不抛）。"""
        if short in self._cache:
            return self._cache[short]
        if doc.blocks.get(block_name) is None:
            log.warning("DWG 里找不到 meter 块 %r，跳过该图元", block_name)
            return None
        k = self.key(doc, short)
        png = self.dir / f"{short}-{k}.png"
        if not png.exists():
            self._render(doc, block_name, png)
        self._cache[short] = png
        return png

    @staticmethod
    def _render(doc, block_name: str, out: Path) -> None:
        """把一个块渲染成透明底 PNG。

        这里**只是把 DXF 实体交给 ezdxf 的 drawing 前端**，
        等价于 CAD 里「导出块为图片」，不新增任何几何。
        """
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.config import (
            BackgroundPolicy, ColorPolicy, Configuration, LineweightPolicy,
        )
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        from ezdxf.bbox import extents

        cfg = Configuration(
            lineweight_policy=LineweightPolicy.RELATIVE_FIXED,
            lineweight_scaling=1.0,
            # meter 细部线宽只有 5~9（0.05~0.09mm），下限设 6 会把它们
            # 全抬到 0.06mm，在成图上叠成实心黑块。1 才是能看清表盘的值。
            min_lineweight=1,
            color_policy=ColorPolicy.BLACK,
            background_policy=BackgroundPolicy.WHITE,
        )

        tmp = ezdxf_new_doc()
        src = doc.blocks.get(block_name)
        for e in src:
            if _is_group_frame(e, block_name):
                continue
            try:
                tmp.modelspace().add_foreign_entity(e)
            except Exception:
                continue
        ents = list(tmp.modelspace())
        if not ents:
            log.warning("块 %r 克隆后为空", block_name)
            return
        box = extents(ents, fast=True)
        w_mm, h_mm = max(box.size.x, 1.0), max(box.size.y, 1.0)

        # 出图尺寸按真实毫米算：合成时按同一比例缩放，放大不会失真
        w_px = max(64, int(w_mm * SPRITE_DPI / 25.4))
        h_px = max(64, int(h_mm * SPRITE_DPI / 25.4))
        f = plt.figure(figsize=(w_px / SPRITE_DPI, h_px / SPRITE_DPI),
                        dpi=SPRITE_DPI)
        try:
            ax = f.add_axes([0, 0, 1, 1])
            ax.set_axis_off()
            # 透明底：合成时叠在白底柜面上，边缘不会拖出一圈白框
            f.patch.set_alpha(0.0)
            Frontend(RenderContext(tmp), MatplotlibBackend(ax),
                     config=cfg).draw_layout(tmp.modelspace(), finalize=True)
            f.savefig(str(out), format="png", transparent=True,
                      dpi=SPRITE_DPI)
            _trim_sprite_frame(out)
        finally:
            plt.close(f)
        log.info("图元 %r → %s (%dx%d px, %.1fx%.1f mm)", block_name, out.name,
                 w_px, h_px, w_mm, h_mm)


def ezdxf_new_doc():
    import ezdxf

    return ezdxf.new()


def measure_sprite_mm(doc, block_name: str) -> tuple[float, float] | None:
    """图元的真实外接尺寸（mm），用于合成时按真实比例缩放。"""
    from ezdxf.bbox import extents

    blk = doc.blocks.get(block_name)
    if blk is None:
        return None
    try:
        box = extents(list(blk), fast=True)
    except Exception:
        return None
    return max(box.size.x, 1.0), max(box.size.y, 1.0)


def plan_from_positions(cup) -> CabinetPlan:
    """从解析器给出的**真实表位**构造合成方案。

    为什么不用「按套数均分」的猜法：解析器已经把柜内每个表位的
    包围盒和类型（gas / water）都量出来了（``cup.positions``，
    每项都带 ``rect`` 和 ``kind``）。直接用它，图上每个图元的位置
    就对应 DWG 里真实存在的那一个实例。

    排布行/列由表位中心坐标聚类得出（先按 y 分行，再按 x 分列），
    行距列距也取实测值 —— 同样是量出来的，不是定的。
    """
    plan = CabinetPlan(width_mm=float(cup.rect.width),
                       height_mm=float(cup.rect.height))
    rx0, ry0, rx1, ry1 = cup.rect.x0, cup.rect.y0, cup.rect.x1, cup.rect.y1

    #: kind → 图元简称。解析器的 kind 是 gas / water / mixed。
    kind_map = {
        "gas": "gas",
        "water": "water_v",
        "mixed": "gas",
        "hot": "hot",
        "hot_water": "hot",
    }

    for mp in getattr(cup, "positions", []) or []:
        r = mp.rect
        short = kind_map.get((mp.kind or "").lower())
        if short is None:
            continue
        # 表位包围盒的宽高比能区分横装/竖装的水表：
        # DWG 里 water meter h 是 491.9x48.0（横），v 是 48.0x492.4（竖）
        if short == "water_v" and r.width > r.height * 2:
            short = "water_h"
        plan.placements.append(
            MeterPlacement(
                kind=short,
                x=float(r.cx - rx0),
                y=float(ry1 - r.cy),          # 转成图像 y 向下
                rot=0.0,
                scale=1.0,
            )
        )

    # 行数/列数：按 y 聚类（容差取柜高的 8%）
    if plan.placements:
        tol = plan.height_mm * 0.08
        ys = sorted({round(p.y / tol) for p in plan.placements})
        plan.rows = len(ys)
        plan.cols = max(1, -(-len(plan.placements) // plan.rows))
    return plan


def _trim_sprite_frame(png: Path, margin_ratio: float = 0.004) -> None:
    """裁掉图元PNG 自带的矩形边框，只留实际线条。

    为什么必须裁：DWG 里这几个 meter 块外面都套了一个矩形框
    （图纸上表示「这是一组表」的框）。直接贴到柜型图上，每个
    图元都会带一个方框，看着像 26 个小柜子，而不是 26 个表。

    裁完还会把**尺寸基准同步改掉** —— 原来 PNG 的宽高对应块
    的完整包围盒（含外框），裁掉外框后要重新记录实际尺寸，
    否则合成时会按含框尺寸缩放，图元就会偏小。
    """
    from PIL import Image
    import numpy as np

    im = Image.open(png).convert("RGBA")
    a = np.array(im)
    alpha = a[:, :, 3]
    nz = np.argwhere(alpha > 8)
    if nz.size == 0:
        return
    y0, y1 = int(nz[:, 0].min()), int(nz[:, 0].max()) + 1
    x0, x1 = int(nz[:, 1].min()), int(nz[:, 1].max()) + 1
    # 外扩一点margin，避免擦掉最外侧的细线
    mx = int((x1 - x0) * margin_ratio) + 2
    my = int((y1 - y0) * margin_ratio) + 2
    x0 = max(0, x0 - mx); y0 = max(0, y0 - my)
    x1 = min(im.width, x1 + mx); y1 = min(im.height, y1 + my)
    im.crop((x0, y0, x1, y1)).save(png)

    # 尺寸基准跟着改：{简称: (裁后宽mm, 裁后高mm)}
    # 注意不要 clear() —— 4 个图元是**分 4 次**渲染的，
    # 每次都清会把前面几个的尺寸抹掉。
    _SPRITE_MM[png.stem.split("-")[0]] = (
        (x1 - x0) / SPRITE_DPI * 25.4,
        (y1 - y0) / SPRITE_DPI * 25.4,
    )


#: 图元裁边后的真实毫米尺寸（由 _trim_sprite_frame 填充）
_SPRITE_MM: dict[str, tuple[float, float]] = {}


def sprite_size_mm(doc, block_name: str, short: str,
                   png: Path | None = None) -> tuple[float, float] | None:
    """图元**裁边后**的真实尺寸（mm）。

    合成时必须用这个尺寸而不是 :func:`measure_sprite_mm` 的原始包围盒 ——
    后者含DWG 里那个矩形外框，用它缩放会让图元偏小一圈。
    """
    if short in _SPRITE_MM:
        return _SPRITE_MM[short]
    # 缓存命中（PNG 已存在、这次没重新渲染）时，直接从 PNG 像素反算
    if png is None:
        return None
    try:
        from PIL import Image
        with Image.open(png) as im:
            w, h = im.size
    except Exception:
        return measure_sprite_mm(doc, block_name)
    mm = (w / SPRITE_DPI * 25.4, h / SPRITE_DPI * 25.4)
    _SPRITE_MM[short] = mm
    return mm


def _is_group_frame(e, block_name: str) -> bool:
    """判断实体是不是「表组外框」—— 需要剔除，不是仪表本身。

    实测：``gas meter 1`` 块里有一个 **250.0 x 450.0 mm 的闭合
    LWPOLYLINE**，尺寸恰好等于整个块的包围盒，且是 4 点闭合矩形。
    它在图纸上的作用是圈出「这一组表」的范围（层名
    ``G-Anno-Std-Cntr``），**不是 gas meter 的外形**。

    不剔除的后果很直观：合成出来的柜型图上，每个 gas 表都带一个
    大方框，看起来像「13 个小柜子」而不是「13 个表」。

    判定条件（同时满足，避免误伤仪表自身的轮廓线）：
      · 4 点闭合折线
      · 宽高都 > 100mm（仪表轮廓线不会有这么大的单条矩形）
      · 面积占整个块包围盒的 95% 以上（说明它就是块的外框）
    """
    if e.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
        return False
    try:
        pts = list(e.get_points("xy"))
    except Exception:
        return False
    if len(pts) != 4 or not getattr(e, "closed", False):
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if w < 100.0 or h < 100.0:
        return False
    # 与整块包围盒比：占 95% 以上就认定是块的外框
    try:
        from ezdxf.bbox import extents
        blk = e.doc.blocks.get(block_name)
        box = extents(list(blk), fast=True)
        bw, bh = box.size.x, box.size.y
    except Exception:
        #拿不到全块尺寸就退回面积阈值：仪表本身不会占满 250x450
        return w * h > 60000.0
    if bw <= 0 or bh <= 0:
        return False
    return (w * h) / (bw * bh) > 0.95
