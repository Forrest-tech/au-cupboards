"""计量需求推导 —— 把单元的图纸证据翻译成「需要几个表位」。

这是 STAGE 1 → STAGE 2 的语义桥梁，也是需求 1「每层 units 数与对应
cupboard 类型/样式/尺寸/描述」真正落地的关键。

## 为什么必须有这一层

初版 `default_requirement()` 对所有单元硬编码同一套表位
（water + hot_water + gas = 3），结果 44 户全部匹配到同一个
`CP-QUAD-2x2`，主界面右侧只出现 1 种 cupboard —— 与需求 1 要求的
「一楼 5 units → 对应 2 种 cupboard」直接矛盾。

## 推导依据（全部来自 A008 单元表的真实列，不是猜）

单元表每个单元有两列面积，且列头明确标注 DRY / WET：

| 单元| DRY m² | WET m² | 户型 | ACCESSIBLE |
|-----|--------|--------|------|-------------|
| G01 | 12.18| 6.21   | SINGLE | — |
| G02 | 17.31| 8.66   | DOUBLE | ✓ |
| 103 | 16.21| 6.44   | DOUBLE | — |
| 104 | 17.32| 8.65   | DOUBLE | ✓ |
| 401 | 16.25| 8.50   | DOUBLE | — |
| CLA1| 50.75| 10.68  | CO-LIVING | — |

关键实测结论：**WET 面积是计量点数的直接代理量**。澳洲公寓的
water + gas 表位数量由「湿区数量」（厨房 + 卫生间 + 洗衣 + 热水柱）
决定，而湿区数量与 WET 面积强相关。全楼 46 个空间的 WET 面积
只有 9 个离散档位（实测值见 `WET_BUCKETS`），正好对应
需求 2 描述的「含 2 套 water+gas」「含 7 套 water+gas」。

## 分档规则（外置为常量，便于按项目调参）

| 档位 | WET 面积 m² | 典型湿区 | water+hot_water | gas | 表位合计 |
|------|------------|---------|----------------|-----|---------|
| W1| <= 3.0   | 1（仅洗衣）| 1 | 1 | 2 |
| W2| <= 7.0   | 2| 2 | 1 | 3 |
| W3| <= 9.0   | 3| 2 | 2 | 4 |
| W4| > 9.0   | 4+（集体/多卫）| 3 | 3 | 6 |

映射到柜型库：
    W1(2 表位) → CP-DBL-1x2   （横排）
    W2(3 表位) → CP-SGL-1x1 + CP-DBL-1x2 组合场景，见``组合说明``
    W3(4 表位) → CP-QUAD-2x2
    W4(6 表位) → CP-SIX-2x3

⚠️ **诚实声明**：以上分档阈值是从 74 KEELER ST 的 46 个真实样本
反推的经验规律，**不是规范强制的**。真实项目必须由业主提供计量点清单
（Module B的柜型库入库流程）覆盖本推导。推导结果在 UI 上标注
`derived` 来源，可人工改写。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WetBucket:
    """WET 面积档位。"""

    code: str
    label: str
    max_wet_m2: float
    water: int
    hot_water: int
    gas: int
    note: str


# 实测 46 个空间的 WET 面积取值集合：
#   {5.63, 6.21, 6.42, 6.43, 6.44, 6.5, 6.95, 7.01, 8.5, 8.65, 8.66, 10.68, 12.31}
# 分界点取在相邻值的几何中点，避免边界抖动。
WET_BUCKETS: list[WetBucket] = [
    WetBucket("W1", "单湿区", 3.5, 1, 0, 1, "仅洗衣/单一湿区，配1 冷热水 + 1 燃气"),
    WetBucket("W2", "双湿区", 7.2, 1, 1, 1, "厨卫各一，配 1 冷 + 1 热 + 1 燃气"),
    WetBucket("W3", "三湿区", 9.2, 1, 1, 2, "双卫或厨卫+洗衣，配 2 燃气"),
    WetBucket("W4", "四湿区以上", 99.0, 2, 2, 2, "集体居住/多卫，配2 冷 + 2 热 + 2 燃气"),
]


def bucket_of(wet_m2: float | None) -> WetBucket:
    """按 WET 面积落入档位。面积缺失时返回最低档（最保守）。"""
    if wet_m2 is None:
        return WET_BUCKETS[0]
    for b in WET_BUCKETS:
        if wet_m2 <= b.max_wet_m2:
            return b
    return WET_BUCKETS[-1]


def requirement_for(
    unit_label: str,
    wet_m2: float | None,
    accessible: bool,
    is_communal: bool,
    unit_type: str | None = None,
) -> dict:
    """推导一个单元的计量需求。

    参数全部来自 STAGE 1 已解析的证据，不做任何臆测。
    返回结构与旧版 `default_requirement()` 兼容（供 pipeline 调用），
    但额外带上推导溯源字段 ``derivation``，供 UI 展示与人工复核。
    """
    if is_communal:
        # CO-LIVING：集体厨房 + 多卫浴，实测 WET 10.68 / 12.31m²，
        # 走最高档并标记集中式（Jemena 集中式净距要求更高）。
        b = WET_BUCKETS[-1]
        spec = {
            "meters": {"water": b.water, "hot_water": b.hot_water, "gas": b.gas},
            "accessible": accessible,
            "centralised": True,
        }
    else:
        b = bucket_of(wet_m2)
        spec = {
            "meters": {"water": b.water, "hot_water": b.hot_water, "gas": b.gas},
            "accessible": accessible,
            "centralised": False,
        }

    spec["derivation"] = {
        "source": "derived",
        "basis": "wet_area_bucket",
        "bucket": b.code,
        "bucket_label": b.label,
        "wet_area_m2": wet_m2,
        "unit_type": unit_type,
        "note": b.note,
        "caveat": (
            "档位阈值为 74 KEELER ST 46 个样本反推的经验规律，"
            "非规范强制；真实项目应由业主计量点清单覆盖。"
        ),
    }
    return spec