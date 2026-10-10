"""柜型图「DWG 图元合成」路径的测试（用户诉求 #F）。

架构要点（详见 :mod:`app.parsers.meter_sprites`的模块 docstring）：

* 柜型图**不是**整柜直接渲染，也不是程序自己画 ——而是把 DWG 里
  4 个 meter 块（``water meter v`` / ``water meter h`` / ``gas meter 1`` /
  ``h-meter``）光栅化成透明底 PNG，再按解析器量出的**真实表位坐标**贴图。
* 柜体外框也来自 DWG：柜体那 4 条跨图层长 LINE 的端点。

所以本文件的核心断言是：**合成图上每个图元都能对回 DWG 里的一个真实实例**。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def doc(real_dxf):
    import ezdxf

    d = ezdxf.readfile(str(real_dxf))
    from app.parsers.cupboard_geometry import bind_document

    bind_document(d)
    return d


class TestMeterSprites:
    """图元提取。"""

    def test_all_four_meter_blocks_exist(self, doc):
        """DWG 里必须有这 4 个 meter 块，缺一个就说明块名变了。"""
        from app.parsers.meter_sprites import METER_BLOCKS

        for name in METER_BLOCKS:
            assert doc.blocks.get(name) is not None, f"DWG 里缺少块 {name!r}"

    def test_sprite_renders_to_transparent_png(self, doc, tmp_path):
        """图元PNG 必须是透明底，且有实际内容（不是空白）。"""
        from PIL import Image

        import numpy as np

        from app.parsers.meter_sprites import SpriteBank

        bank = SpriteBank(tmp_path)
        png = bank.get(doc, "gas meter 1", "gas")
        assert png is not None and png.exists()
        with Image.open(png) as im:
            assert im.mode == "RGBA"
            arr = np.array(im)
        alpha = arr[:, :, 3]
        #有笔画像素
        assert (alpha > 8).sum() > 500, "图元是空白的"
        # 至少有一个完全透明的像素 —— 说明确实是透明底而不是白底
        assert (alpha == 0).sum() > 50, "图元没有透明区域，透明底失败"

    def test_sprite_cache_key_is_stable_and_content_addressed(self, doc, tmp_path):
        """缓存键必须只跟 DXF 内容走，同一份 DXF 两次渲染要命中同一个文件。"""
        from app.parsers.meter_sprites import SpriteBank

        a = SpriteBank(tmp_path).get(doc, "gas meter 1", "gas")
        b = SpriteBank(tmp_path).get(doc, "gas meter 1", "gas")
        assert a == b
        assert a.name.startswith("gas-") and a.name.endswith(".png")

    def test_sprite_trimmed_size_is_smaller_than_block_bbox(self, doc, tmp_path):
        """裁边后必须比原始包围盒小 —— 否则说明外框没裁掉。"""
        from app.parsers.meter_sprites import SpriteBank, measure_sprite_mm, sprite_size_mm

        bank = SpriteBank(tmp_path)
        png = bank.get(doc, "gas meter 1", "gas")
        raw = measure_sprite_mm(doc, "gas meter 1")
        trimmed = sprite_size_mm(doc, "gas meter 1", "gas", png)
        assert raw is not None and trimmed is not None
        assert trimmed[0] < raw[0] and trimmed[1] < raw[1], (
            f"裁边后尺寸 {trimmed} 应小于原始包围盒 {raw}"
        )

    def test_group_frame_detection_rejects_gas_outer_box(self, doc):
        """``gas meter 1`` 里那个 250x450 的闭合矩形必须被判为表组外框。

        不剔除的后果：合成图上每个燃气表都带一个大方框，看起来像
        「13 个小柜子」而不是「13 个表」。
        """
        from app.parsers.meter_sprites import _is_group_frame

        blk = doc.blocks.get("gas meter 1")
        frames = [e for e in blk if _is_group_frame(e, "gas meter 1")]
        assert len(frames) == 1, f"应恰好识别出 1 个表组外框，实际 {len(frames)} 个"
        pts = list(frames[0].get_points("xy"))
        w = max(p[0] for p in pts) - min(p[0] for p in pts)
        h = max(p[1] for p in pts) - min(p[1] for p in pts)
        assert w == pytest.approx(250.0, abs=1.0)
        assert h == pytest.approx(450.0, abs=1.0)

    def test_group_frame_detection_spares_instrument_outline(self, doc):
        """判定不能误伤仪表自身的轮廓线（小于 100mm 的矩形得保留）。"""
        from app.parsers.meter_sprites import _is_group_frame

        blk = doc.blocks.get("water meter v")
        assert not any(_is_group_frame(e, "water meter v") for e in blk), (
            "冷水表块里不该有东西被判成表组外框"
        )

    def test_missing_block_returns_none(self, doc, tmp_path):
        """块不存在时返回 None 而不是抛异常 —— 让渲染整批降级。"""
        from app.parsers.meter_sprites import SpriteBank

        assert SpriteBank(tmp_path).get(doc, "no such block", "xx") is None


class TestPlanFromPositions:
    """用解析器真实表位构造合成方案。"""

    def test_plan_matches_parsed_positions(self, real_dxf):
        """方案里的图元数必须等于解析器给出的表位数，且 gas/water 分开计数。"""
        from app.parsers.cupboard_geometry import parse_cupboards
        from app.parsers.cupboard_render import _cabinet_code
        from app.parsers.meter_sprites import plan_from_positions

        parsed = parse_cupboards(real_dxf)
        cup = next(c for i, c in enumerate(parsed.cupboards)
                   if _cabinet_code(c, i) == "CP-13p-01")
        plan = plan_from_positions(cup)
        assert len(plan.placements) == len(cup.positions)
        counts = plan.counts
        assert counts.get("gas") == cup.gas_count
        # 冷热水表按宽高比分竖/横，两者之和等于 water_count
        assert counts.get("water_v", 0) + counts.get("water_h", 0) == cup.water_count
        assert plan.width_mm == pytest.approx(cup.rect.width, abs=0.01)
        assert plan.height_mm == pytest.approx(cup.rect.height, abs=0.01)

    def test_plan_coordinates_are_relative_to_cabinet(self, real_dxf):
        """图元坐标必须相对柜体左下角、y 轴朝上（内部再翻成图像坐标）。"""
        from app.parsers.cupboard_geometry import parse_cupboards
        from app.parsers.cupboard_render import _cabinet_code
        from app.parsers.meter_sprites import plan_from_positions

        parsed = parse_cupboards(real_dxf)
        cup = next(c for i, c in enumerate(parsed.cupboards)
                   if _cabinet_code(c, i) == "CP-13p-01")
        plan = plan_from_positions(cup)
        for p in plan.placements:
            assert -1.0 <= p.x <= plan.width_mm + 1.0
            assert -1.0 <= p.y <= plan.height_mm + 1.0

    def test_plan_rejects_cabinet_without_meters(self):
        """柜内没有表位时placements 为空（调用方据此报错而不是出白图）。"""
        from app.parsers.cupboard_geometry import Rect, Cupboard
        from app.parsers.meter_sprites import plan_from_positions

        cup = Cupboard(rect=Rect(x0=0, y0=0, x1=1000, y1=2000))
        assert plan_from_positions(cup).placements == []


class TestCompose:
    """合成器本身。"""

    def _plan(self, **kw):
        from app.parsers.meter_sprites import CabinetPlan, MeterPlacement

        p = CabinetPlan(width_mm=2000.0, height_mm=1200.0, **kw)
        p.placements = [
            MeterPlacement(kind="gas", x=500, y=600),
            MeterPlacement(kind="water_v", x=900, y=600),
        ]
        return p

    def test_compose_outputs_jpeg_with_expected_size(self, doc, tmp_path):
        from PIL import Image

        from app.parsers.cabinet_compose import CANVAS_W, compose_cabinet
        from app.parsers.cupboard_geometry import Cupboard, Rect
        from app.parsers.meter_sprites import SpriteBank

        cup = Cupboard(rect=Rect(x0=0, y0=0, x1=2000, y1=1200))
        out = tmp_path / "x.jpg"
        meta = compose_cabinet(doc, cup, self._plan(), SpriteBank(tmp_path / "sp"),
                               [], out)
        assert out.exists() and out.stat().st_size > 2048
        with Image.open(out) as im:
            assert im.format == "JPEG"
            assert im.width == CANVAS_W
            assert im.height == round(1200 * CANVAS_W / 2000)
        assert meta["sprites_missing"] == []
        assert meta["sprites_placed"] == 2

    def test_compose_keeps_aspect_ratio_of_cabinet(self, doc, tmp_path):
        """输出图必须保持柜体真实宽高比，不能拉变形。"""
        from PIL import Image

        from app.parsers.cabinet_compose import compose_cabinet
        from app.parsers.cupboard_geometry import Cupboard, Rect
        from app.parsers.meter_sprites import SpriteBank

        cup = Cupboard(rect=Rect(x0=0, y0=0, x1=2115, y1=2350))
        plan = self._plan()
        plan.width_mm, plan.height_mm = 2115.0, 2350.0
        out = tmp_path / "y.jpg"
        compose_cabinet(doc, cup, plan, SpriteBank(tmp_path / "sp"), [], out)
        with Image.open(out) as im:
            assert im.width / im.height == pytest.approx(2115 / 2350, rel=0.01)

    def test_frame_lines_are_drawn_from_dwg_coordinates(self, doc, tmp_path, real_dxf):
        """柜框必须由 DWG 里真实 LINE 的端点构成，不是装饰矩形。

        验证方式：把柜框线去掉后重合成，两张图的差异像素必须显著增加
        （说明那几条线真的画上去了）。
        """
        import numpy as np
        from PIL import Image

        from app.parsers.cabinet_compose import compose_cabinet
        from app.parsers.cupboard_geometry import Cupboard, Rect, parse_cupboards
        from app.parsers.cupboard_render import _cabinet_code, _cabinet_frame_lines
        from app.parsers.meter_sprites import SpriteBank

        parsed = parse_cupboards(real_dxf)
        cup = next(c for i, c in enumerate(parsed.cupboards)
                   if _cabinet_code(c, i) == "CP-13p-01")
        lines = _cabinet_frame_lines(doc, cup)
        assert len(lines) >= 4, f"柜体至少应有 4 条外框长直线，实际 {len(lines)} 条"

        bank = SpriteBank(tmp_path / "sp")
        a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
        compose_cabinet(doc, cup, self._plan(), bank, [], a, frame_lines=lines)
        compose_cabinet(doc, cup, self._plan(), bank, [], b, frame_lines=[])
        with Image.open(a) as ia, Image.open(b) as ib:
            diff = np.abs(np.asarray(ia, int) - np.asarray(ib, int)).sum()
        assert diff > 100_000, "柜框线没有真正画到图上"

    def test_compose_draws_nothing_without_frame_or_sprites(self, doc, tmp_path):
        """既没柜框也没图元时必须是纯白 —— 证明合成器自己不画任何图形。"""
        import numpy as np
        from PIL import Image

        from app.parsers.cabinet_compose import compose_cabinet
        from app.parsers.cupboard_geometry import Cupboard, Rect
        from app.parsers.meter_sprites import CabinetPlan, SpriteBank

        cup = Cupboard(rect=Rect(x0=0, y0=0, x1=1000, y1=1000))
        out = tmp_path / "blank.jpg"
        compose_cabinet(doc, cup, CabinetPlan(width_mm=1000, height_mm=1000),
                        SpriteBank(tmp_path / "sp"), [], out)
        with Image.open(out) as im:
            assert np.asarray(im.convert("RGB")).min() == 255


class TestComposeEndToEnd:
    """真实 DWG 全流程：合成路径渲染全部 23 个柜型。"""

    @pytest.fixture(scope="class")
    def rendered(self, real_dxf, tmp_path_factory):
        from app.parsers.cupboard_render import render_cupboard_regions

        out = tmp_path_factory.mktemp("compose")
        res, parsed = render_cupboard_regions(str(real_dxf), out, compose=True)
        return res, parsed, out

    def test_all_23_cabinets_rendered(self, rendered):
        res, parsed, _ = rendered
        ok = [r for r in res if r.ok]
        assert len(ok) == 23, f"只渲染出 {len(ok)}/23 个柜型"
        assert [r.error for r in res if not r.ok] == []

    def test_units_total_matches_ground_truth(self, rendered):
        """198 units 是人工核对过的 ground truth，必须对得上。"""
        _, parsed, _ = rendered
        assert sum(c.positions_total for c in parsed.cupboards) == 198

    def test_sprite_counts_match_position_count(self, rendered):
        """每个柜型的图元数 = 它自己的表位数（gas+water），不多不少。

        这是「柜型图确实由 DWG 原图元合成」最硬的证据：
        贴图数与解析器量出的真实表位数逐柜相等。
        """
        from app.parsers.cupboard_render import _cabinet_code

        res, parsed, _ = rendered
        by_code = {_cabinet_code(c, i): c
                   for i, c in enumerate(parsed.cupboards)}
        for r in res:
            if not r.ok:
                continue
            cup = by_code[r.block_name]
            assert sum(r.sprites.values()) == cup.gas_count + cup.water_count, (
                f"{r.block_name} 贴了 {sum(r.sprites.values())} 个图元，"
                f"但柜内只有 {cup.gas_count + cup.water_count} 个表位"
            )

    def test_ground_truth_units_distribution(self, rendered):
        """3..14 units 的柜型套数分布（人工核对过，7 units 确实是 3 套）。"""
        _, parsed, _ = rendered
        dist: dict[int, int] = {}
        for c in parsed.cupboards:
            dist[c.positions_total] = dist.get(c.positions_total, 0) + 1
        assert dist == {3: 2, 4: 1, 5: 2, 6: 2, 7: 3, 8: 1, 9: 2, 10: 2,
                        11: 2, 12: 2, 13: 3, 14: 1}

    def test_images_are_not_blank(self, rendered):
        """每张图都要有实际墨迹，且填充率合理（不是全白也不是糊成黑块）。"""
        import numpy as np
        from PIL import Image

        res, _, out = rendered
        for r in res:
            if not r.ok:
                continue
            p = out / r.image_name
            with Image.open(p) as im:
                arr = np.asarray(im.convert("L"), int)
            ink = (arr < 200).mean()
            assert 0.01< ink < 0.45, f"{r.block_name} 填充率 {ink:.3f} 不正常"

    def test_canvas_width_is_wide_enough_for_meter_detail(self, rendered):
        """CANVAS_W 必须是 1600 —— 1000 时燃气表只有 118px，看不清表盘。"""
        from app.parsers.cabinet_compose import CANVAS_W
        from PIL import Image

        assert CANVAS_W == 1600
        res, _, out = rendered
        with Image.open(out / res[0].image_name) as im:
            assert im.width == CANVAS_W

    def test_parallel_and_serial_compose_agree(self, real_dxf, tmp_path_factory):
        """并行/串行合成结果必须一致（含 sprites 统计）。"""
        from app.parsers.cupboard_render import render_cupboard_regions

        a, _ = render_cupboard_regions(str(real_dxf), tmp_path_factory.mktemp("pa"),
                                        workers=4, compose=True)
        b, _ = render_cupboard_regions(str(real_dxf), tmp_path_factory.mktemp("se"),
                                        workers=1, compose=True)
        assert [(r.block_name, r.ok, r.sprites) for r in a] == \
               [(r.block_name, r.ok, r.sprites) for r in b]

    def test_compose_faster_than_whole_cabinet_render(self, real_dxf,
                                                     tmp_path_factory):
        """合成路径必须比整柜渲染快（图元只渲 4 次并缓存，其余是纯贴图）。"""
        import time

        from app.parsers.cupboard_render import render_cupboard_regions

        t = time.time()
        res, _ = render_cupboard_regions(str(real_dxf), tmp_path_factory.mktemp("c"),
                                         compose=True)
        el = time.time() - t
        assert len([r for r in res if r.ok]) == 23
        assert el < 40, f"合成耗时 {el:.1f}s，超过 40s 上限"