"""FastAPI 服务层 —— 对应需求 1/2/3 的三个模块。

端点组织：
  /api/health                健康检查
  /api/projects              Building 列表（需求 3）
  /api/parse                 上传并解析（建筑图纸 / 柜型DWG）
  /api/file/{job_id}         原图预览（需求 1 中栏「原样显示」）
  /api/jobs/{id}             任务详情（楼层矩阵 / 单元明细 / 溯源）
  /api/jobs/{id}/exports     导出文件路径（PDF/Word/JPG/Excel/JSON）
  /api/units                 单元清单
  /api/cupboards             柜型分组（需求 2 左侧按 meter 组合分组）
  /api/cupboards/from-dwg    DWG 解析结果确认入库
  /api/variants              柜型变体 CRUD
  /api/jobs/{id}/corrections 人工修正字典
  /api/download              导出文件下载
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_config
from app.models.entities import (
    Building,
    Confidence,
    Cupboard,
    CupboardVariant,
    Drawing,
    DrawingVersion,
    JobStatus,
    ParseJob,
    Selection,
    Unit,
)
from app.parsers.dwg import detect_backends, parse_dwg
from app.services.pipeline import Pipeline, file_hash

VAR_DIR = Path(__file__).resolve().parents[3] / "var"
UPLOAD_DIR = VAR_DIR / "uploads"
EXPORT_DIR = VAR_DIR / "export"
DB_PATH = VAR_DIR / "aucup.db"
for d in (UPLOAD_DIR, EXPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AU Cupboards", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

SUFFIXES = (".pdf", ".dwg", ".dxf", ".docx", ".doc", ".jpg", ".jpeg", ".png")


def _pipeline() -> Pipeline:
    return Pipeline(DB_PATH, EXPORT_DIR)


# ---------------------------------------------------------------- 概览


@app.get("/api/health")
def health() -> dict[str, Any]:
    backends = detect_backends()
    return {
        "ok": True,
        "config_version": load_config().version,
        "dwg_backends": backends,
        "dwg_ready": any(backends.values()),
    }


# ---------------------------------------------------------------- 需求 3：Building 管理


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    with Session(_pipeline().engine) as s:
        out = []
        for b in s.scalars(select(Building).order_by(Building.id)):
            drawings = s.scalars(
                select(Drawing).where(Drawing.building_id == b.id).order_by(Drawing.id)
            ).all()
            jobs = s.scalars(
                select(ParseJob).join(DrawingVersion, DrawingVersion.id == ParseJob.drawing_version_id)
                .where(DrawingVersion.drawing_id.in_([d.id for d in drawings] or [0]))
                .order_by(ParseJob.id.desc())
            ).all()
            out.append(
                {
                    "id": b.id,
                    "name": b.name,
                    "address": b.address,
                    "drawings": [
                        {
                            "id": d.id,
                            "drawing_no": d.drawing_no,
                            "title": d.title,
                            "page_count": d.page_count,
                            "text_layer": d.text_layer,
                            "latest_job_id": jobs[0].id if jobs else None,
                        }
                        for d in drawings
                    ],
                }
            )
        return out


@app.post("/api/buildings")
def create_building(name: str = Form(...), address: str | None = Form(None)) -> dict[str, Any]:
    """需求 3：先建 Building 节点，再把图纸挂到节点下（树导航的增项）。"""
    with Session(_pipeline().engine) as s:
        b = s.scalar(select(Building).where(Building.name == name))
        if b is None:
            b = Building(name=name, address=address)
            s.add(b)
            s.commit()
        return {"id": b.id, "name": b.name, "address": b.address}


# ---------------------------------------------------------------- 上传与解析


@app.post("/api/parse")
async def upload_and_parse(
    file: UploadFile = File(...),
    project: str = Form("74 KEELER ST CARLINGFORD"),
    kind: str = Query("building", pattern="^(building|cupboard)$"),
) -> dict[str, Any]:
    suffix = Path(file.filename or "upload.pdf").suffix.lower()
    if suffix not in SUFFIXES:
        raise HTTPException(400, f"不支持的文件类型: {suffix}")

    h = file_hash_of(file)
    safe = Path(file.filename or "f").name.replace("/", "_")
    dest = UPLOAD_DIR / f"{h}-{safe}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    if suffix in (".dwg", ".dxf"):
        res = parse_dwg(dest, work_dir=VAR_DIR / "dwg")
        return {
            "kind": "dwg",
            "ok": res.ok,
            "backend": res.backend.value,
            "layers": res.layers,
            "blocks": res.blocks,
            "entity_counts": res.entity_counts,
            # 柜型库的几何主体在 block 定义里，前端分解预览必须一并拿到
            "block_entity_counts": res.block_entity_counts,
            "block_layers": res.block_layers,
            "total_entities": res.total_entities,
            "integrity": res.integrity,
            "integrity_notes": res.integrity_notes,
            "proxy_entity_lost": res.proxy_entity_lost,
            "warnings": res.warnings,
            "error": res.error,
            "stats": res.stats,
            "source_file": str(dest),
        }

    if suffix != ".pdf":
        # DOCX/JPG 有文本层时同样走 STAGE 1；无文本层则明确告知需 OCR/AI
        return {
            "kind": suffix.lstrip("."),
            "ok": False,
            "error": (
                f"{suffix} 已接收并预览，但尚未接入自动解析器。"
                "当前自动解析支持 PDF（含矢量文本层）；"
                "请上传 PDF 版本，或先手动转 PDF。"
            ),
            "source_file": str(dest),
        }

    pl = _pipeline()
    r = pl.run(dest, project=project)
    with Session(pl.engine) as s:
        b = s.scalar(select(Building).where(Building.name == project))
        bid = b.id if b else None
        rule_desc = _rule_descriptions()

    return {
        "kind": "pdf",
        "job_id": r.job_id,
        "building_id": bid,
        "project": project,
        "summary": r.bundle.summary(),
        "floor_summary": r.bundle.floor_summary,
        "cross_validation": r.bundle.cross_validation,
        "multi_signals": pl.job_multi_signals(r.job_id),
        "diagnostics": r.parse.diagnostics,
        "rule_desc": rule_desc,
        "warnings": r.bundle.warnings,
        "units": _units_payload(pl.engine, r.job_id),
        "pages": [
            {"page_no": p.page_no, "title": p.title, "role": p.role,
             "drawing_no": p.drawing_no, "floor_no": p.floor_no}
            for p in r.parse.pages
        ],
        "outputs": r.out_files,
        "used_ai": r.parse.used_ai,
    }


def file_hash_of(f: UploadFile) -> str:
    import hashlib

    h = hashlib.sha256()
    f.file.seek(0)
    for chunk in iter(lambda: f.file.read(1 << 20), b""):
        h.update(chunk)
    f.file.seek(0)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- 需求 1：预览与导出


@app.get("/api/file/{job_id}")
def raw_file(job_id: int):
    """需求 1 中栏「原样显示 PDF」—— 直接回传原始 PDF 字节。"""
    with Session(_pipeline().engine) as s:
        j = s.get(ParseJob, job_id)
        if not j:
            raise HTTPException(404, "任务不存在")
        v = s.get(DrawingVersion, j.drawing_version_id)
        if not v or not Path(v.file_path).exists():
            raise HTTPException(404, "原始文件缺失")
        return FileResponse(v.file_path, media_type="application/pdf")


@app.get("/api/jobs/{job_id}/exports")
def job_exports(job_id: int) -> dict[str, Any]:
    """返回该任务最近一次导出产物路径，供前端一键下载。"""
    export = EXPORT_DIR
    # 流水线按时间戳命名，这里取最新一组（同 stem 的多个格式）
    groups: dict[str, dict[str, Any]] = {}
    for p in sorted(export.glob("selection-*"), key=lambda x: x.stat().st_mtime):
        if p.suffix.lower() in (".pdf", ".docx", ".xlsx", ".json"):
            groups.setdefault(p.stem, {})[p.suffix.lower().lstrip(".")] = str(p)
        elif p.suffix.lower() in (".jpg", ".jpeg"):
            groups.setdefault(p.stem.split("-p")[0], {}).setdefault("jpg", []).append(str(p))
    if not groups:
        return {}
    def _mtime(v: Any) -> float:
        if isinstance(v, list):
            return max(Path(x).stat().st_mtime for x in v)
        return Path(v).stat().st_mtime

    stem = max(groups, key=lambda k: max(_mtime(v) for v in groups[k].values()))
    latest = dict(groups[stem])
    jp = latest.get("jpg")
    if isinstance(jp, list):
        latest["jpg"] = jp[0]
        latest["jpg_pages"] = jp
    return latest


@app.get("/api/download")
def download(path: str):
    p = Path(path).resolve()
    if not str(p).startswith(str(EXPORT_DIR.resolve())):
        raise HTTPException(403, "仅允许下载导出目录下的文件")
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p))


# ---------------------------------------------------------------- 任务详情


def _rule_descriptions() -> dict[str, str]:
    cfg = load_config()
    out: dict[str, str] = {}
    for r in (
        list(cfg.unit_rules)
        + list(cfg.floor_rules)
        + list(cfg.drawing_no_rules)
        + list(cfg.page_role_rules)
        + list(cfg.schedule_rules)
    ):
        out[r.rule_id] = r.description
    return out


def _units_payload(engine, job_id: int) -> list[dict[str, Any]]:
    with Session(engine) as s:
        units = s.scalars(
            select(Unit).where(Unit.parse_job_id == job_id).order_by(Unit.floor_no, Unit.seq)
        ).all()
        out = []
        for u in units:
            sel = u.selection
            v = sel.variant if sel else None
            out.append(
                {
                    "id": u.id,
                    "floor": u.floor_code,
                    "unit_label": u.label,
                    "seq": u.seq,
                    "area_m2": u.area_m2,
                    "wet_area_m2": u.area_wet_m2,
                    "unit_type": u.unit_type,
                    "is_communal": bool(u.is_communal),
                    "accessible": bool(u.accessible),
                    "label_scheme": u.label_scheme.value if u.label_scheme else None,
                    "confidence": u.confidence,
                    "confidence_tier": u.confidence_tier.value,
                    "sources": u.sources or [],
                    "page": u.page_no,
                    "requirement_derivation": (sel.requirement_derivation if sel else {}) or {},
                    "variant": (
                        {
                            "id": v.id,
                            "code": v.variant_code,
                            "name": f"{v.layout_cols}表位/层 × {v.layout_rows}层",
                            "grid": v.grid_aspect,
                            "rows": v.layout_rows,
                            "cols": v.layout_cols,
                            "positions": v.positions_total,
                            "w": v.w, "h": v.h, "d": v.d,
                            "spacing_h": v.meter_spacing_h,
                            "spacing_v": v.meter_spacing_v,
                            "description": v.description,
                            "reason": sel.match_reason,
                            "compliance": sel.compliance,
                            "auto_matched": sel.auto_matched,
                        }
                        if v
                        else None
                    ),
                    "compliance": (sel.compliance if sel else {}) or {},
                    "evidence": u.evidence,
                }
            )
        return out


@app.get("/api/jobs")
def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with Session(_pipeline().engine) as s:
        jobs = s.scalars(select(ParseJob).order_by(ParseJob.id.desc()).limit(limit)).all()
        return [
            {
                "id": j.id,
                "status": j.status.value,
                "config_version": j.config_version,
                "multi_signals": j.multi_signals,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "error": j.error_message,
            }
            for j in jobs
        ]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    pl = _pipeline()
    with Session(pl.engine) as s:
        j = s.get(ParseJob, job_id)
        if not j:
            raise HTTPException(404, "任务不存在")
        ms = j.multi_signals or {}
        return {
            "id": j.id,
            "status": j.status.value,
            "config_version": j.config_version,
            "multi_signals": ms,
            "summary": _summary_from(ms),
            "floors": ms.get("floor_summary", []),
            "units": _units_payload(pl.engine, job_id),
            "diagnostics": j.diagnostics or {},
            "rule_desc": _rule_descriptions(),
            "cross_validation": ms.get("cross_validation", {}),
            "warnings": ms.get("warnings", []),
        }


def _summary_from(ms: dict[str, Any]) -> dict[str, Any]:
    """从 multi_signals 复原汇总视图。

    注意 total_units 已排除 communal（见 stage3.summary() 口径），
    所以这里不能拿 floor_summary 的 unit_count 直接相加。
    """
    floors = ms.get("floor_summary", [])
    by_floor = {f.get("floor_code"): f.get("unit_count", 0) for f in floors}
    return {
        "total_units": ms.get("total_units", 0),
        "communal_units": ms.get("communal_units", 0),
        "floors": len(floors),
        "by_floor": by_floor,
        "by_variant": ms.get("by_variant", {}),
        "errors": ms.get("compliance_errors", 0),
        "warnings": ms.get("compliance_warnings", 0),
        "avg_confidence": ms.get("avg_confidence", 0),
    }


@app.get("/api/units")
def list_units(job_id: int) -> list[dict[str, Any]]:
    return _units_payload(_pipeline().engine, job_id)


# ---------------------------------------------------------------- 需求 2：柜型库


@app.get("/api/cupboards")
def list_cupboards() -> list[dict[str, Any]]:
    """需求 2：左侧按「含几套 water+gas」分组展示柜型种类。"""
    pl = _pipeline()
    with Session(pl.engine) as s:
        vs = s.scalars(
            select(CupboardVariant).order_by(CupboardVariant.positions_total, CupboardVariant.id)
        ).all()
        groups: dict[str, list[dict[str, Any]]] = {}
        for v in vs:
            key = v.meter_combo or f"{v.positions_total} 表位"
            groups.setdefault(key, []).append(
                {
                    "id": v.id,
                    "code": v.variant_code,
                    "rows": v.layout_rows,
                    "cols": v.layout_cols,
                    "positions": v.positions_total,
                    "grid": v.grid_aspect,
                    "name": f"{v.layout_cols}表位/层 × {v.layout_rows}层",
                    "w": v.w, "h": v.h, "d": v.d,
                    "spacing_h": v.meter_spacing_h,
                    "spacing_v": v.meter_spacing_v,
                    "description": v.description,
                    "source": v.source,
                }
            )
        out = []
        for key, items in groups.items():
            out.append(
                {
                    "key": key,
                    "title": key,
                    "count": len(items),
                    # 需求 2：同一 meter 组合有多种排布形式
                    "layout_forms": sorted({i["grid"] for i in items}),
                    "variants": items,
                }
            )
        return out


@app.get("/api/variants")
def list_variants() -> list[dict[str, Any]]:
    with Session(_pipeline().engine) as s:
        vs = s.scalars(select(CupboardVariant).order_by(CupboardVariant.positions_total)).all()
        return [
            {
                "id": v.id,
                "code": v.variant_code,
                "rows": v.layout_rows,
                "cols": v.layout_cols,
                "positions": v.positions_total,
                "grid": v.grid_aspect,
                "name": f"{v.layout_cols}表位/层 × {v.layout_rows}层",
                "w": v.w, "h": v.h, "d": v.d,
                "spacing_h": v.meter_spacing_h,
                "spacing_v": v.meter_spacing_v,
                "description": v.description,
                "source": v.source,
                "meter_combo": v.meter_combo,
            }
            for v in vs
        ]


class VariantIn(BaseModel):
    code: str
    rows: int = 1
    cols: int = 1
    w: float | None = None
    h: float | None = None
    d: float | None = None
    spacing_h: float | None = None
    spacing_v: float | None = None
    description: str | None = None
    supported_meters: list[str] | None = None


@app.post("/api/variants")
def create_variant(body: VariantIn) -> dict[str, Any]:
    """需求 2：新增柜型排布形式（CRUD 的 Create）。"""
    pl = _pipeline()
    with Session(pl.engine) as s:
        if s.scalar(select(CupboardVariant).where(CupboardVariant.variant_code == body.code)):
            raise HTTPException(409, f"变体 {body.code} 已存在")
        c = s.scalar(select(Cupboard).where(Cupboard.code == "CP-GENERIC"))
        if c is None:
            c = Cupboard(code="CP-GENERIC", name="通用计量柜",
                         description="冷/热水 + 燃气表组合柜",
                         meter_types=["water", "hot_water", "gas"])
            s.add(c)
            s.flush()
        v = CupboardVariant(
            cupboard_id=c.id,
            variant_code=body.code,
            layout_rows=body.rows,
            layout_cols=body.cols,
            positions_total=body.rows * body.cols,
            grid_aspect=f"{body.rows}x{body.cols}",
            w=body.w, h=body.h, d=body.d,
            meter_spacing_h=body.spacing_h,
            meter_spacing_v=body.spacing_v,
            description=body.description,
            source="manual",
            meter_combo=f"{body.rows * body.cols} 套 water+gas",
        )
        s.add(v)
        s.commit()
        return {"id": v.id, "code": v.variant_code}


@app.patch("/api/variants/{variant_id}")
def update_variant(variant_id: int, body: VariantIn) -> dict[str, Any]:
    """需求 2：编辑柜型文字/尺寸（CRUD 的 Update）。"""
    pl = _pipeline()
    with Session(pl.engine) as s:
        v = s.get(CupboardVariant, variant_id)
        if not v:
            raise HTTPException(404, "变体不存在")
        for f in ("description", "w", "h", "d", "spacing_h", "spacing_v", "spacing_v"):
            val = getattr(body, f.replace("spacing_h", "spacing_h"), None)
            if val is not None:
                setattr(v, f if f != "spacing_h" else "meter_spacing_h", val)
                if f == "spacing_v":
                    v.meter_spacing_v = val
        if body.rows:
            v.layout_rows = body.rows
        if body.cols:
            v.layout_cols = body.cols
        v.positions_total = v.layout_rows * v.layout_cols
        v.grid_aspect = f"{v.layout_rows}x{v.layout_cols}"
        if body.code and body.code != v.variant_code:
            if s.scalar(select(CupboardVariant).where(CupboardVariant.variant_code == body.code)):
                raise HTTPException(409, f"变体 {body.code} 已存在")
            v.variant_code = body.code
        s.commit()
        return {"id": v.id, "code": v.variant_code}


@app.delete("/api/variants/{variant_id}")
def delete_variant(variant_id: int) -> dict[str, Any]:
    """需求 2：删除柜型（CRUD 的 Delete）。"""
    pl = _pipeline()
    with Session(pl.engine) as s:
        v = s.get(CupboardVariant, variant_id)
        if not v:
            raise HTTPException(404, "变体不存在")
        used = s.scalar(
            select(Selection).where(Selection.variant_id == variant_id).limit(1)
        )
        if used:
            raise HTTPException(409, "该变体已被单元选用，请先改派后再删除")
        s.delete(v)
        s.commit()
        return {"deleted": variant_id}


class CompareIn(BaseModel):
    ids: list[int]


@app.post("/api/variants/compare")
def compare_variants(body: CompareIn) -> list[dict[str, Any]]:
    """需求 2：柜型对比 —— 逐字段并列，标出差异。"""
    pl = _pipeline()
    with Session(pl.engine) as s:
        vs = s.scalars(select(CupboardVariant).where(CupboardVariant.id.in_(body.ids))).all()
        order = {vid: i for i, vid in enumerate(body.ids)}
        vs = sorted(vs, key=lambda v: order.get(v.id, 0))
        if len(vs) < 2:
            raise HTTPException(400, "至少选择 2 个柜型进行对比")
        fields = ["variant_code", "grid_aspect", "positions_total", "w", "h", "d",
                  "meter_spacing_h", "meter_spacing_v", "meter_combo", "description"]
        out = []
        for f in fields:
            vals = [getattr(v, f) for v in vs]
            out.append(
                {
                    "field": f,
                    "values": vals,
                    "differs": len({str(x) for x in vals}) > 1,
                }
            )
        return out


class DwgCommitIn(BaseModel):
    """需求 2：用户在预览确认后提交入库。"""

    source_file: str
    variants: list[VariantIn] = []
    cupboard_name: str | None = None
    note: str | None = None


@app.post("/api/cupboards/from-dwg")
def commit_from_dwg(body: DwgCommitIn) -> dict[str, Any]:
    """需求 2：分解预览 → 人工确认 → 入库。"""
    pl = _pipeline()
    created = 0
    with Session(pl.engine) as s:
        c = s.scalar(select(Cupboard).where(Cupboard.code == "CP-GENERIC"))
        if c is None:
            c = Cupboard(code="CP-GENERIC", name=body.cupboard_name or "通用计量柜",
                         description=body.note, meter_types=["water", "hot_water", "gas"])
            s.add(c)
            s.flush()
        for v in body.variants:
            if s.scalar(select(CupboardVariant).where(CupboardVariant.variant_code == v.code)):
                continue
            s.add(
                CupboardVariant(
                    cupboard_id=c.id,
                    variant_code=v.code,
                    layout_rows=v.rows,
                    layout_cols=v.cols,
                    positions_total=v.rows * v.cols,
                    grid_aspect=f"{v.rows}x{v.cols}",
                    w=v.w, h=v.h, d=v.d,
                    meter_spacing_h=v.spacing_h,
                    meter_spacing_v=v.spacing_v,
                    description=v.description,
                    source="dwg",
                    meter_combo=f"{v.rows * v.cols} 套 water+gas",
                )
            )
            created += 1
        s.commit()
    return {"created": created, "cupboard_id": c.id}


# ---------------------------------------------------------------- 修正字典


class CorrectionIn(BaseModel):
    kind: str
    target_ref: str
    before_value: dict[str, Any] = {}
    after_value: dict[str, Any]
    note: str | None = None
    created_by: str = "local"


@app.post("/api/jobs/{job_id}/corrections")
def add_correction(job_id: int, body: CorrectionIn) -> dict[str, Any]:
    """人工修正 —— 复利型护城河：每次修正都沉淀为可复用规则。"""
    from app.models.entities import Correction

    with Session(_pipeline().engine) as s:
        if not s.get(ParseJob, job_id):
            raise HTTPException(404, "任务不存在")
        c = Correction(
            parse_job_id=job_id,
            kind=body.kind,
            target_ref=body.target_ref,
            before_value=body.before_value,
            after_value=body.after_value,
            note=body.note,
            created_by=body.created_by,
        )
        s.add(c)
        s.commit()
        return {"id": c.id, "kind": c.kind, "target_ref": c.target_ref}


@app.get("/api/jobs/{job_id}/corrections")
def list_corrections(job_id: int) -> list[dict[str, Any]]:
    from app.models.entities import Correction

    with Session(_pipeline().engine) as s:
        cs = s.scalars(select(Correction).where(Correction.parse_job_id == job_id)).all()
        return [
            {
                "id": c.id,
                "kind": c.kind,
                "target_ref": c.target_ref,
                "before": c.before_value,
                "after": c.after_value,
                "note": c.note,
                "promoted_to_rule": c.promoted_to_rule,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in cs
        ]


# ---------------------------------------------------------------- 静态前端


@app.get("/")
def index() -> FileResponse:
    fe = Path(__file__).resolve().parents[3] / "frontend/index.html"
    if fe.exists():
        return FileResponse(str(fe))
    raise HTTPException(404, "前端未构建")