"""端到端流水线：PDF → STAGE1解析 → STAGE2 选型 → STAGE3 导出。

同时负责落库（SQLite）。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.config import load_config
from app.models.entities import (
    Base,
    Building,
    Confidence,
    Correction,
    Cupboard,
    CupboardVariant,
    Drawing,
    DrawingVersion,
    JobStatus,
    LabelScheme,
    PageParse,
    PageRole,
    ParseJob,
    Selection,
    Unit,
    UnitType,
)
from app.parsers.stage1 import Stage1Parser
from app.services.cupboard_seed import seed_variants
from app.services.meter_requirement import requirement_for
from app.services.stage2 import MeterRequirement, Stage2Matcher, Variant
from app.services.stage3 import ExportBundle, ExportRow


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class PipelineResult:
    job_id: int
    parse: Any
    bundle: ExportBundle
    out_files: dict[str, str]


#: 模型里新增列时同步登记。
#:
#: 为什么需要：``Base.metadata.create_all()`` 只建**新表**，
#: 对已存在的表**不会加列**。老用户的 var/aucup.db 里 cupboard_variants
#: 早就建好了，之后往模型里加字段，create_all 会安静地跳过 ——
#: 结果是代码里读 image_path 报 "no such column"，
#: 而 create_all 明明「成功」了。这类问题极难定位。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (表名, 列名, 列定义)
    ("cupboard_variants", "image_path", "TEXT"),
    ("cupboard_variants", "image_dpi", "INTEGER DEFAULT 110"),
    ("cupboard_variants", "size_source", "TEXT DEFAULT 'estimated'"),
    ("cupboard_variants", "source_name", "TEXT"),
)


def _migrate_add_columns(engine) -> list[str]:
    """给已存在的表补上新增列。幂等 —— 已有列直接跳过。"""
    added: list[str] = []
    insp = inspect(engine)
    try:
        tables = set(insp.get_table_names())
    except Exception:
        return added
    with engine.begin() as conn:
        for table, col, ddl in _ADDED_COLUMNS:
            if table not in tables:
                continue          # 表本身还不存在，create_all 会按模型建全
            try:
                have = {c["name"] for c in insp.get_columns(table)}
            except Exception:
                continue
            if col in have:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                added.append(f"{table}.{col}")
            except Exception:
                continue          # 并发/已存在等，忽略
    return added


class Pipeline:
    def __init__(self, db_path: str | Path, out_dir: str | Path) -> None:
        self.db_path = f"sqlite:///{db_path}"
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(self.db_path, future=True)
        Base.metadata.create_all(self.engine)
        _migrate_add_columns(self.engine)
        self.cfg = load_config()
        self.parser = Stage1Parser(self.cfg)
        self.matcher = Stage2Matcher(seed_variants())

    # ------------------------------------------------------------ 建库

    def ensure_project(self, name: str, address: str | None) -> tuple[int, int]:
        with Session(self.engine) as s:
            b = s.scalar(select(Building).where(Building.name == name))
            if b is None:
                b = Building(name=name, address=address)
                s.add(b)
                s.flush()
            d = s.scalar(select(Drawing).where(Drawing.building_id == b.id))
            if d is None:
                d = Drawing(building_id=b.id, title=name)
                s.add(d)
                s.flush()
            bid, did = b.id, d.id
            s.commit()
        return bid, did

    def ensure_variants(self) -> int:
        """柜型库入库（Module B）。POC 用种子数据。"""
        n = 0
        with Session(self.engine) as s:
            for v in seed_variants():
                code = v.code
                if s.scalar(select(CupboardVariant).where(CupboardVariant.variant_code == code)):
                    continue
                c = s.scalar(select(Cupboard).where(Cupboard.code == "CP-GENERIC"))
                if c is None:
                    c = Cupboard(
                        code="CP-GENERIC",
                        name="通用计量柜",
                        description="冷/热水 + 燃气表组合柜",
                        meter_types=["water", "hot_water", "gas"],
                    )
                    s.add(c)
                    s.flush()
                s.add(
                    CupboardVariant(
                        cupboard_id=c.id,
                        variant_code=code,
                        layout_rows=v.rows,
                        layout_cols=v.cols,
                        positions_total=v.positions_total,
                        grid_aspect=v.grid_aspect,
                        w=v.w, h=v.h, d=v.d,
                        meter_spacing_h=v.meter_spacing_h,
                        meter_spacing_v=v.meter_spacing_v,
                        description=v.description,
                        source="seed_poc",
                        # 需求 2 的分组键：water+gas 组合
                        meter_combo=_meter_combo(v),
                        meter_counts={"positions_total": v.positions_total},
                    )
                )
                n += 1
            s.commit()
        return n

    # ------------------------------------------------------------ 主流程

    def run(
        self,
        pdf_path: str | Path,
        project: str = "74 KEELER ST CARLINGFORD",
        address: str | None = "74 Keeler St, Carlingford NSW",
    ) -> PipelineResult:
        pdf_path = Path(pdf_path)
        bid, did = self.ensure_project(project, address)
        self.ensure_variants()

        # ---- STAGE 1
        pr = self.parser.parse(str(pdf_path))

        with Session(self.engine) as s:
            job = ParseJob(
                drawing_version_id=self._ensure_version(s, did, pdf_path),
                status=JobStatus.RUNNING,
                config_version=self.cfg.version,
                multi_signals={
                    "total_units": pr.total_units,
                    "communal_units": pr.total_communal,
                    "used_ai": pr.used_ai,
                    "pages_with_text": sum(1 for p in pr.pages if p.text_layer_present),
                    "floor_summary": pr.floor_summary(),
                    "cross_validation": pr.cross_validation,
                    "raw_units_before_dedup": sum(len(p.units) for p in pr.pages),
                },
                diagnostics=pr.diagnostics,
            )
            s.add(job)
            s.flush()
            job_id = job.id

            for page in pr.pages:
                s.add(
                    PageParse(
                        parse_job_id=job.id,
                        page_no=page.page_no,
                        drawing_no=page.drawing_no,
                        title=page.title,
                        role=PageRole(page.role),
                        floor_no=page.floor_no,
                        text_layer_present=page.text_layer_present,
                        unit_count=len(page.units),
                        confidence=page.confidence,
                        signals=_jsonable(page.signals),
                    )
                )

            # 落库单元：**必须去重**。实测同一张 GROUND 平面图会在
            # A010/A011/A014/A102 等多个图号下重复出现，逐页累加会
            # 得到 GROUND=77 户的荒谬结果。
            deduped = pr.deduped_units()
            for u in deduped:
                s.add(
                    Unit(
                        parse_job_id=job.id,
                        page_no=u.evidence.get("page", 0),
                        floor_no=u.floor_no,
                        label_scheme=_label_scheme(u.label_scheme),
                        label=u.label,
                        seq=u.seq,
                        floor_code=u.floor_code,
                        area_m2=u.area_m2,
                        area_wet_m2=u.evidence.get("wet_area_m2"),
                        area_source="plan_dry" if "floor_plan" in u.sources else "schedule_provided",
                        accessible=u.accessible,
                        is_communal=u.is_communal,
                        unit_type=_unit_type(u),
                        sources=list(u.sources),
                        confidence=u.confidence,
                        confidence_tier=Confidence(u.confidence_tier),
                        evidence=_jsonable(u.evidence),
                    )
                )
            s.commit()
            s2 = s.get(Drawing, did)
            if s2:
                s2.page_count = pr.page_count
                s2.title = project
                s2.drawing_no = next(
                    (p.drawing_no for p in pr.pages if p.role == "floor_plan" and p.drawing_no),
                    None,
                )
            s.commit()
        bundle_rows, compliance_summary = self._match_and_persist(job_id)

        # ---- 组装导出
        gen = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        bundle = ExportBundle(
            project=project,
            generated_at=gen,
            rows=bundle_rows,
            diagnostics=pr.diagnostics,
            warnings=pr.warnings + compliance_summary,
            floor_summary=pr.floor_summary(),
            cross_validation=pr.cross_validation,
        )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = {
            "json": str(self.out_dir / f"selection-{stamp}.json"),
            "excel": str(self.out_dir / f"selection-{stamp}.xlsx"),
            "pdf": str(self.out_dir / f"selection-{stamp}.pdf"),
            "word": str(self.out_dir / f"selection-{stamp}.docx"),
            "jpg": [],
        }
        from app.services.stage3 import (
            export_excel,
            export_jpg,
            export_json,
            export_pdf,
            export_word,
        )

        export_json(bundle, out["json"])
        # 需求 1 要求三种导出格式：PDF / Word / JPG。
        # Excel/JSON 作为机读中间格式一并产出。任何一种失败都不应中断其它格式。
        for key, fn, label in (
            ("excel", export_excel, "Excel"),
            ("pdf", export_pdf, "PDF"),
            ("word", export_word, "Word"),
        ):
            try:
                fn(bundle, out[key])
            except Exception as exc:  # 缺依赖/字体缺失不应中断流水线
                out[key] = ""
                bundle.warnings.append(f"{label} 导出失败: {exc}")
        try:
            # 必须显式带 .jpg 后缀 —— export_jpg 用 p.suffix 派生分页文件名，
            # 若传入无后缀路径会生成 "selection-xxx" 这种无法打开的文件。
            out["jpg"] = [
                str(p) for p in export_jpg(bundle, self.out_dir / f"selection-{stamp}.jpg")
            ]
        except Exception as exc:
            bundle.warnings.append(f"JPG 导出失败: {exc}")

        # ---- 更新任务状态（显式建模，不靠日志推断）
        with Session(self.engine) as s:
            job = s.get(ParseJob, job_id)
            tiers = [u.confidence_tier for u in s.scalars(
                select(Unit).where(Unit.parse_job_id == job_id))]
            has_review = any(t != Confidence.HIGH for t in tiers)
            job.status = JobStatus.NEEDS_REVIEW if has_review else JobStatus.CONFIRMED
            job.finished_at = datetime.now(timezone.utc)
            job.multi_signals = {
                **(job.multi_signals or {}),
                "tier_counts": {
                    "high": tiers.count(Confidence.HIGH),
                    "medium": tiers.count(Confidence.MEDIUM),
                    "low": tiers.count(Confidence.LOW),
                },
                # 选型结果侧的统计：让API / 前端无需重算
                "by_variant": bundle.summary()["by_variant"],
                "by_unit_type": bundle.summary()["by_unit_type"],
                "total_spaces": bundle.summary()["total_spaces"],
                "compliance_errors": bundle.summary()["errors"],
                "compliance_warnings": bundle.summary()["warnings"],
                "avg_confidence": bundle.summary()["avg_confidence"],
                "warnings": bundle.warnings,
                "outputs": out,
                # 页面清单：供前端刷新后恢复页码列表与标题栏
                "page_count": pr.page_count,
                "pages": [
                    {
                        "page_no": p.page_no,
                        "title": p.title,
                        "role": p.role,
                        "drawing_no": p.drawing_no,
                        "floor_no": p.floor_no,
                        "unit_count": len(p.units),
                    }
                    for p in pr.pages
                ],
            }
            s.commit()

        return PipelineResult(job_id=job_id, parse=pr, bundle=bundle, out_files=out)

    # ------------------------------------------------------------ 辅助

    def job_multi_signals(self, job_id: int) -> dict[str, Any]:
        """读取任务的 multi_signals（含 warnings），供 API 直接返回。"""
        with Session(self.engine) as s:
            j = s.get(ParseJob, job_id)
            return dict(j.multi_signals or {}) if j else {}

    def _ensure_version(self, s: Session, drawing_id: int, pdf_path: Path) -> int:
        h = file_hash(pdf_path)
        v = s.scalar(
            select(DrawingVersion).where(
                DrawingVersion.drawing_id == drawing_id, DrawingVersion.file_hash == h
            )
        )
        if v is None:
            last = s.scalar(
                select(DrawingVersion)
                .where(DrawingVersion.drawing_id == drawing_id)
                .order_by(DrawingVersion.version.desc())
            )
            v = DrawingVersion(
                drawing_id=drawing_id,
                version=(last.version + 1) if last else 1,
                file_path=str(pdf_path),
                file_size=pdf_path.stat().st_size,
                file_hash=h,
                change_note="POC 导入",
            )
            s.add(v)
            s.flush()
            d = s.get(Drawing, drawing_id)
            if d:
                d.file_hash = h
                d.text_layer = "vector"
        return v.id

    def _match_and_persist(self, job_id: int) -> tuple[list[ExportRow], list[str]]:
        rows: list[ExportRow] = []
        warns: list[str] = []
        with Session(self.engine) as s:
            units = list(s.scalars(select(Unit).where(Unit.parse_job_id == job_id).order_by(Unit.floor_no, Unit.seq)))

            for u in units:
                # 表位需求由单元表实测的 WET 面积分档推导，
                # 不再对所有单元硬编码同一套表位（那样会让 44 户
                # 全部匹配到同一个柜型，违反需求 1「同层对应多种 cupboard」）。
                spec = requirement_for(
                    unit_label=u.label,
                    wet_m2=u.area_wet_m2,
                    accessible=bool(u.accessible),
                    is_communal=bool(u.is_communal),
                    unit_type=u.unit_type,
                )
                req = MeterRequirement(
                    meters=spec["meters"],
                    accessible=bool(spec["accessible"]),
                    centralised=bool(spec["centralised"]),
                )
                mr = self.matcher.match(u.label, u.floor_no, req)

                # 落库Selection
                if mr.variant is not None:
                    dbv = s.scalar(
                        select(CupboardVariant).where(CupboardVariant.variant_code == mr.variant.code)
                    )
                    if dbv is not None:
                        s.add(
                            Selection(
                                unit_id=u.id,
                                variant_id=dbv.id,
                                auto_matched=mr.auto_matched,
                                match_reason=mr.reason,
                                requirement_derivation=_jsonable(spec["derivation"]),
                                compliance={
                                    "issues": [
                                        {"severity": i.severity, "code": i.code, "message": i.message}
                                        for i in mr.compliance
                                    ]
                                },
                            )
                        )

                if not mr.ok and mr.variant is None:
                    warns.append(f"单元 {u.label}(L{u.floor_no}): 未匹配到柜型 —— {mr.reason}")

                sev = ""
                if mr.compliance:
                    top = mr.compliance[0]
                    sev = ("ERROR: " if top.severity == "error" else "WARN: ") + top.code

                rows.append(
                    ExportRow(
                        floor=u.floor_code,
                        unit_label=u.label,
                        area_m2=u.area_m2,
                        variant_code=mr.variant.code if mr.variant else None,
                        grid=mr.variant.grid_aspect if mr.variant else None,
                        w=mr.variant.w if mr.variant else None,
                        h=mr.variant.h if mr.variant else None,
                        d=mr.variant.d if mr.variant else None,
                        positions=mr.variant.positions_total if mr.variant else None,
                        compliance=sev,
                        confidence=u.confidence,
                        confidence_tier=u.confidence_tier.value,
                        evidence_page=u.evidence.get("page"),
                        corrected=u.corrected,
                        is_communal=bool(getattr(u, "is_communal", False)),
                        unit_type=u.unit_type,
                        accessible=bool(u.accessible),
                        variant_name=mr.variant.name if mr.variant else None,
                        variant_description=mr.variant.description if mr.variant else None,
                        layout_form=(f"{mr.variant.rows}x{mr.variant.cols}" if mr.variant else None),
                        sources=list(getattr(u, "sources", []) or []),
                        requirement_derivation=_jsonable(spec["derivation"]),
                        wet_area_m2=u.area_wet_m2,
                    )
                )
            s.commit()
        return rows, warns


def _meter_combo(v: Any) -> str:
    """柜型的meter 组合键 —— 需求 2「左侧按 water+gas 数量分组」。

    柜型库截图实测：同一个 2 套 water+gas 有2 种排布样式，
    同一个 7 套有 4 种排布形式。故分组键用**表位总数**，
    排布形式（rows×cols）作为组内第二层维度。
    """
    n = v.positions_total
    if n <= 2:
        return "1套 water+gas"
    return f"{n} 套 water+gas"


def _label_scheme(scheme: str) -> LabelScheme:
    """解析器的 label_scheme 字符串 → 数据库枚举。

    实测三套体系：floor_coded(101) / ground_coded(G01) / communal(CLA1)
    """
    return {
        "floor_coded": LabelScheme.FLOOR_CODED,
        "floor_encoded": LabelScheme.FLOOR_CODED,
        "sequential": LabelScheme.SEQUENTIAL,
        "ground_coded": LabelScheme.GROUND_CODED,
        "ground_encoded": LabelScheme.GROUND_CODED,
        "communal": LabelScheme.COMMUNAL,
    }.get(scheme, LabelScheme.FLOOR_CODED)


def _unit_type(u: Any) -> str | None:
    """按 A008 单元表规则判定单元类型。

    实测规则：MIN DRY AREA: SINGLE ROOM 12SQM, DOUBLE ROOM 16SQM
    → DRY 面积 < 16m2 判为 SINGLE，≥ 16m2 判为 DOUBLE。
    这条规则让封面「43 DOUBLE + 1 SINGLE」的申报可被自动验证。
    """
    if u.is_communal:
        return UnitType.COMMUNAL.value
    if u.area_m2 is None:
        return None
    return UnitType.SINGLE.value if u.area_m2 < 16.0 else UnitType.DOUBLE.value


def _jsonable(d: Any, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """把嵌套 dict 中的 numpy 类型转成原生类型，便于 JSON 序列化。

    实测踩坑：解析器的 signals 里存在自引用结构（area 信号回指自身），
    无限递归会直接 RecursionError 崩掉整个落库流程。故加三重保护：
    深度上限 + 循环引用检测 + 未知对象降级为字符串。
    """
    import numpy as np

    if _depth > 12:
        return "<max-depth>"
    if _seen is None:
        _seen = set()

    # 不可哈希对象（如 list）不进 _seen；只对容器做 id 去重
    if isinstance(d, (dict, list)):
        if id(d) in _seen:
            return "<circular>"
        _seen = {*_seen, id(d)}

    if isinstance(d, dict):
        return {str(k): _jsonable(v, _depth + 1, _seen) for k, v in d.items()}
    if isinstance(d, (list, tuple, set)):
        return [_jsonable(v, _depth + 1, _seen) for v in d]
    if isinstance(d, (str, int, float, bool)) or d is None:
        return d
    if isinstance(d, (np.integer,)):
        return int(d)
    if isinstance(d, (np.floating,)):
        return float(d)
    if isinstance(d, (np.bool_,)):
        return bool(d)
    if isinstance(d, (set, frozenset)):
        return sorted(str(x) for x in d)
    return str(d)
