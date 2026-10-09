"""SQLite 数据模型 —— 对应报告第 09 章。

设计要点
--------
· Building 树：Module C 的建筑/图纸节点
· Drawing + DrawingVersion：文件哈希 + 版本（替代DMS，报告 5.6 裁决）
· Unit：解析产物。label_scheme 显式区分两套编号体系（报告核心发现）
· ParseJob：任务状态显式建模（queued/running/needs_review/confirmed/failed）
· Cupboard / CupboardVariant：柜型库
· Selection：STAGE 2 选型结果
· Correction：修正字典 —— 复利型技术护城河，每次人工修正沉淀为规则
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 枚举


class LabelScheme(str, Enum):
    """单元编号体系。本项目实测确认**三套**体系并存，必须显式区分。

    实测 74KEELER ST：
      · GROUND 层用字母编码 G01~G08（完全不用数字）
      · L1~L4 用楼层编码 101/110/201/...
      · CO-LIVING 集体区用 CLA1/CLA2
    """

    FLOOR_CODED = "floor_encoded"   # 101/102/... 楼层编码（A008 单元表用）
    SEQUENTIAL = "sequential"        # UNIT 8/9/10... 全楼连续序号（平面图用）
    GROUND_CODED = "ground_encoded"  # G01~G08 地面层字母编码（实测新增）
    COMMUNAL = "communal"            # CLA1/CLA2 CO-LIVING 集体居住区


class UnitType(str, Enum):
    """单元类型。实测依据 A008 单元表 MIN DRY AREA 规则：
    SINGLE ROOM 12SQM / DOUBLE ROOM 16SQM。
    """

    SINGLE = "SINGLE"
    DOUBLE = "DOUBLE"
    COMMUNAL = "CO-LIVING"


class AreaSource(str, Enum):
    """面积数据来源 —— 用于溯源与交叉验证。"""

    PLAN_DRY = "plan_dry"            # 平面图 DRY 面积（权威）
    SCHEDULE_PROVIDED = "schedule_provided"  # 单元表 PROVIDED AREA 列
    SCHEDULE_GROSS = "schedule_gross"        # 单元表 GROSS AREA列（含湿区）
    NONE = "none"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class PageRole(str, Enum):
    FLOOR_PLAN = "floor_plan"
    UNIT_SCHEDULE = "unit_schedule"
    OTHER = "other"


# ---------------------------------------------------------------- 建筑树


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    # 树形：父节点为 None 即根
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("buildings.id"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    parent: Mapped["Building | None"] = relationship(remote_side=[id], backref="children")
    drawings: Mapped[list["Drawing"]] = relationship(back_populates="building")


class Drawing(Base):
    __tablename__ = "drawings"

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int | None] = mapped_column(ForeignKey("buildings.id"), nullable=True)
    drawing_no: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    # 原始提取规则：矢量 / 扫描 / 混合
    text_layer: Mapped[str] = mapped_column(String(32), default="unknown")
    # 文件内容哈希，用于去重与变更检测
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    building: Mapped["Building | None"] = relationship(back_populates="drawings")
    versions: Mapped[list["DrawingVersion"]] = relationship(back_populates="drawing")


class DrawingVersion(Base):
    """图纸版本 —— 替代通用 DMS（报告 5.6：版本管理本质是业务实体，4张表即可）。"""

    __tablename__ = "drawing_versions"
    __table_args__ = (UniqueConstraint("drawing_id", "version", name="uq_drawing_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_id: Mapped[int] = mapped_column(ForeignKey("drawings.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    file_path: Mapped[str] = mapped_column(String(1000))
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    change_note: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str] = mapped_column(String(128), default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    drawing: Mapped["Drawing"] = relationship(back_populates="versions")


# ---------------------------------------------------------------- 解析产物


class ParseJob(Base):
    """解析任务。状态显式建模，不靠日志推断（报告 5.5 裁决）。"""

    __tablename__ = "parse_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_version_id: Mapped[int] = mapped_column(ForeignKey("drawing_versions.id"))
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus), default=JobStatus.QUEUED, index=True)
    # 三信号原始证据，完整保留供审计
    multi_signals: Mapped[dict] = mapped_column(JSON, default=dict)
    # 本次使用的规则版本
    config_version: Mapped[str] = mapped_column(String(32), default="1.0")
    # 诊断模式：记录每条规则的命中数
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    # 人工修正记录（未确认前可改）
    corrections: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    drawing_version: Mapped["DrawingVersion"] = relationship()
    units: Mapped[list["Unit"]] = relationship(back_populates="parse_job")
    pages: Mapped[list["PageParse"]] = relationship()


class PageParse(Base):
    """逐页解析结果。"""

    __tablename__ = "page_parses"
    __table_args__ = (UniqueConstraint("parse_job_id", "page_no", name="uq_job_page"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    parse_job_id: Mapped[int] = mapped_column(ForeignKey("parse_jobs.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    drawing_no: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[PageRole] = mapped_column(SAEnum(PageRole), default=PageRole.OTHER)
    floor_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_layer_present: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)


class Unit(Base):
    """单元 —— STAGE 1 的核心产物。"""

    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    parse_job_id: Mapped[int] = mapped_column(ForeignKey("parse_jobs.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    floor_no: Mapped[int] = mapped_column(Integer, index=True)

    # 两套编号体系并存 —— 报告的核心发现
    label_scheme: Mapped[LabelScheme] = mapped_column(SAEnum(LabelScheme), default=LabelScheme.FLOOR_CODED)
    label: Mapped[str] = mapped_column(String(32), index=True)  # 101 / G01 / CLA1
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 楼层内序号 1..11
    floor_code: Mapped[str] = mapped_column(String(8), index=True)    # 如 "L1"/"GROUND"

    # 附加属性
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 湿区面积（DRY/WET 干湿分离的 WET 部分）
    area_wet_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    area_source: Mapped[str] = mapped_column(String(24), default="plan_dry")
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    bedrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_type: Mapped[str | None] = mapped_column(String(64))  # SINGLE/DOUBLE/CO-LIVING

    # CO-LIVING 集体居住区（非独立 unit，但需配计量柜）
    is_communal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # 数据来源（多源交叉验证）：["floor_plan", "unit_schedule", "backfill"]
    sources: Mapped[list] = mapped_column(JSON, default=list)

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_tier: Mapped[Confidence] = mapped_column(SAEnum(Confidence), default=Confidence.MEDIUM)
    # 每个单元的证据：命中规则、坐标、原始文本
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # 是否被人工修正过
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)

    parse_job: Mapped["ParseJob"] = relationship(back_populates="units")

    # 选型结果
    selection: Mapped["Selection | None"] = relationship(back_populates="unit", uselist=False)


# ---------------------------------------------------------------- 柜型库


class Cupboard(Base):
    """柜型（父），如「冷水热水燃气表柜」。"""

    __tablename__ = "cupboards"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # 该柜服务的水/燃气表种类
    meter_types: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    variants: Mapped[list["CupboardVariant"]] = relationship(back_populates="cupboard")


class CupboardVariant(Base):
    """柜型变体 —— STAGE 2 的匹配单元。

    借鉴 BidWright 的装配体建模：排布由 rows×cols 计算而非硬编码。
    """

    __tablename__ = "cupboard_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    cupboard_id: Mapped[int] = mapped_column(ForeignKey("cupboards.id"))
    variant_code: Mapped[str] = mapped_column(String(64), index=True)

    # 几何分解
    layout_rows: Mapped[int] = mapped_column(Integer, default=1)
    layout_cols: Mapped[int] = mapped_column(Integer, default=1)
    positions_total: Mapped[int] = mapped_column(Integer, default=1)
    grid_aspect: Mapped[str | None] = mapped_column(String(16))  # 如 "2x3"

    # 尺寸（mm）—— 净距校验依据
    w: Mapped[float | None] = mapped_column(Float)
    h: Mapped[float | None] = mapped_column(Float)
    d: Mapped[float | None] = mapped_column(Float)
    # 表间距（澳洲规范硬约束）
    meter_spacing_h: Mapped[float | None] = mapped_column(Float)
    meter_spacing_v: Mapped[float | None] = mapped_column(Float)
    bbox: Mapped[dict] = mapped_column(JSON, default=dict)

    description: Mapped[str | None] = mapped_column(Text)
    # 来源：人工录入 / DWG 解析
    source: Mapped[str] = mapped_column(String(32), default="manual")
    # 需求 2：按「含几套 water+gas」分组。
    # 实测柜型库截图里同一个 2 套组合有 2 种排布样式、
    # 同一个 7 套组合有 4 种排布形式 —— 所以分组键必须是
    # meter 组合，排布形式是组内的第二层维度。
    meter_combo: Mapped[str | None] = mapped_column(String(64), index=True)
    meter_counts: Mapped[dict] = mapped_column(JSON, default=dict)

    # ---- 柜型缩略图（需求 2：柜型库可点击查看图纸）----
    # DWG 解析时渲染 block 得到的 JPG 相对路径，相对于 var/thumbs/
    image_path: Mapped[str | None] = mapped_column(String(255))
    # 渲染时用的 dpi —— 用户放大看细节时可据此请求更高分辨率
    image_dpi: Mapped[int] = mapped_column(Integer, default=110)
    # 尺寸来源：measured=DWG 实测 / estimated=POC 占位 / manual=人工录入
    # 必须区分 —— 之前所有尺寸都是 estimated，用户无法判断可信度
    size_source: Mapped[str] = mapped_column(String(16), default="estimated")

    cupboard: Mapped["Cupboard"] = relationship(back_populates="variants")


class Selection(Base):
    """STAGE 2 选型结果。"""

    __tablename__ = "selections"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    variant_id: Mapped[int] = mapped_column(ForeignKey("cupboard_variants.id"))
    # 匹配依据：是否自动匹配 / 人工指定
    auto_matched: Mapped[bool] = mapped_column(Boolean, default=True)
    match_reason: Mapped[str | None] = mapped_column(Text)
    # 计量需求的推导溯源（meter_requirement.requirement_for 的 derivation）。
    # 关键：必须能回答「这个单元为什么需要 3 个表位」，
    # 否则人工无法复核自动选型是否可信。
    requirement_derivation: Mapped[dict] = mapped_column(JSON, default=dict)
    # 澳洲规范校验结果
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    unit: Mapped["Unit"] = relationship(back_populates="selection")
    variant: Mapped["CupboardVariant"] = relationship()


# ---------------------------------------------------------------- 修正字典


class Correction(Base):
    """修正字典 —— 复利型护城河。每次人工修正沉淀为可复用规则。"""

    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    parse_job_id: Mapped[int | None] = mapped_column(ForeignKey("parse_jobs.id"), nullable=True)
    # 修正类型：add_unit / remove_unit / relabel / reassign_floor / fix_variant
    kind: Mapped[str] = mapped_column(String(64))
    target_ref: Mapped[str] = mapped_column(String(128))# 如 "p19:104"
    before_value: Mapped[dict] = mapped_column(JSON, default=dict)
    after_value: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
    # 是否已沉淀为 config 规则
    promoted_to_rule: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(128), default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
