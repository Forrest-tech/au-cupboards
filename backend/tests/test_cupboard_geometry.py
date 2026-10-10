"""柜型几何分解测试 —— 锁住「柜体→表位」的正确粒度。

**这轮测试存在的理由**

前两轮都在同一件事上翻车：

1. 第 1 轮：遍历命名 block逐个渲染 → 141 个 block 里 130+ 个是
   ``Aect_Duct_*`` 风管零件。
2. 第 2 轮：改成按 block 名 + 图层打分筛选 → 粒度错，渲出来的是
   柜内**单个 water 表 / gas 表**，而不是柜子。

根因是同一个错误的��提：「一个 block = 一个柜型」。真实图纸里
block 是按零件组织的，柜子在 modelspace 里。所以第 2 轮的关键
断言不是「认出了几个柜型」，而是**柜型数与表位数必须等于图纸里
空间并列的柜子**。

样本由 ``fixtures/make_cabinet_sample.py`` 按用户 2024-10 实图结构
构造：3 个柜型（2x3=6 套 / 2x2=4 套 / 2x1=2 套）+ Aect_Duct 噪声。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from app.parsers.cupboard_geometry import (
    CABINET_LAYER_KEYWORDS,
    GAS_LAYER_KEYWORDS,
    JUNK_LAYER_KEYWORDS,
    WATER_LAYER_KEYWORDS,
    MeterPosition,
    Rect,
    classify_position,
    extract_rects,
    parse_cupboards,
    rect_from_entity,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def sample_dxf(tmp_path_factory) -> str:
    """按用户实图结构生成的样本 DXF（3 柜型 + 噪声 block）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_cabinet_sample", FIXTURES / "make_cabinet_sample.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build(str(tmp_path_factory.mktemp("cup") / "sample.dxf"))


@pytest.fixture(scope="module")
def parsed(sample_dxf):
    return parse_cupboards(sample_dxf)


# ---------------------------------------------------------------- 基本几何


class TestRectGeometry:
    def test_contains_and_margin(self):
        outer = Rect(0, 0, 1000, 2000)
        inner = Rect(100, 100, 400, 500)
        assert outer.contains(inner)
        assert outer.margin_to(inner) == 100.0
        assert not inner.contains(outer)

    def test_overlap_detects_duplicate_stroke(self):
        a = Rect(0, 0, 100, 100)
        b = Rect(2, 2, 102, 102)      # 双层描边
        assert a.overlap(b) > 0.9
        c = Rect(500, 500, 600, 600)
        assert a.overlap(c) == 0.0

    def test_non_rectangular_polyline_rejected(self):
        """斜多边形不是柜体也不是表位 —— 放宽会把噪声引进来。"""
        import ezdxf
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        tri = msp.add_lwpolyline([(0, 0), (100, 50), (50, 100)], close=True)
        assert rect_from_entity(tri) is None, "三角形不应被识别为矩形"

    def test_open_polyline_rejected(self):
        import ezdxf
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        op = msp.add_lwpolyline([(0, 0), (100, 0), (100, 100)], close=False)
        assert rect_from_entity(op) is None, "开放多段线不是闭合矩形"

    def test_rect_from_closed_lwpolyline(self):
        """真实图纸里柜体就是闭合 LWPOLYLINE —— 这是主路径。"""
        import ezdxf
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        r = msp.add_lwpolyline([(0, 0), (1300, 0), (1300, 2400), (0, 2400)],
                               close=True)
        rect = rect_from_entity(r)
        assert rect is not None
        assert rect.width == pytest.approx(1300)
        assert rect.height == pytest.approx(2400)
        assert rect.source_type == "LWPOLYLINE"

    def test_classic_polyline_also_accepted(self):
        """老图纸用 POLYLINE 而非 LWPOLYLINE，两种都要认。"""
        import ezdxf
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        pl = msp.add_polyline2d([(0, 0), (800, 0), (800, 600), (0, 600)])
        pl.close(True)
        rect = rect_from_entity(pl)
        assert rect is not None, "POLYLINE 闭合矩形应被识别"
        assert rect.width == pytest.approx(800)

    def test_extract_rects_ignores_blocks(self, sample_dxf):
        """柜型在 modelspace，block 里的零件不该被当成矩形。"""
        import ezdxf
        """柜型在 modelspace，不在 block 里。

        这是第1 轮翻车的根因：``Aect_Duct_*`` 全在 block 中，
        遍历 block 就会把它们全认成柜型。
        """
        rects, total = extract_rects(ezdxf.readfile(sample_dxf))
        assert total > 0
        assert rects, "样本应有矩形"
        # 噪声 block 里的圆不该被当成矩形
        for r in rects:
            assert r.source_type in ("LWPOLYLINE", "POLYLINE", "RECTANG")


