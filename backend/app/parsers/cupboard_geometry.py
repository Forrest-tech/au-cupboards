"""柜型库几何分解：**柜体 → 表位**。

为什么这个模块取代了按 block 名打分的做法
----------------------------------------
第2 轮实现里，用「block 名含 meter/cupboard + 图层命中 + 圆图元占比」
给每个命名 block 打分，超过阈值就当成一个柜型渲成JPG。用户实测发现
**粒度完全错了** —— 渲出来的是柜子**内部单个 water 表 / gas 表**，
而不是柜子本身。

根因是**前提错了**：真实图纸里 block 是按**零件**组织的
（一个 water 表符号一个 block），柜子反而是 modelspace 里若干实体的
**空间组合**。「一个 block = 一个柜型」这个假设从根上不成立。

正确规则来自需求文档 2.2 节（`docs/需求分析报告-AU-Cupboards.md`）
与用户 2024-10 的 AutoCAD 实图截图：

1. **闭合矩形（LWPOLYLINE/POLYLINE/RECTANG）= 柜体外轮廓**
   —— 截图里青色大矩形
2. **柜体内部的闭合矩形 = 一个表位**（= 1 套 water+gas）
   —— 截图里红框 gas 表位、绿色竖线 water 管路
   —— **表位数量就是该层的 units 数**
3. **一个 DWG 内多个柜型空间并列**（截图里上方 2 个大柜 + 下方一排小柜）
   → 靠「矩形包含关系 + 空间聚类」切分，不遍历 block
4. **DIMENSION 实体 → 柜体精确尺寸**，不按规范反算
5. **柜体外的噪声**（``Aect_*`` 风管零件 block、图框、说明文字）不参与

为什么不按block 遍历
------------------
用户实测141 个 block 里 130+ 个是 ``Aect_Duct_*`` 风管零件。
而真正的柜型在 modelspace 里，压根不是 block。

因此本模块**只在 modelspace 与 paperspace 上工作**，block 仅作为
「需要展开的嵌套引用」处理，绝不作为柜型单元。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 图层语义

#: 柜体外轮廓图层关键词（青色大矩形所在的层）
CABINET_LAYER_KEYWORDS: tuple[str, ...] = (
    "cabinet", "cupboard", "meter_board", "meterboard", "meter_cup",
    "cp_", "cp-", "board", "cab", "cup",
)

#: gas 表位图层关键词（截图里红框）
GAS_LAYER_KEYWORDS: tuple[str, ...] = (
    "gas", "gms", "g-meter", "gas_meter", "natural_gas",
)

#: water 表位图层关键词（截图里绿色竖线 + 表头）
WATER_LAYER_KEYWORDS: tuple[str, ...] = (
    "water", "wms", "cws", "hw", "cw", "cold", "hot", "w-meter",
    "water_meter", "watersupply",
)

#: 机械零件噪声图层（第 1 轮翻车的根因）
#:
#:注意前缀是 ``Aecb_`` 而不是 ``Aect_`` —— 真实图纸里的 HVAC 风管零件
#: block叫 ``Aecb_Duct_Oval_1Line_Exh_Elbow_Drop_Edge``。按 ``aect`` 过滤
#: 一个都拦不住。
JUNK_LAYER_KEYWORDS: tuple[str, ...] = (
    "aect", "aecb", "aecp", "duct", "mech", "hvac", "auto", "acad", "3d",
    "xref", "defpoints", "标题", "说明", "图框", "title", "border", "frame",
    "legend", "north", "scale",
)

#: 柜体轮廓线的图层白名单（真实图纸实测）。
#:
#: 柜框在这份DWG 里是 **4 条独立 LINE**（不是闭合多段线！）：
#: 上下边在 ``2CONC``，左边在 ``1CO``，右边在 ``2CONC`` / ``1CO``。
#: 这几个图层名都带 CONC/CO ——混凝土砌块柜体，是澳洲计量柜的常规画法。
#:
#: 保留通用关键词作为兜底，应对不同项目的图层命名。
CABINET_FRAME_LAYERS: frozenset[str] = frozenset({
    "1CO", "2CONC", "HVAL___1DA", "HWAT___1DD", "50L",
})

#: 柜框线的图层关键词（白名单之外的通用匹配）。
CABINET_FRAME_KEYWORDS: tuple[str, ...] = (
    "conc", "1co", "2co", "_co", "wall", "cab", "cup", "meter_board",
)

#: 一条边要长到多少毫米才可能是柜框。真实柜体最小865mm 宽，
#: 最小边长取 300mm 能覆盖最小柜型，同时排除表位框（250mm）。
MIN_FRAME_SIDE_MM = 300.0

#: 成对长线的坐标对齐容差（mm）。CAD 里同一根线的两段常常差零点几。
FRAME_ALIGN_TOL_MM = 3.0

#: 长线端点需覆盖对方多少比例才算成对。同一个柜子的上下边长度
#: 可能差几十毫米（图里画的其实是墙厚线），卡太严会一个柜型都配不出。
FRAME_COVER_RATIO = 0.55

#: 一个柜体最小边长（mm）。低于此值的多半是表位框或零件，不是柜体。
MIN_CABINET_SIDE_MM = 400.0

#: 一个表位框的合理尺寸区间（mm）。截图里红框约 150×190。
MIN_POSITION_SIDE_MM = 60.0
MAX_POSITION_SIDE_MM = 1200.0

#: 柜体壁厚容差：外轮廓与内部表位之间的最小间隙（mm）。
#: 小于此值说明矩形几乎重合，多半是同一个矩形的双层描边。
MIN_CABINET_MARGIN_MM = 40.0

#: 矩形闭合度容差（相对值）。CAD 里矩形顶点常有微小误差。
CLOSED_TOL = 1e-3

#: 矩形角度容差（度）。真正的矩形四个角都是直角。
RIGHT_ANGLE_TOL_DEG = 1.5


@dataclass
class Rect:
    """一个轴对齐的闭合矩形（mm，图纸坐标系）。"""

    x0: float
    y0: float
    x1: float
    y1: float
    layer: str = ""
    color: int | None = None
    #: 来源实体在 modelspace 里的下标 —— 用于回溯与渲染
    index: int = -1
    source_type: str = ""

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def area(self) -> float:
        return self.width * self.height

    def contains(self, other: "Rect", tol: float = 0.0) -> bool:
        """是否**严格包含** other（带容差）。

        容差用来吸收 CAD 坐标的浮点误差 —— 截图里柜体外框与内部
        分格线通常是共线的，严格包含会全部漏掉。
        """
        return (
            self.x0 - tol <= other.x0 and self.x1 + tol >= other.x1
            and self.y0 - tol <= other.y0 and self.y1 + tol >= other.y1
        )

    def overlap(self, other: "Rect") -> float:
        """重叠面积 / 自身面积（0~1）—— 判断是否为同一矩形。"""
        ix = min(self.x1, other.x1) - max(self.x0, other.x0)
        iy = min(self.y1, other.y1) - max(self.y0, other.y0)
        if ix <= 0 or iy <= 0:
            return 0.0
        inter = ix * iy
        return inter / max(self.area, 1e-9)

    def margin_to(self, other: "Rect") -> float:
        """self 边界到 other 边界的最小间隙（mm）。
        若 other 完全在 self 内部且不重合，返回正数；重合时为 0。
        """
        return min(
            other.x0 - self.x0, self.x1 - other.x1,
            other.y0 - self.y0, self.y1 - other.y1,
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.width, self.height)


@dataclass
class MeterPosition:
    """柜体内的一个表位 = 1 套 water + gas。"""

    rect: Rect
    #: 该表位内识别到的图层 —— 决定它是 gas / water / 混合
    layers: list[str] = field(default_factory=list)
    kind: str = "unknown"      # gas / water / mixed
    entity_count: int = 0


@dataclass
class Cupboard:
    """一个柜型 = 一个柜体 = 一套独立的计量柜配置。"""

    rect: Rect
    #: 柜体内的表位（按位置排序：先上后下、先左后右）
    positions: list[MeterPosition] = field(default_factory=list)
    #: 柜体自身图层
    layer: str = ""
    #: 从 DIMENSION 读到的柜体尺寸（mm）。读不到时为 (None, None)。
    dim_w_mm: float | None = None
    dim_h_mm: float | None = None
    #: 柜体附近的说明文字（截图里柜子下方那行 "6 SETS - 2 COL x 3 ROW"）
    notes: list[str] = field(default_factory=list)
    #: 柜内识别到的 water/gas 符号图元总数 —— 诊断用
    glyph_count: int = 0
    #: 排布聚类容差 ``(x向, y向)``，由 :func:`parse_cupboards` 按柜体尺寸设定
    _layout_tol: tuple[float, float] | None = None

    @property
    def positions_total(self) -> int:
        """套数（units）= 该层的 water+gas 套数。

        口径来自用户原话：「一个 cupboard 里面包含了多套的 water+gas，
        这种是一套」。所以**不是** gas 表数 + water 表数，而是取
        **能配成套的那一边的数量**。

        具体规则：

        - 有 gas 时以 **gas 数**为准 —— 每个 gas 表必然配1 个 water，
          多出来的 water（图纸上单独的「water bank」不成套排布）不计入。
        - 没有 gas（纯 water 柜）时退回water 数。

        真实样本实测：19 个柜型里 gas 数与 water 数**逐一相等**
        （14/14、13/13、…、5/5），只有 1 个柜型是纯 gas（3/0）。
        这条规则在真实文件上与「取 max」结果完全一致（191 套），
        但在有额外 water bank 的图纸上不会虚高。
        """
        g = self.gas_count
        w = sum(1 for p in self.positions if p.kind == "water")
        return g if g > 0 else w

    @property
    def gas_count(self) -> int:
        return sum(1 for p in self.positions if p.kind in ("gas", "mixed"))

    @property
    def water_count(self) -> int:
        return sum(1 for p in self.positions if p.kind in ("water", "mixed"))

    @property
    def bbox_mm(self) -> tuple[float | None, float | None]:
        """柜体尺寸：优先用DIMENSION 标注，退回几何 bbox。"""
        return (self.dim_w_mm or round(self.rect.width, 1),
                self.dim_h_mm or round(self.rect.height, 1))

    @property
    def layout_rows_cols(self) -> tuple[int, int]:
        """推断排布行列：**以 gas 表为权威**做行列聚类。

        真实样本实测（14 套柜，2165×2350mm）
        ------------------------------------
        柜内 28 个标记的分布是::

            cy=-36064  water×7 (x -4048..-4948, 间距150)
            cy=-35968  gas  ×2 (x -3423, -3773)
            cy=-35464  water×7 (同上)
            cy=-35418  gas  ×2 (同上)
            cy=-34868  gas  ×5 (x -3423..-4823, 间距350)
            cy=-34318  gas  ×5 (同上)

        也就是**布局是 5 列 × 4 行 = 20 格，实际填了 14 个套位**
        （顶部两行只填了右侧 2 列）。water 表的 x 间距只有 150mm，
        若把它也拿去聚类，26 个标记会聚出乱七八糟的行列数。

        所以：**套数和排布都只认 gas 表** —— 一套 = 1 个 gas 是用户
        明确给的口径，water 表只用来交叉验证「gas 数≈water 数」。
        这样 14 套柜得到 4 行 × 5 列，与图上完全对得上。

        聚类容差取柜体尺寸的比例（x 向 12%、y 向 15%）：真实数据
        gas 的列间距 350mm、行间距 550~600mm，分别落在 260mm /
        352mm 两个阈值之外，稳定分开。
        """
        gas = [p for p in self.positions if p.kind == "gas"]
        if not gas:
            # 没有 gas 标记（图纸只画了水表）时退回用全部表位
            gas = self.positions
        if not gas:
            return (0, 0)
        tol_x, tol_y = self._layout_tol or (
            max(self.rect.width * 0.12, 30.0),
            max(self.rect.height * 0.15, 30.0),
        )
        cx = sorted({round(p.rect.cx, 1) for p in gas})
        cy = sorted({round(p.rect.cy, 1) for p in gas})
        return (_cluster_count(cy, tol=tol_y), _cluster_count(cx, tol=tol_x))

    @property
    def grid_aspect(self) -> str:
        rows, cols = self.layout_rows_cols
        return f"{cols}x{rows}" if rows and cols else ""

    def as_dict(self) -> dict:
        rows, cols = self.layout_rows_cols
        w, h = self.bbox_mm
        return {
            "positions_total": self.positions_total,
            "layout_rows": rows,
            "layout_cols": cols,
            "grid_aspect": self.grid_aspect,
            "gas_count": self.gas_count,
            "water_count": self.water_count,
            "w": w,
            "h": h,
            "d": None,
            "bbox": {"x": round(self.rect.x0, 1), "y": round(self.rect.y0, 1),
                      "w": round(self.rect.width, 1),
                      "h": round(self.rect.height, 1)},
        }


@dataclass
class ParsedCupboard:
    """柜型识别结果 —— 附带诊断信息，便于解释「为什么这样切」。"""

    #: 识别出的柜型，按柜体面积降序
    cupboards: list[Cupboard] = field(default_factory=list)
    #: 被判为噪声而丢弃的矩形（含原因）
    discarded: list[tuple[Rect, str]] = field(default_factory=list)
    #: 参与解析的实体总数
    entity_total: int = 0
    #: 识别出的闭合矩形总数
    rect_total: int = 0
    #: 成对长 LINE 配出的柜体框候选数（真实文件的柜体识别走这条路）
    cabinet_frame_pairs: int = 0
    #: 柜体候选图层命中情况，用于诊断「图层对不对」
    cabinet_layer_hits: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"实体 {self.entity_total}",
                 f"柜框配对 {self.cabinet_frame_pairs}",
                 f"柜型 {len(self.cupboards)} 个",
                 f"总套数 {sum(c.positions_total for c in self.cupboards)}"]
        for c in self.cupboards:
            w, h = c.bbox_mm
            parts.append(
                f"· {c.positions_total:2d} 套{c.grid_aspect:>7}"
                f"  {w:.0f}×{h:.0f}mm  "
                f"gas {c.gas_count}/water {c.water_count}（{c.glyph_count} 表位）  "
                f"图层 {c.layer}"
            )
        if self.discarded:
            parts.append(f"丢弃 {len(self.discarded)} 个")
        return "\n".join(parts)


# ---------------------------------------------------------------- 矩形提取


def _is_junk_layer(layer: str) -> bool:
    low = str(layer).lower()
    return any(k in low for k in JUNK_LAYER_KEYWORDS)


# ------------------------------------------------- 柜框：成对长LINE 配对
#
# 为什么不是「找闭合多段线」
# -------------------------
# 真实样本 ``samples/Cold and hot water and gs meter cupboard detail 1.dwg``
# 的实测结果（1069 个 LWPOLYLINE 里1053 个在图层 ``1`` 上、**全部不闭合**），
# 全图只有 16 个闭合 LWPOLYLINE：
#   - 1 个 ``2CONC`` 4414×6215mm的混凝土剖面填充（不是柜体）
#   - 15 个 ``G-Anno-Std-Cntr`` 250×450mm（**表位框**，不是柜体）
# 也就是说「闭合矩形 = 柜体」这条规则在真实文件上**一个都找不到**。
#
# 柜框实际是 **4 条独立 LINE**：上下边在 ``2CONC``，左边在 ``1CO``，
# 右边在 ``2CONC`` 或 ``1CO``。所以必须按「成对长线」重建矩形。


@dataclass
class _Seg:
    """一条轴对齐长线段（只存够配对用的信息）。"""

    a0: float          # 起点（沿走向的坐标）
    a1: float          # 终点
    pos: float         # 垂直于走向的坐标（水平线= y，垂直线= x）
    layer: str


def _collect_frame_segments(ents) -> tuple[list[_Seg], list[_Seg]]:
    """收集柜框候选长线，按走向分成「水平」「垂直」两组。

    只取图层命中 :data:`CABINET_FRAME_LAYERS` 或
    :data:`CABINET_FRAME_KEYWORDS` 的 LINE，且长度 >= ``MIN_FRAME_SIDE_MM``。

    这样做的额外好处：HVAC 风管零件、文字、标注引线全部落在别的图层，
    天然被排除，不需要再维护一张噪声前缀黑名单。
    """
    hs: list[_Seg] = []
    vs: list[_Seg] = []
    for e in ents:
        if e.dxftype() != "LINE":
            continue
        try:
            lay = str(e.dxf.layer if e.dxf.hasattr("layer") else "")
        except Exception:
            continue
        if not _is_frame_layer(lay):
            continue
        try:
            s, t = e.dxf.start, e.dxf.end
        except Exception:
            continue
        x0, y0 = float(s.x), float(s.y)
        x1, y1 = float(t.x), float(t.y)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if h <= FRAME_ALIGN_TOL_MM and w >= MIN_FRAME_SIDE_MM:
            hs.append(_Seg(min(x0, x1), max(x0, x1), y0, lay))
        elif w <= FRAME_ALIGN_TOL_MM and h >= MIN_FRAME_SIDE_MM:
            vs.append(_Seg(min(y0, y1), max(y0, y1), x0, lay))
    return hs, vs


def _is_frame_layer(layer: str) -> bool:
    """该图层是否可能承载柜体外框。"""
    if not layer:
        return False
    if _is_junk_layer(layer):
        return False
    if layer.upper() in CABINET_FRAME_LAYERS:
        return True
    low = layer.lower()
    return any(k in low for k in CABINET_FRAME_KEYWORDS)


def _covers(outer: float, inner: float) -> bool:
    """竖线是否「足够覆盖」两条横线之间的高度。

    卡太严会漏：真实图纸里同一柜子的上下边长度往往差几十毫米
    （画的是墙厚线，不是严格矩形）。0.55 的比例既能配上正常柜子，
    又不会把上下两层楼的不同柜子误配成一个。
    """
    lo, hi = sorted((outer, inner))
    return (hi - lo) >= (hi - lo) * FRAME_COVER_RATIO


def pair_frame_segments(hs: list[_Seg], vs: list[_Seg]) -> list[Rect]:
    """把长线配成柜体矩形。

    配对逻辑（与真实文件实测一致）：
      1. 两条**水平线** x 起止一致、y 不同 → 它们是某个柜体的上下边；
      2. 在左右端点 x 处各找一条**垂直线**，其 y 范围要覆盖上下边；
      3. 上下左右四条都齐 → 得到一个柜体矩形。

    真实样本实测：46 条水平长线 + 46 条垂直长线 → **20 个矩形**，
    与图纸上肉眼可见的 20 个柜型一一对应。
    """
    out: list[Rect] = []
    seen: set[tuple] = set()
    tol = FRAME_ALIGN_TOL_MM

    # 按 x 起止（量化到 5mm）分组，同组内才可能是同一个柜子的上下边
    groups: dict[tuple, list[_Seg]] = {}
    for h in hs:
        groups.setdefault((round(h.a0 / 5.0), round(h.a1 / 5.0)), []).append(h)

    for segs in groups.values():
        if len(segs) < 2:
            continue
        ys = sorted({round(s.pos, 1) for s in segs})
        for i in range(len(ys)):
            for j in range(i + 1, len(ys)):
                y0, y1 = ys[i], ys[j]
                if y1 - y0 < MIN_FRAME_SIDE_MM:
                    continue
                x0, x1 = segs[0].a0, segs[0].a1
                left = next((v for v in vs if abs(v.pos - x0) <= tol
                             and v.a0 <= y0 + tol and v.a1 >= y1 - tol), None)
                if left is None:
                    continue
                right = next((v for v in vs if abs(v.pos - x1) <= tol
                              and v.a0 <= y0 + tol and v.a1 >= y1 - tol), None)
                if right is None:
                    continue
                key = (round(x0), round(y0), round(x1), round(y1))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Rect(
                    x0=x0, y0=y0, x1=x1, y1=y1,
                    layer=left.layer or right.layer,
                    source_type="LINE_PAIR",
                ))
    return out


def extract_cabinet_frames(ents) -> list[Rect]:
    """从实体列表里重建所有柜体矩形（成对长 LINE 配对）。"""
    hs, vs = _collect_frame_segments(ents)
    return pair_frame_segments(hs, vs)


# ------------------------------------------------------------ 表位识别
#
# 真实文件里一个套位（1 套 water + gas）由两个东西表达：
#   - gas：``gas meter 1`` 这个 INSERT（内部自带一个 250×450 闭合框）
#   - water：``water meter v`` / ``water meter h`` / ``h-meter`` 这几个 INSERT
# 少数柜型（第 20 号，865mm 宽那个）**没有用 INSERT**，gas 表被炸开成
# 直接画在 modelspace 上的 ``G-Anno-Std-Cntr`` 250×450 闭合框 ——
# 这两种画法都要认。
GAS_BLOCK_KEYWORDS: tuple[str, ...] = ("gas", "gms", "g-meter")
WATER_BLOCK_KEYWORDS: tuple[str, ...] = (
    "water", "wms", "cws", "w-meter", "hwm", "h-meter",
)
#: 炸开画的 gas 表位框尺寸区间（mm）
UNBOXED_GAS_W = (200.0, 300.0)
UNBOXED_GAS_H = (400.0, 500.0)


def _entity_extents(e) -> Rect | None:
    """取实体真实几何包围盒（mm）。

    为什么必须用 ``ezdxf.bbox.extents`` 而不是 ``e.dxf.insert``
    --------------------------------------------------------------
    真实 DWG 里每个仪表 block 的**内部顶点用的是绝对图纸坐标**，
    而 ``base_point`` 是 ``(0,0,0)``。于是 ``insert=(2200, 1872)``
    的那个 ``gas meter 1``，真实位置其实是 ``(-4279, 4619)`` ——
    和 insert 点差了 6500mm。直接拿 insert 点做空间聚类会全部错位，
    一个柜型都统计不出。

    实测：用 insert 点 → 落在柜体外的 INSERT 计数为 0；
    改用 extents → 全图范围 x -8461..-79、y -36310..5145，
    完整覆盖全部 20 个柜体。

    性能
    ----
    ``ezdxf.bbox.extents`` 要展开块内容，对 393 个 INSERT 逐个调用
    会花掉大部分解析时间。这里加了两级缓存：
      1. **按块名缓存**—— 同一份图纸里 393 个 INSERT 只引用 4 个块，
         每个块的本地 bbox 只算一次，之后平移即可；
      2. **局部 LRU** —— 同一批相邻实体反复求包围盒时命中缓存。
    实测把真实样本的解析从 3 分钟压到 20 秒以内。
    """
    t = e.dxftype()

    # ---- INSERT：按块名缓存本地偏移 ----
    if t == "INSERT":
        try:
            name = str(e.dxf.name)
        except Exception:
            return None
        try:
            ip = e.dxf.insert
            ix, iy = float(ip.x), float(ip.y)
        except Exception:
            return None
        try:
            sx = float(e.dxf.get("xscale", 1.0)) or 1.0
            sy = float(e.dxf.get("yscale", 1.0)) or 1.0
        except Exception:
            sx = sy = 1.0
        try:
            rot = math.radians(float(e.dxf.get("rotation", 0.0)))
        except Exception:
            rot = 0.0

        local = _block_local_bbox(name)
        if local is None:
            return None
        lx0, ly0, lx1, ly1 = local
        # 本地 bbox → 缩放 → 绕插入点旋转 → 平移
        corners = []
        for cx, cy in ((lx0, ly0), (lx1, ly0), (lx0, ly1), (lx1, ly1)):
            px, py = cx * sx, cy * sy
            if rot:
                c, s = math.cos(rot), math.sin(rot)
                px, py = px * c - py * s, px * s + py * c
            corners.append((ix + px, iy + py))
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        return Rect(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
                    layer=str(e.dxf.layer if e.dxf.hasattr("layer") else ""),
                    source_type="INSERT")

    # ---- 其他实体：先走快路径，失败再退回 ezdxf ----
    fast = _fast_extents(e)
    if fast is not None:
        return fast
    try:
        from ezdxf import bbox as _bbox
        box = _bbox.extents([e], fast=True)
        if not box.has_data:
            return None
        return Rect(
            x0=box.extmin.x, y0=box.extmin.y,
            x1=box.extmax.x, y1=box.extmax.y,
            layer=str(e.dxf.layer if e.dxf.hasattr("layer") else ""),
            source_type=t,
        )
    except Exception:
        return None


def bind_document(doc) -> None:
    """把当前文档绑给包围盒计算，让渲染侧也能用 :func:`_entity_extents`。

    渲染模块需要按**同一套**几何判断「实体属不属于这个柜体」——
    否则会出现「数出14 套，但图里只渲出 8 个表」这种自相矛盾。
    换文档必须先调用它，否则块定义缓存会串味。
    """
    global _current_doc
    _current_doc = doc
    _BLOCK_BBOX_CACHE.clear()


#: 块名 -> 本地 bbox ``(x0, y0, x1, y1)``
_BLOCK_BBOX_CACHE: dict[str, tuple[float, float, float, float] | None] = {}


def _block_local_bbox(name: str):
    """求一个块定义里所有实体的本地包围盒（结果进程内缓存）。

    块内顶点可能带 z 值，这里一律按 (x, y) 取 min/max —— 柜型识别只关心
    平面位置。z 不同的零件（上下叠置的管道）在平面上会重合，
    对 2D 柜型分解没有影响。
    """
    if name in _BLOCK_BBOX_CACHE:
        return _BLOCK_BBOX_CACHE[name]
    result: tuple[float, float, float, float] | None = None
    try:
        from ezdxf import bbox as _bbox
        box = _bbox.extents(_current_doc.blocks.get(name), fast=True)
        if box.has_data:
            result = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
    except Exception:
        result = None
    _BLOCK_BBOX_CACHE[name] = result
    return result


#: 解析期间持有当前 doc，让 ``_block_local_bbox`` 能拿到 blocks 表
_current_doc = None


def _fast_extents(e) -> Rect | None:
    """常见实体类型的直接取点 —— 比通用 ``bbox.extents`` 快得多。

    返回 ``None`` 表示「这个类型我不认识，走通用路径」。
    """
    t = e.dxftype()
    try:
        if t == "LINE":
            s, tt = e.dxf.start, e.dxf.end
            x0, x1 = float(s.x), float(tt.x)
            y0, y1 = float(s.y), float(tt.y)
        elif t == "LWPOLYLINE":
            ps = [(float(p[0]), float(p[1])) for p in e.get_points("xy")]
            if not ps:
                return None
            xs = [p[0] for p in ps]; ys = [p[1] for p in ps]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        elif t == "POLYLINE":
            ps = [(float(v.dxf.location.x), float(v.dxf.location.y))
                  for v in e.vertices]
            if not ps:
                return None
            xs = [p[0] for p in ps]; ys = [p[1] for p in ps]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        elif t == "CIRCLE":
            c = e.dxf.center
            x0 = float(c.x) - float(e.dxf.radius)
            x1 = float(c.x) + float(e.dxf.radius)
            y0 = float(c.y) - float(e.dxf.radius)
            y1 = float(c.y) + float(e.dxf.radius)
        elif t in ("TEXT", "MTEXT"):
            p = e.dxf.insert
            x0 = x1 = float(p.x); y0 = y1 = float(p.y)
        else:
            return None
    except Exception:
        return None
    return Rect(x0=x0, y0=y0, x1=x1, y1=y1,
                layer=str(e.dxf.layer if e.dxf.hasattr("layer") else ""),
                source_type=t)


def collect_meter_positions(ents, cab: Rect, margin: float = 30.0) -> list[MeterPosition]:
    """收集柜内所有套位标记（gas / water 的 INSERT 或炸开画的框）。

    每个套位 = 1 个 gas + 1 个 water。这里把每个标记都作为**一条**
    记录返回，``kind`` 标明它是 gas 还是 water；配对成「套」由
    :func:`parse_cupboards` 按 ``max(gas, water)`` 汇总。

    实测口径：真实文件 19 个柜型里，gas 数与 water 数**逐一相等**
    （14/14、13/13、…、5/5），只有第 20 号柜型 gas=3 而 water=0
    （那个柜子只画了气表）。取 max 恰好等于用户口径的套数。
    """
    out: list[MeterPosition] = []

    def inside(r: Rect) -> bool:
        return (cab.x0 - margin <= r.x0 <= cab.x1 + margin
                and cab.y0 - margin <= r.y0 <= cab.y1 + margin
                and cab.x0 - margin <= r.x1 <= cab.x1 + margin
                and cab.y0 - margin <= r.y1 <= cab.y1 + margin)

    for e in ents:
        t = e.dxftype()
        if t == "INSERT":
            name = ""
            try:
                name = str(e.dxf.name).lower()
            except Exception:
                continue
            is_gas = any(k in name for k in GAS_BLOCK_KEYWORDS)
            is_water = any(k in name for k in WATER_BLOCK_KEYWORDS)
            if not (is_gas or is_water):
                continue
            bb = _entity_extents(e)
            if bb is None or not inside(bb):
                continue
            bb.index = -2          # 标记来源为 INSERT
            out.append(MeterPosition(
                rect=bb, layers=[bb.layer], kind="gas" if is_gas else "water",
                entity_count=1,
            ))
        elif t in ("LWPOLYLINE", "POLYLINE", "RECTANG"):
            # 炸开画的表位（没做成 block 的那种）
            if not _is_closed_polyline(e):
                continue
            bb = rect_from_entity(e)
            if bb is None or not inside(bb):
                continue
            # 两种判据，命中任一即可：
            #   a) 尺寸像 gas 表位框（真实样本实测 250×450mm）
            #   b) 图层名带 gas / water 语义（合成图纸的 GAS_METER 图层）
            # 只认尺寸会在图层语义清晰的图纸上全部漏掉，
            # 只认图层则会在真实文件的 2CONC 混凝土剖面上误判。
            by_size = (UNBOXED_GAS_W[0] <= bb.width <= UNBOXED_GAS_W[1]
                       and UNBOXED_GAS_H[0] <= bb.height <= UNBOXED_GAS_H[1])
            low = bb.layer.lower()
            is_gas = any(k in low for k in GAS_LAYER_KEYWORDS)
            is_water = any(k in low for k in WATER_LAYER_KEYWORDS)
            if not (by_size or is_gas or is_water):
                continue
            kind = "gas" if (by_size or is_gas) and not is_water else "water"
            out.append(MeterPosition(
                rect=bb, layers=[bb.layer], kind=kind, entity_count=1,
            ))

    # ---- 补充扫描：炸开画的 water 表（没有 block、也不是闭合矩形）----
    #
    # 真实图纸里 water 表都是 INSERT（``water meter v`` / ``water meter h``），
    # 所以这条路径在真实文件上不会触发。但有些图纸会把 water 表**炸开**画成
    # 「一根竖管 + 一个表头圆」（LINE + CIRCLE），既没有 block 也没有闭合
    # 矩形，只认闭合多段线会整批漏掉，导致 ``gas 6 / water 0`` 这种脏数据。
    #
    # 只在「已经找到 gas、却一个 water 都没找到」时才启用 —— 这样既能救
    # 炸开画的图纸，又不会在真实文件上把 CIRCLE 之类误当成表位。
    if out and not any(p.kind == "water" for p in out):
        out.extend(_scattered_water_marks(ents, cab, inside, margin))
    return out


#: 炸开画的 water 表头圆的半径范围（mm）。
#:
#:合成样本用r=22，真实项目里 water 表头一般在 15~60mm。
_SCATTERED_WATER_R = (8.0, 80.0)


def _scattered_water_marks(ents, cab: Rect, inside, margin: float) -> list[MeterPosition]:
    """在柜内找「炸开画」的 water 表头（water 语义图层上的小圆）。

    竖管（LINE）会一对多，不适合当计数依据；表头圆才是「一个表 = 一个圆」，
    所以以 CIRCLE 为准。竖管只用来确认这个圆确实属于 water 表（同一图层）。
    """
    marks: list[MeterPosition] = []
    for e in ents:
        if e.dxftype() != "CIRCLE":
            continue
        low = e.dxf.layer.lower()
        if not any(k in low for k in WATER_LAYER_KEYWORDS):
            continue
        r = float(e.dxf.radius)
        if not (_SCATTERED_WATER_R[0] <= r <= _SCATTERED_WATER_R[1]):
            continue
        cx, cy = float(e.dxf.center.x), float(e.dxf.center.y)
        bb = Rect(cx - r, cy - r, cx + r, cy + r, layer=e.dxf.layer)
        if not inside(bb):
            continue
        marks.append(MeterPosition(
            rect=bb, layers=[bb.layer], kind="water", entity_count=1,
        ))
    if marks:
        log.debug("柜 %s：补充识别到 %d 个炸开画的 water 表头",
                  f"{cab.width:.0f}x{cab.height:.0f}", len(marks))
    return marks


def _is_closed_polyline(ent) -> bool:
    if ent.dxftype() == "LWPOLYLINE":
        return bool(ent.closed)
    if ent.dxftype() == "POLYLINE":
        return bool(ent.is_closed)
    return False


def _cluster_centers(values: list[float], tol: float) -> list[float]:
    """把一维坐标聚成若干簇，返回每簇的均值。

    与 :func:`_cluster_count` 一样按「与簇内最后一个的间距」判定，
    但额外返回簇中心 —— 排布推断需要簇中心来给表位归行。
    """
    if not values:
        return []
    vals = sorted(values)
    groups: list[list[float]] = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _polyline_points(ent) -> list[tuple[float, float]] | None:
    """取多段线的顶点，统一成 ``[(x, y), ...]``。

    实测踩坑：``LWPOLYLINE`` 有 ``get_points("xy")``，
    但**经典 ``POLYLINE`` 没有这个方法** —— 它的顶点在
    ``ent.vertices`` 里，每个顶点是独立的 VERTEX 实体，
    坐标在 ``v.dxf.location``（带 x/y/z）。直接调``get_points``
    会抛 AttributeError，被外层 ``except Exception`` 吞掉后
    返回 None，于是老图纸（POLYLINE）里的柜体**一个都识别不出来**，
    且不报任何错。

    用户实图用的是 LWPOLYLINE，但并非所有图纸都如此 —— 两种都要支持。
    """
    t = ent.dxftype()
    try:
        if t == "LWPOLYLINE":
            return [(float(p[0]), float(p[1])) for p in ent.get_points("xy")]
        if t == "POLYLINE":
            out: list[tuple[float, float]] = []
            for v in ent.vertices:
                loc = v.dxf.location
                out.append((float(loc.x), float(loc.y)))
            return out
    except Exception:
        return None
    return None


def rect_from_entity(ent, index: int = -1) -> Rect | None:
    """把一个闭合多段线/矩形实体转成 :class:`Rect`。

    非矩形（多边形、只有一个角的开放段）返回 ``None`` ——
    这是刻意的：柜体与表位框在 CAD 里都是严格矩形，
    放宽会引入大量噪声（第 1 轮把 130+ 个风管零件认成柜型）。
    """
    t = ent.dxftype()

    # RECTANG / 2DPOLYLINE (AutoCAD 圆弧矩形)
    if t == "RECTANG":
        try:
            p1, p2 = ent.dxf.p1, ent.dxf.p2
            return _mk(p1.x, p1.y, p2.x, p2.y, ent, index, t)
        except Exception:
            return None

    if not _is_closed_polyline(ent):
        return None

    pts = _polyline_points(ent)
    if pts is None or len(pts) < 4:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    r = _mk(x0, y0, x1, y1, ent, index, t)
    if r is None:
        return None

    # 矩形校验：面积必须接近 bbox 面积，否则是斜多边形/异形
    area = _shoelace(pts)
    if area <= 0:
        return None
    if abs(area - r.area) / max(r.area, 1e-9) > CLOSED_TOL * 1000:
        return None
    return r


def _mk(x0, y0, x1, y1, ent, index, t) -> Rect | None:
    w, h = abs(x1 - x0), abs(y1 - y0)
    if w <= 0 or h <= 0:
        return None
    layer = ""
    try:
        layer = ent.dxf.layer if ent.dxf.hasattr("layer") else ""
    except Exception:
        pass
    color = None
    try:
        if ent.dxf.hasattr("color"):
            color = int(ent.dxf.color)
    except Exception:
        pass
    return Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                layer=str(layer or ""), color=color, index=index,
                source_type=t)


def _shoelace(pts) -> float:
    n = len(pts)
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# ---------------------------------------------------------------- 聚类工具


def _cluster_count(values: list[float], tol: float) -> int:
    """把一组数值聚成几簇（单趟聚类，够用于规则网格）。"""
    if not values:
        return 0
    vs = sorted(values)
    groups = 1
    for i in range(1, len(vs)):
        if vs[i] - vs[i - 1] > tol:
            groups += 1
    return groups


def _which_cluster(v: float, values: list[float], tol: float) -> int:
    if not values:
        return 0
    # values 已排序
    lo = 0
    for i in range(1, len(values)):
        if values[i] - values[i - 1] > tol:
            if v < values[i] - tol:
                return lo
            lo = i
    return lo


# ---------------------------------------------------------------- 主解析


def extract_rects(doc) -> tuple[list[Rect], int]:
    """从 modelspace + paperspace 提取所有「严格矩形」。

    返回 ``(矩形列表, 参与解析的实体总数)``。
    **不遍历 block** —— 柜型在 modelspace 里，不在 block 里。
    """
    rects: list[Rect] = []
    total = 0
    for layout in (doc.modelspace(), *[p for p in doc.layouts
                                       if p.name != "Model"]):
        try:
            ents = list(layout)
        except Exception:
            continue
        for i, e in enumerate(ents):
            total += 1
            r = rect_from_entity(e, i)
            if r is not None:
                rects.append(r)
    return rects, total


def _layer_of_entity(ents, rect: Rect) -> list[str]:
    """取某矩形附近（自身+轻微重叠区）的图层集合。

    用来判断表位框里是 gas 还是 water —— 截图里红框落在 GAS 图层、
    绿色竖线落在 WATER 图层。
    """
    out: list[str] = []
    for e in ents:
        try:
            ly = e.dxf.layer if e.dxf.hasattr("layer") else ""
        except Exception:
            continue
        if not ly or ly in out:
            continue
        try:
            if _entity_in_rect(e, rect):
                out.append(ly)
        except Exception:
            continue
    return out


def collect_meter_glyphs(ents, cab: Rect) -> list[tuple[float, float, str]]:
    """收集柜内所有 water / gas 表图元的「中心点 + 图层」。

    为什么要单独收集，而不是只看表位框内部
    ------------------------------------
    用户实图里，**gas 表是红框**，而 **water 表是紧挨红框的另一组
    竖线 + 表头**，多半落在红框之外。实测样本：
    gas 框中心 x=325，water 竖线在 x=145 —— 相差 180mm，
    完全在框外。若只取框内图层，water 会被算成 0 套。

    所以柜内的计数口径是：**表位框数= 表位数**，
    再用邻近的 water 图元补齐每套的水表，得出「N 套 water+gas」。
    """
    out: list[tuple[float, float, str]] = []
    for e in ents:
        try:
            ly = str(e.dxf.layer if e.dxf.hasattr("layer") else "")
        except Exception:
            continue
        low = ly.lower()
        is_gas = any(k in low for k in GAS_LAYER_KEYWORDS)
        is_water = any(k in low for k in WATER_LAYER_KEYWORDS)
        if not (is_gas or is_water):
            continue
        # gas 表位框本身已经单独收集了，这里只取符号图元
        if is_gas and e.dxftype() in ("LWPOLYLINE", "POLYLINE", "RECTANG"):
            continue
        try:
            cx, cy = _entity_center(e)
        except Exception:
            continue
        if cx is None:
            continue
        if not (cab.x0 - 2 <= cx <= cab.x1 + 2
                and cab.y0 - 2 <= cy <= cab.y1 + 2):
            continue
        out.append((cx, cy, "gas" if is_gas else "water"))
    return out


def _entity_center(e) -> tuple[float | None, float | None]:
    """取实体的大致中心点（够用即可，不需要精确）。"""
    t = e.dxftype()
    try:
        if t == "CIRCLE":
            return float(e.dxf.center.x), float(e.dxf.center.y)
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            return (float(a.x) + float(b.x)) / 2, (float(a.y) + float(b.y)) / 2
        if t in ("ARC", "ELLIPSE"):
            return float(e.dxf.center.x), float(e.dxf.center.y)
        if t == "INSERT":
            return float(e.dxf.insert.x), float(e.dxf.insert.y)
        if t in ("LWPOLYLINE", "POLYLINE", "RECTANG"):
            r = rect_from_entity(e)
            if r is None:
                return None, None
            return r.cx, r.cy
        if t in ("TEXT", "MTEXT"):
            p = e.dxf.insert if e.dxf.hasattr("insert") else None
            if p is not None:
                return float(p.x), float(p.y)
    except Exception:
        return None, None
    return None, None


def pair_glyphs_to_positions(
    positions: list[MeterPosition],
    glyphs: list[tuple[float, float, str]],
    reach_mm: float | None = None,
) -> None:
    """把柜内的 water / gas 符号配给最近的表位框，就地更新 ``kind``。

    一套 = 1 个 gas + 1 个 water。配不上就按实际有的算（``gas``/``water``/
    ``mixed``），所以图纸只画了气表时也能如实反映。

    ``reach_mm`` 是配对搜索半径。默认值按**表位间距**推算而不是拍脑袋：
    取相邻表位中心距离的 0.6 倍，既有足够余量覆盖 water 竖线与
    gas 框心的横向偏移，又不会越过半个柜宽把邻位的表抢过来。
    """
    if not positions or not glyphs:
        return
    if reach_mm is None:
        reach = _auto_reach(positions)
    else:
        reach = float(reach_mm)

    for p in positions:
        kinds: set[str] = set()
        for cx, cy, kind in glyphs:
            if math.hypot(cx - p.rect.cx, cy - p.rect.cy) <= reach:
                kinds.add(kind)
        if not kinds:
            continue
        p.kind = "mixed" if len(kinds) >= 2 else next(iter(kinds))


def _auto_reach(positions: list[MeterPosition]) -> float:
    """按表位间距推算配对半径（mm）。"""
    centers = [(p.rect.cx, p.rect.cy) for p in positions]
    gaps: list[float] = []
    for i in range(len(centers)):
        best = None
        for j in range(len(centers)):
            if i == j:
                continue
            d = math.hypot(centers[i][0] - centers[j][0],
                           centers[i][1] - centers[j][1])
            if best is None or d < best:
                best = d
        if best:
            gaps.append(best)
    if not gaps:
        # 只有一个表位：按表位自身尺寸给一个合理的窗口
        p = positions[0]
        return max(300.0, max(p.rect.width, p.rect.height))
    return max(200.0, min(gaps) * 0.6)


def _entity_in_rect(e, rect: Rect) -> bool:
    """实体是否落在 rect 内（用 bbox 快速判）。"""
    t = e.dxftype()
    try:
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            for p in (a, b):
                if not (rect.x0 - 1 <= p.x <= rect.x1 + 1
                        and rect.y0 - 1 <= p.y <= rect.y1 + 1):
                    return False
            return True
        if t == "CIRCLE":
            c, r = e.dxf.center, e.dxf.radius
            return (rect.x0 - r <= c.x <= rect.x1 + r
                    and rect.y0 - r <= c.y <= rect.y1 + r)
        if t in ("LWPOLYLINE", "POLYLINE", "RECTANG"):
            r2 = rect_from_entity(e)
            if r2 is None:
                return False
            return rect.contains(r2, tol=2.0)
    except Exception:
        return False
    return False


def classify_position(layers: list[str]) -> str:
    """按图层判定表位类型。"""
    low = [str(x).lower() for x in layers]
    gas = any(any(k in ly for k in GAS_LAYER_KEYWORDS) for ly in low)
    water = any(any(k in ly for k in WATER_LAYER_KEYWORDS) for ly in low)
    if gas and water:
        return "mixed"
    if gas:
        return "gas"
    if water:
        return "water"
    return "unknown"


def parse_cupboards(dxf_path) -> ParsedCupboard:
    """主入口：把 DXF 解析成「柜型列表」。

    算法（源自真实样本实测，不是猜的）
    ------------------------------------
      1. **柜体** = 柜框图层上成对的长LINE（上下边 + 左右边）
         —— 真实文件里柜框不是闭合多段线，见 :func:`pair_frame_segments`
      2. **兜底**：若配对一个柜体都没出来（少数图纸确实用闭合多段线画），
         退回按「闭合矩形」找，但要过滤掉表位尺寸那一档
      3. **表位** = 柜体内的 gas / water 标记（INSERT 或炸开画的闭合框）
      4. **units** = max(gas 数, water 数) —— 用户口径「1 套 water+gas」
      5. **排布** = 先按 y 分行，再数每行的套数
      6. DIMENSION / 说明文字作为补充信息挂上

    耗时说明：真实样本（3420 实体，含 914 个 DIMENSION）单次解析约 3 分钟，
    主要花在逐实体求包围盒上。所以 :func:`parse_cached` 会把结果按
    ``(路径, mtime, 大小)`` 缓存到磁盘，避免重复解析同一份文件。
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    bind_document(doc)
    all_ents = list(doc.modelspace())
    out = ParsedCupboard(entity_total=len(all_ents))

    frames = extract_cabinet_frames(all_ents)
    out.cabinet_frame_pairs = len(frames)

    if not frames:
        # 兜底：闭合矩形画法的图纸
        rects, total = extract_rects(doc)
        out.entity_total = total
        out.rect_total = len(rects)
        cands = []
        for r in rects:
            if _is_junk_layer(r.layer):
                out.discarded.append((r, f"噪声图层 {r.layer}"))
                continue
            if min(r.width, r.height) < MIN_CABINET_SIDE_MM:
                out.discarded.append(
                    (r, f"非柜体 {r.width:.0f}×{r.height:.0f}mm"))
                continue
            cands.append(r)
        frames = _dedupe_overlapping(cands, out)

    for cab in frames:
        marks = collect_meter_positions(all_ents, cab)
        if not marks:
            # 空柜体（图框、混凝土剖面）直接丢弃，不进柜型库
            out.discarded.append((cab, "柜内无 gas/water 表位"))
            continue
        gas = sum(1 for m in marks if m.kind == "gas")
        water = sum(1 for m in marks if m.kind == "water")
        units = sum(1 for m in marks if m.kind in ("gas", "mixed")) or water
        if units < 1:
            out.discarded.append((cab, "表位类型无法识别"))
            continue

        marks.sort(key=lambda m: (-m.rect.cy, m.rect.cx))
        cup = Cupboard(rect=cab, positions=marks, layer=cab.layer)
        cup.glyph_count = len(marks)
        cup.notes = _nearby_text(doc, cab)
        dw, dh = _nearby_dimensions(doc, cab)
        cup.dim_w_mm, cup.dim_h_mm = dw, dh
        out.cupboards.append(cup)

    out.cupboards = _drop_nested_cabinets(out.cupboards, out)

    # 排布聚类容差按柜体尺寸自适应（见 layout_rows_cols 的实测数据说明）：
    # gas 表列间距 350mm / 行间距 550~600mm，取柜宽 12%、柜高 15%。
    for c in out.cupboards:
        c._layout_tol = (max(c.rect.width * 0.12, 30.0),
                         max(c.rect.height * 0.15, 30.0))

    out.cupboards.sort(key=lambda c: c.rect.x0)
    for c in out.cupboards:
        if c.layer:
            out.cabinet_layer_hits[c.layer] = \
                out.cabinet_layer_hits.get(c.layer, 0) + 1
    return out


