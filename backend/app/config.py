"""外部化可配置解析规则 —— 全部规则来自 config，不硬编码。

设计依据（因DWG 无法上传而必须外置）：
  · 每条规则都有唯一 rule_id，便于人工修正后沉淀
  · 规则改动不需要改代码，重启即生效
  · 每条规则记录命中数，诊断模式可据此发现失效规则
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 注意：本文件为 app/config.py，配置目录为同级的 app/config/
CONFIG_DIR = Path(__file__).resolve().parent / "config"


@dataclass
class UnitLabelRule:
    """单元标签识别规则。"""

    rule_id: str
    description: str
    # 单元编号：楼层编码式101/110/201，或地面层字母式 G01
    code_pattern: str
    # 面积标签：与编号邻近的面积文本
    area_pattern: str
    # 该规则适用的图纸类型
    applies_to: list[str] = field(default_factory=lambda: ["floor_plan"])
    # 配对窗口：编号后多少个词内寻找面积标签
    pair_window: int = 8
    # 序号范围（用于缺号检测）
    seq_min: int = 1
    seq_max: int = 30
    # 楼层前缀隔离方式：
    #   leading_digit  编号首位数字 == 楼层号（L1 的 101/110）
    #   letter_ground  编号形如 G0X，仅 GROUND 层（floor_no == 0）
    #   any_floor      不做楼层隔离（CLA 等）
    floor_prefix_mode: str = "leading_digit"
    # 编号体系标识，写入 Unit.label_scheme
    label_scheme: str = "floor_coded"
    # 已知噪声标签（如电话数字被拆词）
    noise_labels: list[str] = field(default_factory=list)
    # CO-LIVING 集体区标记
    communal_pattern: str | None = None
    communal_description: str | None = None

    compiled_code: re.Pattern = field(init=False, repr=False)
    compiled_area: re.Pattern = field(init=False, repr=False)
    compiled_communal: re.Pattern | None = field(init=False, repr=False, default=None)
    noise_set: frozenset[str] = field(init=False, repr=False, default=frozenset())

    def __post_init__(self) -> None:
        self.compiled_code = re.compile(self.code_pattern)
        self.compiled_area = re.compile(self.area_pattern)
        self.compiled_communal = re.compile(self.communal_pattern) if self.communal_pattern else None
        self.noise_set = frozenset(self.noise_labels)

    def accepts_floor(self, token: str, floor_no: int) -> bool:
        """按floor_prefix_mode 判断该编号是否属于给定楼层。"""
        if self.floor_prefix_mode == "any_floor":
            return True
        if self.floor_prefix_mode == "letter_ground":
            return floor_no == 0
        # leading_digit
        return token[:1] == str(floor_no)

    def seq_of(self, token: str) -> int | None:
        """从编号提取**层内序号**（1 起）。

        关键修正：不能用 `(\\d+)$` 直接抓末尾数字 ——
        实测 '101' 会返回 101，导致序号连续性信号出现 1~101 的假缺口
        （expected=111/ found=11）。正确做法是去掉**楼层前缀**再取序号：
            '101' (L1) -> '01'  -> 1
            '110' (L1) -> '10'  -> 10
            'G03'(GROUND) -> '3'
        """
        body = token
        if self.floor_prefix_mode == "letter_ground":
            m = re.fullmatch(r"[A-Za-z](\d+)", token)
            return int(m.group(1)) if m else None
        if self.floor_prefix_mode == "any_floor":
            m = re.search(r"(\d+)$", token)
            return int(m.group(1)) if m else None
        # leading_digit：首位是楼层号，其余是层内序号
        m = re.fullmatch(r"[1-9](\d{1,2})", token)
        if not m:
            m = re.search(r"(\d+)$", token)
            return int(m.group(1)) if m else None
        return int(m.group(1))

    def is_communal(self, token: str) -> bool:
        if self.compiled_communal is None:
            return False
        return bool(self.compiled_communal.fullmatch(token))


@dataclass
class UnitScheduleRule:
    """UNIT SCHEDULE 单元表解析规则。"""

    rule_id: str
    description: str
    storey_markers: list[str]
    storey_floor_map: dict[str, int]
    code_pattern: str
    area_pattern: str

    compiled_code: re.Pattern = field(init=False, repr=False)
    compiled_area: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.compiled_code = re.compile(self.code_pattern)
        self.compiled_area = re.compile(self.area_pattern)


@dataclass
class FloorDetectionRule:
    """楼层识别规则。"""

    rule_id: str
    description: str
    patterns: list[str]
    # 楼层序号提取：正则需含一个命名组 floor
    floor_group: str = "floor"
    # 仅在页面角色为这些值时生效（空=不限）
    applies_to_roles: list[str] = field(default_factory=list)
    compiled: list[re.Pattern] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.compiled = [re.compile(p) for p in self.patterns]


@dataclass
class DrawingNoRule:
    """图号（Drawing No.）识别规则。"""

    rule_id: str
    description: str
    pattern: str
    compiled: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.compiled = re.compile(self.pattern)


@dataclass
class PageRoleRule:
    """页面角色判定：区分平面图 / 单元表 / 图例页。"""

    rule_id: str
    description: str
    # 命中则判定为 floor_plan
    floor_plan_markers: list[str]
    # 命中则判定为 unit_schedule
    unit_schedule_markers: list[str]
    # 明确的单元表标题（强信号，优先于 marker 计数）
    unit_schedule_title: str = "UNIT SCHEDULE"
    # 这些图号前缀的页面不判为 floor_plan（如 A004 Site 图）
    excluded_drawing_prefixes: list[str] = field(default_factory=list)
    # marker 词命中数阈值（仅在无单元编号时作为兜底判据）
    floor_plan_marker_threshold: int = 6
    # 标题栏区域（图号、图名所在带）y 比例下限
    titleblock_y_min: float = 0.86


@dataclass
class ConfidenceWeights:
    """置信度权重。多信号加权，权重之和自动归一。"""

    w_text: float = 0.60  # 矢量文本层编号（主信号）
    w_seq: float = 0.25   # 序号连续性 / 缺号检测
    w_geo: float = 0.15   # 几何/连通域
    w_schedule: float = 0.40  # 单元表交叉验证命中

    # 分诊阈值
    auto_accept: float = 0.85   # >= 自动通过
    needs_review: float = 0.55  # >= 需人工确认；< 则标记低置信


@dataclass
class ParseConfig:
    unit_rules: list[UnitLabelRule]
    floor_rules: list[FloorDetectionRule]
    drawing_no_rules: list[DrawingNoRule]
    page_role_rules: list[PageRoleRule]
    confidence: ConfidenceWeights
    schedule_rules: list[UnitScheduleRule] = field(default_factory=list)
    version: str = "1.0"

    def rule_stats(self) -> dict[str, int]:
        """所有可调规则的 rule_id 清单，供诊断模式遍历。"""
        out: dict[str, int] = {}
        for r in self.unit_rules:
            out[r.rule_id] = 0
        for r in self.schedule_rules:
            out[r.rule_id] = 0
        for r in self.floor_rules:
            out[r.rule_id] = 0
        for r in self.drawing_no_rules:
            out[r.rule_id] = 0
        for r in self.page_role_rules:
            out[r.rule_id] = 0
        return out

    def unit_rule_for(self, floor_no: int | None) -> UnitLabelRule | None:
        """挑选适用于该楼层的单元标签规则。"""
        if floor_no == 0:
            for r in self.unit_rules:
                if r.floor_prefix_mode == "letter_ground":
                    return r
        for r in self.unit_rules:
            if r.floor_prefix_mode == "leading_digit":
                return r
        return self.unit_rules[0] if self.unit_rules else None

    @property
    def communal_rule(self) -> UnitLabelRule | None:
        for r in self.unit_rules:
            if r.floor_prefix_mode == "any_floor":
                return r
        return None


def _filter_kwargs(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """只保留数据类声明过的字段，丢弃 _ 开头的说明性注释键。"""
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    return {k: v for k, v in raw.items() if k in allowed and not k.startswith("_")}


def load_config(path: Path | None = None) -> ParseConfig:
    path = path or (CONFIG_DIR / "parse_rules.json")
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    known_floor = {f.name for f in FloorDetectionRule.__dataclass_fields__.values()}
    floor_rules = [FloorDetectionRule(**_filter_kwargs(FloorDetectionRule, r)) for r in raw["floor_detection_rules"]]

    known_role = {f.name for f in PageRoleRule.__dataclass_fields__.values()}
    role_rules = [PageRoleRule(**_filter_kwargs(PageRoleRule, r)) for r in raw["page_role_rules"]]

    conf = ConfidenceWeights(**_filter_kwargs(ConfidenceWeights, raw["confidence"]))

    return ParseConfig(
        unit_rules=[UnitLabelRule(**_filter_kwargs(UnitLabelRule, r)) for r in raw["unit_label_rules"]],
        schedule_rules=[
            UnitScheduleRule(**_filter_kwargs(UnitScheduleRule, r))
            for r in raw.get("unit_schedule_rules", [])
        ],
        floor_rules=floor_rules,
        drawing_no_rules=[DrawingNoRule(**_filter_kwargs(DrawingNoRule, r)) for r in raw["drawing_no_rules"]],
        page_role_rules=role_rules,
        confidence=conf,
        version=raw.get("version", "1.0"),
    )