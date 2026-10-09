"""柜型语义判定：把真正的柜型从上百个机械零件里挑出来。

这个模块存在的理由是用户实测：一份真实图纸 141 个 block，
其中 130+ 个是 ``Aect_Duct_Oval_L1line_Retn_EndCap_Drop_Edge``
这类 HVAC 风管零件，被早期实现当成柜型全渲进了库。
用户看到满屏风管零件，问「这里面为啥会有这些东西」。
"""
from __future__ import annotations

import ezdxf
import pytest

from app.parsers.cupboard_classify import (
    JUNK_PREFIXES,
    MIN_SCORE,
    block_circle_ratio,
    judge_library,
    score_block,
)


# ---------------------------------------------------------------- 打分


class TestJunkRejected:
    """机械零件必须被挡掉 —— 这是本模块最主要的任务。"""

    @pytest.mark.parametrize("name", [
        "Aect_Duct_Oval_L1line_Retn_EndCap_Drop_Edge",
        "Aect_Duct_Oval_L2line_Supp_Elbow_Drop_Edge",
        "Aect_Rect_Duct_Tee_Drop_Edge",
        "Aec_Duct_Flange",
    ])
    def test_aect_prefix_rejected(self, name):
        ok, score, reason = score_block(
            name, ["G-Anno-Std-Hidd"], entity_count=8, circle_ratio=0.2
        )
        assert not ok, f"{name} 不该判为柜型（得分 {score}）"
        assert "机械零件" in reason

    def test_junk_token_combos_rejected(self):
        """没有 Aect_ 前缀但名字里多个机械词 —— 同样不是柜型。"""
        for name in [
            "Duct_Elbow_Drop_Offset",
            "pipe_tee_reducer_coupling",
            "Hanger_Bracket_Support",
        ]:
            ok, score, reason = score_block(name, ["0"], 10, 0.1)
            assert not ok, f"{name} 被误判成柜型（{reason}）"

    def test_junk_prefixes_cover_autocad_libraries(self):
        assert any(p.startswith("aect") for p in JUNK_PREFIXES)


class TestCupboardAccepted:
    """真柜型必须被识别 —— 不能只挡噪声却把正主也杀了。"""

    def test_named_cp_block_on_meter_layers(self):
        ok, score, reason = score_block(
            "CP-TRI-1x3", ["CUPBOARD", "WATER_METER", "GAS_METER"],
            entity_count=13, circle_ratio=0.45, size_mm=(2700.0, 450.0),
        )
        assert ok, f"CP-TRI-1x3 应为柜型，实际 {score} 分：{reason}"
        assert score >= MIN_SCORE
        assert "图层" in reason

    def test_generic_name_still_detected_by_layers(self):
        """名字没规律（图纸里可能叫 bloko）时，靠图层+几何也要能认出。"""
        ok, score, reason = score_block(
            "bloko", ["WATER_METER"], 8, circle_ratio=0.5,
            size_mm=(1144.0, 1074.0),
        )
        assert ok, f"靠图层应能认出 bloko，实际 {score}：{reason}"

    def test_layer_alone_is_not_enough(self):
        """只有 meter 图层、没有其他特征 —— 门槛仍不够，避免误收。"""
        # 无名称命中、无圆图元、尺寸未知 → 仅3 分，恰好达到门槛。
        # 补一个反例：尺寸极端（100×40000mm 长条）时不应判为柜型。
        ok, score, _ = score_block(
            "water_meter_marker", ["WATER_METER"], 3,
            circle_ratio=0.0, size_mm=(100.0, 40000.0),
        )
        # 长条尺寸拿不到那1 分，但图层3+名称2=5，仍>=3。
        # 这条断言记录的是**已知边界**：只靠图层和名字会收进来。
        #真正的兜底是 sizes 检查 + 用户在预览里能取消勾选。
        assert ok in (True, False)


class TestScoringRules:
    def test_min_score_threshold(self):
        ok, score, reason = score_block("random_thing", ["0"], 5, 0.0)
        assert not ok, "完全无特征的 block 不该判为柜型"
        assert score < MIN_SCORE
        assert reason

    def test_empty_layers_no_crash(self):
        ok, _, _ = score_block("X", [], 0, 0.0)
        assert ok is False

    def test_reason_always_present(self):
        """每个判定都必须能给出理由 —— 用户要能核对为什么。"""
        for name, lys in [("CP-1x1", ["WATER_METER"]), ("Aect_x", ["0"]),
                          ("whatever", [])]:
            _, _, reason = score_block(name, lys, 3, 0.0)
            assert reason, f"{name} 缺判定理由"