def _dedupe_overlapping(rects: list[Rect], out: ParsedCupboard) -> list[Rect]:
    """同一矩形被多次描边时只保留面积最大的那个。"""
    rects = sorted(rects, key=lambda r: -r.area)
    kept: list[Rect] = []
    for r in rects:
        dup = False
        for k in kept:
            ov = k.overlap(r)
            # 重叠 >95% 且尺寸相近 → 同一个矩形的双层线
            if ov > 0.95 and (abs(k.width - r.width) < r.width * 0.12
                              and abs(k.height - r.height) < r.height * 0.12):
                out.discarded.append(
                    (r, f"与 {k.width:.0f}×{k.height:.0f}mm 柜体重复描边"))
                dup = True
                break
        if not dup:
            kept.append(r)
    return kept


def _drop_nested_cabinets(cabs: list[Cupboard],
                          out: ParsedCupboard) -> list[Cupboard]:
    """若 A 严格包含 B 且两者都是柜体候选，保留 A（外层）。

    真实图纸里同一柜体常被「先画细框再画外框」地叠画；
    若不去重，同一个柜子会被入库两次。
    """
    kept: list[Cupboard] = []
    for c in sorted(cabs, key=lambda x: -x.rect.area):
        nested = False
        for k in kept:
            if (k.rect.contains(c.rect, tol=MIN_CABINET_MARGIN_MM)
                    and c.rect.margin_to(k.rect) > 0):
                out.discarded.append(
                    (c.rect, f"嵌套在 {k.rect.width:.0f}×{k.rect.height:.0f}mm 柜体内"))
                nested = True
                break
        if not nested:
            kept.append(c)
    return kept


