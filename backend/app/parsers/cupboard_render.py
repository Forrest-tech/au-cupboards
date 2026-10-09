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

log = logging.getLogger(__name__)

#: 渲染输出目录名（相对 var/）
THUMB_DIR = "thumbs"

#: 默认渲染宽度（像素）。1400足够看清柜型排布又不至于太慢。
DEFAULT_WIDTH = 1400

#: 图片最长边像素上限 —— 防止超大图纸生成几十MB 的 JPG
MAX_SIDE = 2600

#: JPEG 质量
JPEG_QUALITY = 88

#: 渲染线宽（1/100 mm）。默认配置太细，白底上几乎不可见。
MIN_LINEWEIGHT = 25

#: 产出 JPG 的最小合理字节数。
#:
#: 判定「空白图」的依据：一张1400px宽的白底图 JPEG 压到1-2KB；
#: 有实际线条的柜型图在黑线配置下普遍 >8KB。
#: 实测踩坑：默认配置渲染出 4587 字节的「看起来空白」的图，
#: 而代码只判断了文件存在且>512 字节，于是静默入库了空图。
MIN_IMAGE_BYTES = 6_000


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
    error: str | None = None


def _pick_size(bbox, width: int) -> tuple[int, int]:
    """按 bbox 比例算画布尺寸，并夹在上限内。"""
    try:
        (x0, y0), (x1, y1) = bbox.extmin, bbox.extmax
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
        tmsp = tmp_doc.modelspace()
        for e in ents:
            #COPY 走 ezdxf 的深拷贝，保留图层/颜色/线型
            tmsp.add_entity(e.copy())
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
    # matplotlib 画布上确实画了东西（patches/lines 有值）也可能因为
    # 颜色/线宽过淡而视觉上不可见。这里用产出体积做二次兜底：
    # 尺寸明显偏小就判定为可疑，宁可让用户知道「这张没渲染出来」，
    # 也不能把白图当柜型图存进库里。
    size = fpath.stat().st_size
    if size < MIN_IMAGE_BYTES:
        # 退一步再看：若实体里含文字/圆/填充等强可见图元，
        # 体积小也可能是真的（内容极少）
        strong = sum(1 for e in ents
                     if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF",
                                        "CIRCLE", "ARC", "ELLIPSE",
                                        "SOLID", "HATCH", "3DFACE"))
        if strong == 0:
            res.error = (
                f"渲染结果疑似空白（{size} 字节 < {MIN_IMAGE_BYTES}），"
                "已丢弃 —— 图纸可能全部是构造线或不受支持的代理实体"
            )
            try:
                fpath.unlink()
            except OSError:
                pass
            return res
        log.debug("%s 体积偏小(%d)但含 %d 个强可见图元，保留",
                  block_name, size, strong)

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

    筛选规则：
      · 跳过匿名 block（``*`` 开头 = 系统定义/标注）
      · 跳过实体数过少的（< ``min_entities``）—— 那种不是柜型
      · ``only_blocks`` 非空时只渲染指定 block

    返回**所有** block 的结果（含失败的），便于把失败原因暴露给用户，
    而不是静默跳过。
    """
    dxf_path = Path(dxf_path)
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        return [BlockRender(block_name="<dxf>", ok=False,
                            error=f"无法读取 DXF: {exc}")]

    results: list[BlockRender] = []
    for blk in doc.blocks:
        name = blk.name
        if name.startswith("*"):
            continue
        if only_blocks and name not in only_blocks:
            continue
        if not only_blocks and len(list(blk)) < min_entities:
            continue
        results.append(render_block_to_jpg(dxf_path, name, out_dir, width=width))
    return results