# ---------------------------------------------------------------- 语义判定


class TestLayerSemantics:
    def test_gas_and_water_keywords_disjoint(self):
        overlap = set(GAS_LAYER_KEYWORDS) & set(WATER_LAYER_KEYWORDS)
        assert not overlap, f"gas 与 water 关键词重叠：{overlap}"

    def test_classify_position(self):
        assert classify_position(["GAS_METER"]) == "gas"
        assert classify_position(["WATER_METER"]) == "water"
        assert classify_position(["GAS_METER", "WATER_METER"]) == "mixed"
        assert classify_position(["SOMETHING_ELSE"]) == "unknown"
        assert classify_position([]) == "unknown"

    def test_junk_layers_cover_aect(self):
        """Aect 是 AutoCAD 风管组件库前缀 —— 第 1 轮 130+ 噪声的来源。"""
        assert any("aect" in k for k in JUNK_LAYER_KEYWORDS)

    def test_cabinet_layer_keywords_present(self):
        assert CABINET_LAYER_KEYWORDS, "柜体图层关键词不能为空"


# ---------------------------------------------------------------- 核心分解


class TestCupboardDecomposition:
    def test_finds_three_cabinets(self, parsed):
        """样本里空间并列 3 个柜型（上方2 大柜 + 下方 1 小柜）。"""
        assert len(parsed.cupboards) == 3, (
            f"应识别 3 个柜型，实际 {len(parsed.cupboards)}："
            f"{[c.rect.as_tuple() for c in parsed.cupboards]}"
        )

    def test_positions_count_matches_design(self, parsed):
        """**这是本轮最关键的断言**。

        用户口径：「每层楼有一个柜，每层有几个 units 就有几套
        water+gas」。所以柜子的表位数必须等于图纸画的表位框数，
        而不是「柜内 water 图元数」或「block 数」。
        """
        counts = sorted(c.positions_total for c in parsed.cupboards)
        assert counts == [2, 4, 6], f"表位数应��� 2/4/6，实际 {counts}"

    def test_each_position_is_one_water_plus_gas(self, parsed):
        """每个表位标记必须是 gas 或 water 之一，不能是未知类型。

        实测踩坑：water 竖线常落在 gas 红框**之外**
        （样本里相距180mm），只看框内图层会把 water 算成 0 套。
        所以这里不要求预先配对成 mixed —— 解析器按「谁在柜内」
        逐个登记，套数由 :attr:`positions_total` 按gas 口径汇总。
        """
        for c in parsed.cupboards:
            for p in c.positions:
                assert p.kind in ("gas", "water", "mixed"), (
                    f"柜内表位 {p.rect.as_tuple()} 类型无法识别"
                    f"（kind={p.kind}, 图层={p.layers}）"
                )

    def test_gas_count_equals_positions(self, parsed):
        """套数 == gas 表数（每套必配1 个 gas）。

        water 数可能**多于**套数 —— 图纸上单独的 water bank
        （不成套排布的水表组）不该被算成额外的套。
        """
        for c in parsed.cupboards:
            assert c.gas_count == c.positions_total
            assert c.water_count >= c.positions_total

    def test_layout_grid_inferred(self, parsed):
        """排布行列要从表位坐标聚类得出，不能靠猜。"""
        got = sorted(c.grid_aspect for c in parsed.cupboards)
        assert got == ["2x1", "2x2", "2x3"], f"排布应2x1/2x2/2x3，实际 {got}"

    def test_layout_rows_cols_match(self, parsed):
        for c in parsed.cupboards:
            rows, cols = c.layout_rows_cols
            assert rows * cols >= c.positions_total, (
                f"{rows}x{cols} 装不下 {c.positions_total} 个表位"
            )

    def test_positions_sorted_top_to_bottom(self, parsed):
        """表位应按「先上后下、先左后右」排序，便于前端按序展示。"""
        for c in parsed.cupboards:
            ys = [p.rect.cy for p in c.positions]
            assert ys == sorted(ys, reverse=True), "表位未按从上到下排序"

    def test_no_junk_cabinet_from_duct_blocks(self, parsed):
        """Aect_Duct 风管零件绝不能被当成柜型。"""
        for c in parsed.cupboards:
            low = c.layer.lower()
            assert not any(k in low for k in JUNK_LAYER_KEYWORDS), (
                f"柜型来自噪声图层 {c.layer}"
            )

    def test_cabinet_sizes_are_plausible(self, parsed):
        """柜体尺寸应在「人能装的壁龛」区间（截图里 1000~2400mm）。"""
        for c in parsed.cupboards:
            w, h = c.bbox_mm
            assert w and h, "柜体尺寸必须算出来"
            assert 400 <= w <= 5000, f"柜宽 {w} 不合理"
            assert 400 <= h <= 5000, f"柜高 {h} 不合理"