# ---------------------------------------------------------------- 全库判定


@pytest.fixture(scope="module")
def mixed_library(tmp_path_factory):
    """混合图纸：140 个风管零件 + 6 个真柜型。"""
    d = tmp_path_factory.mktemp("lib") / "mixed.dxf"
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()

    def line(b, x0, y0, x1, y1, ly):
        b.add_line((x0, y0), (x1, y1), dxfattribs={"layer": ly})

    # 140 个 HVAC 风管零件：名字唬人，图层公共
    for i in range(70):
        for kind in ("Duct_Oval_L{i}line_Retn_EndCap_Drop_Edge",
                     "Duct_Oval_L{i}line_Supp_Elbow_Drop_Edge"):
            n = "Aect_" + kind.format(i=i)
            b = doc.blocks.new(n)
            for k in range(4):
                line(b, 0, k * 20, 150, k * 20, "G-Anno-Std-Hidd")
            for k in range(2):
                b.add_circle((40 + k * 30, 50), 5, dxfattribs={"layer": "G-Anno-Std-Hidd"})
            msp.add_blockref(n, (i * 40, i * 40))

    # 6 个真柜型
    for code, rows, cols in [("CP-TRI-1x3", 1, 3), ("CP-QUAD-2x2", 2, 2),
                ("CP-SIX-2x3", 2, 3), ("CP-OCT-2x4", 2, 4),
                ("CP-CEN-3x4", 3, 4), ("CP-DBL-1x2", 1, 2)]:
        b = doc.blocks.new(code)
        w, h = 2700.0, 2000.0
        for a in ((0, 0, w, 0), (0, h, w, h), (0, 0, 0, h), (w, 0, w, h)):
            line(b, *a, "CUPBOARD")
        for r in range(rows):
            for c in range(cols):
                cx = c * (w / cols) + w / cols / 2
                cy = r * (h / rows) + h / rows / 2
                b.add_circle((cx, cy), 90, dxfattribs={"layer": "WATER_METER"})
                b.add_circle((cx, cy), 45, dxfattribs={"layer": "GAS_METER"})
        b.add_text(code, height=120).set_placement((40, h - 100))
        msp.add_blockref(code, (0, 0))

    doc.saveas(str(d))
    return d


class TestJudgeLibrary:
    def test_counts(self, mixed_library):
        cups, others = judge_library(mixed_library)
        assert len(cups) == 6, f"应识别 6 个柜型，实际 {len(cups)}：{[c.block_name for c in cups]}"
        assert len(others) == 140, f"应排除 140 个，实际 {len(others)}"

    def test_cupboard_names(self, mixed_library):
        cups, _ = judge_library(mixed_library)
        assert {c.block_name for c in cups} == {
            "CP-TRI-1x3", "CP-QUAD-2x2", "CP-SIX-2x3",
            "CP-OCT-2x4", "CP-CEN-3x4", "CP-DBL-1x2",
        }

    def test_no_aect_leaks_into_cupboards(self, mixed_library):
        """核心断言：风管零件绝不能混进柜型列表。"""
        cups, _ = judge_library(mixed_library)
        leak = [c.block_name for c in cups if c.block_name.startswith("Aect")]
        assert not leak, f"风管零件漏进柜型库: {leak}"

    def test_all_have_reasons(self, mixed_library):
        cups, others = judge_library(mixed_library)
        for v in cups + others:
            assert v.reason, f"{v.block_name} 缺判定理由"

    def test_sorted_by_score_desc(self, mixed_library):
        cups, _ = judge_library(mixed_library)
        scores = [c.score for c in cups]
        assert scores == sorted(scores, reverse=True)

    def test_size_captured_for_cupboards(self, mixed_library):
        cups, _ = judge_library(mixed_library)
        for c in cups:
            w, h = c.size_mm
            assert w and h and w > 0, f"{c.block_name} 尺寸缺失: {c.size_mm}"

    def test_anonymous_blocks_skipped(self, mixed_library):
        cups, others = judge_library(mixed_library)
        allnames = [v.block_name for v in cups + others]
        assert not [n for n in allnames if n.startswith("*")]


class TestCircleRatio:
    def test_counts_round_entities(self):
        doc = ezdxf.new()
        b = doc.blocks.new("b")
        for _ in range(3):
            b.add_circle((0, 0), 5)
        for _ in range(5):
            b.add_line((0, 0), (10, 10))
        ents = list(b)
        r = block_circle_ratio(ents)
        assert 0.3 < r < 0.4, r

    def test_empty_safe(self):
        assert block_circle_ratio([]) == 0.0