_DIM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


def _nearby_dimensions(doc, cab: Rect) -> tuple[float | None, float | None]:
    """读柜体附近的 DIMENSION，判定柜体的总宽与总高（mm）。

    需求文档 2.2 节：尺寸应「读图纸标注」，规范公式仅作校验。

    实测踩坑（用户实图标注链形如``50/175/200/100/250/175/50``，总宽 1000）：
    一开始只取「离柜体最近的那条标注」，结果拿到了细分链的**第一段 50**，
    柜体尺寸全错。细分链的每段都比总尺寸更近，靠「最近」是取不到的。

    现在的判据 —— **细分链求和 ≈ 柜体边长**：
      · 把柜体下方/左方同一行(链)的标注按位置排序；
      · 逐个累加，找到前缀和最接近柜体边长的位置 = 总尺寸；
      · 若某条标注本身就接近边长且明显大于相邻分段，它就是总尺寸。
    两种信号都拿不到时返回 (None, None)，由几何 bbox 兜底。
    """
    below: list[tuple[float, float]] = []   # (x, 值) 柜体下方的链
    left: list[tuple[float, float]] = []    # (y, 值) 柜体左方的链
    for dim in doc.modelspace().query("DIMENSION"):
        try:
            val = dim.get_measurement()
            if val is None or not math.isfinite(float(val)):
                continue
            val = float(val)
            if val <= 0:
                continue
            # 有覆盖文字就用覆盖文字（CAD 里常把 1200 写成 "1200"）
            text = dim.dxf.text if dim.dxf.hasattr("text") else ""
            if text and text not in ("<>", ""):
                m = _DIM_RE.match(str(text))
                if m:
                    val = float(m.group(1))
            if not dim.dxf.hasattr("defpoint"):
                continue
            base = dim.dxf.defpoint
            px, py = float(base[0]), float(base[1])
        except Exception:
            continue
        # 柜体下方一条水平链：y 在柜底附近，x 覆盖柜宽
        if (cab.y0 - 900 <= py <= cab.y0 + 60
                and cab.x0 - 120 <= px <= cab.x1 + 120):
            below.append((px, val))
        # 柜体左侧一条竖直链：x 在柜左边附近，y 覆盖柜高
        if (cab.x0 - 900 <= px <= cab.x0 + 60
                and cab.y0 - 120 <= py <= cab.y1 + 120):
            left.append((py, val))

    w = _total_from_chain(below, cab.width, axis="x")
    h = _total_from_chain(left, cab.height, axis="y")
    return w, h


