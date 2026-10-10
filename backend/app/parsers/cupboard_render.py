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
#: 历史：默认配置下白底上几乎看不见（产出 4587字节的「空白图」），
#: 于是把它调到 25（0.25mm）强行加粗 —— 那是在**按 block 渲染**时期，
#: 因为整个 block 缩到一张图里，线条太细就真的看不见了。
#:
#: 现在改回细线：按**柜体区域**渲染后，一个柜子独占一张图，
#: 细节足够大。实测 min_lineweight=25 会把 150×190mm 的表位框
#: 糊成一块黑斑，完全看不出柜内排布；改成 6 才能看清
#: 「水表竖线+圆头」与「气表框」的结构。
#:
#: 6 = 0.06mm，在 1400px 宽的图上约 1~2px，清晰且不糊。
MIN_LINEWEIGHT = 6

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

    used: set[str] = set()
    for e in ents:
        if e.dxftype() == "INSERT":
            try:
                used.add(str(e.dxf.name))
            except Exception:
                pass

    for name in used:
        try:
            src_block = src_doc.blocks.get(name)
        except Exception:
            continue
        try:
            dst_block = tmp_doc.blocks.get(name)
            if dst_block is None:
                dst_block = tmp_doc.blocks.new(name)
        except Exception as exc:
            errors.append(f"块 {name}: {exc}")
            continue
        dst_space = dst_block.entity_space
        for be in src_block:
            try:
                dst_space.add(be.copy())
            except Exception as exc:
                errors.append(f"块 {name}/{be.dxftype()}: {exc}")

    ok = 0
    target = tmp_doc.modelspace().entity_space
    for e in ents:
        try:
            target.add(e.copy())
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

    used: set[str] = set()
    for e in kept:
        if e.dxftype() == "INSERT":
            try:
                used.add(str(e.dxf.name))
            except Exception:
                pass

    for name in used:
        try:
            src_block = src_doc.blocks.get(name)
        except Exception:
            continue
        try:
            dst_block = tmp_doc.blocks.get(name)
            if dst_block is None:
                dst_block = tmp_doc.blocks.new(name)
        except Exception:
            continue
        dst_space = dst_block.entity_space
        for be in src_block:
            if be.dxftype() in drop:
                continue
            try:
                dst_space.add(be.copy())
            except Exception:
                continue

    target = tmp_doc.modelspace().entity_space
    for e in kept:
        try:
            target.add(e.copy())
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
            im = im.convert("L")
            # 阈值 245：JPEG 压缩会在纯白区产生 235~250 的轻微噪点，
            # 用 255 去比会把噪点当内容，导致裁不掉。
            #
            # 映射成「**白=0、黑=255**」后直接 getbbox()：它找的是
            # 非零像素区域，正好就是有内容的部分。
            #（不要再invert —— 那会把大片白底当成内容，反而裁不动。）
            mask = im.point(lambda v: 0 if v > 245 else 255, "L")
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

