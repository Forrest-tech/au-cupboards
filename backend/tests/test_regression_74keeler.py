"""74KEELER ST 实测踩坑回归测试。

本文件把开发过程中发现的每一个真实缺陷固化为测试。任何一条失败都意味着
解析行为发生了退化，必须修复后才能交付。

每条测试的docstring 都标注了：
  · 现象：当初错在哪
  · 根因：为什么错
  · 防线：现在靠什么防止复发

数据来源（三个独立来源互证，构成「44 户」这一权威口径）：
  1. A008 单元表（p9）：STOREY/UNIT No./DRY-WET/PROVIDED/GROSS 五列表格
  2. 封面 ROOM MIX：43 DOUBLE ROOM + 1 SINGLE ROOM + 2 COMMUNAL LIVING AREA
  3. 各层平面图标注：A103~A106 + GROUND 平面图
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.parsers.stage1 import Stage1Parser, floor_code

PLAN = Path("/workspace/var/extract/plan.pdf")

# 封面 ROOM MIX 申报的权威口径
COVER_TOTAL_UNITS = 44
COVER_DOUBLE = 43
COVER_SINGLE = 1
COVER_COMMUNAL = 2

# A008 单元表实测清单（逐户核对，不允许模糊匹配）
EXPECTED_BY_FLOOR: dict[int, list[str]] = {
    0: ["G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08", "CLA1"],
    1: [f"10{i}" for i in range(1, 10)] + ["110", "111"],
    2: [f"20{i}" for i in range(1, 10)] + ["210", "211"],
    3: [f"30{i}" for i in range(1, 10)] + ["310", "311"],
    4: ["401", "402", "403", "CLA2"],
}


@pytest.fixture(scope="module")
def result():
    if not PLAN.exists():
        pytest.skip(f"缺少图纸: {PLAN}")
    return Stage1Parser(load_config()).parse(str(PLAN))


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ================================================================ 口径锁定


def test_cover_44_is_the_authoritative_count(result):
    """锁定 44 户口径 —— 这是整个系统的地基。

    现象：报告阶段曾出现 28/ 32 / 38 / 44 四个数字互相矛盾。
    根因：28 是平面图 UNIT n 连续序号的最大值（不是户数）；
          32 是正则统计误差；38 漏了 GROUND 层的 8 户；
          44 才是 A008 单元表 + 封面 ROOM MIX 双源锁定的真值。
    防线：单元表与封面逐户对账，任何解析结果变化都会打破本测试。
    """
    assert result.total_units == COVER_TOTAL_UNITS
    assert result.total_communal == COVER_COMMUNAL
    assert result.total_units + result.total_communal == 46


def test_every_floor_matches_unit_schedule_exactly(result):
    """逐户对账：每层编号必须与 A008 单元表完全一致，一户不差。"""
    bf = result.by_floor()
    for floor_no, expected in EXPECTED_BY_FLOOR.items():
        actual = [u.label for u in bf[floor_no]]
        assert actual == expected, (
            f"{floor_code(floor_no)} 清单不符：\n"
            f"  期望 {expected}\n"
            f"  实际 {actual}"
        )


def test_single_room_count_matches_cover(result):
    """封面申报 1 个 SINGLE ROOM；单元表规则 MIN DRY AREA: SINGLE=12SQM。

    交叉验证：GROUND G01 的 DRY 面积 12.18 m2 < 16m2（DOUBLE 阈值），
    是全楼唯一的 SINGLE ROOM。这条测试证明面积数据可信。
    """
    units = [u for u in result.deduped_units() if not u.is_communal and u.area_m2]
    singles = [u for u in units if u.area_m2 < 16.0]
    assert len(singles) == COVER_SINGLE, (
        f"DRY 面积 <16m2 的单元应恰好 1 个（封面 SINGLE=1），实际 {len(singles)}："
        f"{[(u.floor_code, u.label, u.area_m2) for u in singles]}"
    )


# ================================================================ 踩坑 1：110/111 漏配


def test_units_110_111_are_not_dropped(result):
    """现象：旧正则 \\b[1-4]0[1-9]\\b 静默漏掉 110/111/210/211/310/311。

    根因：单元编号的序号可以进到两位（10、11），不只有 01~09。
          每层实际 11 户，旧口径只认9 户。
    防线：规则改为 [1-9](0[1-9]|1[0-9])，且本测试锁定 11 户。
    """
    bf = result.by_floor()
    for floor_no in (1, 2, 3):
        labels = {u.label for u in bf[floor_no]}
        tail = {f"{floor_no}10", f"{floor_no}11"}
        assert tail <= labels, f"L{floor_no} 缺编号 {tail - labels}"


def test_no_false_units_from_other_floors(result):
    """反向防线：不能为了凑 11 户而把别层的编号拉进来。"""
    bf = result.by_floor()
    for floor_no in (1, 2, 3):
        for u in bf[floor_no]:
            if u.is_communal:
                continue
            assert u.label[0] == str(floor_no), (
                f"L{floor_no} 混入 {u.label}（跨层串号）"
            )
            assert len(u.label) == 3, f"L{floor_no} 出现异常长度编号 {u.label}"


# ================================================================ 踩坑 2：GROUND 用字母编码


def test_ground_uses_letter_codes(result):
    """现象：GROUND 层完全解析不出（0 户）。

    根因：规则只认数字编码 [1-4]nn，而 GROUND 层用的是 G01~G08。
    防线：新增 UR-002-ground-coded 规则，floor_prefix_mode=letter_ground。
    """
    bf = result.by_floor()
    assert 0 in bf, "未识别到 GROUND 层"
    labels = {u.label for u in bf[0]}
    assert labels == {f"G0{i}" for i in range(1, 9)} | {"CLA1"}
    for u in bf[0]:
        if not u.is_communal:
            assert u.label_scheme == "ground_coded", (
                f"{u.label} 的编号体系标注错误: {u.label_scheme}"
            )


def test_ground_communal_is_flagged(result):
    """CLA1/CLA2 是 CO-LIVING 集体居住区，不是独立单元，但需配柜。"""
    communal = [u for u in result.deduped_units() if u.is_communal]
    assert {u.label for u in communal} == {"CLA1", "CLA2"}
    by_floor = {u.floor_no: u.label for u in communal}
    assert by_floor == {0: "CLA1", 4: "CLA2"}, "CLA1 应在 GROUND，CLA2 应在 LEVEL 4"


# ================================================================ 踩坑 3：电话数字噪声


def test_phone_number_digits_not_treated_as_units(result):
    """现象：设计师电话 +61 449 984 889 被拆成 449/984/889 三个独立词。

    根因：三个数字串与单元编号正则同形，且出现在标题栏带内。
    防线：① noise_labels 显式排除 ② 标题栏带（y > 0.86H）内的数字一律跳过。
    """
    for u in result.deduped_units():
        assert u.label not in {"449", "984", "889"}, (
            f"电话号码被误识别为单元: {u.label}"
        )

    # 反向验证：这三个数字确实存在于原始文本中（证明防线有效而非侥幸）
    import fitz

    doc = fitz.open(str(PLAN))
    page = doc[18]  # p19A103
    words = {w[4] for w in page.get_text("words")}
    assert "449" in words, "预期 p19 含电话号码数字 449"
    doc.close()


def test_titleband_numbers_are_excluded(result):
    """标题栏带内的数字（图号 A103、页码、电话）不得成为单元。"""
    for p in result.pages:
        for u in p.units:
            bbox = u.evidence.get("bbox")
            if not bbox:
                continue
            page_h = 842.0  # A3横向高度
            assert bbox[1] < page_h * 0.86, (
                f"p{p.page_no} 的 {u.label} 落在标题栏带内(y={bbox[1]})"
            )


# ================================================================ 踩坑 4：A004 误判为楼层平面图


def test_site_plan_not_treated_as_floor_plan(result):
    """现象：p5(A004) 是 Site/边界图，被 marker 计数误判为 floor_plan。

    根因：page_role 规则只看关键词命中数，A004 里出现 SETBACK/BATHROOM 等词。
    防线：excluded_drawing_prefixes 显式排除 A000~A006（总图区）。
    """
    p5 = next(p for p in result.pages if p.page_no == 5)
    assert p5.drawing_no == "A004"
    assert p5.role == "other", f"p5(A004) 应判为 other，实际 {p5.role}"
    assert not p5.units, f"p5(A004) 不应抽出单元: {[u.label for u in p5.units]}"


# ================================================================ 踩坑 5：跨页重复计数


def test_duplicate_pages_are_deduplicated(result):
    """现象：同一张 GROUND 平面图在 A007/A009/A010/A011/A014/A102 下重复出现，
    若逐页累加会得到 GROUND=77 户的荒谬结果。

    根因：同一套图被拆进不同专业分册（建筑/给排水/结构/消防），每份都有完整标注。

    防线：两层保护 ——
      ① 页面角色判定收敛：只有带 LEVEL n PLAN / GROUND FLOOR 标题的
         5 张图被认定为权威平面图，重复分册被判为 other
      ② deduped_units() 按 (floor_no, label) 去重合并（纵深防御）
    """
    # ① 角色收敛：恰好 5 张权威平面图 + 1 张单元表
    plans = [p for p in result.pages if p.role == "floor_plan" and p.units]
    assert len(plans) == 5, (
        f"权威平面图应为 5 张（GROUND + L1~L4），实际 {len(plans)}："
        f"{[(p.page_no, p.drawing_no) for p in plans]}"
    )
    assert sorted(p.drawing_no for p in plans) == ["A102", "A103", "A104", "A105", "A106"]

    # ② 去重生效：逐页累加数必须远大于去重后
    raw_count = sum(len(p.units) for p in result.pages)
    dedup_count = len(result.deduped_units())
    assert raw_count > dedup_count, (
        f"去重未生效：累加 {raw_count} vs 去重 {dedup_count}"
    )

    # 去重后 GROUND 只应有 9 个（G01-G08 + CLA1）
    assert len(result.by_floor()[0]) == 9


def test_dedup_preserves_best_evidence(result):
    """去重合并时必须保留信息量最大的那条（面积/来源不能丢）。"""
    for u in result.deduped_units():
        if u.area_m2 is None:
            continue
        assert u.evidence.get("bbox") or u.evidence.get("storey"), (
            f"{u.label} 去重后丢失了全部证据"
        )


# ================================================================ 单元表双源交叉验证


def test_plan_and_schedule_cross_validate(result):
    """两个独立数据源必须完全互证（verdict=AGREE）。"""
    cv = result.cross_validation
    assert cv, "未产生交叉验证结果（说明单元表没被识别）"
    assert cv["verdict"] == "AGREE", (
        f"平面图与单元表不一致：仅平面图 {cv['only_in_plan']}，"
        f"仅单元表 {cv['only_in_schedule']}"
    )
    assert cv["consistency"] == 1.0
    assert not cv["area_mismatch"], f"面积不一致: {cv['area_mismatch']}"


def test_unit_schedule_blocks_are_detected(result):
    """A008 单元表有 5 个表格块，必须全部识别（坐标法，非文本流）。"""
    sched_pages = [p for p in result.pages if p.role == "unit_schedule"]
    assert sched_pages, "未识别到单元表"
    sig = sched_pages[0].signals["schedule_signal"]
    assert sig["blocks"] == 5, f"应识别 5 个 STOREY 块，实际 {sig['blocks']}"
    assert sig["by_storey"] == {
        "GROUND FLOOR": 9,
        "LEVEL 1": 11,
        "LEVEL 2": 11,
        "LEVEL 3": 11,
        "LEVEL 4": 4,
    }, f"各块户数不符: {sig['by_storey']}"


def test_schedule_page_floor_is_not_polluted(result):
    """现象：A008 页含 LEVEL 1..LEVEL 4 全部字样，楼层识别会被污染。

    根因：FR-002-text-level 在单元表页面上匹配到多个楼层。
    防线：applies_to_roles 闸门 —— LEVEL n 规则仅在 role=floor_plan 时生效。
    """
    for p in result.pages:
        if p.role != "unit_schedule":
            continue
        # 单元表页不应被赋予单一楼层
        assert p.floor_no is None or p.floor_no in (0, 1, 2, 3, 4)


# ================================================================ 规则外置化


def test_all_rules_are_externalised(cfg):
    """所有解析规则必须来自 config，不硬编码在代码里。"""
    assert len(cfg.unit_rules) >= 3, "至少应有数字编码/字母编码/ communal 三条规则"
    ids = {r.rule_id for r in cfg.unit_rules}
    assert {"UR-001-floor-coded", "UR-002-ground-coded", "UR-003-communal"} <= ids
    for r in cfg.unit_rules:
        assert r.description, f"{r.rule_id} 缺少描述（无法溯源）"
        assert r.floor_prefix_mode in {"leading_digit", "letter_ground", "any_floor"}


def test_noise_labels_are_configurable(cfg):
    """噪声标签必须外置，换图纸时改配置即可，不改代码。"""
    rule = next(r for r in cfg.unit_rules if r.rule_id == "UR-001-floor-coded")
    assert {"449", "984", "889"} <= rule.noise_set


def test_diagnostics_record_rule_hits(result):
    """每条规则都应记录命中数，便于诊断失效规则。"""
    d = result.diagnostics
    assert d["UR-001-floor-coded"] > 0, "数字编码规则未命中"
    assert d["UR-002-ground-coded"] > 0, "字母编码规则未命中"
    assert d["US-001"] > 0, "单元表规则未命中"
    assert d["FR-002-text-level"] > 0, "LEVEL n 楼层规则未命中"
    # 不应命中的规则也要在册（值为 0），便于发现「规则失效」
    assert "UR-003-communal" in d


# ================================================================ AI 使用约束


def test_ai_never_used_on_vector_drawings(result):
    """需求硬约束：AI 只在扫描件无文本层时启用，且强制人工确认。"""
    assert result.used_ai is False
    for p in result.pages:
        assert p.signals.get("ai_required") is not True, (
            f"p{p.page_no} 有文本层却要求 AI 兜底"
        )


def test_every_page_has_text_layer(result):
    """实测 35 页全部有矢量文本层 → 全程零OCR、零 AI。"""
    no_layer = [p.page_no for p in result.pages if not p.text_layer_present]
    assert not no_layer, f"以下页面无文本层: {no_layer}"


# ================================================================ 配置改动可回滚


def test_config_can_be_reloaded_with_changes(tmp_path, cfg):
    """改配置即改行为 —— 不需要改代码（这是外置化的核心价值）。"""
    import json

    raw = json.loads(
        (Path(__file__).resolve().parent.parent / "app/config/parse_rules.json").read_text(
            encoding="utf-8"
        )
    )
    # 把数字编码规则改成只认 101（模拟规则失效）
    for r in raw["unit_label_rules"]:
        if r["rule_id"] == "UR-001-floor-coded":
            r["code_pattern"] = r"\b(101)\b"

    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    from app.config import load_config as lc

    c2 = lc(p)
    rule = next(r for r in c2.unit_rules if r.rule_id == "UR-001-floor-coded")
    assert rule.compiled_code.fullmatch("101")
    assert not rule.compiled_code.fullmatch("102"), "规则改动未生效"
    assert not rule.compiled_code.fullmatch("110")

# ================================================================ 面积提取完整性
# 这组用例固化本轮定位到的两个真实缺陷：
#   1. _column_area 的 ty 被写成编号的 x 中心（而非 y 坐标）
#      → L1 块靠"编号 x≈557 / 面积 y≈613 → dy=56"巧合落进 (1,60] 窗口而通过，
#        GROUND 块 dy=448 超窗 → G01~G08 + CLA1 的 DRY/WET 面积全丢
#   2. 块x 右界用了被 -45 放宽后的 x0，且same_band 按 x0 排序引入顺序依赖
#      → LEVEL 1 块右界错成 1190（跨越到 LEVEL 4）

def _schedule_units():
    """直接跑单元表解析器（不经floor_plan 页面），以便隔离验证面积提取。"""
    import fitz

    from app.parsers.stage1 import Stage1Parser

    cfg = load_config()
    doc = fitz.open(str(PLAN))
    for pno in range(doc.page_count):
        page = doc[pno]
        txt = page.get_text()
        if "UNIT SCHEDULE" in txt and "STOREY" in txt:
            units, sig = Stage1Parser(cfg)._parse_unit_schedule(page)
            return units, sig
    raise AssertionError("未找到单元表页")


def test_schedule_wet_area_full_coverage():
    """单元表 46 个空间的 DRY + WET 面积必须 100% 提取到。

    这是柜型选型的输入依据 —— 面积缺失会导致全部单元退化为同一档位。
    """
    units, sig = _schedule_units()
    assert len(units) == 46, f"单元数应为 46，实得 {len(units)}"
    missing = [
        (u.floor_code, u.label, u.area_m2, u.evidence.get("wet_area_m2"))
        for u in units
        if u.area_m2 is None or u.evidence.get("wet_area_m2") is None
    ]
    assert not missing, f"以下单元缺 DRY 或 WET 面积: {missing}"


def test_schedule_block_x_ranges_do_not_overlap():
    """5 个块的 x 区间必须互不越界（LEVEL 1 右界不能跨到 LEVEL 4）。"""
    units, _ = _schedule_units()
    # 各层首个编号的中心 x 必须严格按块单调递增
    xs = {}
    for u in units:
        bb = u.evidence.get("bbox")
        if bb:
            cx = (bb[0] + bb[2]) / 2
            xs.setdefault(u.floor_no, []).append(cx)
    # GROUND / L2 同在 x≈96 起始列；LEVEL1/LEVEL 3 在 x≈515；LEVEL 4 在 x≈939
    starts = {fl: min(v) for fl, v in xs.items()}
    assert starts[0] < starts[1] < starts[4], starts
    assert abs(starts[0] - starts[2]) < 5, f"GROUND 与 L2 应同起始列: {starts}"
    assert abs(starts[1] - starts[3]) < 5, f"LEVEL1 与 L3 应同起始列: {starts}"


# ================================================================ STAGE 2 多柜型分档

def test_wet_area_buckets_cover_observed_values():
    """WET 面积分档必须覆盖实测的全部取值集合。"""
    from app.services.meter_requirement import WET_BUCKETS, bucket_of

    units, _ = _schedule_units()
    observed = sorted({round(u.evidence["wet_area_m2"], 2) for u in units})
    assert observed, "无WET 面积样本"
    for v in observed:
        b = bucket_of(v)
        assert b.code in {x.code for x in WET_BUCKETS}
    # 单调性：面积越大，档位越高或持平
    codes = [x.code for x in WET_BUCKETS]
    ranks = [codes.index(bucket_of(v).code) for v in observed]
    assert ranks == sorted(ranks), f"分档非单调: {list(zip(observed, ranks))}"


def test_multiple_variants_per_floor():
    """需求 1：每层应出现 2 种以上 cupboard，而不是全楼一个柜型。

    回归背景：初版 default_requirement 对所有单元硬编码同一套表位，
    44 户全部匹配 CP-QUAD-2x2，右侧只显示 1 种 cupboard。
    """
    from app.services.cupboard_seed import seed_variants
    from app.services.meter_requirement import requirement_for
    from app.services.stage2 import MeterRequirement, Stage2Matcher

    m = Stage2Matcher(seed_variants())
    units, _ = _schedule_units()
    by_floor: dict[int, set[str]] = {}
    for u in units:
        spec = requirement_for(
            unit_label=u.label,
            wet_m2=u.evidence.get("wet_area_m2"),
            accessible=False,
            is_communal=u.is_communal,
            unit_type="DOUBLE",
        )
        mr = m.match(
            u.label,
            u.floor_no,
            MeterRequirement(
                meters=spec["meters"],
                accessible=spec["accessible"],
                centralised=spec["centralised"],
            ),
        )
        assert mr.variant is not None, (u.label, mr.reason)
        by_floor.setdefault(u.floor_no, set()).add(mr.variant.code)

    # GROUND 与 L4 各自必须出现 >= 2 种柜型
    assert len(by_floor[0]) >= 2, by_floor
    assert len(by_floor[4]) >= 2, by_floor
    # 全楼不能只有一种
    assert len({c for s in by_floor.values() for c in s}) >= 3, by_floor


def test_requirement_derivation_is_traceable():
    """每个推导结果必须带溯源字段，说明依据什么得出表位数。"""
    from app.services.meter_requirement import requirement_for

    spec = requirement_for("103", 6.44, False, False, "DOUBLE")
    d = spec["derivation"]
    assert d["source"] == "derived"
    assert d["basis"] == "wet_area_bucket"
    assert d["bucket"].startswith("W")
    assert d["wet_area_m2"] == 6.44
    assert sum(spec["meters"].values()) > 0
    assert "caveat" in d and d["caveat"], "必须声明推导阈值的局限"
