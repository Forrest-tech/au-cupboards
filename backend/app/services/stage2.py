"""STAGE 2：units → 匹配柜型变体 + 澳洲规范校验。

建模借鉴 BidWright 的装配体思路（报告 5.3）：
  柜型 = 装配体模板；变体 = rows × cols 计算出的具体排布，而非硬编码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class MeterRequirement:
    """一个单元的计量需求。"""

    # 该单元需要配置的表位类型与数量，如 {"water": 1, "hot_water": 1, "gas": 1}
    meters: dict[str, int]
    # 是否无障碍（ACCESSIBLE）—— 影响柜体高度与净空
    accessible: bool = False
    # 是否有集中式（centralised）计量需求
    centralised: bool = False


@dataclass
class Variant:
    """柜型变体。"""

    code: str
    rows: int
    cols: int
    positions_total: int
    # 尺寸 mm
    w: float
    h: float
    d: float
    meter_spacing_h: float
    meter_spacing_v: float
    description: str = ""
    # 支持的表类型
    supported_meters: set[str] = field(default_factory=set)

    @property
    def grid_aspect(self) -> str:
        return f"{self.rows}x{self.cols}"

    @property
    def name(self) -> str:
        """排布形式的可读名称。

        需求 2 要求右侧显示「对应排布种类（1/2/3 等）」。
        实测柜型库命名规律：rows 决定层数，cols 决定每层表位数，
        故用「{cols}联排 × {rows}层」比裸 code 更符合现场沟通语言。
        """
        return f"{self.cols}表位/层 × {self.rows}层"

    @property
    def size_text(self) -> str:
        return f"{self.w:.0f}W × {self.h:.0f}H × {self.d:.0f}D mm"

    def fits(self, req: MeterRequirement) -> bool:
        """是否能容纳需求的全部表位。

        修正记录：初版只检查 ``req.meters[t] <= positions_total``，
        导致「需要 water+hot_water+gas 共 3 个表位」被匹配到 1 表位柜
        ——因为每种表都是 1 个，1 <= 1 成立。
        正确判定是**总表位数**必须够：
            sum(req.meters.values()) <= positions_total
        """
        total_required = sum(req.meters.values())
        if total_required > self.positions_total:
            return False
        # 柜型必须支持所有需要的表类型
        return all(t in self.supported_meters for t in req.meters if req.meters[t] > 0)


@dataclass
class ComplianceIssue:
    severity: str  # error / warning
    code: str
    message: str
    actual: float | None = None
    required: float | None = None


@dataclass
class MatchResult:
    unit_label: str
    floor_no: int
    variant: Variant | None
    auto_matched: bool
    reason: str
    compliance: list[ComplianceIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.variant is not None and not any(i.severity == "error" for i in self.compliance)


# ---------------------------------------------------------------- 澳洲规范

# 来源：报告第 07 章实测调研的澳洲各州规范要点
# 注意：这些阈值必须外置为配置（不同项目/地区可能不同），此处为 POC 默认值
@dataclass
class AUStandard:
    """澳洲计量柜安装规范阈值。"""

    # Jemena ADG-002 (NSW 燃气)
    single_meter_h: float = 100.0
    single_meter_w: float = 600.0
    single_meter_d: float = 100.0
    single_min_clearance: float = 100.0     # 净距 >= 100mm
    centralised_min_clearance: float = 150.0
    # 最高点限制
    max_top_height: float = 2200.0
    # ATCO Gas
    atco_surface_h_min: float = 300.0
    atco_surface_h_max: float = 1000.0
    atco_recessed_h_min: float = 200.0
    atco_recessed_h_max: float = 1500.0
    # Water Corporation WA
    wa_horizontal_spacing: float = 300.0
    wa_vertical_spacing: float = 1200.0
    # 柜内表位垂直间距下限（多行排布才适用）
    min_vertical_spacing: float = 100.0


DEFAULT_STANDARD = AUStandard()


def check_compliance(
    v: Variant, req: MeterRequirement, std: AUStandard = DEFAULT_STANDARD
) -> list[ComplianceIssue]:
    """按澳洲规范校验柜体几何与安装要求。

    这是本项目的核心竞争力之一：把规范内建为可校验的规则，
    而不是靠人工翻规范文档。
    """
    issues: list[ComplianceIssue] = []

    # 单表位尺寸下限
    if v.positions_total == 1:
        if v.w < std.single_meter_w:
            issues.append(
                ComplianceIssue("error", "AU-JEM-W", f"单表位柜宽 {v.w:.0f} < {std.single_meter_w:.0f}mm",
                                v.w, std.single_meter_w)
            )
        if v.d < std.single_meter_d:
            issues.append(
                ComplianceIssue("error", "AU-JEM-D", f"单表位柜深 {v.d:.0f} < {std.single_meter_d:.0f}mm",
                                v.d, std.single_meter_d)
            )
        need_clear = (
            std.centralised_min_clearance if req.centralised else std.single_min_clearance
        )
        if v.meter_spacing_h < need_clear:
            issues.append(
                ComplianceIssue("warning", "AU-JEM-CLR",
                                f"表位净距 {v.meter_spacing_h:.0f} < 规范要求 {need_clear:.0f}mm",
                                v.meter_spacing_h, need_clear)
            )

    # 柜体高度上限（Jemena：最高点 2200mm）
    if v.h > std.max_top_height:
        issues.append(
            ComplianceIssue("error", "AU-JEM-H", f"柜高 {v.h:.0f} 超过最高点限值 {std.max_top_height:.0f}mm",
                            v.h, std.max_top_height)
        )

    # 无障碍单元：ATCO 嵌入式下限
    if req.accessible and v.h < std.atco_recessed_h_min:
        issues.append(
            ComplianceIssue("warning", "AU-ATCO-ACC",
                            f"无障碍单元柜高 {v.h:.0f} 低于嵌入式下限 {std.atco_recessed_h_min:.0f}mm",
                            v.h, std.atco_recessed_h_min)
        )

    # WA 水平/垂直表间距
    if v.positions_total > 1:
        if v.meter_spacing_h < std.wa_horizontal_spacing:
            issues.append(
                ComplianceIssue("warning", "AU-WA-H",
                                f"多表位水平间距 {v.meter_spacing_h:.0f} < WA 要求 {std.wa_horizontal_spacing:.0f}mm",
                                v.meter_spacing_h, std.wa_horizontal_spacing)
            )
        # 垂直间距只对**多行**排布有意义。
        # 实测踩坑：初版对所有 positions_total>1 的柜型都查垂直间距，
        # 导致 1x3 / 1x4 横排柜（meter_spacing_v 恒为 0，本就无垂直间距）
        # 全部误报 AU-GEN-V —— 44 户里刷出 40 条无意义警告，
        # 真正该看的警告会被淹没。
        if v.rows > 1 and v.meter_spacing_v < std.min_vertical_spacing:
            issues.append(
                ComplianceIssue("warning", "AU-GEN-V",
                                f"垂直表间距 {v.meter_spacing_v:.0f} 过小，建议 >= {std.min_vertical_spacing:.0f}mm",
                                v.meter_spacing_v, std.min_vertical_spacing)
            )

    return issues


# ---------------------------------------------------------------- 匹配器


class Stage2Matcher:
    """units → 柜型变体匹配。

    匹配策略（按优先级）：
      1. 精确匹配：表位数与排布完全吻合
      2. 容量匹配：容量 >= 需求且无规范错误
      3. 人工指派：返回 None，由人工在 UI 中指定
    """

    def __init__(self, variants: Iterable[Variant], std: AUStandard = DEFAULT_STANDARD) -> None:
        self.variants = list(variants)
        self.std = std

    def _candidates(self, req: MeterRequirement) -> list[tuple[Variant, list[ComplianceIssue]]]:
        out = []
        for v in self.variants:
            if not v.fits(req):
                continue
            issues = check_compliance(v, req, self.std)
            if any(i.severity == "error" for i in issues):
                continue
            out.append((v, issues))
        # 排序决定「同容量有多种排布形式时选哪个」，必须确定性：
        #   1. 容量小的优先（避免过度配置）
        #   2. 同容量优先**非无障碍**变体 —— 普通单元不该被推给-ACC 柜。
        #实测踩坑：曾用「表位间距大者优先」，结果 -ACC 变体（间距 350）
        #      排在普通变体（间距 300）之前，44 户里有 39 户被错配成无障碍柜。
        #      现在改为显式用 is_acc 布尔项排序，语义明确且可测。
        #   3. 同容量同类型取「更扁平」的排布（宽>高）—— 澳洲壁龛多为横向
        #   4. 最后按 code 字典序兜底，保证结果可复现
        out.sort(
            key=lambda t: (
                t[0].positions_total,
                t[0].code.endswith("-ACC"),
                -t[0].cols,
                t[0].code,
            )
        )
        return out

    def match(
        self, unit_label: str, floor_no: int, req: MeterRequirement
    ) -> MatchResult:
        cands = self._candidates(req)
        if not cands:
            return MatchResult(
                unit_label=unit_label,
                floor_no=floor_no,
                variant=None,
                auto_matched=False,
                reason=f"无满足条件的变体：需求 {req.meters}",
                compliance=[],
            )

        need = sum(req.meters.values())

        def acc_flag(item: tuple[Variant, list[ComplianceIssue]]) -> bool:
            return item[0].code.endswith("-ACC")

        # 无障碍单元：优先匹配 -ACC 变体。
        # 依据 A008 单元表 LEVEL 1 的ACCESSIBLE UNIT 标记：
        # 104 /204 / 304 为无障碍单元，必须用加高柜。
        if req.accessible:
            acc = [c for c in cands if acc_flag(c)]
            if acc:
                exact_acc = [c for c in acc if c[0].positions_total == need]
                pool = exact_acc or acc
                best, issues = pool[0]
                return MatchResult(
                    unit_label=unit_label,
                    floor_no=floor_no,
                    variant=best,
                    auto_matched=True,
                    reason=(
                        f"无障碍精确匹配 {best.grid_aspect}"
                        f"（{best.positions_total} 表位 = 需求 {need}，无障碍规格）"
                        if exact_acc
                        else f"无障碍容量匹配：需求 {need} → {best.grid_aspect}"
                        f"（{best.positions_total} 表位）"
                    ),
                    compliance=issues,
                )

        # 精确匹配优先：容量恰等于需求
        exact = [c for c in cands if c[0].positions_total == need]
        if exact:
            best, issues = exact[0]
            reason = f"精确匹配 {best.grid_aspect}（{best.positions_total} 表位 = 需求 {need}）"
        else:
            # 否则取容量最小者（避免过度配置）
            best, issues = cands[0]
            reason = f"容量匹配：需求 {need} 表位 → {best.grid_aspect}（{best.positions_total} 表位）"
        return MatchResult(
            unit_label=unit_label,
            floor_no=floor_no,
            variant=best,
            auto_matched=True,
            reason=reason,
            compliance=issues,
        )