def render_cupboard_regions(
    dxf_path: str | Path,
    out_dir: str | Path,
    only_codes: list[str] | None = None,
    width: int = DEFAULT_WIDTH,
    dpi: int = 110,
    parsed=None,
) -> tuple[list[BlockRender], object]:
    """把每个**柜体**渲染成一张 JPG。

    这是正确粒度的渲染入口：一张图 = 一个柜子 = 若干套water+gas。
    与旧的 :func:`render_cupboard_library`（一个 block 一张图）的区别，
    就是前两轮反复踩的坑—— 用户要的是**整个柜子**，
    不是柜子内部的单个 water 表 / gas 表符号。

    实现方式：柜体在 modelspace 里是一个矩形区域，不是一个命名 block。
    所以把**落在该矩形内的所有实体**复制到临时 modelspace 再渲染，
    等价于「把柜子这一块单独抠出来画」。

    返回 ``(渲染结果列表, 解析结果)``。
    """
    from app.parsers.cupboard_geometry import parse_cupboards

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
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.config import (
            BackgroundPolicy,
            ColorPolicy,
            Configuration,
            LineweightPolicy,
        )
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    except Exception as exc:
        return ([BlockRender(block_name="<deps>", ok=False,
                             error=f"渲染依赖缺失: {exc}")], parsed)

    results: list[BlockRender] = []
    for idx, cup in enumerate(parsed.cupboards):
        code = _cabinet_code(cup, idx)
        res = BlockRender(block_name=code, ok=False)
        res.layer = cup.layer

        if only_codes and code not in only_codes:
            continue

        # 抠出柜体区域内的实体。留2mm 外扩，柜框线本身才不会被裁掉。
        inside = [e for e in all_ents if _entity_near(e, cup.rect, tol=2.0)]
        res.entity_count = len(inside)
        if not inside:
            res.error = "柜体区域内没有实体"
            results.append(res)
            continue

        tmp = ezdxf.new()
        try:
            _cloned, clone_errs = _clone_into_cabinet_view(doc, tmp, inside)
            tmsp = tmp.modelspace()
        except Exception as exc:
            res.error = f"实体复制失败: {exc}"
            results.append(res)
            continue
        if clone_errs:
            # 不再静默：记进诊断信息，便于判断是不是块搬运失败导致空图
            res.clone_errors = clone_errs[:5]

        try:
            box = _ext(list(tmsp), fast=True)
            pw, ph = _pick_size(box, width)
        except Exception:
            pw, ph = width, int(width * 0.6)

        fname = f"{code}.jpg"
        fpath = out_dir / fname
        cfg = None
        try:
            from ezdxf.addons.drawing.config import (
                BackgroundPolicy,
                ColorPolicy,
                Configuration,
                LineweightPolicy,
            )
            cfg = Configuration(
                lineweight_policy=LineweightPolicy.RELATIVE_FIXED,
                lineweight_scaling=1.0,
                min_lineweight=MIN_LINEWEIGHT,
                color_policy=ColorPolicy.BLACK,
                background_policy=BackgroundPolicy.WHITE,
            )
        except Exception:
            cfg = None

        # ---- 渲染主流程（含文字降级）----
        def _draw(target_doc) -> Path:
            """把 target_doc 渲染到 fpath，返回 fpath。"""
            f = plt.figure(figsize=(pw / dpi, ph / dpi), dpi=dpi)
            try:
                a = f.add_axes([0, 0, 1, 1])
                a.set_axis_off()
                a.set_facecolor("white")
                Frontend(RenderContext(target_doc), MatplotlibBackend(a),
                         config=cfg).draw_layout(
                             target_doc.modelspace(), finalize=True)
                f.savefig(str(fpath), format="jpg", facecolor="white",
                          dpi=dpi, pil_kwargs={"quality": JPEG_QUALITY})
            finally:
                plt.close(f)
            return fpath

        try:
            try:
                _draw(tmp)
            except Exception as exc:
                # 标注文字渲染失败不该让整张柜型图挂掉。
                #
                # 实测踩坑（真实样本 20 个柜型**全部**失败）：
                #   1.首次报的是 MTEXT 的字体查找失败
                #      (``fonts.find_font_file_name`` →
                #       ``AttributeError: 'NoneType' object has no
                #       attribute 'filename'``）；
                #   2. 剥掉 TEXT/MTEXT 后仍然失败 —— 因为柜型区域内
                #      一个 TEXT 都没有，真正触发字体查找的是
                #      **50 个 DIMENSION**，标注文字同样要查字体。
                #
                # 降级：把 DIMENSION / LEADER / TEXT 等「带文字的实体」
                # 全部删掉，只留线条与块图形重渲。图里少了标注说明，
                # 但柜型排布、表位、结构都完整 —— 对「看清柜型」这个
                # 用途来说远比一张白图有用。
                log.warning("柜型 %s 标注渲染失败(%s)，降级为纯几何渲染",
                            code, exc)
                res.text_render_failed = True
                stripped_doc, stripped = _clone_without_text(doc, inside)
                res.stripped_count = stripped
                if not stripped:
                    raise
                _draw(stripped_doc)
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
    """
    try:
        r = _entity_extents(e)
    except Exception:
        r = None
    if r is None:
        return False
    # 有交集即算命中：柜框线正好压在边界上，纯 contains 会漏
    return not (r.x1 < rect.x0 - tol or r.x0 > rect.x1 + tol
                or r.y1 < rect.y0 - tol or r.y0 > rect.y1 + tol)


def rect_from_entity_safe(ent):
    from app.parsers.cupboard_geometry import rect_from_entity
    try:
        return rect_from_entity(ent)
    except Exception:
        return None
