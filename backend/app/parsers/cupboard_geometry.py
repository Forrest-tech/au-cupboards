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
JUNK_LAYER_KEYWORDS: tuple[str, ...] = (
    "aect", "duct", "mech", "hvac", "auto", "acad", "3d", "xref",
    "defpoints", "标题", "说明", "图框", "title", "border", "frame",
)

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

    @property
    def positions_total(self) -> int:
        """表位总数 = 该层的 units 数。"""
        return len(self.positions)

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
        """从表位中心的聚类推断排布行列。

        用坐标聚类而不是猜：表位在图上是规则网格，
        先按 y 聚成行、再按 x 聚成列。
        """
        if not self.positions:
            return (0, 0)
        ys = sorted({round(p.rect.cy, 1) for p in self.positions})
        rows = _cluster_count(ys, tol=self.rect.height * 0.08)
        if rows == 0:
            return (0, 0)
        # 每行的表位数取众数
        counts: dict[int, int] = {}
        for p in self.positions:
            r = _which_cluster(p.rect.cy, ys, tol=self.rect.height * 0.08)
            counts[r] = counts.get(r, 0) + 1
        cols = max(counts.values()) if counts else 0
        return (rows, cols)

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
    #: 柜体候选图层命中情况，用于诊断「图层对不对」
    cabinet_layer_hits: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"实体 {self.entity_total} / 闭合矩形 {self.rect_total}",
                 f"柜型 {len(self.cupboards)} 个"]
        for c in self.cupboards:
            w, h = c.bbox_mm
            parts.append(
                f"  · {c.positions_total} 表位 "
                f"{c.grid_aspect}  {w:.0f}×{h:.0f}mm  "
                f"gas {c.gas_count}/water {c.water_count}  "
                f"图层 {c.layer}"
            )
        if self.discarded:
            parts.append(f"丢弃 {len(self.discarded)} 个矩形")
        return "\n".join(parts)


# ---------------------------------------------------------------- 矩形提取


def _is_junk_layer(layer: str) -> bool:
    low = str(layer).lower()
    return any(k in low for k in JUNK_LAYER_KEYWORDS)


def _is_closed_polyline(ent) -> bool:
    if ent.dxftype() == "LWPOLYLINE":
        return bool(ent.closed)
    if ent.dxftype() == "POLYLINE":
        return bool(ent.is_closed)
    return False


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

    算法（需求文档 2.2 节）：
      1. 抽出所有严格矩形
      2. 丢弃噪声图层上的矩形
      3. **候选柜体** = 足够大的矩形（边长 >= MIN_CABINET_SIDE_MM），
         或图层名命中柜体关键词
      4. **去重**：同一位置被双层描边画两次的，只留外层那个
      5. **表位** = 柜体内部、不与柜体边界重合的矩形
      6. 排除嵌套：若A 包含 B 且 A 也是柜体候选，只保留更外层的那个
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    rects, total = extract_rects(doc)
    out = ParsedCupboard(entity_total=total, rect_total=len(rects))

    usable: list[Rect] = []
    for r in rects:
        if _is_junk_layer(r.layer):
            out.discarded.append((r, f"噪声图层 {r.layer}"))
            continue
        if (r.width < MIN_POSITION_SIDE_MM
                or r.height < MIN_POSITION_SIDE_MM):
            out.discarded.append((r, f"过小 {r.width:.0f}×{r.height:.0f}mm"))
            continue
        usable.append(r)

    # ---- 柜体候选：够大，或图层语义命中 ----
    cands: list[Rect] = []
    for r in usable:
        big = (min(r.width, r.height) >= MIN_CABINET_SIDE_MM)
        sem = any(k in r.layer.lower() for k in CABINET_LAYER_KEYWORDS)
        if big or sem:
            cands.append(r)
        else:
            out.discarded.append((r, f"非柜体 {r.width:.0f}×{r.height:.0f}mm"))

    # ---- 去重：同位置双层描边只留最外层 ----
    cands = _dedupe_overlapping(cands, out)

    # ---- 表位：柜体内、且不与柜体边界重合的矩形 ----
    all_ents = list(doc.modelspace())
    for cab in cands:
        inside = []
        for r in usable:
            if r is cab:
                continue
            if not cab.contains(r, tol=MIN_CABINET_MARGIN_MM):
                continue
            # 排除与柜体边界几乎重合的「双层描边」
            if cab.margin_to(r) < MIN_CABINET_MARGIN_MM:
                continue
            # 尺寸合理性
            if not (MIN_POSITION_SIDE_MM <= r.width <= MAX_POSITION_SIDE_MM
                    and MIN_POSITION_SIDE_MM <= r.height <= MAX_POSITION_SIDE_MM):
                continue
            lys = _layer_of_entity(all_ents, r)
            inside.append(MeterPosition(
                rect=r, layers=lys,
                kind=classify_position(lys),
                entity_count=len(lys),
            ))
        # 排序：先上后下（y 降序），再左到右
        inside.sort(key=lambda p: (-p.rect.cy, p.rect.cx))
        cup = Cupboard(rect=cab, positions=inside, layer=cab.layer)
        # 把柜内的 water/gas 符号配给最近表位 —— water 竖线常落在
        # gas 红框之外，只看框内图层会把 water 算成 0 套（见函数 docstring）
        glyphs = collect_meter_glyphs(all_ents, cab)
        pair_glyphs_to_positions(cup.positions, glyphs)
        cup.glyph_count = len(glyphs)
        cup.notes = _nearby_text(doc, cab)
        dw, dh = _nearby_dimensions(doc, cab)
        cup.dim_w_mm, cup.dim_h_mm = dw, dh
        out.cupboards.append(cup)

    # ---- 嵌套柜体：内层若是外层的重复描述，只留外层 ----
    out.cupboards = _drop_nested_cabinets(out.cupboards, out)

    out.cupboards.sort(key=lambda c: -c.rect.area)
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
