"""从 DWG/DXF 里识别「哪些 block 才是计量柜」。

为什么需要这个模块
------------------
早期版本只按「block 内实体数 >= 2」筛选，结果用户传一份真实图纸进来，
141 个 block 全部被当成柜型渲进库 —— 其中 130 多个是
``Aect_Duct_Oval_L1line_Retn_EndCap_Drop_Edge`` 这类 **HVAC 风管零件**
（``Aect`` = AutoCAD Electrical的duct 组件库），和柜型毫无关系。
用户看到满屏风管零件，自然会问「这里面为啥会有这些东西」。

判定策略：图层语义 + 几何特征 + 尺寸合理性，三者加权
------------------------------------------------------
只看名字不可靠（图纸里柜型可能叫 ``bloko``，风管也可能叫 ``CU``），
只看图层也不够（风管也画在 ``G-Anno-Std-*`` 这类公共层上）。所以组合判断：

1. **图层命中**（最强信号）—— 柜型几乎必然画在
   ``WATER_METER``/ ``GAS_METER``/ ``CUPBOARD``/ ``METER`` 这类图层上。
2. **名称命中** —— ``CP-xxx`` / ``CUPBOARD`` / ``METER`` / ``水表`` 等。
3. **几何特征**（兜底）—— 柜型是一组**近圆形**图元（表盘）+
   矩形柜体，且整体尺寸落在「人能装的壁龛」区间（宽200~3000mm）。
   风管是长条形、图层也不含 meter，所以被挡掉。
4. **明确黑名单** —— ``Aect_*``（电气风管组件库）、``*_Drop_Edge``、
   ``Anonymous`` 等成规模的机械零件前缀。

分数 >= ``MIN_SCORE`` 才认定为柜型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 图层/名称关键词

#: 柜型专用图层关键词（小写匹配）。命中权重最高。
LAYER_KEYWORDS: tuple[str, ...] = (
    "water_meter", "gas_meter", "hot_water", "cold_water",
    "cupboard", "meter_cup", "meterboard", "meter",
    "wms_", "cms_", "gms_",
    "hwat", "hwat__",  # 图纸里常见的 hot water 缩写变体
)

#: block 名称关键词。
NAME_KEYWORDS: tuple[str, ...] = (
    "cupboard", "meter", "cp-", "cp_", "cabinet", "水表", "表箱",
    "water meter", "gas meter",
)

#: 成规模的机械零件前缀 —— 直接排除，不参与打分。
#: Aect_* 是 AutoCAD Electrical 的风管/桥架组件库，一个图纸能有上百个，
#: 用户实测里141 个 block 里有 130+ 个是这类，纯噪声。
JUNK_PREFIXES: tuple[str, ...] = (
    "aect_", "aec_", "autocad_", "acad_", "_acad",
    "3dsolids", "3d_", "import_", "acis_", "dwf_", "xref_",
)

#: 成规模的机械零件名片段（配合 _SEP 拆分后匹配）。
JUNK_TOKENS: tuple[str, ...] = (
    "drop", "elbow", "tee", "reducer", "duct", "flange", "gasket",
    "hanger", "strap", "cap", "plug", "outlet", "inlet", "damper",
    "insulation", "bend", "offset", "cross", "coupling", "sleeve",
    "plate", "bracket", "support", "channel", "trapeze", "fitting",
    "nut", "screw", "bolt", "washer", "seal", "filter", "motor",
)

#: 名称分隔符 —— block 名常写成 Aect_Duct_Oval_L1line_Retn_EndCap_Drop_Edge
_SEP = re.compile(r"[_\-.:/\\]+")

#: 判定为柜型的最低分
MIN_SCORE = 3


@dataclass
class BlockVerdict:
    """单个 block 的判定结果。"""

    block_name: str
    is_cupboard: bool
    score: int
    reason: str
    #: 宽高（mm），用于给用户解释「为什么不算柜型」
    size_mm: tuple[float | None, float | None] = (None, None)
    layers: list[str] = field(default_factory=list)
    entity_count: int = 0


def _tokens(name: str) -> set[str]:
    return {t.lower() for t in _SEP.split(name) if t}


def score_block(
    block_name: str,
    layers: list[str],
    entity_count: int,
    circle_ratio: float = 0.0,
    size_mm: tuple[float | None, float | None] = (None, None),
) -> tuple[bool, int, str]:
    """给单个 block 打分，返回 ``(是否柜型, 分数, 理由)``。

    分数构成：
      +3图层命中 meter/cupboard 类关键词
      +2名称命中 cupboard/meter/CP-类关键词
      +1圆图元占比高（表盘特征）
      +1 尺寸落在「壁龛可装」区间
      -99命中机械零件黑名单（直接否决）

    ``circle_ratio`` 是 CIRCLE/ARC 实体占总数的比例。
    """
    low = block_name.lower()
    toks = _tokens(block_name)
    layers_low = [str(x).lower() for x in layers]

    # ---- 黑名单：一票否决 ----
    if low.startswith(JUNK_PREFIXES):
        return False, 0, f"机械零件库（{block_name.split('_')[0]}_*）"
    junk_hits = toks & set(JUNK_TOKENS)
    if len(junk_hits) >= 2:
        # 命中 2 个以上机械零件词 —— 风管零件几乎必然如此
        return False, 0, f"机械零件（{', '.join(sorted(junk_hits)[:3])}）"

    score = 0
    why: list[str] = []

    # ---- 图层 ----
    layer_hit = next(
        (k for k in LAYER_KEYWORDS for ly in layers_low if k in ly),
        None,
    )
    if layer_hit:
        score += 3
        why.append(f"图层含 {layer_hit}")

    # ---- 名称 ----
    name_hit = next((k for k in NAME_KEYWORDS if k in low), None)
    if name_hit:
        score += 2
        why.append(f"名称含 {name_hit}")

    # ---- 几何：表盘是近圆形 ----
    if circle_ratio >= 0.15:
        score += 1
        why.append(f"圆图元 {circle_ratio:.0%}")

    # ---- 尺寸：柜体是壁龛量级 ----
    w, h = size_mm
    if w and h:
        long_side, short_side = max(w, h), min(w, h)
        # 宽 200~3000mm，高 200~3000mm，且不是极端长条（长宽比 < 8）
        if 200 <= long_side <= 3000 and 200 <= short_side <= 3000:
            if long_side / max(short_side, 1e-6) < 8:
                score += 1
                why.append(f"尺寸 {long_side:.0f}×{short_side:.0f}mm")

    ok = score >= MIN_SCORE
    if ok:
        return True, score, " + ".join(why) or f"分数 {score}"
    if not why:
        return False, score, f"无柜型特征（分数 {score}，图层 {layers_low[:3]}）"
    return False, score, "；".join(why) + f"（分数 {score} < {MIN_SCORE}）"


def block_circle_ratio(ents) -> float:
    """CIRCLE/ARC 等近圆图元占比 —— 表盘的典型特征。"""
    if not ents:
        return 0.0
    n = 0
    for e in ents:
        t = e.dxftype()
        if t in ("CIRCLE", "ARC", "ELLIPSE"):
            n += 1
    return n / len(ents)


def block_size_mm(ents) -> tuple[float | None, float | None]:
    """block 自身几何的宽高（mm）—— 用 block 的实体，不是整个图纸。

    注意 ezdxf 的 ``extents()`` 返回的是 ``((x0,y0,z0), (x1,y1,z1))``
    **三元组**，按二元组解包会抛 ValueError。早期版本正是这么写的，
    而异常被 ``except Exception`` 吞掉后返回 (None, None) ——
    于是所有柜型尺寸都算不出来，且没有任何报错（静默失败）。
    """
    try:
        from ezdxf.bbox import extents

        box = extents(list(ents), fast=True)
        (x0, y0, _z0), (x1, y1, _z1) = box.extmin, box.extmax
        w, h = abs(float(x1) - float(x0)), abs(float(y1) - float(y0))
        return (w if w > 0 else None, h if h > 0 else None)
    except Exception:
        return (None, None)


def block_layers(ents) -> list[str]:
    out: list[str] = []
    for e in ents:
        try:
            if e.dxf.hasattr("layer") and e.dxf.layer and e.dxf.layer not in out:
                out.append(e.dxf.layer)
        except Exception:
            continue
    return out


def judge_library(dxf_path) -> tuple[list[BlockVerdict], list[BlockVerdict]]:
    """扫描 DXF 的所有命名 block，分成「柜型」与「非柜型」两组。

    返回 ``(柜型列表, 非柜型列表)``，两组都带判定理由 ——
    用户抱怨「为啥会有这些东西」时，必须能逐条解释，而不是静默过滤。
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    cupboards: list[BlockVerdict] = []
    others: list[BlockVerdict] = []

    for blk in doc.blocks:
        name = blk.name
        if name.startswith("*"):
            continue
        try:
            ents = list(blk)
        except Exception:
            continue
        if not ents:
            others.append(BlockVerdict(
                name, False, 0, "空 block（无实体）",
                layers=[], entity_count=0))
            continue
        lys = block_layers(ents)
        ratio = block_circle_ratio(ents)
        size = block_size_mm(ents)
        ok, score, reason = score_block(name, lys, len(ents), ratio, size)
        v = BlockVerdict(
            block_name=name, is_cupboard=ok, score=score, reason=reason,
            size_mm=size, layers=lys, entity_count=len(ents),
        )
        (cupboards if ok else others).append(v)

    # 柜型按分数降序：最像柜型的排最前
    cupboards.sort(key=lambda v: (-v.score, v.block_name))
    return cupboards, others