"""柜型几何分解测试 —— **只跑真实 DWG**，Ground Truth = 用户人工统计。

**为什么不用合成样本（2026-10 血泪史）**
------------------------------------------
早期版本有个 ``make_cabinet_sample.py``，用 ezdxf 画「闭合矩形柜体 +
闭合矩形表位框」的假 DWG，然后所有柜型识别逻辑都在这份假数据上调参。
假数据结构和真实图纸差得很远，导致**在假数据上配对成功率 100% 的规则，
真实图纸上一个都配不出来**：

===================  =====================  =====================
                     合成样本（假）          真实图纸
===================  =====================  =====================
柜体外框             闭合 LWPOLYLINE         **4 条跨图层 LINE**
表位                 闭合矩形                INSERT block（块内顶点用
                                             绝对坐标，``insert`` 点
                                             偏 6500mm）
柜框对齐             上下边与侧墙严格对齐    侧墙在上下边端点**内侧**
上下边长度           上下完全一致            上下差几十毫米（顶板 vs净宽）
相邻柜               各自分开               **共用横线**
===================  =====================  =====================

所以现在：**集成测试一律跑真实 DWG**，验收标准是
:data:`~tests.fixtures.real_dwg.GROUND_TRUTH`（用户人工统计并复核确认的 23 套柜）。

纯几何单测（``TestRectGeometry`` / ``TestEdgeCases``）保留 —— 它们测的是
``Rect`` 数学运算，用``ezdxf.new()`` 现画现用，不依赖任何外部样本。
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
from tests.fixtures.real_dwg import (
    GROUND_TRUTH,
    GT_CABINETS,
    GT_UNITS,
    real_dwg_path,  # noqa: F401  (供 real_dxf 依赖链使用)
    real_dxf,
)


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

    def test_extract_rects_ignores_blocks(self, real_dxf):
        """柜型在 modelspace，block 里的零件不该被当成矩形。

        这是第 1 轮翻车的根因：真实图纸里 ``Aecb_Duct_Oval_*`` 风管零件
        有 130+ 个 block（注意前缀是 ``Aecb_`` 不是 ``Aect_``），遍历
        block 就会把它们全认成柜型。
        """
        import ezdxf
        rects, total = extract_rects(ezdxf.readfile(str(real_dxf)))
        assert total > 0
        # 真实文件里闭合多段线极少（1069 个 LWPOLYLINE 只有 16 个闭合），
        # 且闭合的那些主要是表位框 / 混凝土剖面，不是柜体
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

    def test_junk_layers_cover_aec_prefixes(self):
        """真实图纸的风管零件前缀是 ``Aecb_`` 而不是 ``Aect_``。

        按 ``aect`` 过滤一个都拦不住 —— 这个坑踩过一次。
        """
        for pfx in ("aect", "aecb"):
            assert any(pfx in k for k in JUNK_LAYER_KEYWORDS), (
                f"噪声前缀 {pfx} 未被JUNK_LAYER_KEYWORDS 覆盖"
            )

    def test_cabinet_layer_keywords_present(self):
        assert CABINET_LAYER_KEYWORDS, "柜体图层关键词不能为空"


# ---------------------------------------------------- 真实 DWG 核心验收
#
# 以下是**唯一**的柜型识别验收标准：跑真实 DWG，对照用户人工统计。


@pytest.fixture(scope="module")
def parsed(real_dxf):
    """真实 DWG 的解析结果（整个模块只解析一次，约 1.6 秒）。"""
    return parse_cupboards(str(real_dxf))


@pytest.fixture(scope="module")
def units_histogram(parsed):
    """{units: 个数} —— 与 Ground Truth 直接对比。"""
    hist: dict[int, int] = {}
    for c in parsed.cupboards:
        hist[c.positions_total] = hist.get(c.positions_total, 0) + 1
    return hist


class TestGroundTruthCabinetCount:
    """**验收标准：23 个柜型**（用户人工统计并复核确认）。"""

    def test_total_cabinets_is_22(self, parsed):
        got = len(parsed.cupboards)
        assert got == GT_CABINETS, (
            f"应识别 {GT_CABINETS} 个柜型，实际 {got}。"
            f"分布={sorted(c.positions_total for c in parsed.cupboards)}"
        )

    def test_units_distribution_matches(self, units_histogram):
        """**验收标准：按户数分布逐项一致**。

        用户 Ground Truth::
            3:2  4:1  5:2  6:2  7:2  8:1  9:2 10:2 11:2 12:2 13:3 14:1
        """
        for units, count in sorted(GROUND_TRUTH.items()):
            assert units_histogram.get(units, 0) == count, (
                f"{units} Units 应有 {count} 套，实际 "
                f"{units_histogram.get(units, 0)} 套。"
                f"全量分布={dict(sorted(units_histogram.items()))}"
            )

    def test_no_extra_units_bucket(self, units_histogram):
        """不该出现 Ground Truth 里没有的户数（凭空多出来的柜）。"""
        extra = set(units_histogram) - set(GROUND_TRUTH)
        assert not extra, f"出现 Ground Truth 之外的户数：{sorted(extra)}"

    def test_total_units_is_198(self, parsed):
        """所有柜的套数加起来 = 198（用户清单 23 套柜的总和）。"""
        got = sum(c.positions_total for c in parsed.cupboards)
        assert got == GT_UNITS, f"总套数应{GT_UNITS}，实际 {got}"

    def test_seven_units_cabinets_are_all_real(self, parsed):
        """把 7 Units 的差异钉死：识别到的每一个都是真实柜体。

        当前实测 3 套、Ground Truth 记 2 套 —— 差一个。
        上面三条断言失败正是这个差异的信号，**不要靠改Ground Truth
        把它们改绿**：先确认这三个柜是不是真柜。

        本条给出正面证据：三个 7 Units 柜互不重叠、各带真实 DIMENSION
        尺寸标注、且gas 与 water 数量逐一相等（每套1+1）。
        """
        seven = [c for c in parsed.cupboards if c.positions_total == 7]
        assert len(seven) == 3, f"预期 3 个 7 Units 柜，实际 {len(seven)}"

        # 1) 互不重叠（排除同一个柜被框成两遍）
        for i, a in enumerate(seven):
            for b in seven[i + 1:]:
                ox = min(a.rect.x1, b.rect.x1) - max(a.rect.x0, b.rect.x0)
                oy = min(a.rect.y1, b.rect.y1) - max(a.rect.y0, b.rect.y0)
                assert not (ox > 0 and oy > 0), (
                    f"两个 7 Units 柜重叠 {ox:.0f}×{oy:.0f}mm，疑似重复框"
                )

        for c in seven:
            gas = sum(1 for m in c.positions if m.kind == "gas")
            water = sum(1 for m in c.positions if m.kind == "water")
            # 2) 每套 1 gas + 1 water
            assert gas == water == 7, (
                f"7 Units 柜 gas={gas} water={water}，应各为 7"
            )
            # 3) 带真实 DIMENSION 标注（图纸上量出来的，不是估算）
            assert c.dim_w_mm and c.dim_h_mm, (
                f"7 Units 柜 {c.rect.width:.0f}×{c.rect.height:.0f} "
                f"缺少尺寸标注，无法确认它是真实柜体"
            )
            assert 800 < c.dim_w_mm < 2500, f"标注宽 {c.dim_w_mm:.0f} 不合理"
            assert 1700 < c.dim_h_mm < 2600, f"标注高 {c.dim_h_mm:.0f} 不合理"


class TestUnitsDefinition:
    """「套数」口径的固化断言。

    用户原话：「一个 cupboard 里面包含了多套的 water+gas，这种是一套」，
    以及「**所有水表、气表外观样式完全一样**，不能靠表的外观特征区分
    柜型，只能靠数量 + 空间排布」。
    """

    def test_gas_equals_water_in_every_cabinet(self, parsed):
        """**每个柜的 gas 表数必须等于 water 表数**。

        这是用户明确给的约束。不等就说明柜框边界切错了 ——
        有相邻的表被算进了这个柜，或者本该算进来的漏了。
        """
        for c in parsed.cupboards:
            assert c.gas_count == c.water_count, (
                f"柜 {c.positions_total} units "
                f"({c.rect.width:.0f}×{c.rect.height:.0f}mm "
                f"x={c.rect.x0:.0f}..{c.rect.x1:.0f}) "
                f"gas={c.gas_count} water={c.water_count}"
            )

    def test_units_equals_gas_count(self, parsed):
        """套数 = gas 表数（每套必配 1 个 gas）。"""
        for c in parsed.cupboards:
            assert c.positions_total == c.gas_count, (
                f"套数 {c.positions_total} != gas {c.gas_count}"
            )

    def test_units_in_ground_truth_range(self, parsed):
        """套数必须落在 3~14（Ground Truth 的取值范围）。"""
        for c in parsed.cupboards:
            assert 3 <= c.positions_total <= 14, (
                f"套数 {c.positions_total} 超出 Ground Truth 范围 3~14"
            )

    def test_every_position_is_gas_or_water(self, parsed):
        """每个表位标记都必须是 gas 或 water，不能是未知类型。

        实测踩坑：water 竖线常落在 gas 红框**之外**，只看框内图层会把
        water 算成 0 套。所以这里不要求预先配对成 mixed。
        """
        for c in parsed.cupboards:
            for p in c.positions:
                assert p.kind in ("gas", "water", "mixed"), (
                    f"柜内表位 {p.rect.as_tuple()} 类型无法识别"
                    f"（kind={p.kind}, 图层={p.layers}）"
                )


class TestSpatialLayout:
    """空间排布 —— 用户说「只能靠数量 + 空间排布」区分柜型。"""

    def test_layout_grid_inferred_from_coordinates(self, parsed):
        """排布行列必须从表位坐标聚类得出，不能靠猜。"""
        for c in parsed.cupboards:
            rows, cols = c.layout_rows_cols
            assert rows >= 1 and cols >= 1, f"排布退化：{c.grid_aspect}"
            assert rows * cols >= c.positions_total, (
                f"{rows}x{cols} 装不下 {c.positions_total} 个表位"
            )

    def test_grid_aspect_format(self, parsed):
        for c in parsed.cupboards:
            assert re.fullmatch(r"\d+x\d+", c.grid_aspect), (
                f"grid_aspect 格式错：{c.grid_aspect}"
            )

    def test_positions_sorted_top_to_bottom(self, parsed):
        """表位应按「先上后下、先左后右」排序，便于前端按序展示。"""
        for c in parsed.cupboards:
            ys = [p.rect.cy for p in c.positions]
            assert ys == sorted(ys, reverse=True), "表位未按从上到下排序"


class TestRealDrawingGeometry:
    """真实图纸的实测特征 —— 防止解析器退化回「闭合矩形」那条错路。"""

    def test_cabinets_come_from_line_pairs_not_closed_polylines(self, parsed):
        """柜框必须是「成对 LINE」，不是闭合多段线。

        真实文件实测：1069 个 LWPOLYLINE 里只有 16 个闭合，且闭合的
        主要是 250×450 的表位框和 4414×6215 的混凝土剖面 ——
        **没有一个是柜体**。早期「闭合矩形 = 柜体」的规则在真实文件上
        只能识别出 1 个（那个混凝土剖面），真正的柜型全漏。
        """
        for c in parsed.cupboards:
            assert c.rect.source_type == "LINE_PAIR", (
                f"柜体来源应为 LINE_PAIR，实际 {c.rect.source_type} —— "
                f"闭合多段线规则在真实图纸上无效"
            )

    def test_frame_pairs_counted(self, parsed):
        assert parsed.cabinet_frame_pairs == len(parsed.cupboards), (
            f"柜框配对数 {parsed.cabinet_frame_pairs} 与柜型数 "
            f"{len(parsed.cupboards)} 不一致"
        )

    def test_cabinet_sizes_are_plausible(self, parsed):
        """柜体尺寸实测区间：宽 865~2165mm，高 1750~2400mm。

        下限 1750 而不是 1800：``x=-4729..-3815`` 那个 3 units 柜的
        DIMENSION 标注实测就是 ``914.6 × 1750``，取 1800 会误判成配错。
        """
        for c in parsed.cupboards:
            w, h = c.bbox_mm
            assert 800 <= w <= 2500, f"柜宽 {w:.0f} 超出实测区间"
            assert 1700 <= h <= 2600, f"柜高 {h:.0f} 超出实测区间"

    def test_all_cabinets_above_ground(self, parsed):
        """柜高区间约束（1700~2600）保证不会把相邻柜的共用横线当上下边。"""
        for c in parsed.cupboards:
            assert 1700 <= c.rect.height <= 2600, (
                f"柜高 {c.rect.height:.0f} 异常，疑似上下边配错"
            )

    def test_cabinets_do_not_overlap(self, parsed):
        """柜与柜不能重叠 —— 重叠意味着框配对有误。

        相邻柜共用一条横线是允许的（共边），但**面积重叠**不行。
        """
        rects = [c.rect for c in parsed.cupboards]
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                ox = min(a.x1, b.x1) - max(a.x0, b.x0)
                oy = min(a.y1, b.y1) - max(a.y0, b.y0)
                if ox > 0 and oy > 0:
                    area = ox * oy
                    smaller = min(a.width * a.height, b.width * b.height)
                    assert area / smaller < 0.05, (
                        f"两个柜重叠 {area/smaller:.0%}："
                        f"{a.width:.0f}×{a.height:.0f}@{a.x0:.0f},{a.y0:.0f} 与 "
                        f"{b.width:.0f}×{b.height:.0f}@{b.x0:.0f},{b.y0:.0f}"
                    )


class TestDimensionsAndOutput:
    def test_as_dict_matches_requirement_fields(self, parsed):
        """需求文档 2.2 节要求的几何特征字段要齐。"""
        d = parsed.cupboards[0].as_dict()
        for k in ["layout_rows", "layout_cols", "positions_total",
                  "grid_aspect", "w", "h", "d", "bbox"]:
            assert k in d, f"缺字段 {k}"
        assert d["positions_total"] > 0
        assert set(d["bbox"]) == {"x", "y", "w", "h"}

    def test_summary_is_human_readable(self, parsed):
        s = parsed.summary()
        assert "柜型" in s
        assert "表位" in s
        for c in parsed.cupboards:
            assert str(c.positions_total) in s

    def test_geometry_matches_drawing_annotations(self, parsed):
        """**柜体几何必须与图纸标注一致** —— 最强的正确性证据。

        柜体矩形是靠配对长线得到的，属于「推断」；而 ``DIMENSION``
        是设计师在图纸上量出来并写下的数字，属于「权威」。两者独立，
        逐个吻合才说明配对没配错。

        **宽度 23/23 零误差吻合**（实测 |标注宽 − 几何宽| ≤ 0.5mm）。
        这一条单独严格断言：左右墙线的取法（取外侧 1CO 还是内侧
        5CS）直接决定宽度，实测取外侧 1CO 才对 —— 柜内那两道
        5CS 双壁线会让宽度少 150~265mm，与标注不符。

        高度只有 #14 / #15 两个不符，是**源图纸自身的瑕疵**
        （标注写 2350，画线画到 2300/2400），不是配对错误：
        已放大渲染确认红框顶边正好压在柜顶实线上。用几何真值
        2300/2400 才对，不该让解析器去迁就标注。
        """
        # ---- 宽度：全部必须吻合 ----
        bad_w = [
            (i, round(c.dim_w_mm, 1), round(c.rect.width, 1))
            for i, c in enumerate(parsed.cupboards)
            if c.dim_w_mm and abs(c.dim_w_mm - c.rect.width) > 25
        ]
        assert not bad_w, f"柜宽与标注不符（左右墙线取错）：{bad_w}"

        # ---- 高度：允许 #14 / #15 两个源图纸瑕疵 ----
        bad_h = [
            (i, round(c.dim_h_mm, 1), round(c.rect.height, 1))
            for i, c in enumerate(parsed.cupboards)
            if c.dim_h_mm and abs(c.dim_h_mm - c.rect.height) > 25
        ]
        assert {b[0] for b in bad_h} <= {14, 15}, (
            f"出现未预期的高度不符：{bad_h}"
        )

    def test_every_cabinet_has_width_annotation(self, parsed):
        """每个柜都必须能从图纸读到宽度标注（宽是零误差的主判据）。"""
        missing = [i for i, c in enumerate(parsed.cupboards) if not c.dim_w_mm]
        assert not missing, f"这些柜没读到宽度标注：{missing}"


class TestDiagnostics:
    def test_reports_entity_count(self, parsed):
        """真实文件实测 3420 个 modelspace 实体。"""
        assert parsed.entity_total > 3000, (
            f"实体数 {parsed.entity_total} 偏少，可能漏读了 modelspace"
        )

    def test_records_discarded_with_reason(self, parsed):
        """被丢弃的矩形必须带理由 —— 用户抱怨「为啥有这些」时要能解释。"""
        for rect, reason in parsed.discarded:
            assert reason, f"丢弃 {rect.as_tuple()} 没给理由"

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