class TestDimensionsFromDrawing:
    def test_width_read_from_dimension_chain(self, parsed):
        """总尺寸要从标注链读出，而不是取「最近那条」。

        实测踩坑：用户实图标注链形如 ``50/175/200/100/250/175/50``，
        总宽 1000。最初取最近标注，拿到的是第一段 **50**，
        柜体尺寸全错。现在靠「细分链求和 ≈ 边长」判定。
        """
        for c in parsed.cupboards:
            assert c.dim_w_mm == pytest.approx(c.rect.width, rel=0.10), (
                f"读到的宽 {c.dim_w_mm} 与几何 {c.rect.width:.0f} 差太远"
            )
            assert c.dim_w_mm and c.dim_w_mm > 500, (
                f"读到细分段而非总尺寸：{c.dim_w_mm}"
            )

    def test_as_dict_matches_requirement_fields(self, parsed):
        """需求文档 2.2 节要求的几何特征字段要齐。"""
        d = parsed.cupboards[0].as_dict()
        for k in ["layout_rows", "layout_cols", "positions_total",
                  "grid_aspect", "w", "h", "d", "bbox"]:
            assert k in d, f"缺字段 {k}"
        assert d["positions_total"] > 0
        assert set(d["bbox"]) == {"x", "y", "w", "h"}


class TestNotesIsolation:
    def test_notes_do_not_cross_cabinets(self, parsed):
        """并排柜子的说明文字不能互相串台。

        实测踩坑：一开始允许文字水平超出柜体 400mm，结果
        「6 SETS - 2 COL x 3 ROW」被两个柜子同时收进来。
        """
        allnotes = [n for c in parsed.cupboards for n in c.notes]
        assert len(allnotes) == len(set(allnotes)), f"说明文字串台：{allnotes}"

    def test_each_cabinet_has_its_own_note(self, parsed):
        """每个柜子都应该认领到自己的那行说明。"""
        for c in parsed.cupboards:
            assert c.notes, f"柜 {c.rect.as_tuple()} 没读到说明文字"


class TestDiagnostics:
    def test_reports_entity_and_rect_counts(self, parsed):
        assert parsed.entity_total > 0
        assert parsed.rect_total > 0
        assert parsed.rect_total <= parsed.entity_total

    def test_records_discarded_with_reason(self, parsed):
        """被丢弃的矩形必须带理由 —— 用户抱怨「为啥有这些」时要能解释。"""
        assert parsed.discarded
        for rect, reason in parsed.discarded:
            assert reason, f"丢弃 {rect.as_tuple()} 没给理由"

    def test_summary_is_human_readable(self, parsed):
        s = parsed.summary()
        assert "柜型" in s
        assert "表位" in s
        for c in parsed.cupboards:
            assert str(c.positions_total) in s

    def test_cabinet_layer_hits_recorded(self, parsed):
        """记录柜体图层命中情况，便于诊断「图层对不对」。"""
        assert parsed.cabinet_layer_hits


