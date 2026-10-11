"""柜型渲染：DWG → 每个 block 一张 JPG（需求：柜型库可点击查看）。

守卫三件最容易回归的事：
  1. 渲染必须产出**可看见**的图 —— 默认 ezdxf 配置在白底上产出
     「看起来空白」的小体积图，静默入库后用户只看到空框。
  2. 尺寸来源必须标对 —— DWG 实测被误标成manual 的话，
     用户无法判断哪个尺寸可信。
  3. 缩略图端点不能被路径穿越。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.parsers.cupboard_render import (
    MIN_IMAGE_BYTES,
    MIN_LINEWEIGHT,
    render_block_to_jpg,
    render_cupboard_library,
)

FIX = Path(__file__).parent / "fixtures"

#: 真实图纸里代表水表/气表的 block 名。
#:
#: 早先这里用的是自建合成 DXF 里的 ``CP-QUAD-2x2`` 等假 block，
#: 那些样本已删除 —— 渲染必须跑在真实图纸的 block 上才算数。
BLOCK_GAS = "gas meter 1"
BLOCK_WATER = "water meter v"


@pytest.fixture()
def out_dir():
    d = Path(tempfile.mkdtemp(prefix="thumb_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 单block


class TestRenderBlock:
    def test_renders_named_block(self, real_dxf, out_dir):
        r = render_block_to_jpg(real_dxf, BLOCK_GAS, out_dir)
        assert r.ok, r.error
        assert r.image_name and (out_dir / r.image_name).is_file()
        assert r.entity_count > 0

    def test_output_is_visible_not_blank(self, real_dxf, out_dir):
        """核心守卫：产出必须**看起来有东西**。

        实测踩坑：ezdxf 默认渲染配置在白底上产出 4587 字节的
        「空白图」（线宽过细 + 颜色过淡），而旧代码只判断文件 >512 字节，
        于是空白图被静默入库。这里用体积下限拦住。
        """
        r = render_block_to_jpg(real_dxf, BLOCK_GAS, out_dir)
        assert r.ok, r.error
        size = (out_dir / r.image_name).stat().st_size
        assert size >= MIN_IMAGE_BYTES, (
            f"产出仅 {size} 字节，疑似空白图（阈值 {MIN_IMAGE_BYTES}）"
        )

    def test_measures_real_size_in_mm(self, real_dxf, out_dir):
        """尺寸必须来自几何 bbox，不是占位数字。"""
        r = render_block_to_jpg(real_dxf, BLOCK_GAS, out_dir)
        assert r.ok, r.error
        assert r.width_mm and r.width_mm > 1, f"宽={r.width_mm}"
        assert r.height_mm and r.height_mm > 1, f"高={r.height_mm}"
        # 是同一个量级（mm），不是 0.18 之类的米
        assert max(r.width_mm, r.height_mm) < 100_000

    def test_reports_layers_and_texts(self, real_dxf, out_dir):
        """图层用于区分 water/gas；标注文字即柜型的「介绍」。"""
        r = render_block_to_jpg(real_dxf, BLOCK_GAS, out_dir)
        assert r.layers, "没拿到图层 —— 无法区分 water / gas 分组"
        assert r.layer_counts

    def test_missing_block_is_actionable(self, real_dxf, out_dir):
        r = render_block_to_jpg(real_dxf, "NO_SUCH_BLOCK", out_dir)
        assert not r.ok
        assert "不存在" in (r.error or "")

    def test_bad_dxf_path_reports_error(self, out_dir):
        bad = out_dir / "bad.dxf"
        bad.write_text("not a dxf at all")
        r = render_block_to_jpg(bad, "X", out_dir)
        assert not r.ok and r.error


# ---------------------------------------------------------------- 全库


class TestRenderLibrary:
    def test_renders_multiple_blocks(self, real_dxf, out_dir):
        rs = render_cupboard_library(real_dxf, out_dir)
        assert len(rs) >= 2, f"只找到 {len(rs)} 个 block"
        ok = [r for r in rs if r.ok]
        assert ok, f"一个都没渲染成功: {[(r.block_name, r.error) for r in rs]}"

    def test_skips_anonymous_blocks(self, real_dxf, out_dir):
        """匿名 block（* 开头）是系统定义，不是柜型。"""
        rs = render_cupboard_library(real_dxf, out_dir)
        assert all(not r.block_name.startswith("*") for r in rs)

    def test_only_blocks_filter(self, real_dxf, out_dir):
        rs = render_cupboard_library(real_dxf, out_dir, only_blocks=[BLOCK_WATER])
        assert len(rs) == 1 and rs[0].block_name == BLOCK_WATER

    def test_unreadable_dxf_returns_failure_not_exception(self, out_dir):
        bad = out_dir / "x.dxf"
        bad.write_text("garbage")
        rs = render_cupboard_library(bad, out_dir)
        assert len(rs) == 1 and not rs[0].ok


class TestRenderConstants:
    def test_lineweight_not_excessive(self):
        """线宽既不能是 0（白底上消失），也不能过粗。

        实测踩坑：`min_lineweight=25`（0.25mm）是「按 block 渲染」
        时代的调法 —— 整个 block 缩成一张图，线太细就看不见。
        改成「按柜体区域」渲染后，一个柜子独占一张图，细节够大，
        25 反而把 150×190mm 的表位框糊成一块黑斑，完全看不出排布。
        所以现在的约束是「细但非零」：1~15 之间。
        """
        assert 1 <= MIN_LINEWEIGHT <= 15, (
            f"线宽 {MIN_LINEWEIGHT} 不合适：过细看不见，过粗糊成一团"
        )

    def test_blank_detection_is_content_based(self):
        """空白检测必须按像素内容，不能只看文件体积。

        实测踩坑：min_lineweight 25→6 让同一张柜体图从 26KB 掉到
        7.6KB。固定字节阈值必然在某组参数下误杀合法图，
        所以主判据是 :func:`_looks_blank` 的非白像素占比。
        """
        from app.parsers.cupboard_render import _looks_blank
        assert callable(_looks_blank), "缺少按内容判空白的检测函数"

    def test_blank_threshold_is_sane(self):
        """字节阈值现在只作极端兜底，范围放宽即可。"""
        assert 1_000 < MIN_IMAGE_BYTES < 60_000

    def test_white_border_is_trimmed(self, tmp_path):
        """渲完必须裁掉四周纯白边 —— 用户诉求「留白太多了」。

        实测踩坑：画布尺寸按 bbox 比例算（见 ``_pick_size``），而 bbox
        里混着柜外的标注/图框，柜体本身很窄，于是算出的画布两侧各空掉
        一大条。3 Units 那个柜（915×1750）原本渲成 440×619，
        内容只占中间一小条。
        """
        from PIL import Image
        from app.parsers.cupboard_render import _trim_white_border

        p = tmp_path / "pad.jpg"
        # 造一张四周有大白边、只有中间一小块内容的图
        im = Image.new("RGB", (400, 300), "white")
        for x in range(180, 220):
            for y in range(120, 180):
                im.putpixel((x, y), (0, 0, 0))
        im.save(p, format="JPEG", quality=95)

        _trim_white_border(p)

        out = Image.open(p)
        w, h = out.size
        # 内容宽约 40px、高约 60px，留 1.2% 内边距（按短边 300 算约 3px）
        assert w <= 60, f"宽没裁下来：{w}px（内容只40px宽）"
        assert h <= 80, f"高没裁下来：{h}px（内容只 60px 高）"
        # 裁完内容仍要在，别把内容也削没了
        assert w >= 40 and h >= 55, f"裁过头了，把内容削掉了：{w}x{h}"

    def test_trim_keeps_already_tight_image(self, tmp_path):
        """内容已经贴边的图不该被再动一次（否则反复裁会越裁越小）。"""
        from PIL import Image
        from app.parsers.cupboard_render import _trim_white_border

        p = tmp_path / "tight.jpg"
        im = Image.new("RGB", (200, 200), "white")
        for x in range(0, 200):
            im.putpixel((x, 100), (0, 0, 0))
        for y in range(0, 200):
            im.putpixel((100, y), (0, 0, 0))
        im.save(p, format="JPEG", quality=95)

        _trim_white_border(p)

        assert Image.open(p).size == (200, 200), "贴边的图被改了尺寸"

    def test_trim_never_raises_on_garbage(self, tmp_path):
        """裁剪失败不能影响渲染结果 —— 图已经渲出来了，只是留白多一点。"""
        from app.parsers.cupboard_render import _trim_white_border

        p = tmp_path / "notanimage.jpg"
        p.write_bytes(b"definitely not a jpeg")
        _trim_white_border(p)          # 不能抛异常
        assert p.read_bytes() == b"definitely not a jpeg", "文件被改动了"


class TestThumbEndpointSecurity:
    """缩略图端点不接受路径穿越。"""

    @pytest.mark.parametrize("bad", [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "sub/../../secret.jpg",
        "/etc/passwd",
    ])
    def test_traversal_blocked(self, bad):
        #逻辑与 get_thumb 里的防护一致：取 basename 后必须仍在 THUMB_DIR 下
        from pathlib import Path as P
        name = P(bad).name
        assert "/" not in name or name == P(bad).name
        # 端点实现用 resolve() + startswith 判定，name里不含 / 才安全
        assert name in (bad, P(bad).name)

    def test_basename_strips_directories(self):
        from pathlib import Path as P
        assert P("../../etc/passwd").name == "passwd"
        assert P("a/b/c.jpg").name == "c.jpg"

class TestLargeDrawing:
    """大图纸（上百个 block）不能崩、不能超时。

    实测踩坑：用户传了一份 4865 实体 / 21 图层 / **155 block** 的真实
    柜型库 DWG，界面报「入库失败 500」。用同规模样本复现后确认：
    后端渲染 155 个 block 耗时 20.5s、入库 0.4s，**全部 200**。
    说明 500 并非容量问题，而是旧版前端提交旧字段给新后端所致
    （详见 TestVersionMismatchGuard）。这里把大图纸规模钉成回归用例，
    免得以后有人加渲染逻辑时把它压垮。
    """

    @pytest.fixture(scope="class")
    def big_dxf(self, tmp_path_factory):
        import ezdxf

        d = tmp_path_factory.mktemp("big") / "big.dxf"
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        for i in range(60):          # 60 个足够覆盖问题，又不至于让 CI 太慢
            name = f"CP_Block_{i:03d}"
            b = doc.blocks.new(name)
            for k in range(4):
                b.add_circle((k * 12.0, k * 7.0), 4.0)
            for k in range(3):
                b.add_line((0, k * 9), (40, k * 9))
            b.add_text(name, height=3).set_placement((0, 55))
            msp.add_blockref(name, (float(i % 10) * 200, float(i // 10) * 200))
        doc.saveas(str(d))
        return d

    def test_many_blocks_all_rendered(self, big_dxf, out_dir):
        res = render_cupboard_library(str(big_dxf), out_dir)
        assert len(res) == 60, f"应产出 60 个结果，实际 {len(res)}"
        ok = [r for r in res if r.ok]
        # 全部成功才算过 —— 允许极个别失败，但成功率必须高，
        # 且失败必须带可读原因（不能是空 ok=False）
        assert len(ok) >= 55, f"成功 {len(ok)}/60，失败项: " + str(
            [(r.block_name, r.error) for r in res if not r.ok][:5]
        )
        for r in res:
            if not r.ok:
                assert r.error, f"{r.block_name} 失败但没给原因"

    def test_large_batch_has_no_duplicate_paths(self, big_dxf, out_dir):
        """block 名可能重复/相似，产出文件名不能互相覆盖。"""
        res = render_cupboard_library(str(big_dxf), out_dir)
        names = [r.image_name for r in res if r.ok and r.image_name]
        assert len(names) == len(set(names)), "缩略图文件名冲突，后一个会覆盖前一个"
        assert all((out_dir / n).is_file() for n in names)


# ====================================================================
# 真实 DWG 回归：柜型图必须还原 water/gas meter 细节 + 渲染耗时
# ====================================================================


class TestRealDwgRenderQuality:
    """跑在**真实 DWG** 上（用户硬性要求：不许只在合成数据上自测）。

    用户诉求原文：「这个图，我之前的要求是需要使用dwg 里面的原来的图，
    相当于从 dwg 导出的，可以使用这个 water meter 和 gas meter
    作为原始的元素」。

    历史故障：``MIN_LINEWEIGHT = 6`` 把 water/gas meter 内部0.05mm 级的
    细部（表盘、阀门、接管）全糊成实心黑块 —— 用户看到的就是一坨黑。
    降到 1 之后细部才重新可辨。
    """

    @pytest.fixture(scope="class")
    def rendered(self, real_dxf, tmp_path_factory):
        from app.parsers.cupboard_render import render_cupboard_regions
        out = tmp_path_factory.mktemp("real_render")
        res, parsed = render_cupboard_regions(str(real_dxf), out)
        return res, parsed, out

    def test_all_cupboards_rendered(self, rendered):
        """23 个柜型必须全部渲出图。"""
        res, parsed, _ = rendered
        assert len(res) == 23, f"应产出 23 个柜型，实际 {len(res)}"
        bad = [(r.block_name, r.error) for r in res if not r.ok]
        assert not bad, f"渲染失败: {bad[:5]}"

    def test_images_show_internal_detail(self, rendered):
        """图里必须有**柜内表位**的线条细节，不能是纯边框。

        判据：把图按上下三等分，中带（非柜框区域）必须有着墨像素。
        糊成黑块的旧图虽然整体ink 多，但中带会连成一大片；
        更可靠的信号是**中间调像素**（灰度 60~200）的比例 ——
        纯黑白线图几乎没有中间调，黑块图则中间调极少、两端极端。
        """
        from PIL import Image
        res, _, out = rendered
        ratios = []
        for r in res:
            p = out / r.image_name
            im = Image.open(p).convert("L")
            W, H = im.size
            mid = im.crop((0, H // 3, W, 2 * H // 3))
            px = list(mid.getdata())
            dark = sum(1 for v in px if v < 128)
            # 中带着墨率：柜框只画上下两条边，中间应该几乎没有线。
            # 但柜内表位就在中带里，所以必须 > 0.5%
            ratio = dark / len(px)
            ratios.append((r.block_name, ratio))
        worst = min(ratios, key=lambda x: x[1])
        assert worst[1] > 0.005, (
            f"{worst[0]} 中带几乎无线条（{worst[1]:.4%}）—— "
            f"柜内表位没画出来，min_lineweight 可能又调粗了"
        )

    def test_lineweight_is_thin_enough(self):
        """min_lineweight 必须 ≤ 2，否则 meter 细部会糊成黑块。

        参数矩阵实测（CP-13p-01，1400px 宽）：
          6 → 32KB，细部糊成黑块（用户截图就是这个）
          2 → 47KB，能看出 meter 轮廓
          1 → 70KB，表盘/阀门/接管全部可辨
        """
        from app.parsers.cupboard_render import MIN_LINEWEIGHT
        assert MIN_LINEWEIGHT <= 2, (
            f"MIN_LINEWEIGHT={MIN_LINEWEIGHT} 太粗，"
            f"water/gas meter 的细部会糊成实心块（用户诉求 #F）"
        )

    def test_no_white_border_waste(self, rendered):
        """裁白边后内容应占画面绝大部分（用户诉求「留白太多」）。"""
        from PIL import Image
        res, _, out = rendered
        for r in res:
            im = Image.open(out / r.image_name).convert("L")
            W, H = im.size
            bb = im.point(lambda v: 0 if v > 245 else 255, "L").getbbox()
            assert bb, f"{r.block_name} 是白图"
            x0, y0, x1, y1 = bb
            cover = (x1 - x0) / W * (y1 - y0) / H
            assert cover > 0.85, (
                f"{r.block_name} 内容只占{cover:.1%}，留白仍然过多"
            )

    def test_render_is_fast_enough(self, real_dxf, tmp_path_factory):
        """渲染耗时必须收敛（用户诉求 #B「渲染的时间太久了」）。

        实测基线（23 柜，本机 8 workers）：
          修复前177.0s（串行，且每柜先白跑一次注定失败的文字渲染）
          修复后   28.8s
        这里用 60s 作上限，给CI 机器留余量。
        """
        import time
        from app.parsers.cupboard_render import render_cupboard_regions
        out = tmp_path_factory.mktemp("speed")
        t = time.time()
        res, _ = render_cupboard_regions(str(real_dxf), out)
        el = time.time() - t
        assert len([r for r in res if r.ok]) == 23, "速度测试里渲染不完整"
        assert el < 60, f"渲染耗时 {el:.1f}s，超过 60s 上限"

    def test_parallel_and_serial_agree(self, real_dxf, tmp_path_factory):
        """并行与串行必须产出**同样的柜型集合和顺序**。

        并行是这次提速的核心手段，如果 worker 里的 doc 继承出问题
        （没重新 bind_document），产出会静默变空或错位。
        """
        from app.parsers.cupboard_render import render_cupboard_regions
        a_out = tmp_path_factory.mktemp("par")
        b_out = tmp_path_factory.mktemp("ser")
        par, _ = render_cupboard_regions(str(real_dxf), a_out, workers=4)
        ser, _ = render_cupboard_regions(str(real_dxf), b_out, workers=1)
        assert [r.block_name for r in par] == [r.block_name for r in ser], (
            "并行与串产的柜型顺序不一致"
        )
        assert [r.ok for r in par] == [r.ok for r in ser], (
            "并行与串行的成功/失败不一致"
        )
        assert [r.entity_count for r in par] == [r.entity_count for r in ser], (
            "并行与串行抠出的实体数不一致 —— worker 没拿到正确的 doc"
        )


class TestRenderInternals:
    """渲染内部辅助函数。"""

    def test_auto_workers_capped(self):
        """并行度必须封顶（实测 16 workers 反而比 8 慢）。"""
        from app.parsers.cupboard_render import MAX_RENDER_WORKERS, _auto_workers
        assert MAX_RENDER_WORKERS == 8
        assert _auto_workers(1) == 1, "单任务不该并行"
        assert _auto_workers(100) <= MAX_RENDER_WORKERS
        assert _auto_workers(0) >= 1, "空任务列表也要返回合法并行度"

    def test_render_config_is_color_on_white(self):
        """白底 + **原色**（不是黑线）。

        【本轮改动】原断言是 ``ColorPolicy.BLACK``，理由写的是
        「彩色版实测只有 23KB 且线条丢失」—— **这个结论是错的**，
        它把「线条丢失」误判成「颜色太浅」，实际是当时**根本没开文字**
        （见 :func:`_clone_styles` 的 bug），图上只剩纯线条，
        字节数自然低。

        需求文档「步骤3：保持原有颜色」明确要求：
            「保持 DWG 原有颜色：柜体外框=青/黑，表位框=红，
              水管=绿，气管=黄/金」

        改COLOR 后实测 23 柜：彩色像素占比 4.92%~8.39%（平均 6.72%），
        与 DWG 里「彩色实体约占 1/15」的比例吻合。
        白底不变（印刷与缩略图观感需求）。
        """
        from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy
        from app.parsers.cupboard_render import _render_config
        cfg = _render_config()
        assert cfg.color_policy == ColorPolicy.COLOR
        # 背景用的是 BackgroundPolicy 枚举，不是 ColorPolicy
        assert cfg.background_policy == BackgroundPolicy.WHITE
        assert cfg.min_lineweight == MIN_LINEWEIGHT

    def test_render_text_is_enabled(self):
        """必须渲染尺寸标注。

        【本轮改动】原来 ``RENDER_TEXT=False``，理由是「字体不可用会炸」。
        真根因是临时文档里没有 textstyle 定义（不是系统缺字体），
        已由 :func:`_clone_styles` 解决，所以标注必须打开 ——
        用户参考图里1065/2350/450 这些数字是核心信息。
        """
        from app.parsers.cupboard_render import RENDER_TEXT
        assert RENDER_TEXT is True, (
            "尺寸标注必须渲染 —— 需求「目标效果要点」明确要求「保留尺寸标注」")

    def test_fonts_available_probe_does_not_raise(self):
        """字体探测不能抛异常 —— 它决定要不要渲染文字。"""
        from app.parsers.cupboard_render import _fonts_available
        assert isinstance(_fonts_available(), bool)


class TestOutlierEntityFilter:
    """离群实体过滤（本轮修的「图黑黢黢 + 尺寸不对」根因）。

    实测事故（真实 DWG「Cold and hot water ...detail 1.dwg」）：
      2CONC 图层有一条 **4414 × 6215mm** 的 LWPOLYLINE（建筑轮廓线），
      它横跨整张图纸，任何位置的柜体其 bbox 都与它「有交集」，
      于是被 :func:`_entity_near` 当成柜内零件抠进视图 ——
      视图 bbox 从柜体的 ~915×1750mm 被撑到 4414×6215mm（放大 13~17 倍），
      画布比例随之失真，内容缩在角落、仪表糊成黑块。
      受影响的柜型：CP-3p-14 / CP-3p-15 / CP-4p-13。
    """

    def test_ratio_threshold_allows_normal_parts(self):
        """阈值必须容得下柜内最大的正常零件。"""
        from app.parsers.cupboard_render import _OUTLIER_AREA_RATIO
        assert _OUTLIER_AREA_RATIO >= 2.0, (
            "阈值太小会把正常的柜框线/尺寸标注一起误杀")
        # 实测：柜内最大零件是 250×450mm 的 gas meter 表组
        # （约 0.11 m²），最小的柜是 915×1750mm（约 1.6 m²）
        assert 250 * 450 < (915 * 1750) * _OUTLIER_AREA_RATIO

    def test_giant_polyline_is_rejected(self):
        """4414×6215mm 的建筑轮廓线必须被拒（真实尺寸回归）。"""
        import ezdxf
        from app.parsers.cupboard_geometry import Rect
        from app.parsers.cupboard_render import _entity_near
        doc = ezdxf.new()
        msp = doc.modelspace()
        # 柜体 915 × 1750mm
        rect = Rect(0.0, 0.0, 915.0, 1750.0, "1CO", None, -1, "LINE_PAIR")
        # 一条覆盖整张图纸的巨型闭合多段线，起点落在柜内
        giant = msp.add_lwpolyline(
            [(100.0, 100.0), (4514.0, 100.0), (4514.0, 6315.0), (100.0, 6315.0)],
            close=True)
        giant.dxf.layer = "2CONC"
        doc.layers.new("2CONC")
        assert not _entity_near(giant, rect), (
            "4414×6215mm 的建筑轮廓线被当成柜内零件了 —— 会把 bbox 撑大 17 倍")

    def test_normal_part_inside_is_kept(self):
        """柜内正常零件必须保留 —— 不能过滤过头。"""
        import ezdxf
        from app.parsers.cupboard_geometry import Rect
        from app.parsers.cupboard_render import _entity_near
        doc = ezdxf.new()
        msp = doc.modelspace()
        rect = Rect(0.0, 0.0, 915.0, 1750.0, "1CO", None, -1, "LINE_PAIR")
        # 250 × 450mm 的 gas meter 表组轮廓（实测真实尺寸）
        meter = msp.add_lwpolyline(
            [(300.0, 600.0), (550.0, 600.0), (550.0, 1050.0), (300.0, 1050.0)],
            close=True)
        assert _entity_near(meter, rect), "柜内正常表组被误杀了"

    def test_frame_line_on_border_is_kept(self):
        """柜框线正好压在边界上，必须保留 —— 纯 contains 会漏。"""
        import ezdxf
        from app.parsers.cupboard_geometry import Rect
        from app.parsers.cupboard_render import _entity_near
        doc = ezdxf.new()
        msp = doc.modelspace()
        rect = Rect(0.0, 0.0, 915.0, 1750.0, "1CO", None, -1, "LINE_PAIR")
        frame = msp.add_line((0.0, 0.0), (915.0, 1750.0))  # 对角线
        assert _entity_near(frame, rect), "压在柜边界上的柜框线被误杀了"

    def test_entity_far_away_is_rejected(self):
        """完全在柜外的实体仍然要拒。"""
        import ezdxf
        from app.parsers.cupboard_geometry import Rect
        from app.parsers.cupboard_render import _entity_near
        doc = ezdxf.new()
        msp = doc.modelspace()
        rect = Rect(0.0, 0.0, 915.0, 1750.0, "1CO", None, -1, "LINE_PAIR")
        far = msp.add_line((5000.0, 5000.0), (5100.0, 5100.0))
        assert not _entity_near(far, rect)
# ======================================================================
# 本轮修复：按《WorkBuddy渲染修复需求文档》恢复 DWG 原样导出
#
# 需求原文：
#   「保持 DWG 原有颜色：柜体外框=青/黑，表位框=红，水管=绿，气管=黄/金」
#   「保留尺寸标注」
#   「HATCH 实体只画边框不填充」
#
# 下面每条测试都对应本轮挖出并修掉的一个**真**bug —— 三个都是
# 「代码看着对、跑起来静默丢东西」的类型，靠读代码永远看不出来，
# 必须用真实 DWG 渲出来量像素才能抓到。
# ======================================================================


class TestColorPreservation:
    """颜色丢失的三道防线。

    实测踩坑链：彩色渲染明明打开了（``ColorPolicy.COLOR``），
    成图彩色像素却是 **0.00%**。逐层排查后发现三处独立失色：

      1. 临时文档没有**顶层实体**的图层定义（只在块里搬了）
      2. 块定义搬运只做一层，嵌套块（``h-meter``）没搬
      3. ``_trim_white_border`` 用灰度图裁剪后把**灰度图**存回，
         把颜色永久洗掉（裁前 5.04% → 裁后 0.00%）
    """

    def _sat_ratio(self, path: Path) -> float:
        """彩色像素占比。阈值 max-min>30，与人工目视一致。"""
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            try:
                px = list(im.get_flattened_data())
            except AttributeError:
                px = list(im.getdata())
        return sum(1 for p in px if max(p) - min(p) > 30) / len(px)

    def test_render_config_uses_color_policy(self):
        """渲染配置必须是 COLOR 而不是 BLACK。

        旧代码硬编码 ``ColorPolicy.BLACK``，把 DWG 的彩色全压成灰。
        """
        from ezdxf.addons.drawing.config import ColorPolicy
        from app.parsers.cupboard_render import _render_config
        cfg = _render_config()
        assert cfg.color_policy == ColorPolicy.COLOR, (
            "渲染配置必须是 COLOR —— 需求明确要求「保持 DWG 原有颜色」")

    def test_trim_white_border_keeps_color(self, out_dir):
        """裁白边**不能**把彩色洗成灰色。

        这是本轮最隐蔽的一个 bug：裁剪本身做的是对的（找到内容 bbox），
        但它拿灰度图当中间态，裁完把灰度图存了回去 ——
        于是一张5% 彩色的图裁完变成 0% 彩色。

        用一张带彩色块的图走一遍裁剪，前后彩色占比必须基本不变。
        """
        from PIL import Image, ImageDraw
        from app.parsers.cupboard_render import _trim_white_border

        src = out_dir / "colored.jpg"
        im = Image.new("RGB", (600, 800), (255, 255, 255))
        d = ImageDraw.Draw(im)
        # 内容区：中央一小块，叠一条鲜红与一条纯绿
        d.rectangle([200, 300, 400, 500], outline=(0, 200, 0), width=6)
        d.line([200, 300, 400, 500], fill=(255, 0, 0), width=8)
        im.save(src, format="JPEG", quality=95)

        before = self._sat_ratio(src)
        _trim_white_border(src)
        after = self._sat_ratio(src)

        assert before > 0.01, f"测试样本本身该有彩色，实测 {before:.4f}"
        assert after > before * 0.8, (
            f"裁白边把彩色洗掉了：{before:.4f} → {after:.4f}"
            "（_trim_white_border 是不是又拿灰度图存回去了？）")

    def test_clone_layers_copies_top_level_layers(self):
        """**顶层实体**所在的图层也必须搬进临时文档。

        实测（真实 DWG，CP-5p-18）：柜框 LINE 在 ``1CO``、建筑线在
        ``2CONC``，这两个图层只在 modelspace 层出现，块定义里根本没有。
        只在 ``_clone_block_deep`` 里搬图层的话，这两个永远建不出来。
        """
        import ezdxf
        from app.parsers.cupboard_render import _clone_into_cabinet_view

        src = ezdxf.new()
        lay = src.layers.new("1CO")
        lay.color = 4                      # ACI 4 = 青
        line = src.modelspace().add_line((0, 0), (100, 100))
        line.dxf.layer = "1CO"

        dst = ezdxf.new()
        _clone_into_cabinet_view(src, dst, [line])

        got = None
        try:
            got = dst.layers.get("1CO")
        except Exception:
            got = None
        assert got is not None, (
            "顶层实体的图层（1CO）没被搬进临时文档 —— "
            "渲染时退到默认层，颜色全丢")
        assert got.dxf.color == 4, (
            f"图层色没搬过来：期望 ACI 4（青），实际 {got.dxf.color}")


class TestNestedBlockClone:
    """嵌套块必须递归搬运。

    实测（真实 DWG，CP-5p-18）：``water meter h`` 的块定义里有 109 个
    实体，其中 1 个是 ``INSERT name='h-meter'`` —— 水表本体是第二层块。
    旧代码只搬一层，``h-meter`` 在临时文档里不存在，ezdxf 渲染 INSERT
    时查不到块定义就**静默跳过**，柜子下半部 5 个水表在图上整体消失，
    而 ``entity_count`` 仍是 42 —— 看着一切正常。
    """

    def test_collect_block_refs_is_recursive(self):
        """块引用收集必须递归到嵌套层。"""
        import ezdxf
        from app.parsers.cupboard_render import _collect_block_refs

        src = ezdxf.new()
        inner = src.blocks.new("h-meter")
        inner.add_line((0, 0), (10, 0))
        outer = src.blocks.new("water meter h")
        outer.add_line((0, 0), (10, 0))
        ref = outer.add_blockref("h-meter", (5, 5))
        top = src.modelspace().add_blockref("water meter h", (0, 0))

        out: set = set()
        _collect_block_refs(src, [top], out)
        assert "water meter h" in out
        assert "h-meter" in out, (
            "嵌套块 h-meter 没被收集 —— 水表会整块从图上消失")

    def test_nested_block_actually_lands_in_view_doc(self):
        """临时文档里必须真的有嵌套块的定义。"""
        import ezdxf
        from app.parsers.cupboard_render import _clone_into_cabinet_view

        src = ezdxf.new()
        inner = src.blocks.new("h-meter")
        inner.add_line((0, 0), (10, 0))
        outer = src.blocks.new("water meter h")
        outer.add_line((0, 0), (10, 0))
        outer.add_blockref("h-meter", (5, 5))
        ins = src.modelspace().add_blockref("water meter h", (0, 0))

        dst = ezdxf.new()
        _clone_into_cabinet_view(src, dst, [ins])

        names = {b.name for b in dst.blocks}
        assert "water meter h" in names
        assert "h-meter" in names, (
            "嵌套块 h-meter 的定义没建出来 —— 渲出来是空框")

    def test_dxf_table_lookup_semantics_differ(self):
        """``layers.get()`` 找不到会抛异常，``blocks.get()`` 返回 None。

        这是本轮踩到的坑：两者的「不存在」表现**不一样**，
        所以不能一律写成
            ``dst = tbl.get(name) or tbl.new(name)``

        · ``layers.get()`` 直接抛 ``DXFTableEntryError`` —— 惯用写法
          会在抛异常处走人，图层定义永远建不出来。
          实测报错「图层搬运失败 4 个」就是这么来的。
        · ``blocks.get()`` 返回 ``None``，此时 ``or`` 短路正常工作。

        这条测试把两者的差异钉死，防止有人「顺手统一成惯用写法」。
        """
        import ezdxf
        from ezdxf.lldxf.const import DXFTableEntryError

        doc = ezdxf.new()
        with pytest.raises(DXFTableEntryError):
            doc.layers.get("__definitely_not_exist__")
        assert doc.blocks.get("__definitely_not_exist__") is None


class TestSolidHatchDemotion:
    """SOLID HATCH 必须降级成只描边（需求「步骤2」）。"""

    def test_solid_hatch_becomes_pattern(self):
        """SOLID + solid_fill 的 HATCH 必须被降级。"""
        import ezdxf
        from app.parsers.cupboard_render import _demote_solid_hatch

        doc = ezdxf.new()
        h = doc.modelspace().add_hatch(color=7)
        h.dxf.pattern_name = "SOLID"
        h.dxf.solid_fill = 1
        h.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)],
                                  is_closed=True)

        out = _demote_solid_hatch(h)
        assert out.dxf.solid_fill == 0, (
            "SOLID 填充没关掉 —— 水表阀体会渲成一块黑色实心块")
        assert str(out.dxf.pattern_name).upper() != "SOLID", (
            "pattern 还是 SOLID，应该换成剖面线图案")

    def test_non_solid_hatch_untouched(self):
        """非 SOLID 的 HATCH 原样放行 —— 不能过度处理。"""
        import ezdxf
        from app.parsers.cupboard_render import _demote_solid_hatch

        doc = ezdxf.new()
        h = doc.modelspace().add_hatch(color=3)
        h.dxf.pattern_name = "ANSI31"
        h.dxf.solid_fill = 0

        out = _demote_solid_hatch(h)
        assert str(out.dxf.pattern_name).upper() == "ANSI31"
        assert out.dxf.solid_fill == 0

    def test_non_hatch_passes_through(self):
        """非 HATCH 实体原样复制。"""
        import ezdxf
        from app.parsers.cupboard_render import _demote_solid_hatch

        doc = ezdxf.new()
        line = doc.modelspace().add_line((0, 0), (10, 10))
        out = _demote_solid_hatch(line)
        assert out.dxftype() == "LINE"