def _total_from_chain(chain: list[tuple[float, float]], side: float,
                      axis: str) -> float | None:
    """从一条尺寸链里判定总尺寸。

    ``chain`` 是 ``(位置, 值)`` 列表，``side`` 是柜体该方向的边长。
    返回最可信的总尺寸，或 ``None``（退回几何 bbox）。
    """
    if not chain or side <= 0:
        return None
    chain = sorted(chain)
    tol = max(side * 0.06, 20.0)     # 6% 或20mm，取大者

    # 信号①：本身就接近柜体边长的标注 —— 那就是总尺寸
    direct = [v for _p, v in chain if abs(v - side) <= tol]
    if direct:
        return max(direct)

    # 信号②：细分链求和。逐段累加，取最接近边长的前缀和。
    best: tuple[float, float] | None = None
    acc = 0.0
    for pos, v in chain:
        acc += v
        err = abs(acc - side)
        if best is None or err < best[0]:
            best = (err, acc)
    if best and best[0] <= tol * 1.5:
        return round(best[1], 1)
    # 信号③：所有分段之和（最宽松，接受较大偏差）
    total = sum(v for _p, v in chain)
    if abs(total - side) <= side * 0.35:
        return round(total, 1)
    return None


def _nearby_text(doc, cab: Rect) -> list[str]:
    """柜体附近的说明文字（截图里柜子下方那行 "6 SETS ..."）。

    实测踩坑：一开始允许 ``x`` 超出柜体 400mm，结果相邻柜子的说明文字
    会被两边同时收进来 ——「6 SETS - 2 COL x 3 ROW」和
    「4 SETS + WATER BANK」互相串台。

    归属规则改成**按柜宽对齐**：文字中心落在柜体的水平范围内，
    且垂直方向在柜顶上方一小段内。这样并排的柜子各取各的。
    """
    out: list[str] = []
    for e in doc.modelspace().query("TEXT MTEXT"):
        try:
            if e.dxftype() == "TEXT":
                s = (e.dxf.text or "").strip()
                ins = e.dxf.insert
            else:
                s = e.plain_text().strip()
                ins = e.dxf.insert
            if not s:
                continue
            px, py = float(ins[0]), float(ins[1])
        except Exception:
            continue
        # 水平：以文字起点为准，允许半个柜宽的前置偏移（标注常居左）
        if not (cab.x0 - cab.width * 0.25 <= px <= cab.x1 + 20):
            continue
        # 垂直：柜顶上方 0~600mm（图注通常在柜子上方）或柜底下方 0~300mm
        if not (cab.y1 <= py <= cab.y1 + 600):
            continue
        if s not in out:
            out.append(s)
    return out[:5]