class TestEdgeCases:
    def test_empty_drawing(self, tmp_path):
        import ezdxf
        p = tmp_path / "empty.dxf"
        doc = ezdxf.new("R2018")
        doc.modelspace().add_line((0, 0), (100, 100))
        doc.saveas(str(p))
        r = parse_cupboards(str(p))
        assert r.cupboards == []
        assert r.summary()

    def test_no_closed_rect_at_all(self, tmp_path):
        import ezdxf
        p = tmp_path / "nolines.dxf"
        doc = ezdxf.new("R2018")
        for i in range(5):
            doc.modelspace().add_line((i * 10, 0), (i * 10 + 5, 100))
        doc.saveas(str(p))
        r = parse_cupboards(str(p))
        assert r.cupboards == [], "无闭合矩形时不应凭空造出柜型"

    def test_single_cabinet_no_grid(self, tmp_path):
        """只有一个表位时排布退化成 1x1，不该崩。"""
        import ezdxf
        p = tmp_path / "one.dxf"
        doc = ezdxf.new("R2018", setup=True)
        doc.layers.add("CABINET", color=4)
        doc.layers.add("GAS_METER", color=1)
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (1300, 0), (1300, 2400), (0, 2400)],
                           close=True, dxfattribs={"layer": "CABINET"})
        msp.add_lwpolyline([(400, 900), (550, 900), (550, 1090), (400, 1090)],
                           close=True, dxfattribs={"layer": "GAS_METER"})
        doc.saveas(str(p))
        r = parse_cupboards(str(p))
        assert len(r.cupboards) == 1
        assert r.cupboards[0].positions_total == 1
        assert r.cupboards[0].layout_rows_cols == (1, 1)

    def test_tiny_rects_ignored(self, tmp_path):
        """图纸角落的小方块（螺丝孔之类）不该撑出一个柜型。"""
        import ezdxf
        p = tmp_path / "tiny.dxf"
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        for i in range(20):
            msp.add_lwpolyline(
                [(i * 50, 0), (i * 50 + 20, 0), (i * 50 + 20, 20), (i * 50, 20)],
                close=True)
        doc.saveas(str(p))
        r = parse_cupboards(str(p))
        assert r.cupboards == []


# ---------------------------------------------------------------- API 端点


class TestRenderEndpointUsesCabinetGranularity:
    """``POST /api/cupboards/render`` 必须按**柜体**返回，而不是 block。

    实测踩坑（用户 2024-10 纠正）：「不是解析单个 water 或者 gas 的表，
    而是一个 cupboard，里面包含了多套的 water+gas」。
    端点返回的``positions_total`` 就是该柜的表位数（= units 数）。
    """

    def test_endpoint_uses_regions_not_blocks(self):
        src = (Path(__file__).resolve().parents[2] / "backend"
               / "app" / "api" / "server.py").read_text()
        fn = re.search(r'def render_cupboards\(.*?\n(?=@app|\ndef )', src, re.S)
        assert fn, "render_cupboards 未找到"
        body = fn.group(0)
        assert "render_cupboard_regions" in body, \
            "渲染端点仍走 block 粒度（render_cupboard_library）"
        assert "judge_library" not in body, \
            "端点还在用按 block 名打分的旧判定"

    def test_endpoint_returns_positions_total(self):
        """响应里必须有 positions_total —— 前端按它（=units）分组。"""
        src = (Path(__file__).resolve().parents[2] / "backend"
               / "app" / "api" / "server.py").read_text()
        fn = re.search(r'def _cup_out\(.*?\n(?=@app|\ndef |\Z)', src, re.S)
        assert fn, "_cup_out 未找到"
        assert "as_dict" in fn.group(0), "_cup_out 应基于 Cupboard.as_dict()"

    def test_diagnostics_expose_discarded_reason(self):
        """丢弃的矩形必须带理由 —— 用户会问「为啥有这些」。"""
        src = (Path(__file__).resolve().parents[2] / "backend"
               / "app" / "api" / "server.py").read_text()
        assert '"discarded"' in src, "响应缺 discarded 明细"
        assert '"reason": why' in src, "discarded 条目缺 reason"
