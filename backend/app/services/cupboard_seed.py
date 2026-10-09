"""柜型库种子数据 —— POC 用示例数据。

⚠️ 重要：以下尺寸为**占位示例**，用于验证 STAGE 2 匹配与合规校验逻辑，
   并非真实柜型参数。真实数据必须由业主提供的 DWG 解析入库
   （报告第 02 章 / Module B）。

尺寸来源标注为 estimated，表示待DWG 校准。
澳洲规范阈值见 services/stage2.py 的 AUStandard。
"""
from __future__ import annotations

from app.services.stage2 import Variant


def seed_variants() -> list[Variant]:
    """柜型库。

    排布由 rows×cols 计算，符合报告 5.3 的装配体建模原则。
    尺寸标注为 estimated —— 真实参数必须由业主 DWG 解析入库覆盖
    （Module B）。此处保证 STAGE 2 能覆盖 A008 实测出的 2~6 表位分档。
    """
    return [
        # ---- 2 表位（W1 档：单湿区，1 冷 + 1 燃气）
        Variant(
            code="CP-DBL-1x2",
            rows=1, cols=2, positions_total=2,
            w=900, h=800, d=200,
            meter_spacing_h=300, meter_spacing_v=0,
            description="双表位横排（1x2）：1 cold water + 1 gas。水平间距按 WA 规范 300mm。",
            supported_meters={"water", "gas"},
        ),
        # ---- 3 表位（W2 档：厨卫各一，1 冷 + 1 热 + 1 燃气）—— 实测最多户型
        Variant(
            code="CP-TRI-1x3",
            rows=1, cols=3, positions_total=3,
            w=1200, h=800, d=200,
            meter_spacing_h=300, meter_spacing_v=0,
            description="三表位横排（1x3）：1 cold + 1 hot water + 1 gas。最常见户型组合。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        Variant(
            code="CP-TRI-3x1",
            rows=3, cols=1, positions_total=3,
            w=600, h=1400, d=200,
            meter_spacing_h=0, meter_spacing_v=300,
            description="三表位竖排（3x1）：同上表位竖向布置，适合层高受限的壁龛。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        # ---- 4 表位（W3 档：双卫，1 冷 + 1 热 + 2 燃气）
        Variant(
            code="CP-QUAD-2x2",
            rows=2, cols=2, positions_total=4,
            w=1200, h=1200, d=200,
            meter_spacing_h=300, meter_spacing_v=250,
            description="四表位 2x2：1 cold + 1 hot water + 2 gas。适配双卫户型。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        Variant(
            code="CP-QUAD-1x4",
            rows=1, cols=4, positions_total=4,
            w=1800, h=800, d=200,
            meter_spacing_h=300, meter_spacing_v=0,
            description="四表位横排（1x4）：与 2x2 等容量，横向展开。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        # ---- 6 表位（W4 档：多卫/集体，2 冷 + 2 热 + 2 燃气）
        Variant(
            code="CP-SIX-2x3",
            rows=2, cols=3, positions_total=6,
            w=1800, h=1200, d=200,
            meter_spacing_h=300, meter_spacing_v=250,
            description="六表位 2x3：2 cold + 2 hot water + 2 gas。适配多卫或集体计量。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        # ---- 8 表位（CO-LIVING 集体居住）
        Variant(
            code="CP-OCT-2x4",
            rows=2, cols=4, positions_total=8,
            w=2400, h=1200, d=250,
            meter_spacing_h=300, meter_spacing_v=250,
            description="八表位 2x4：CO-LIVING 集体居住区专用，2 冷 + 2 热 + 4 燃气。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        # ---- 集中式（多楼层共享主干管）
        Variant(
            code="CP-CEN-3x4",
            rows=3, cols=4, positions_total=12,
            w=2400, h=1800, d=250,
            meter_spacing_h=150, meter_spacing_v=200,
            description="集中式表柜 3x4：净距提升至 150mm 满足 Jemena 集中式要求。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        # ---- 无障碍变体
        Variant(
            code="CP-TRI-1x3-ACC",
            rows=1, cols=3, positions_total=3,
            w=1300, h=1000, d=250,
            meter_spacing_h=350, meter_spacing_v=0,
            description="三表位无障碍柜：加高至 1000mm + 间距加宽至 350mm，便于轮椅操作。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        Variant(
            code="CP-QUAD-2x2-ACC",
            rows=2, cols=2, positions_total=4,
            w=1300, h=1400, d=250,
            meter_spacing_h=350, meter_spacing_v=300,
            description="四表位无障碍柜：2x2 加高加宽，适配 ACCESSIBLE UNIT。",
            supported_meters={"water", "hot_water", "gas"},
        ),
        # ---- 故意不合规的变体：用于验证合规校验会拦下它
        Variant(
            code="CP-BAD-1x1-TIGHT",
            rows=1, cols=1, positions_total=1,
            w=500, h=800, d=80,
            meter_spacing_h=50, meter_spacing_v=0,
            description="【仅测试】故意不合规：宽 500 < 600、深 80 < 100、净距 50 < 100。应被校验拦下。",
            supported_meters={"water", "gas"},
        ),
    ]


def default_requirement(unit_label: str) -> dict:
    """保留的向后兼容入口。

    ⚠️ 旧接口对所有单元返回同一套表位，是「44 户只匹配出1 种柜型」
    这个 bug 的根因，已废弃。新的调用方应改用
    `app.services.meter_requirement.requirement_for()`，
    它依据单元表实测的 WET 面积分档推导表位数。
    """
    return {
        "meters": {"water": 1, "hot_water": 1, "gas": 1},
        "accessible": False,
        "centralised": False,
        "derivation": {
            "source": "fallback",
            "basis": "legacy_default",
            "note": "旧版硬编码假设，已废弃；请改用 meter_requirement.requirement_for()",
        },
    }
