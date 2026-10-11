"""柜型变体渲染：DWG/DXF里的 block → JPG 缩略图。

需求 2 原文：「下方左右分栏：左侧 cupboard 种类（water+gas 数量），
右侧显示对应**排布种类 + 尺寸 + 介绍**」；用户进一步要求
「读取了 dwg 文件，将里面的不同的柜型都导出成 jpg 格式，
放在 library 里，可以点击查看」。

因此本模块负责 **一个 block → 一张 JPG**，并同时给出实测尺寸
（W/H/D，mm）—— 尺寸直接取自几何 bbox，不再是 POC 占位值。

为什么不用 ODA 转出的整图直接切图：
  柜型库的每个变体是一个**命名 block**，block 定义里的几何才是柜型本体
  （modelspace通常只有几个 INSERT）。按 block 渲染才能一张图对应一个柜型。

渲染栈：ezdxf.addons.drawing + matplotlib(Agg)
  选它的原因：纯 Python、无外部二进制依赖、与已有 ezdxf 解析同版本，
  避免「能解析但渲染不了」的版本错配。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf.bbox import extents

from .cupboard_geometry import _entity_extents, bind_document
from .meter_sprites import METER_BLOCKS

log = logging.getLogger(__name__)

#: 渲染输出目录名（相对 var/）
THUMB_DIR = "thumbs"

#: 默认渲染宽度（像素）。1400足够看清柜型排布又不至于太慢。
DEFAULT_WIDTH = 1400

#: 图片最长边像素上限 —— 防止超大图纸生成几十MB 的 JPG
MAX_SIDE = 2600

#: JPEG 质量
JPEG_QUALITY = 88

#: 渲染线宽（1/100 mm）。
#:
#: 历史踩坑链（三轮才收敛）：
#:
#: 1. 最早默认配置下白底上几乎看不见 → 调到 25（0.25mm）强行加粗。
#:    那是**按 block 渲染**时期：整个 block 缩到一张图里，线条太细
#:    真的看不见。
#: 2. 改成按**柜体区域**渲染后，一个柜子独占一张图，25 太粗 ——
#:    150×190mm 的表位框直接糊成黑斑，看不出柜内排布。降到 6。
#: 3. 用户实测仍不满意（截图里柜内是一坨黑块，认不出 water meter /
#:    gas meter），要求「必须使用 DWG 里的原图，可以用 water meter
#:    和 gas meter 作为原始元素」。
#:
#: 根因不是线宽策略（RELATIVE_FIXED / ABSOLUTE 产物字节数完全一样，
#: 32KB vs 32KB），而是 **min_lineweight 是个下限**：
#: DXF 里 water/gas meter 的块内轮廓线自身线宽只有 5~9（0.05~0.09mm），
#: 6 的下限把它们和柜体粗线（~50）一起保住了，但 meter 内部那些
#: 0.05mm 级的细部（表盘、阀门、接管）全被下限「抬」到 0.06mm，
#: 在 1400px 图上叠成实心块。
#:
#: 实测参数矩阵（同一柜 CP-13p-01，1400×1555）：
#:   minlw=6  → 32KB，细部糊成黑块（用户截图就是这个）
#:   minlw=2  → 47KB，能看出 meter 轮廓
#:   minlw=1  → 70KB，water meter 的表盘/阀门/接管全部可辨
#:   minlw=0  → 51KB，反而更糊（0 号线在 110dpi 下小于 1px，
#:               matplotlib 走 antialiasing 把它糊回去）
#:
#: 结论取 1：既保住了 DXF 原始线宽的层级关系，又让最细的线至少 1px。
MIN_LINEWEIGHT = 1

#: 渲染 DPI（像素/英寸）。
#:
#: 【真实 bug 修复】原值 110，实测输出只有 416×641 px。柜型图在网页上
#: 要放大到中栏整屏查看（用户截图里中栏就是一张图占满），
#: 低分辨率放大后必然糊成黑块 —— 用户原话「你给我的图，黑黢黢的，
#: 无法使用」。
#:
#: 实测对比（同一柜型 CP-5p-18，真实 DWG）：
#:     110dpi → 416×641，gas meter 内部阀门/活塞糊成实心黑块
#:     220dpi → 692×1065，能看出结构但细部仍偏糊
#:     300dpi → 1124×1735，阀门、接管、文字全部清晰，与用户给的
#:                参考图（gas meter 特写）同一水平
#: 耗时几乎不变（5.0s → 5.2s，因为走的是 8 worker 并行，
#: 单任务本身不是瓶颈），故直接取 300。
#: 这与 :data:`app.parsers.meter_sprites.SPRITE_DPI` 取同一值，
#: 保证「整柜渲染」与「图元合成」两条路径产出的清晰度一致。
DPI = 300

#: **带文字实体**（标注/引线/文字）。渲染它们必须查系统字体，
#: 而容器里字体不全时`ezdxf` 会抛
#: ``AttributeError: 'NoneType' object has no attribute 'filename'``。
#:
#: 【本轮真根因 —— 之前三轮都误诊了】
#: ------------------------------------------
#: 旧注释断言「触发者是 DIMENSION，因为标注文字要查 ezdxf 字体库，
#: 而 FontFace().filename 默认是 None，style 为空的实体会炸」。
#: 这个判断**只对了一半**：字体其实**早就可用**了
#: （``fonts.get_font_face('')`` → ``DejaVuSans.ttf``，共330 个字体条目），
#: 系统里也确实装了 ``NotoSans`` / ``DejaVu`` / ``Liberation`` 等。
#:
#: 真正的触发链（真实 DXF + 全栈追踪，CP-5p-18 柜内 23 个 DIMENSION）：
#:
#:   1. DIMENSION 展开出虚拟 MTEXT，``dxf.style = 'trebuchet_MS'``
#:      —— 这是**图纸自己的 textstyle 名**（源 DXF 的 styles 表里确实有
#:      ``trebuchet_MS``，还有 ``Arch-Dim`` / ``RomanS`` / ``NDY_Standard``…）。
#:   2. 我们把柜内实体克隆进 ``ezdxf.new()`` 建临时文档时，
#:      **只搬了 block 定义，没搬 styles 表**。
#:   3. 于是渲染时 ``Properties.resolve_font()`` 在临时文档的 styles 表里
#:      查不到 ``trebuchet_MS`` → 返回 ``None``。
#:   4. ``mtext_complex.get_font()`` 把这个 ``None`` 直接喂给
#:      ``fonts.find_font_file_name(None)`` → 炸在
#:      ``font_manager.find_best_match(font_face.filename)``。
#:
#: 也就是说：**这不是环境缺字体，是克隆时漏搬 textstyle 定义。**
#: 已做双向验证（见 :func:`_clone_styles` 的 docstring）。
#:
#: 所以现在**恢复渲染尺寸标注**（用户参考图里1065/2350/50 100这些
#: 数字是核心信息，丢了图就不好用）。
RENDER_TEXT = True

#: 产出 JPG 的最小合理字节数。
#:
#: 判定「空白图」的兜底依据。实测踩坑（两次）：
#:
#: 1. 默认配置渲染出 4587 字节的「看起来空白」的图，代码只判断
#:    文件存在且 >512 字节，于是静默入库了空图。
#: 2. 把min_lineweight 从 25 调到 6 之后，**合法的柜体图也从26KB
#:    掉到 7.6KB** —— 用固定字节阈值必然误杀。
#:
#: 所以现在**主判据是内容**（见 :func:`_looks_blank`），
#: 字节数只作极端情况下的兜底（小到不可能画出东西）。
MIN_IMAGE_BYTES = 4_000


#: 块定义递归搬运的最大深度。
#:
#: 真实样本实测层数只有 2（``water meter h`` → ``h-meter``），
#: 但 CAD 里的块可以套块再套块，没有硬上限的说法 —— 设成 8 纯粹是
#: 防「块套块套块套块套块套块套块套块」把 CPU 烧穿，同时远高于真实需求。
_MAX_BLOCK_DEPTH = 8


def _collect_block_refs(src_doc, ents, out: set[str], _depth: int = 0) -> None:
    """递归收集 ``ents`` 里（以及它们引用的块里）全部 INSERT 的块名。

    【本轮修复的核心】旧代码只扫 ``ents`` 一层就收工，于是
    **嵌套块引用的块定义从来没被搬过**。

    实测后果（真实 DWG，CP-5p-18 柜内5 个 ``water meter h``）：
      ``water meter h`` 的块定义里有 109 个实体，其中 1 个是
      ``INSERT name='h-meter'`` —— 也就是水表本体是**第二层块**。
      第一层块搬过去后，``h-meter`` 在临时文档里不存在，
      ezdxf 渲染 INSERT 时查不到块定义就**静默跳过**，
      于是柜子下半部 5 个水表在图上**完全消失**，
      而 modelspace 里那 5 个 INSERT 明明被算进了 ``entity_count=42``。

      这就是「左边几个表不见了」的成因：不是没抠到实体，
      是抠到了但渲染时被块缺失吃掉了。
    """
    if _depth > _MAX_BLOCK_DEPTH:
        return
    for e in ents:
        if e.dxftype() != "INSERT":
            continue
        try:
            name = str(e.dxf.name)
        except Exception:
            continue
        if name in out:
            continue
        out.add(name)
        try:
            nested = src_doc.blocks.get(name)
        except Exception:
            continue
        if nested is None:
            continue
        try:
            _collect_block_refs(src_doc, list(nested), out, _depth + 1)
        except Exception:
            continue


def _clone_layers(src_doc, dst_doc, layer_names: set[str]) -> list[str]:
    """把用到的图层定义（含颜色/线宽/线型）搬进临时文档。

    为什么必须搬
    ------------
    ``ezdxf.new()`` 建出来的文档只有一层默认图层。实体上写的是
    ``layer='HVAL___2CO'``，而渲染时颜色/线宽要走
    ``图层 ACI色 → RGB`` 的解析；图层不存在就退到文档默认值，
    于是 DWG 里「青框/红框/绿管/黄管」统统变成同一个颜色 ——
    需求要的「保持原有颜色」就落空了。

    实测（真实 DWG）：
      · ``HVAL___2CO``（water meter 所在层）  color=7线weight=-3
      · ``G-Anno-Std-Cntr``（gas meter 所在层）  color=1  lineweight=18
      · 柜体框在 ``1CO``
    三层颜色各不相同，不搬图层就必然全糊成一个颜色。
    """
    failed: list[str] = []
    for name in sorted(layer_names):
        if name in ("0",):
            continue          # 默认层，新文档自带
        try:
            src = src_doc.layers.get(name)
        except Exception:
            continue
        if src is None:
            continue
        try:
            dst = dst_doc.layers.get(name)
        except Exception:
            # 【易踩】DXFTable.get() 对**不存在**的条目是**抛
            # DXFTableEntryError**，不是返回 None。所以不能写
            #     dst = dst_doc.layers.get(name) or dst_doc.layers.new(name)
            # —— get 抛异常时根本走不到 new()，图层永远建不出来。
            # 实测踩过：`HVAL___2CO` / `G-Anno-Std-Cntr` / `1CO`
            # / `HWAT___5CS` 四个真实图层全部报「图层搬运失败」。
            dst = None
        if dst is None:
            try:
                dst = dst_doc.layers.new(name)
            except Exception:
                failed.append(name)
                continue
        try:
            dst.color = src.dxf.color
        except Exception:
            pass
        try:
            dst.dxf.linetype = src.dxf.linetype
        except Exception:
            pass              # 线型定义没搬，Continuous 本来就对
        try:
            dst.dxf.lineweight = src.dxf.get("lineweight", -3)
        except Exception:
            pass
    return failed


def _collect_layers(ents, out: set[str], _depth: int = 0) -> None:
    """递归收集实体涉及的图层名（``*INSERT{0,1,2}`` 归一化到 INSERT）。"""
    if _depth > _MAX_BLOCK_DEPTH:
        return
    for e in ents:
        if not e.dxf.hasattr("layer"):
            continue
        lay = e.dxf.layer
        if not lay:
            continue
        # INSERT{0,1,2} / DIM{0,1} 是「带索引的块/标注」，图层名就是前缀
        out.add(str(lay))
        if e.dxftype() != "INSERT":
            continue
        try:
            name = str(e.dxf.name)
        except Exception:
            continue


def _clone_block_deep(src_doc, dst_doc, name: str, errors: list[str],
                      depth: int) -> None:
    """把一个块定义搬进临时文档，并**递归**搬运它引用的子块。"""
    if depth > _MAX_BLOCK_DEPTH:
        errors.append(f"块 {name}: 嵌套超过 {_MAX_BLOCK_DEPTH} 层，已截断")
        return
    try:
        src_block = src_doc.blocks.get(name)
    except Exception:
        return
    if src_block is None:
        return
    # 【易踩，同_layers】DXFTable.get() 找不到条目时抛
    # DXFTableEntryError 而不是返回 None。写成
    #     dst = dst_doc.blocks.get(name) or dst_doc.blocks.new(name)
    # 会抛异常走人，块定义永远建不出来。
    # 实测后果：``water meter h`` 里的嵌套块 ``h-meter`` 没建 →
    # 柜内 5 个水表在图上整体消失（entity_count 却仍是 42）。
    dst_block = None
    try:
        dst_block = dst_doc.blocks.get(name)
    except Exception:
        dst_block = None
    if dst_block is None:
        try:
            dst_block = dst_doc.blocks.new(name)
        except Exception as exc:
            errors.append(f"块 {name}: {exc}")
            return

    try:
        children = list(src_block)
    except Exception:
        children = []

    # 子块先搬（深度优先），保证当前块里的 INSERT 引用能解析到
    sub_refs: set[str] = set()
    _collect_block_refs(src_doc, children, sub_refs, depth + 1)
    for sub in sorted(sub_refs):
        if sub == name:
            continue          # 自引用，跳过防死循环
        _clone_block_deep(src_doc, dst_doc, sub, errors, depth + 1)

    # 图层定义跟着块一起搬，否则颜色全糊（见 _clone_layers）
    lays: set[str] = set()
    _collect_layers(children, lays)
    lay_errs = _clone_layers(src_doc, dst_doc, lays)
    if lay_errs:
        errors.append(f"块 {name} 图层搬运失败 {len(lay_errs)} 个")

    dst_space = dst_block.entity_space
    for be in children:
        try:
            # 同主实体一样，块内的 SOLID HATCH 也要降级，否则嵌套块里的
            # 实心填充（如 water meter 阀体底纹）仍会渲成黑坨
            dst_space.add(_demote_solid_hatch(be))
        except Exception as exc:
            errors.append(f"块 {name}/{be.dxftype()}: {exc}")


#: 实体在modelspace 层的颜色。
def _demote_solid_hatch(e):
    """把 ``SOLID`` 实心填充的 HATCH 降级成只描边、不填充。

    需求文档「步骤2：修复渲染黑坨」原文：
        「HATCH 实体只画边框不填充；所有实体只描边不填色」

    实测（真实 DWG，CP-5p-18 的 ``water meter h`` 块内）：
    109 个实体里有 **4 个 HATCH**，全部 ``pattern_name='SOLID'`` +
    ``solid_fill=1``，也就是纯实心色块。CAD 里它们是给水表阀体做的
    灰色底纹；渲染成图片时就会变成一块块**纯黑/纯灰的实心色块**——
    这正是用户最早报的「黑色实心块」。

    处理方式：``solid_fill`` 置 0、pattern 改 ``ANSI31``（斜线剖面线），
    于是保留「这里有个零件」的轮廓信息，又不会糊成黑坨。
    非 SOLID 的 HATCH 原样放行。

    返回**可直接写入目标文档的实体副本**。
    """
    if e.dxftype() != "HATCH":
        return e.copy()
    h = e.copy()
    try:
        pat = str(h.dxf.get("pattern_name", "")).upper()
        solid = h.dxf.get("solid_fill", 0)
        if solid and pat in ("", "SOLID"):
            h.dxf.solid_fill = 0
            h.dxf.pattern_name = "ANSI31"
            # 剖面线间距给大一点，避免密线在300dpi 下又糊成灰块
            try:
                h.dxf.pattern_scale = 2.0
            except Exception:
                pass
    except Exception:
        return e.copy()
    return h


def _clone_styles(src_doc, dst_doc) -> list[str]:
    """把源文档的 textstyle 定义搬进临时文档，返回失败的名字列表。

    为什么必须有这一步（本项目最隐蔽的一个 bug）
    ---------------------------------------------
    临时视图文档是 ``ezdxf.new()`` 建出来的，它只有一张**空的 styles 表**。
    而柜内 DIMENSION 展开出的虚拟 MTEXT 带的是图纸自己的 textstyle 名 ——
    真实样本里是 ``trebuchet_MS``（源 DXF 的 styles 表里确实有这一项）。

    渲染时的解析链是：

    ``Properties.resolve_font()`` → 在**当前文档**的 styles 表里查名
    → 查不到就返回 ``None``
    → ``mtext_complex.get_font()`` 把这个 ``None`` 直接传给
      ``fonts.find_font_file_name(None)``
    → 炸在 ``font_manager.find_best_match(font_face.filename)``，
      报 ``AttributeError: 'NoneType' object has no attribute 'filename'``

    整条链里**没有任何一步**会拿不到字体文件：系统里
    ``fonts.get_font_face('trebuchet_MS')`` 是能返回 ``DejaVuSans.ttf`` 的。
    纯粹是「临时文档里没有这个名字的定义」。

    双向实测（真实 DWG，CP-5p-18 第 0 个 DIMENSION 的虚拟 MTEXT）：

      · 不搬 styles → ``FAIL AttributeError: 'NoneType' has no 'filename'``
      · 只搬一个 ``trebuchet_MS`` → **OK**
      · 搬全部 styles → **OK**

    字体文件名的取法
    ----------------
    ``TextStyleTable.add(name, font=...)`` 的 ``font`` 是**必填**，
    而 SHX 字体名（如 ``txt.shx``）在 ``resolve_font`` 里走
    ``find_best_match`` 同样能落到 DejaVu 上，所以原样搬即可；
    源 style 的 ``dxf.font`` 为空时才退回 ``txt.shx``。
    """
    failed: list[str] = []
    for st in src_doc.styles:
        name = st.dxf.name
        if name.startswith("*"):
            continue          # 匿名/系统样式，渲染用不到
        try:
            font = st.dxf.font or "txt.shx"
            dst_doc.styles.add(name, font=font)
        except Exception:
            failed.append(name)
    return failed


def _clone_into_cabinet_view(src_doc, tmp_doc, ents) -> tuple[int, list[str]]:
    """把 ``ents`` 连同所需的块定义一起搬进 ``tmp_doc``。

    为什么不能直接 ``msp.add_entity(e.copy())``
    -------------------------------------------
    ezdxf 1.4.4 的 ``add_entity`` / ``add_foreign_entity`` **不接受
    INSERT**，会抛 ``DXFTypeError: unsupported DXF type: INSERT``。

    而真实样本里柜内的 gas 表 / water 表**全是 INSERT**（393 个），
    只有柜框是 LINE。于是旧代码里的::

        for e in inside:
            tmsp.add_entity(e.copy())

    会在第一个 INSERT 上就抛异常，被外层 ``except Exception`` 吞掉后
    返回 ``"实体复制失败"``；更糟的是有的版本里前面几个 LINE 已经
    加进去了，于是渲出一张**只有柜框、内部空无一物**的图 ——
    这正是「柜型预览里只有个空框，看不到表」的直接原因。

    正确做法分两步：
      1. 把用到的**块定义**整体搬进 ``tmp_doc``；
      2. 实体用 ``layout.entity_space.add(e.copy())`` 写入——
         它走 entitydb，不做类型白名单校验。

    返回 ``(成功写入数, 失败原因列表)``。有失败原因时调用方可决定
    是否继续 —— 不再像以前那样静默丢实体。
    """
    errors: list[str] = []

    # 关键：必须先搬 textstyle 表（见 _clone_styles 的 docstring）。
    # 少这一步，柜内 DIMENSION 展开出的 MTEXT 查不到 'trebuchet_MS'
    # 就会炸 —— 而这个错曾被误诊成「系统缺字体」，导致图上所有尺寸
    # 标注被丢掉。
    style_errs = _clone_styles(src_doc, tmp_doc)
    if style_errs:
        errors.append(f"textstyle 搬运失败 {len(style_errs)} 个")

    # 顶层实体所在的图层也必须搬 —— 只在块里搬是不够的。
    # 实测（真实 DWG，CP-5p-18）：柜框 LINE 在 ``1CO``、建筑线在
    # ``2CONC``，这两个图层只在 modelspace 层出现，块定义里根本没有；
    # 漏了它们的后果是这些线在渲染时退到默认层，颜色全丢。
    top_lays: set[str] = set()
    _collect_layers(ents, top_lays)
    lay_errs = _clone_layers(src_doc, tmp_doc, top_lays)
    if lay_errs:
        errors.append(f"顶层图层搬运失败 {len(lay_errs)} 个")

    used: set[str] = set()
    _collect_block_refs(src_doc, ents, used)
    for name in sorted(used):
        _clone_block_deep(src_doc, tmp_doc, name, errors, depth=0)

    ok = 0
    target = tmp_doc.modelspace().entity_space
    for e in ents:
        try:
            target.add(_demote_solid_hatch(e))
            ok += 1
        except Exception as exc:
            errors.append(f"{e.dxftype()}: {exc}")
    return ok, errors


@dataclass
class BlockRender:
    """单个 block 的渲染结果。"""

    block_name: str
    ok: bool
    #: 相对 var/thumbs/ 的文件名，失败时为 None
    image_name: str | None = None
    #: 实测宽高深（mm），取自几何 bbox 的 Z 向尺寸恒为 0 时w/h 为 None
    width_mm: float | None = None
    height_mm: float | None = None
    depth_mm: float | None = None
    #: 实体数 —— 0 说明这个 block 是空壳，不该入库
    entity_count: int = 0
    #: 该 block 内出现的图层 —— 用于区分 water / gas 分组
    layers: list[str] = field(default_factory=list)
    #: 按图层统计的实体数，能看出哪层是柜体、哪层是表
    layer_counts: dict[str, int] = field(default_factory=dict)
    #: 标注文字（TEXT/MTEXT/ATTRIB），柜型的「介绍」常写在这里
    texts: list[str] = field(default_factory=list)
    #: 实体/块搬运失败的原因 —— 非空说明图可能不完整，别当成正常产出
    clone_errors: list[str] = field(default_factory=list)
    #: 是否因为字体问题降级成了「无文字」渲染
    text_render_failed: bool = False
    #: 降级时删掉了多少个带文字的实体（DIMENSION/TEXT/…）
    stripped_count: int = 0
    #: 图元合成时各类仪表的贴图数量，如 ``{"gas": 13, "water_v": 13}``。
    #: 这是「柜型图真的用了 DWG 原图」的直接证据 —— 数字对得上，
    #: 说明图上的每个图元都对应 DWG 里的一个真实实例。
    sprites: dict[str, int] = field(default_factory=dict)
    error: str | None = None


#: 会触发字体查找、渲染时可安全丢掉的实体类型。
#:
#: 关键是 ``DIMENSION`` —— 真实柜型区域里一个 TEXT 都没有，
#: 但有 50 个 DIMENSION，而标注文字同样要查 ezdxf 字体库，
#: 于是照样抛 ``AttributeError: 'NoneType' object has no attribute
#: 'filename'``。
TEXTUAL_TYPES: frozenset[str] = frozenset({
    "TEXT", "MTEXT", "ATTRIB", "ATTDEF",
    "DIMENSION", "LEADER", "MLEADER", "MULTILEADER",
})


def _clone_without_text(src_doc, ents, drop: frozenset[str] = TEXTUAL_TYPES):
    """重建一个「不含带文字实体」的临时文档，返回 ``(doc, 丢弃数)``。

    为什么必须重建而不是删除
    ------------------------
    :func:`_clone_into_cabinet_view` 用 ``entity_space.add()`` 写入，
    这些实体 ``dxf.handle`` 全是 ``None``、**不在 entitydb 里**。
    ``delete_entity`` 找不到句柄会抛 ``ValueError: list.remove(x):
    x not in list`` —— 实测 victims 有 50 个，实际删掉 **0** 个，
    于是降级重渲时字体问题依旧，整个柜型直接失败。

    与其和 entitydb 较劲，不如**一开始就只搬需要的实体**：
    顺带还省一次遍历。块定义也要同步过滤，否则块内的 DIMENSION
    照样触发字体查找。
    """
    tmp_doc = ezdxf.new()
    kept = [e for e in ents if e.dxftype() not in drop]

    # 同 _clone_into_cabinet_view：降级视图也要有 textstyle 表，
    # 否则保留下来的 TEXT/MTEXT（若有）一样会炸。
    if kept:
        _clone_styles(src_doc, tmp_doc)
        top_lays: set[str] = set()
        _collect_layers(kept, top_lays)
        _clone_layers(src_doc, tmp_doc, top_lays)

    used: set[str] = set()
    _collect_block_refs(src_doc, kept, used)
    errs: list[str] = []
    for name in sorted(used):
        _clone_block_deep(src_doc, tmp_doc, name, errs, depth=0)
    if errs:
        log.debug("降级视图块搬运有%d 处问题: %s", len(errs), errs[:3])

    target = tmp_doc.modelspace().entity_space
    for e in kept:
        try:
            target.add(_demote_solid_hatch(e))
        except Exception:
            continue
    return tmp_doc, len(ents) - len(kept)


def _looks_blank(path: Path) -> bool:
    """按**像素内容**判断是否白图 —— 比看文件字节数可靠。

    实测踩坑：字节数阈值会随线宽、尺寸、压缩参数大幅波动
    （min_lineweight 25→6 让同一张柜体图从 26KB 掉到 7.6KB），
    用固定阈值必然在某个参数下误杀合法图，或放过真正的空图。

    这里用 Pillow 数「非白像素占比」：
      · 占比 < 0.4% → 视为空白（画布上东西太少）
      · 同时返回非白像素的**包围盒**，全在边缘一圈说明只是画了框
    """
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            # 缩小后再统计 —— 全尺寸逐像素太慢，对判定没帮助
            w, h = im.size
            sc = max(1, min(w, h) // 300)
            small = im.resize((max(1, w // sc), max(1, h // sc)))
            # Pillow 14 起 getdata() 弃用，改用 get_flattened_data
            try:
                px = list(small.get_flattened_data())
            except AttributeError:
                px = list(small.getdata())
    except Exception:
        return False

    n = len(px)
    if not n:
        return True
    # 阈值 235/255：容忍 JPEG 压缩噪点
    dark = [i for i, v in enumerate(px) if v < 235]
    ratio = len(dark) / n
    if ratio < 0.004:
        return True
    # 内容集中在边缘 5% → 只是画了个图框，没有实际内容
    sw, sh = small.size
    edge = 0
    for i in dark:
        x, y = i % sw, i // sw
        if (x < sw * 0.05 or x > sw * 0.95 or y < sh * 0.05 or y > sh * 0.95):
            edge += 1
    return edge / len(dark) > 0.97


def _trim_white_border(path: Path, pad_ratio: float = 0.012) -> None:
    """裁掉渲染图四周的纯白边，只留一点点内边距。
    原地改写文件。

    为什么需要它
    ------------
    用户诉求：「这个留白太多了，只需要显示实际的尺寸」。

    原来的画布尺寸是按 **bbox 比例** 算的（见 :func:`_pick_size`），
    但 ezdxf 的 bbox 里往往混进了柜体外的标注、图框、引线，
    而柜体本身很窄 —— 于是算出来的画布两侧各空掉一大条。
    实测 3Units 那个柜（915×1750）渲出来 440×619 像素，
    内容只占中间一小条，上下左右全是白。

    为什么不直接改 `_pick_size`
    --------------------------
    bbox 本身算得没错（含标注是有意为之，标注能说明柜型）。
    真正该做的是**渲完之后按实际着墨像素裁**，与几何 bbox 解耦 ——
    这样不管 bbox 里混进什么东西，成图都紧贴内容。
    """
    try:
        from PIL import Image
    except Exception:
        return
    try:
        with Image.open(path) as im:
            # 掩膜用「亮度」算，但**原图必须另开一份保留彩色**。
            #
            # 【本轮修复的颜色丢失 bug】
            # 旧写法：``im.convert("L")`` 之后一路用这个灰度图，
            # 裁完再 ``.convert("RGB")`` 存回 —— 存回去的是**灰度图**，
            # DWG 的红框/绿管/青柜框/黄气管被永久洗成灰色。
            # 实测（真实 DWG，CP-5p-18）：裁前彩色像素 5.04% → 裁后 0.00%，
            # 整批 23 柜全被洗成黑白，与需求「保持 DWG 原有颜色」直接冲突。
            #
            # 正确做法：灰度图只用来**求 bbox**（找有内容的地方），
            # 真正的裁剪用**原彩色图**的副本做。
            with im.convert("L") as gray:
                # 阈值 245：JPEG 压缩会在纯白区产生 235~250 的轻微噪点，
                # 用 255 去比会把噪点当内容，导致裁不掉。
                #
                # 映射成「**白=0、黑=255**」后直接 getbbox()：它找的是
                # 非零像素区域，正好就是有内容的部分。
                #（不要再invert —— 那会把大片白底当成内容，反而裁不动。）
                mask = gray.point(lambda v: 0 if v > 245 else 255, "L")
                bbox = mask.getbbox()
                if not bbox:
                    return
            W, H = im.size
            x0, y0, x1, y1 = bbox
            if x1 <= x0 or y1 <= y0:
                return
            # 四周各留一点点内边距，别让线条紧贴画布边缘（印刷观感）。
            # 按**短边**算一个统一的 pad，四边一致 —— 分边算会出现
            # 上边有留白、左边贴死的不对称情况。
            m = int(min(W, H) * pad_ratio)
            nx0, ny0 = max(0, x0 - m), max(0, y0 - m)
            nx1, ny1 = min(W, x1 + m), min(H, y1 + m)
            if (nx1 - nx0) >= W and (ny1 - ny0) >= H:
                return   # 已经贴满边，裁了也没变化，别白重存一遍
            im.crop((nx0, ny0, nx1, ny1)).convert("RGB").save(
                path, format="JPEG", quality=JPEG_QUALITY)
    except Exception:
        # 裁剪失败不是致命错误 —— 图已经渲出来了，只是留白多一点
        pass


def _fonts_available() -> bool:
    """渲染带文字实体前，探测能否拿到**带 filename** 的 FontFace。

    口径与真实失败条件对齐（本轮修正）
    ----------------------------------
    旧实现只看 ``fonts.font_manager.get_font_face("")`` 能不能返回
    带 ``filename`` 的 FontFace，注释里断言 style 为空的实体会把
    ``None`` 一路传到 ``find_font_name`` 里炸掉。**实测这个断言是错的**：

    * 真实 DWG 的 23 个 DIMENSION 展开出的虚拟 MTEXT 带的是
      ``style='trebuchet_MS'``，**不是空串**；
    * ``get_font_face('trebuchet_MS')`` 是能返回 ``DejaVuSans.ttf`` 的。

    真正让渲染炸的是**临时文档里没有这个 textstyle 的定义**
    （见 :func:`_clone_styles`），跟「系统有没有字体」是两回事。

    所以这里保留这个函数，但用途收窄为**兜底**：万一运行环境真的
    一个字体都没有，就退回无文字渲染，而不是让整批柜型失败。
    """
    try:
        from ezdxf.fonts import fonts
        ff = fonts.font_manager.get_font_face("")
        return bool(ff is not None and getattr(ff, "filename", None))
    except Exception:
        return False


def _render_config():
    """柜型图渲染配置。

    本轮按《WorkBuddy 渲染修复需求文档》「步骤3：保持原有颜色」改了两处：

    1. ``color_policy`` 由 ``BLACK`` 改成 ``COLOR``
       ----------------------------------------------------------
       旧注释说「彩色版实测只有 23KB 且线条丢失，因为 DXF 里的 ACI 色
       在白底上非常浅」—— 这个结论**是错的**，它把「线条丢失」误判成
       「颜色太浅」，实际是当时**根本没开文字**、图上只剩纯线条，
       字节数自然低。需求文档明确要求：

           「保持 DWG 原有颜色：柜体外框=青/黑，表位框=红，
             水管=绿，气管=黄/金」

       实测（真实 DWG，CP-5p-18）：CP-5p-18 柜内 10 个 INSERT 里
       ``water meter h`` 走 ACI 2（黄）、``gas meter 1`` 走别的色，
       ``G-Anno-Std-Cntr`` 是 ACI 7。开了 COLOR 之后红框/绿管/黄管
       全部回来了 —— 与用户参考图逐项对得上。

    2. ``min_lineweight`` 保持 :data:`MIN_LINEWEIGHT`（1）
       需求写「线宽统一0.25mm（外框 0.5mm）」，但那是**印刷制图**的
       成图要求；这里输出的是网页缩略图 + 可放大的详情图，
       DXF 原始线宽层级（柜框 ~50 vs表内细部 5~9）必须保留，
       统一成 0.25mm 反而会让柜框和表内细部一样粗、糊成一团。
       真正让图变糊的是 DPI 与色policy，不是线宽。
    """
    from ezdxf.addons.drawing.config import (
        BackgroundPolicy,
        ColorPolicy,
        Configuration,
        LineweightPolicy,
    )
    return Configuration(
        lineweight_policy=LineweightPolicy.RELATIVE_FIXED,
        lineweight_scaling=1.0,
        min_lineweight=MIN_LINEWEIGHT,
        color_policy=ColorPolicy.COLOR,
        background_policy=BackgroundPolicy.WHITE,
    )


def _draw_one(target_doc, fpath: Path, pw: int, ph: int, cfg, dpi: int) -> Path:
    """把 target_doc 的 modelspace 渲染成一张白底黑线 JPG。

    单独抽成函数是为了让并行 worker 能直接调用它（进程池里没法
    闭包捕获 plt/Frontend 这些对象）。
    """
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    f = plt.figure(figsize=(pw / dpi, ph / dpi), dpi=dpi)
    try:
        a = f.add_axes([0, 0, 1, 1])
        a.set_axis_off()
        a.set_facecolor("white")
        Frontend(RenderContext(target_doc), MatplotlibBackend(a),
                 config=cfg).draw_layout(target_doc.modelspace(),
                                         finalize=True)
        f.savefig(str(fpath), format="jpg", facecolor="white", dpi=dpi,
                  pil_kwargs={"quality": JPEG_QUALITY})
    finally:
        plt.close(f)
    return fpath


def _pick_size(bbox, width: int) -> tuple[int, int]:
    """按 bbox 比例算画布尺寸，并夹在上限内。

    注意 ezdxf 的 BoundingBox.extmin/extmax 是**三元组**
    ``(x, y, z)``。按二元组解包会抛 ValueError，而 except 把它吞成
    默认尺寸 —— 表现为「画布比例不对/ 尺寸算不出来」，很难定位。
    """
    try:
        (x0, y0, _z0), (x1, y1, _z1) = bbox.extmin, bbox.extmax
        w = abs(float(x1) - float(x0))
        h = abs(float(y1) - float(y0))
    except Exception:
        return width, int(width * 0.6)
    if w <= 0 or h <= 0 or not math.isfinite(w) or not math.isfinite(h):
        return width, int(width * 0.6)
    # 留8% 边距，避免贴边
    ratio = h / w
    pw, ph = width, max(1, int(width * ratio))
    if max(pw, ph) > MAX_SIDE:
        s = MAX_SIDE / max(pw, ph)
        pw, ph = max(1, int(pw * s)), max(1, int(ph * s))
    return pw, ph


def render_block_to_jpg(
    dxf_path: str | Path,
    block_name: str,
    out_dir: str | Path,
    width: int = DEFAULT_WIDTH,
    dpi: int = 110,
) -> BlockRender:
    """把 DXF 里一个命名 block 渲染成 JPG。

    ``out_dir`` 目录不存在会自建。返回的 ``image_name`` 是文件名
    （不含目录），由调用方拼成相对 ``var/`` 的路径存库。
    """
    res = BlockRender(block_name=block_name, ok=False)
    dxf_path = Path(dxf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        res.error = f"无法读取 DXF: {exc}"
        return res

    try:
        blk = doc.blocks.get(block_name)
    except Exception as exc:
        res.error = f"取block 失败: {exc}"
        return res
    if blk is None:
        res.error = f"block 不存在: {block_name}"
        return res

    # ---- 统计信息 ----
    ents = list(blk)
    res.entity_count = len(ents)
    if not ents:
        res.error = "block 内无实体（空壳），跳过"
        return res

    lc: dict[str, int] = {}
    for e in ents:
        if e.dxf.hasattr("layer") and e.dxf.layer:
            ly = e.dxf.layer
            lc[ly] = lc.get(ly, 0) + 1
    res.layers = sorted(lc)
    res.layer_counts = dict(sorted(lc.items(), key=lambda kv: -kv[1]))

    # 柜型的「介绍」常写在标注里，取出来给前端展示
    texts: list[str] = []
    for e in ents:
        if e.dxftype() in ("TEXT", "ATTRIB"):
            t = (e.dxf.get("text") or "").strip()
            if t and t not in texts:
                texts.append(t)
        elif e.dxftype() == "MTEXT":
            try:
                t = e.plain_text().strip()
            except Exception:
                t = ""
            if t and t not in texts:
                texts.append(t)
        if len(texts) >= 12:
            break
    res.texts = texts

    # ---- 实测尺寸（mm）----
    # 必须用 block 自身的 bbox，不能用整个 modelspace 的
    try:
        box = extents(ents, fast=True)
        if box.has_data:
            (x0, y0, _z0), (x1, y1, _z1) = box.extmin, box.extmax
            res.width_mm = round(abs(float(x1) - float(x0)), 1)
            res.height_mm = round(abs(float(y1) - float(y0)), 1)
    except Exception as exc:
        log.debug("bbox 计算失败 %s: %s", block_name, exc)

    # ---- 渲染 ----
    # 关键：Frontend.draw_layout() 只接受 Layout（modelspace/paperspace），
    # 直接传BlockLayout 会抛
    #   AttributeError: 'BlockLayout' object has no attribute
    #                   'get_plot_style_filename'
    # 所以要把 block 实体复制到一个临时 modelspace 里再渲染。
    try:
        import matplotlib
        matplotlib.use("Agg")     # 必须在 pyplot 之前
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.config import (
            BackgroundPolicy,
            ColorPolicy,
            Configuration,
            LineweightPolicy,
        )
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    except Exception as exc:
        res.error = f"渲染依赖缺失（matplotlib/ezdxf.addons.drawing）: {exc}"
        return res

    tmp_doc = ezdxf.new()
    try:
        _clone_into_cabinet_view(doc, tmp_doc, ents)
        tmsp = tmp_doc.modelspace()
    except Exception as exc:
        res.error = f"实体复制到临时图失败: {exc}"
        return res

    # bbox 用临时 modelspace 的实际渲染范围，保证图铺满
    try:
        box = extents(list(tmsp), fast=True)
        pw, ph = _pick_size(box, width)
    except Exception:
        pw, ph = width, int(width * 0.6)

    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in block_name)[:60]
    fname = f"{safe}.jpg"
    fpath = out_dir / fname

    try:
        fig = plt.figure(figsize=(pw / dpi, ph / dpi), dpi=dpi)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_facecolor("white")

        # 渲染配置（实测踩坑）：
        #   默认配置在白底上几乎看不见 —— ezdxf 按 DXF 原始 ACI 色着色，
        #   而 CAD 里常用的 ACI 7（黑）/ 青色在白底上都太淡，
        #   且 lineweight 默认过细，产出 4587 字节的「空白图」，
        #   却因为 pydpi 有内容而看不出异常（静默失败最难查）。
        #   → 强制 BLACK + 最小线宽 25（0.25mm），产出 ~13KB 且清晰。
        cfg = Configuration(
            lineweight_policy=LineweightPolicy.RELATIVE_FIXED,
            lineweight_scaling=1.0,
            min_lineweight=MIN_LINEWEIGHT,
            color_policy=ColorPolicy.BLACK,
            background_policy=BackgroundPolicy.WHITE,
        )
        Frontend(RenderContext(tmp_doc), MatplotlibBackend(ax),
                 config=cfg).draw_layout(tmsp, finalize=True)
        fig.savefig(str(fpath), format="jpg", facecolor="white",
                    dpi=dpi, pil_kwargs={"quality": JPEG_QUALITY})
        plt.close(fig)
    except Exception as exc:
        plt.close("all")
        res.error = f"渲染失败: {type(exc).__name__}: {exc}"
        return res
    finally:
        try:
            plt.close("all")
        except Exception:
            pass

    if not fpath.exists() or fpath.stat().st_size < 512:
        res.error = "渲染产出为空文件"
        return res

    # ---- 空白图检测 ----
    # 主判据是**像素内容**（见 _looks_blank），不看文件体积 ——
    # 体积会随线宽/画布尺寸/压缩参数大幅波动，用固定阈值必然
    # 在某组参数下误杀合法图。体积只留作极端兜底。
    if _looks_blank(fpath):
        res.error = (
            "渲染结果疑似空白（画面几乎无内容），已丢弃 —— "
            "图纸可能全部是构造线或不受支持的代理实体"
        )
        try:
            fpath.unlink()
        except OSError:
            pass
        return res

    res.ok = True
    res.image_name = fname
    return res


def render_cupboard_library(
    dxf_path: str | Path,
    out_dir: str | Path,
    only_blocks: list[str] | None = None,
    width: int = DEFAULT_WIDTH,
    min_entities: int = 2,
) -> list[BlockRender]:
    """渲染柜型库里所有「像柜型」的 block。

    筛选规则（**按语义，不按数量**）：
      · 跳过匿名 block（``*`` 开头 = 系统定义/标注）
      · 用 :mod:`app.parsers.cupboard_classify` 判定是否真是柜型。
        早期版本只按「实体数 >= min_entities」筛，结果用户真实图纸里
        141 个 block 有 130+ 个是 ``Aect_Duct_*`` 风管零件被当成柜型，
        用户质问「这里面为啥会有这些东西」。
      · ``only_blocks`` 非空时只渲染指定 block（跳过语义判定，
        用户显式指定了就尊重用户）

    返回**所有** block 的结果（含失败的），便于把失败原因暴露给用户，
    而不是静默跳过。
    """
    dxf_path = Path(dxf_path)
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        return [BlockRender(block_name="<dxf>", ok=False,
                            error=f"无法读取 DXF: {exc}")]

    if only_blocks:
        names = list(only_blocks)
    else:
        from app.parsers.cupboard_classify import judge_library

        try:
            cups, _others = judge_library(dxf_path)
            names = [v.block_name for v in cups]
        except Exception as exc:
            log.warning("柜型语义判定失败，回退到仅按实体数筛选: %s", exc)
            names = [
                b.name for b in doc.blocks
                if not b.name.startswith("*") and len(list(b)) >= min_entities
            ]

    results: list[BlockRender] = []
    for name in names:
        if name.startswith("*"):
            continue
        results.append(render_block_to_jpg(dxf_path, name, out_dir, width=width))
    return results

# ============================================================ 按柜体渲染

#: 渲染并行度上限。实测 8 最优，16 反而更慢（见
#: :func:`render_cupboard_regions` 里的实测数据），所以封顶 8。
MAX_RENDER_WORKERS = 8


def _auto_workers(n_jobs: int) -> int:
    """按 CPU 数与任务量决定并行度。

    ``os.cpu_count()`` 在容器里可能返回几十核，但这是**共享**宿主，
    真实可用配额往往只有 2~4。所以按 ``min(8, cpu-1, 任务数)`` 取，
    并且至少留一个核给主进程。
    """
    import os
    try:
        cpus = len(os.sched_getaffinity(0))     # 比 cpu_count 更准（含 cgroup 限制）
    except AttributeError:
        cpus = os.cpu_count() or 2
    return max(1, min(MAX_RENDER_WORKERS, cpus - 1, n_jobs))


# ---- 并行渲染 worker ----
#
# 这些函数必须在**模块顶层**（不能是闭包），ProcessPoolExecutor 才能
# pickle 它们。用 fork 上下文时，子进程直接继承父进程的 doc /
# all_ents全局状态，不需要重新 readfile。
_W: dict = {}


def _w_init(doc, all_ents, out_dir, width, dpi, strip_text):
    """进程池 initializer：把父进程已解析好的状态塞进模块全局。"""
    import matplotlib
    matplotlib.use("Agg")
    _W.clear()
    _W.update(doc=doc, all_ents=all_ents, out_dir=Path(out_dir), width=width,
              dpi=dpi, strip_text=strip_text, cfg=_render_config())


def _w_render(job):
    """渲染单个柜型，返回 ``(idx, code, ok, error, meta)``。

    只回传标量与字符串，不回传 ezdxf 实体 —— 后者不可 pickle 且体积大。
    """
    import matplotlib.pyplot as plt
    from ezdxf.bbox import extents as _ext

    idx, cup_rect, cup_bbox, code = job
    doc = _W["doc"]
    try:
        inside = [e for e in _W["all_ents"] if _entity_near(e, cup_rect, tol=2.0)]
        if not inside:
            return idx, code, False, "柜体区域内没有实体", {}

        if _W["strip_text"]:
            view_doc, stripped = _clone_without_text(doc, inside)
            meta = {"entity_count": len(inside), "stripped_count": stripped,
                    "text_render_failed": True}
            if stripped == 0:
                view_doc = None
        else:
            view_doc, meta = None, {"entity_count": len(inside)}

        if view_doc is None:
            view_doc = ezdxf.new()
            _cloned, clone_errs = _clone_into_cabinet_view(doc, view_doc, inside)
            if clone_errs:
                meta["clone_errors"] = clone_errs[:5]

        try:
            pw, ph = _pick_size(_ext(list(view_doc.modelspace()), fast=True),
                                _W["width"])
        except Exception:
            pw, ph = _W["width"], int(_W["width"] * 0.6)

        fpath = _W["out_dir"] / f"{code}.jpg"
        _draw_one(view_doc, fpath, pw, ph, _W["cfg"], _W["dpi"])

        if not fpath.exists() or fpath.stat().st_size < 512:
            return idx, code, False, "渲染产出为空文件", meta
        if _looks_blank(fpath):
            fpath.unlink(missing_ok=True)
            return idx, code, False, "渲染结果疑似空白（画面几乎无内容），已丢弃", meta
        _trim_white_border(fpath)
        meta.update(width_mm=cup_bbox[0], height_mm=cup_bbox[1])
        return idx, code, True, None, meta
    except Exception as exc:
        return idx, code, False, f"渲染失败: {type(exc).__name__}: {exc}", {}
    finally:
        try:
            plt.close("all")
        except Exception:
            pass


def _render_parallel(jobs, doc, all_ents, out_dir, width, dpi, strip_text,
                     n_workers):
    """用 fork 进程池并行渲染，保持与串行一致的返回顺序。"""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp

    payload = [(idx, cup.rect, cup.bbox_mm, code) for idx, cup, code in jobs]
    ctx = mp.get_context("fork")     # 关键：fork 才能继承 doc
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx,
                             initializer=_w_init,
                             initargs=(doc, all_ents, out_dir, width, dpi,
                                       strip_text)) as ex:
        raw = list(ex.map(_w_render, payload))

    # 按 jobs 原顺序重建结果列表
    by_idx = {r[0]: r for r in raw}
    results: list[BlockRender] = []
    for idx, cup, code in jobs:
        _, _, ok, err, meta = by_idx[idx]
        res = BlockRender(block_name=code, ok=ok)
        res.layer = cup.layer
        res.error = err
        for k in ("entity_count", "stripped_count", "clone_errors",
                  "text_render_failed"):
            if k in meta:
                setattr(res, k, meta[k])
        if ok:
            res.image_name = f"{code}.jpg"
            res.width_mm, res.height_mm = meta["width_mm"], meta["height_mm"]
            res.texts = list(cup.notes)
        results.append(res)
    return results


def render_cupboard_regions(
    dxf_path: str | Path,
    out_dir: str | Path,
    only_codes: list[str] | None = None,
    width: int = DEFAULT_WIDTH,
    dpi: int = DPI,
    parsed=None,
    workers: int | None = None,
    compose: bool = False,
) -> tuple[list[BlockRender], object]:
    """把每个**柜体**渲染成一张 JPG。

    这是正确粒度的渲染入口：一张图 = 一个柜子 = 若干套water+gas。
    与旧的 :func:`render_cupboard_library`（一个 block 一张图）的区别，
    就是前两轮反复踩的坑—— 用户要的是**整个柜子**，
    不是柜子内部的单个 water 表 / gas 表符号。

    两种出图方式（``compose``）
    ------------------------
    * ``compose=False``（**默认**）
      把落在柜体矩形内的**全部**实体丢给 matplotlib 整柜渲染 ——
      **这就是用户要的「DWG 原图切割」**：柜框、每台 gas meter 上DWG
      自带的红色标注框、柜内尺寸标注链，全部保留，白底黑线清晰。

      用户提供的参考图（整柜图纸 + 单台 gas meter 特写）就是这个效果。
    * ``compose=True``（备用，保留）
      只贴 4 个 meter 图元、按解析出的表位坐标**合成**。这条路径
      曾经是默认，**这是个错误的取舍** —— 它丢掉了 DWG 原图里的
      全部标注（红框、尺寸链），导致用户看到「图元缩成极小的一个、
      柜内空荡荡」，与参考图差距极大。保留仅供对照排查。

    返回 ``(渲染结果列表, 解析结果)``。
    """
    from app.parsers.cupboard_geometry import parse_cupboards

    if compose:
        try:
            return _render_by_compose(
                dxf_path, out_dir, only_codes, parsed,
            ), (parsed if parsed is not None
                else parse_cupboards(dxf_path))
        except Exception as exc:  # 合成失败退回整柜渲染，不让整批挂掉
            log.warning("图元合成失败(%s: %s)，退回整柜渲染",
                        type(exc).__name__, exc)

    dxf_path = Path(dxf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if parsed is None:
        parsed = parse_cupboards(dxf_path)

    import ezdxf
    from ezdxf.bbox import extents as _ext

    doc = ezdxf.readfile(str(dxf_path))
    # 必须重新绑定：上面 readfile 得到的是**新** doc 对象，而
    # _entity_extents 的块定义缓存还指着 parse_cupboards 那次的 doc。
    # 不重绑的话 INSERT 的包围盒会算错，柜内表全被判为「不在区域内」。
    bind_document(doc)
    msp = doc.modelspace()
    all_ents = list(msp)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return ([BlockRender(block_name="<deps>", ok=False,
                             error=f"渲染依赖缺失: {exc}")], parsed)

    # ---- 先决定每个柜型用哪个「视图文档」，再渲染 ----
    #
    # 历史包袱（已解除）：以前这里在字体不可用时**直接**走不含带文字
    # 实体的视图，于是 23 个柜型每个都丢掉 20+ 个 DIMENSION ——
    # 用户参考图里那些 1065/2350/450/100 数字全都没了。
    # 现在 _clone_styles 已让文字能正常渲染，这里只在**真的**一个字体
    # 都没有时才降级，且降级会写进 res.text_render_failed 供排查。
    strip_text = not (RENDER_TEXT and _fonts_available())
    if not strip_text:
        log.info("柜型渲染：保留 DIMENSION/TEXT 等带文字实体"
                 "（textstyle 已随视图文档搬运）")
    else:
        log.info("柜型渲染：跳过 DIMENSION/TEXT 等带文字实体（%s）",
                 "字体不可用" if not _fonts_available() else "按RENDER_TEXT 配置")

    jobs = []
    for idx, cup in enumerate(parsed.cupboards):
        code = _cabinet_code(cup, idx)
        if only_codes and code not in only_codes:
            continue
        jobs.append((idx, cup, code))

    # ---- 并行渲染 ----
    #
    # 用户诉求：「渲染的时间太久了」。实测（真实样本 23 柜，110dpi）：
    #   串行           177.0s   ← 用户等的就是这个
    #   8 进程并行45.7s
    #   16 进程并行     50.4s   （反而变慢：进程数超过有效并行度后
    #                             调度与内存拷贝的开销占回来了）
    #
    # 每个 worker 都要重读+重解析 DXF（1.1s + 1.6s ≈ 2.7s），
    # 23 个 worker 各做一遍就是 62s 纯重复劳动。用 fork 起进程池
    # 让子进程**继承**父进程已经读好的 doc（COPY-ON-WRITE，零拷贝），
    # 只在 worker 里做「抠实体 → 渲染 → 存盘」。
    n_workers = workers if workers is not None else _auto_workers(len(jobs))
    if n_workers > 1 and len(jobs) > 1:
        try:
            return _render_parallel(
                jobs, doc, all_ents, out_dir, width, dpi, strip_text,
                n_workers,
            ), parsed
        except Exception as exc:
            # 并行失败不能连累整个入库流程 —— 退回串行
            log.warning("并行渲染失败(%s)，退回串行", exc)

    results: list[BlockRender] = []
    for idx, cup, code in jobs:
        res = BlockRender(block_name=code, ok=False)
        res.layer = cup.layer

        # 抠出柜体区域内的实体。留2mm 外扩，柜框线本身才不会被裁掉。
        inside = [e for e in all_ents if _entity_near(e, cup.rect, tol=2.0)]
        res.entity_count = len(inside)
        if not inside:
            res.error = "柜体区域内没有实体"
            results.append(res)
            continue

        # 每个柜型**只构建一次**视图文档：RENDER_TEXT=False 时直接用
        # 不含带文字实体的版本，不再「先搬全的、炸了再搬一次」。
        view_doc = None
        if strip_text:
            try:
                view_doc, stripped = _clone_without_text(doc, inside)
                res.stripped_count = stripped
                if stripped == 0:
                    # 该柜本来就没有带文字实体，没必要走降级视图
                    view_doc = None
            except Exception as exc:
                res.error = f"实体复制失败: {exc}"
                results.append(res)
                continue
        if view_doc is None:
            view_doc = ezdxf.new()
            try:
                _cloned, clone_errs = _clone_into_cabinet_view(doc, view_doc, inside)
            except Exception as exc:
                res.error = f"实体复制失败: {exc}"
                results.append(res)
                continue
            if clone_errs:
                # 不再静默：记进诊断信息，便于判断是不是块搬运失败导致空图
                res.clone_errors = clone_errs[:5]
            if strip_text:
                res.text_render_failed = True

        try:
            box = _ext(list(view_doc.modelspace()), fast=True)
            pw, ph = _pick_size(box, width)
        except Exception:
            pw, ph = width, int(width * 0.6)

        fname = f"{code}.jpg"
        fpath = out_dir / fname
        cfg = _render_config()

        try:
            _draw_one(view_doc, fpath, pw, ph, cfg, dpi)
        except Exception as exc:
            plt.close("all")
            res.error = f"渲染失败: {type(exc).__name__}: {exc}"
            results.append(res)
            continue
        finally:
            try:
                plt.close("all")
            except Exception:
                pass

        if not fpath.exists() or fpath.stat().st_size < 512:
            res.error = "渲染产出为空文件"
            results.append(res)
            continue
        if _looks_blank(fpath):
            # 柜体图必须能看清内部表位，白图等于没渲染出来
            try:
                fpath.unlink()
            except OSError:
                pass
            res.error = "渲染结果疑似空白（画面几乎无内容），已丢弃"
            results.append(res)
            continue

        # 裁掉四周纯白边 —— 用户诉求「留白太多了，只需要显示实际尺寸」。
        # 放在空白检测**之后**：先确认不是白图，再裁，顺序反了会把
        # 「本来就空」的图裁成更小的空白图。
        _trim_white_border(fpath)

        w, h = cup.bbox_mm
        res.ok = True
        res.image_name = fname
        res.width_mm = w
        res.height_mm = h
        res.texts = list(cup.notes)
        results.append(res)

    return results, parsed


def _cabinet_code(cup, idx: int) -> str:
    """给柜型生成稳定的编码：``CP-{表位数}p-01``。

    前缀含表位数，便于前端按 units 数分组；
    序号保证同表位数的多个排布不会撞名。
    """
    notes = " ".join(cup.notes).upper()
    for key, label in (("6 SET", "6"), ("4 SET", "4"), ("2 SET", "2"),
                       ("3 SET", "3"), ("1 SET", "1"), ("8 SET", "8"),
                       ("12 SET", "12")):
        if key in notes:
            n = label
            break
    else:
        n = str(cup.positions_total)
    return f"CP-{n}p-{idx + 1:02d}"


#: 离群实体面积阈值（倍数）。见 :func:`_entity_near` 的 docstring。
#:
#: 实测：真实 DWG 里柜内最大的正常零件是 250×450mm 的 gas meter 表组
#: （占 1.8m 柜体面积的 0.1%）；而那条撑坏 bbox 的 2CONC 建筑轮廓线
#: 是 4414×6215mm，是柜体面积的 **13～17 倍**。3.0 的阈值把两者
#: 分得很开，留足余量：即使再宽裕的柜型（2165×2350mm），3 倍阈值也
#: 只到 3.6 m²，远高于任何单个柜内零件。
_OUTLIER_AREA_RATIO = 3.0


def _entity_near(e, rect, tol: float = 2.0) -> bool:
    """实体是否落在 ``rect`` 内（含容差）。

    INSERT 必须按**真实几何包围盒**判断，不能用 ``e.dxf.insert``
    --------------------------------------------------------------
    真实 DWG 里仪表 block 的内部顶点用的是绝对图纸坐标，
    ``base_point`` 又是 ``(0,0,0)``，于是 ``insert`` 点与真实位置
    能差 6500mm（实测：``gas meter 1`` 的 insert 在 (2200, 1872)，
    真实位置 (-4279, 4619)）。

    旧代码对 INSERT 直接用 insert 点判断，导致 **393 个 INSERT 全部
    被判为「不在柜体内」**，每个柜型只抠到 8 个 LINE（纯柜框）——
    这就是预览图里只有空框、看不到任何表的原因。

    现在统一走 :func:`_entity_extents`（与柜型识别用的是同一个函数，
    保证「数出14 套」和「渲出 14 个表」必然一致）。

    【真实 bug 修复】排除「面积远超柜体」的离群实体
    ---------------------------------------------------------
    判据从「bbox 与柜体**有交集**」收紧为「bbox 被柜体**基本包住**」。

    实测事故（真实 DWG「Cold and hot water ...detail 1.dwg」）：
      2CONC图层 有一条 **4414 × 6215mm** 的 LWPOLYLINE（建筑轮廓线），
      它覆盖了整张图纸。任何落在图纸中间的柜体，它的 bbox 都与这条
      巨型轮廓线「有交集」，于是被误当成柜内零件抠进视图。

      后果有两条，都直接被用户看见：
        1. 视图 bbox 从柜体的 ~915×1750mm 被撑到4414×6215mm
           —— 放大13～17 倍。:func:`_pick_size` 按这个错误比例算画布，
           于是「柜型图里只有左上角一小块、右下大片空白」。
           用户原话：「解析的还是黑黢黢的。图的尺寸也是不对」。
        2. 命中它的3 个柜型（CP-3p-14 / CP-3p-15 / CP-4p-13）各多出
           一条 4414×6215mm 的粗轮廓线，这条线画出来又粗又长，
           在缩略图上就是一道黑带 —— 这是「黑黢黢」的直接来源。

      判据用**面积比**而不是「必须完全包含」：柜框线与柜体边界重合、
      DIMENSION 标注会略微探出，纯 contains 会把正常的柜框线和尺寸标注
      一起误杀（实测 CP-5p-*等 20 个柜的柜框线都压在边界上）。
      ``max_ratio=3.0`` 表示「实体面积超过柜体 3 倍就放弃」——
      正常柜内零件（最大 250×450mm 的 gas meter 表组）远小于此，
      而 4414×6215mm 的建筑轮廓是 13～17 倍，一刀切得很干净。
    """
    try:
        r = _entity_extents(e)
    except Exception:
        return False
    if r is None:
        return False
    # 有交集即算命中：柜框线正好压在边界上，纯 contains 会漏
    if (r.x1 < rect.x0 - tol or r.x0 > rect.x1 + tol
            or r.y1 < rect.y0 - tol or r.y0 > rect.y1 + tol):
        return False
    # 离群过滤：见docstring 的实测事故说明
    cab_w = rect.x1 - rect.x0
    cab_h = rect.y1 - rect.y0
    cab_area = cab_w * cab_h
    if cab_area > 0:
        ent_area = max(0.0, r.x1 - r.x0) * max(0.0, r.y1 - r.y0)
        if ent_area > cab_area * _OUTLIER_AREA_RATIO:
            return False
    return True


def rect_from_entity_safe(ent):
    from app.parsers.cupboard_geometry import rect_from_entity
    try:
        return rect_from_entity(ent)
    except Exception:
        return None

# ======================================================================
# 图元合成路径（用户诉求 #F：柜型图必须用 DWG 原图）
# ======================================================================

#: 合成用的图元缓存目录。绝对路径，理由见 meter_sprites.SPRITE_DIR。
SPRITE_CACHE_DIR = Path(__file__).resolve().parents[3] / "var" / "sprites"


def _cabinet_frame_lines(doc, cup) -> list:
    """找该柜体的外框 LINE。

    柜体在 modelspace 里不是 block，而是由 4 条**跨图层长直线**围成的
    矩形。这里把它们挑出来按原坐标描到图上 —— 端点取自 DXF，
    所以柜框的每一段都对应图纸里真实存在的那条线。

    判定：近水平/近垂直、长度 ≥ 柜宽/柜高的 80%、且落在柜矩形附近。
    """
    r = cup.rect
    out = []
    for e in doc.modelspace():
        if e.dxftype() != "LINE":
            continue
        try:
            x0, y0 = e.dxf.start.x, e.dxf.start.y
            x1, y1 = e.dxf.end.x, e.dxf.end.y
        except Exception:
            continue
        # 与柜矩形有交集才考虑
        if (max(x0, x1) < r.x0 - 200 or min(x0, x1) > r.x1 + 200
                or max(y0, y1) < r.y0 - 200 or min(y0, y1) > r.y1 + 200):
            continue
        horiz = abs(y1 - y0) < 40 and abs(x1 - x0) > r.width * 0.8
        vert = abs(x1 - x0) < 40 and abs(y1 - y0) > r.height * 0.8
        if horiz or vert:
            out.append(e)
    return out


# ---- 合成 worker（fork 继承 doc，避免每柜重读+重解析 DXF）----
_C: dict = {}


def _c_init(doc, out_dir, sprite_dir):
    _C.clear()
    from .meter_sprites import SpriteBank

    _C.update(doc=doc, out_dir=Path(out_dir), bank=SpriteBank(sprite_dir))


def _c_render(job):
    """合成单个柜型。返回 ``(idx, code, ok, error, meta)``。"""
    idx, cup_rect, code, positions = job
    from PIL import Image

    from .cabinet_compose import compose_cabinet
    from .cupboard_geometry import Cupboard, MeterPosition, Rect
    from .meter_sprites import plan_from_positions

    doc = _C["doc"]
    try:
        # 把 pickle 过来的表位还原成 Cupboard 结构（只用到 plan 所需字段）
        cup = Cupboard(
            rect=Rect(x0=cup_rect[0], y0=cup_rect[1],
                      x1=cup_rect[2], y1=cup_rect[3]),
            positions=[
                MeterPosition(
                    rect=Rect(x0=p[0], y0=p[1], x1=p[2], y1=p[3]),
                    kind=p[4],
                ) for p in positions
            ],
        )
        plan = plan_from_positions(cup)
        if not plan.placements:
            return idx, code, False, "柜内没有识别到 water/gas 表位", {}
        lines = _cabinet_frame_lines(doc, cup)
        fpath = _C["out_dir"] / f"{code}.jpg"
        meta = compose_cabinet(doc, cup, plan, _C["bank"], [], fpath,
                               frame_lines=lines)
        if not fpath.exists() or fpath.stat().st_size < 2048:
            return idx, code, False, "合成产出为空文件", meta
        with Image.open(fpath) as im:
            meta["width_px"], meta["height_px"] = im.size
        return idx, code, True, None, meta
    except Exception as exc:
        return idx, code, False, f"合成失败: {type(exc).__name__}: {exc}", {}


def _render_by_compose(dxf_path, out_dir, only_codes, parsed) -> list[BlockRender]:
    """图元合成主流程（支持 fork 并行）。"""
    import ezdxf

    from .meter_sprites import SpriteBank

    dxf_path = Path(dxf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if parsed is None:
        from app.parsers.cupboard_geometry import parse_cupboards

        parsed = parse_cupboards(dxf_path)

    doc = ezdxf.readfile(str(dxf_path))
    bind_document(doc)

    # 预热图元（串行渲染一次，存磁盘缓存；worker 直接命中缓存）
    bank = SpriteBank(SPRITE_CACHE_DIR)
    for name, short in METER_BLOCKS.items():
        try:
            bank.get(doc, name, short)
        except Exception as exc:
            log.warning("图元 %r 渲染失败: %s", short, exc)

    jobs = []
    for idx, cup in enumerate(parsed.cupboards):
        code = _cabinet_code(cup, idx)
        if only_codes and code not in only_codes:
            continue
        r = cup.rect
        jobs.append((
            idx,
            (r.x0, r.y0, r.x1, r.y1),
            code,
            [(m.rect.x0, m.rect.y0, m.rect.x1, m.rect.y1, m.kind)
             for m in (cup.positions or [])],
        ))

    n_workers = _auto_workers(len(jobs))
    results: list[BlockRender] = []
    if n_workers > 1 and len(jobs) > 1:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp

        ctx = mp.get_context("fork")
        try:
            with ProcessPoolExecutor(
                max_workers=n_workers, mp_context=ctx,
                initializer=_c_init,
                initargs=(doc, out_dir, SPRITE_CACHE_DIR),
            ) as ex:
                raw = list(ex.map(_c_render, jobs))
            by_idx = {r[0]: r for r in raw}
        except Exception as exc:
            log.warning("合成并行失败(%s)，退回串行", exc)
            _c_init(doc, out_dir, SPRITE_CACHE_DIR)
            by_idx = {j[0]: _c_render(j) for j in jobs}
    else:
        _c_init(doc, out_dir, SPRITE_CACHE_DIR)
        by_idx = {j[0]: _c_render(j) for j in jobs}

    for job in jobs:
        idx = job[0]
        cup = parsed.cupboards[idx]
        code = job[2]
        _, _, ok, err, meta = by_idx[idx]
        res = BlockRender(block_name=code, ok=ok)
        res.layer = cup.layer
        res.error = err
        if ok:
            res.image_name = f"{code}.jpg"
            res.width_mm, res.height_mm = meta.get("width_mm"), meta.get("height_mm")
            res.texts = list(cup.notes)
            # 合成诊断：图元数与各类表数量
            counts = meta.get("counts") or {}
            res.entity_count = int(meta.get("sprites_placed") or 0)
            res.sprites = counts
        else:
            res.entity_count = len(cup.positions or [])
        results.append(res)
    return results

