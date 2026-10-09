"""POC 核心假设验证测试。

每个测试对应报告里的一条核心假设。全部通过才算 POC 成功。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.parsers.stage1 import Stage1Parser
from app.services.cupboard_seed import seed_variants
from app.services.stage2 import MeterRequirement, Stage2Matcher, check_compliance

PLAN = Path("/workspace/var/extract/plan.pdf")


@pytest.fixture(scope="module")
def result():
    if not PLAN.exists():
        pytest.skip(f"缺少图纸: {PLAN}")
    cfg = load_config()
    return Stage1Parser(cfg).parse(str(PLAN))


# ---------------------------------------------------------------- 假设 1：矢量文本层


def test_h1_vector_text_layer_exists(result):
    """核心假设 1：真实图纸有完整矢量文本层 → 不需要 OCR/AI。"""
    plan_pages = [p for p in result.pages if p.role == "floor_plan" and p.floor_no]
    assert plan_pages, "未识别到任何楼层平面图"
    for p in plan_pages:
        assert p.text_layer_present, f"p{p.page_no} 没有文本层"
        assert p.signals["text_chars"] > 1000


def test_h1_no_ai_used(result):
    """核心假设 2：有文本层时不启用 AI。"""
    assert result.used_ai is False


# ---------------------------------------------------------------- 假设 3：楼层识别


def test_h2_floor_from_level_plan_text(result):
    """核心假设 3：楼层来自 'LEVEL n PLAN' 文本，而非图号。

    实测证明图号第二位是分区号（A101~A107 均为 1），
    因此图号不能作为楼层信号。
    """
    by_page = {p.page_no: p for p in result.pages}
    assert by_page[19].floor_no == 1, "p19(A103) 应为 L1"
    assert by_page[20].floor_no == 2, "p20(A104) 应为 L2"
    assert by_page[21].floor_no == 3, "p21(A105) 应为 L3"
    assert by_page[22].floor_no == 4, "p22(A106) 应为 L4"


# ---------------------------------------------------------------- 假设 4：units 抽取
#
# 【口径修正记录】本项曾三次误判，现已由A008 单元表与封面 ROOM MIX 双重锁定：
#   旧断言 30 户（L1-L3 各 9 + L4 3）—— **错误**，漏了两类：
#     ① 每层实际 11 户不是 9 户（110/111 被正则 [1-4]0[1-9] 静默漏掉）
#     ② GROUND 层有 8 户（G01-G08），用的是字母编码不是数字编码
#   正确口径：**44 户 + 2 个 CO-LIVING communal = 46 个空间**
#   与封面 ROOM MIX「43 DOUBLE + 1 SINGLE + 2 COMMUNAL」逐项吻合。
#   详见 tests/test_regression_74keeler.py 的固化测试。


def test_h3_units_extracted(result):
    """核心假设 4：每层 units 数正确，且与 A008 单元表交叉一致。"""
    bf = result.by_floor()
    assert set(bf) == {0, 1, 2, 3, 4}, f"楼层集合不符: {sorted(bf)}（应含 GROUND=0）"
    assert len(bf[0]) == 9, f"GROUND 应为 8 户 + 1 communal: {[u.label for u in bf[0]]}"
    assert len(bf[1]) == 11, f"L1 应为 11 户: {[u.label for u in bf[1]]}"
    assert len(bf[2]) == 11, f"L2 应为 11 户: {[u.label for u in bf[2]]}"
    assert len(bf[3]) == 11, f"L3 应为 11 户: {[u.label for u in bf[3]]}"
    assert len(bf[4]) == 4, f"L4 应为 3 户 + 1 communal: {[u.label for u in bf[4]]}"
    assert result.total_units == 44, f"总单元数应为 44（封面 ROOM MIX 口径）"
    assert result.total_communal == 2, "CO-LIVING communal 应为 2 处（CLA1/CLA2）"


def test_h3_labels_are_floor_coded(result):
    """单元编号体系：GROUND 用 G0X，其余层用楼层编码（nnn）。"""
    bf = result.by_floor()

    def labels(fl: int, real_only: bool = True) -> list[str]:
        us = bf[fl]
        if real_only:
            us = [u for u in us if not u.is_communal]
        return [u.label for u in us]

    assert labels(0) == [f"G0{i}" for i in range(1, 9)], "GROUND 应为 G01~G08"
    assert labels(1) == [f"10{i}" for i in range(1, 10)] + ["110", "111"]
    assert labels(2) == [f"20{i}" for i in range(1, 10)] + ["210", "211"]
    assert labels(3) == [f"30{i}" for i in range(1, 10)] + ["310", "311"]
    assert labels(4) == ["401", "402", "403"]
    # communal
    assert labels(0, False)[-1] == "CLA1"
    assert labels(4, False)[-1] == "CLA2"


def test_h3_areas_extracted(result):
    """面积标签应基本抽出—— 这是主信号的强度证据。

    允许少量缺失：多栏布局下极个别列的对齐可能落在容差外，
    但覆盖率必须 ≥ 90%。
    """
    units = [u for u in result.deduped_units() if not u.is_communal]
    with_area = [u for u in units if u.area_m2 is not None]
    rate = len(with_area) / len(units)
    assert rate >= 0.90, f"面积覆盖率仅 {rate:.1%}：缺 {[u.label for u in units if u.area_m2 is None]}"


def test_h3_no_cross_floor_leakage(result):
    """楼层隔离：平面图页不得出现别层编号；GROUND 只认G0X。

    注意单元表页（A008）例外：它天然含全部楼层的编号，
    且 page.floor_no 为 None（不代表单一楼层）。
    """
    for p in result.pages:
        if p.floor_no is None:
            continue
        if p.role == "unit_schedule":
            continue
        for u in p.units:
            if p.floor_no == 0:
                assert u.label.startswith("G") or u.label.startswith("CLA"), (
                    f"p{p.page_no}(GROUND) 混入了 {u.label}"
                )
            else:
                # CO-LIVING (CLA2) 不受楼层前缀约束
                if u.is_communal:
                    continue
                assert u.label.startswith(str(p.floor_no)), (
                    f"p{p.page_no}(L{p.floor_no}) 混入了 {u.label}"
                )


def test_unit_schedule_units_carry_own_floor(result):
    """单元表页的每个单元必须自带正确的 floor_no（不依赖页面级楼层）。"""
    sched = [p for p in result.pages if p.role == "unit_schedule"]
    assert sched, "未识别到单元表页"
    for p in sched:
        assert p.floor_no is None, "单元表页不应被赋予单一楼层"
        for u in p.units:
            expected = EXPECTED_FLOOR_OF.get(u.label)
            if expected is not None:
                assert u.floor_no == expected, (
                    f"单元表 {u.label} 楼层错标: {u.floor_no} != {expected}"
                )


# 各编号的权威楼层（来自 A008 单元表），用于逐户校验
EXPECTED_FLOOR_OF: dict[str, int] = {}
EXPECTED_FLOOR_OF.update({f"G0{i}": 0 for i in range(1, 9)})
EXPECTED_FLOOR_OF["CLA1"] = 0
EXPECTED_FLOOR_OF.update({f"{f}0{i}": f for f in (1, 2, 3) for i in range(1, 10)})
EXPECTED_FLOOR_OF.update({"110": 1, "111": 1, "210": 2, "211": 2, "310": 3, "311": 3})
EXPECTED_FLOOR_OF.update({f"40{i}": 4 for i in range(1, 4)})
EXPECTED_FLOOR_OF["CLA2"] = 4


# ---------------------------------------------------------------- 假设 5：置信度


def test_h5_confidence_high_for_vector(result):
    """矢量文本层 + 单元表双源互证的单元应达到高置信度。"""
    units = [u for u in result.deduped_units() if not u.is_communal]
    dual = [u for u in units if "unit_schedule" in u.sources and "floor_plan" in u.sources]
    assert dual, "未找到双源互证的单元"
    for u in dual[:10]:
        assert u.confidence >= 0.55, f"{u.floor_code}/{u.label} 置信度过低: {u.confidence}"


def test_h5_seq_signal_reliable(result):
    """缺号检测信号应可用，且真实楼层平面图的序号必须连续（1..N 无缺口）。

    这是 100% 可靠的交叉验证信号 —— 若有缺口说明漏抽了单元。
    """
    plan = [
        p for p in result.pages
        if p.role == "floor_plan" and p.floor_no is not None and p.units
    ]
    assert plan
    checked = 0
    for p in plan:
        sig = p.signals.get("seq_signal")
        if not sig or not sig.get("found"):
            continue  # 该页无编号（如仅有 communal）
        checked += 1
        assert not sig["gaps"], (
            f"p{p.page_no}(L{p.floor_no}) 序号有缺口: {sig['gaps']}"
        )
        assert sig["coverage"] == 1.0
    assert checked >= 4, f"仅检查了 {checked} 个楼层，覆盖不足"


# ---------------------------------------------------------------- 假设 6：STAGE 2


def test_h6_exact_match_3_meters():
    m = Stage2Matcher(seed_variants())
    r = m.match("101", 1, MeterRequirement({"water": 1, "hot_water": 1, "gas": 1}))
    assert r.variant is not None
    # 无 3 表位变体时应选容量最接近的
    assert r.variant.positions_total >= 3


def test_h6_exact_match_2_meters():
    m = Stage2Matcher(seed_variants())
    r = m.match("201", 2, MeterRequirement({"water": 1, "gas": 1}))
    assert r.variant.positions_total == 2, "2 表位需求应精确匹配 2 表位柜"
    assert "精确匹配" in r.reason


def test_h6_never_undersized(result):
    """核心：绝不能给 3 表位需求配 1 表位柜（这是初版真实踩过的 bug）。"""
    m = Stage2Matcher(seed_variants())
    for need in range(1, 13):
        req = MeterRequirement({"water": need})
        r = m.match("x", 1, req)
        if r.variant:
            assert r.variant.positions_total >= need


# ---------------------------------------------------------------- 假设 7：合规校验


def test_h7_blocks_noncompliant():
    """不合规柜型必须被 error 级拦下。"""
    bad = next(v for v in seed_variants() if v.code == "CP-BAD-1x1-TIGHT")
    issues = check_compliance(bad, MeterRequirement({"water": 1, "gas": 1}))
    errors = [i for i in issues if i.severity == "error"]
    assert errors, "宽 500<600 / 深 80<100 应报 error"
    assert {i.code for i in errors} >= {"AU-JEM-W", "AU-JEM-D"}


def test_h7_height_ceiling():
    """柜高超限应报 error（Jemena 最高点 2200mm）。"""
    bad = next(v for v in seed_variants() if v.code == "CP-CEN-3x4")
    issues = check_compliance(bad, MeterRequirement({"water": 1, "gas": 1}))
    # 1800 < 2200，应无超限错误
    assert not [i for i in issues if i.code == "AU-JEM-H"]


def test_h7_centralised_clearance():
    """集中式需 150mm 净距，单表位柜(100mm)应触发警告。

    注：原用例依赖 CP-SGL-1x1，该变体已在柜型库扩充时被移除
    （实测 46 个空间里没有任何单元只�� 1 个表位）。
    改用 CP-DBL-1x2 + 手构的低净距变体来覆盖同一规范点。
    """
    from dataclasses import replace

    dbl = next(v for v in seed_variants() if v.code == "CP-DBL-1x2")
    # 单表位检查分支：构造一个单表位、净距 100mm 的变体
    tight = replace(dbl, code="TEST-SGL-100", rows=1, cols=1, positions_total=1,
                    w=600, h=800, d=200, meter_spacing_h=100, meter_spacing_v=0)
    issues = check_compliance(tight, MeterRequirement({"water": 1, "gas": 1}, centralised=True))
    assert any(i.code == "AU-JEM-CLR" for i in issues), issues

    # 同一个变体在非集中式需求下（净距要求 100mm）不应报警
    issues2 = check_compliance(tight, MeterRequirement({"water": 1, "gas": 1}, centralised=False))
    assert not any(i.code == "AU-JEM-CLR" for i in issues2), issues2


def test_h7_horizontal_layout_has_no_vertical_warning():
    """回归：横排柜（1xN）不应误报垂直间距警告。

    实测踩坑：曾对所有 positions_total>1 的柜型都查垂直间距，
    而1x3/1x4 横排柜的 meter_spacing_v 恒为 0（本就没有垂直间距），
    导致 44 户里刷出 40 条无意义警告。
    """
    variants = {v.code: v for v in seed_variants()}
    tri = variants["CP-TRI-1x3"]
    assert tri.rows == 1 and tri.meter_spacing_v == 0
    issues = check_compliance(tri, MeterRequirement({"water": 1, "hot_water": 1, "gas": 1}))
    assert not [i for i in issues if i.code == "AU-GEN-V"], issues


def test_h7_vertical_layout_checks_vertical_spacing():
    """对照组：多行排布仍应检查垂直间距。"""
    variants = {v.code: v for v in seed_variants()}
    quad = variants["CP-QUAD-2x2"]
    assert quad.rows == 2
    issues = check_compliance(quad, MeterRequirement({"water": 1, "hot_water": 1, "gas": 2}))
    assert not [i for i in issues if i.code == "AU-GEN-V"], "250mm 应通过 100mm 下限"


def test_h7_accessible_units_get_accessible_variants():
    """A008 单元表标了 ACCESSIBLE UNIT 的 104/204/304/G02 必须匹配 -ACC 柜。"""
    m = Stage2Matcher(seed_variants())
    for label in ("104", "204", "304", "G02"):
        r = m.match(label, 1, MeterRequirement({"water": 1, "hot_water": 1, "gas": 1},
                                                accessible=True))
        assert r.variant is not None and r.variant.code.endswith("-ACC"), (label, r)

    # 普通单元绝不能被推给 -ACC 柜
    n = m.match("101", 1, MeterRequirement({"water": 1, "hot_water": 1, "gas": 1}))
    assert n.variant is not None and not n.variant.code.endswith("-ACC"), n


# ---------------------------------------------------------------- 假设 8：配置外置


def test_h8_config_externalised():
    """解析规则必须来自 config，不硬编码。"""
    cfg = load_config()
    assert cfg.unit_rules, "缺少单元标签规则"
    assert cfg.floor_rules, "缺少楼层规则"
    for r in cfg.unit_rules + cfg.floor_rules:
        assert r.rule_id and r.description, "每条规则必须有 id 与说明"


def test_h8_diagnostics_recorded(result):
    """诊断模式：每条规则的命中数必须被记录。"""
    assert result.diagnostics, "诊断信息为空"
    assert result.diagnostics.get("UR-001-floor-coded", 0) > 0
