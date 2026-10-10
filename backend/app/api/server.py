"""FastAPI 服务层 —— 对应需求 1/2/3 的三个模块。

端点组织：
  /api/health                健康检查
  /api/projects              Building 列表（需求 3）
  /api/parse                 上传并解析（建筑图纸 / 柜型DWG）
  /api/file/{job_id}         原图下载（整份 PDF）
  /api/page/{job_id}/{n}     单页渲染成PNG —— 大图纸翻页走这个，别用整份
  /api/jobs/{id}             任务详情（楼层矩阵 / 单元明细 / 溯源）
  /api/jobs/{id}/exports     导出文件路径（PDF/Word/JPG/Excel/JSON）
  /api/units                 单元清单
  /api/cupboards             柜型分组（需求 2 左侧按 meter 组合分组）
  /api/cupboards/from-dwg    DWG 解析结果确认入库
  /api/cupboards/render      DWG/DXF → 每个柜型 block 渲染成 JPG
  /api/thumbs/{name}柜型缩略图（柜型库点击查看）
  /api/variants              柜型变体 CRUD
  /api/jobs/{id}/corrections 人工修正字典
  /api/download              导出文件下载
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
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
from app.parsers.cupboard_render import (
    DEFAULT_WIDTH as THUMB_WIDTH,
)
from app.parsers.cupboard_render import (
    THUMB_DIR,
    render_cupboard_library,
    render_cupboard_regions,
)
from app.parsers.dwg import detect_backends, parse_dwg
from app.services.pipeline import Pipeline, file_hash

VAR_DIR = Path(__file__).resolve().parents[3] / "var"
UPLOAD_DIR = VAR_DIR / "uploads"
EXPORT_DIR = VAR_DIR / "export"
THUMB_DIR_PATH = VAR_DIR / THUMB_DIR
THUMB_DIR_PATH.mkdir(parents=True, exist_ok=True)
DB_PATH = VAR_DIR / "aucup.db"
for d in (UPLOAD_DIR, EXPORT_DIR, THUMB_DIR_PATH):
    d.mkdir(parents=True, exist_ok=True)

#: 缩略图渲染 dpi —— 与入库时记录的 image_dpi 保持一致
DEFAULT_THUMB_DPI = 110

#: **API 契约版本**。前端 index.html 里的 S.apiver 必须与这里一致。
#:
#: 存在的意义：前端是单文件 HTML，浏览器很容易缓存住旧版本，而用户
#: 往往只记得「重启服务」，不记得强刷。于是出现最难查的一类故障：
#:   旧 JS 提交旧字段→ 新后端反序列化失败 → 500，但界面完全看不出原因。
#: 实测踩坑：用户 pull 到新版 + 重启后端，仍报「入库失败 500」，
#:   根因就是浏览器还在跑旧 index.html。
#: 改成显式版本号后，前端启动时能主动比对并给出「请强制刷新」，
#: 而不是让用户对着 500 猜。
API_VERSION = "2"

app = FastAPI(title="AU Cupboards", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

SUFFIXES = (".pdf", ".dwg", ".dxf", ".docx", ".doc", ".jpg", ".jpeg", ".png")

log = logging.getLogger(__name__)


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):  # noqa: ANN001
    """兜底异常处理：把500 变成能读懂的话。

    没有这个处理器时，FastAPI 只回`Internal Server Error`，
    前端 toast 里就是干巴巴一句「入库失败 500」—— 用户完全无从下手。

    实测踩坑：旧版前端提交旧字段给新版后端，反序列化直接抛
    AttributeError，用户看到的是 500 +「点击没反应」。
    带上异常类型和位置后，这类问题一眼可辨。
    """
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    detail = (
        f"服务端内部错误：{type(exc).__name__}: {exc}\n"
        f"位置：{request.method} {request.url.path}\n"
        "若近期刚 git pull，请强制刷新浏览器（Cmd+Shift+R）"
        "清掉缓存的旧页面再试。"
    )
    return JSONResponse(status_code=500, content={"detail": detail})


def _pipeline() -> Pipeline:
    return Pipeline(DB_PATH, EXPORT_DIR)


def _to_dxf(src: Path) -> tuple[Path | None, str | None]:
    """把 DWG 转成 DXF（ODA 优先 → LibreDWG 兜底），已是 DXF 则原样返回。

    抽出来是因为 /api/parse 与 /api/cupboards/render 都要走这一步，
    两处各写一遍迟早会走偏（选型结论一变就要改两处）。
    """
    if src.suffix.lower() == ".dxf":
        return src, None
    from app.parsers.dwg import convert_with_libredwg, convert_with_oda

    work = VAR_DIR / "render_work"
    work.mkdir(parents=True, exist_ok=True)
    dxf, err = convert_with_oda(src, work)
    if dxf is None:
        dxf, err = convert_with_libredwg(src, work)
    return dxf, err


# ---------------------------------------------------------------- 概览


@app.get("/api/health")
def health() -> dict[str, Any]:
    backends = detect_backends()
    ready = any(backends.values())
    out: dict[str, Any] = {
        "ok": True,
        "config_version": load_config().version,
        "api_version": API_VERSION,
        "dwg_backends": backends,
        "dwg_ready": ready,
    }
    # DWG 不可用时给出**可执行**的下一步 —— 只说「未安装后端」用户无从下手
    if not ready:
        out["dwg_hint"] = (
                "DWG 解析后端缺失。PDF 解析不受影响，仅 DWG 上传不可用。\n"
                "推荐（macOS/Windows/Linux 通用，不需要 Homebrew）：\n"
                "  bash scripts/install_dwg_backend.sh\n"
                "它会自动检测已装的 ODA，没有则从官方源码编译 LibreDWG\n"
                "到 ~/.local/bin，并提示如何加入 PATH。\n\n"
                "或者手动装 ODA File Converter（免费注册下载）：\n"
                "  https://www.opendesign.com/guestfiles/oda_file_converter\n"
                "装完无需改 PATH —— macOS 的 .app 路径本服务会自动探测。\n\n"
                "装完重启服务，顶栏徽章会变成「DWG 就绪」。"
            )
    return out


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
    # 幂等：若客户端把上次的 stored_name（hash-xxx.dwg）又传了回来，
    # 去掉已存在的前缀，避免变成 hash-hash-xxx.dwg 无限增长。
    if safe.startswith(f"{h}-"):
        safe = safe[len(h) + 1:]
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
            # 只回文件名，不回绝对路径 ——
            # 前端后续调 /api/cupboards/render 只需要这个，
            # 泄露服务器目录结构没有任何好处。
            "stored_name": dest.name,
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
    """需求 1 中栏「原样显示 PDF」—— 直接回传原始 PDF 字节。

    注意：前端**不再**用它翻页。大图纸（实测 37MB/35 页）用
    `iframe#page=N` 换页等于重新下载整份 PDF，会把浏览器卡死。
    翻页请走 /api/page/{job_id}/{n} 单页渲染。
    """
    with Session(_pipeline().engine) as s:
        j = s.get(ParseJob, job_id)
        if not j:
            raise HTTPException(404, "任务不存在")
        v = s.get(DrawingVersion, j.drawing_version_id)
        if not v or not Path(v.file_path).exists():
            raise HTTPException(404, "原始文件缺失")
        return FileResponse(v.file_path, media_type="application/pdf")


# 单页渲染缓存：job_id -> (mtime, dpi) -> {page: bytes}
# 翻页会反复请求同一页，不缓存等于每次都重新栅格化整页
_PAGE_CACHE: dict[int, tuple[float, int, dict[int, bytes]]] = {}
_PAGE_CACHE_MAX = 400  # 约 35 页 × 若干dpi，超了整批丢弃


@app.get("/api/page/{job_id}/{page}")
def page_image(job_id: int, page: int, dpi: int = Query(110, ge=40, le=300)):
    """把 PDF 的**单页**渲染成 PNG。

    为什么必须有这个接口：
    用 `<iframe src="整份.pdf#page=N">` 翻页时，浏览器会重新拉取
    整份 PDF。实测 37MB / 35 页的图纸每翻一页就是一次 37MB 下载，
    页面直接卡死、点页码无响应。

    改成服务端按需渲染单页后，每次翻页只传一张位图（约 100-400KB），
    缩放交给前端 CSS，翻页是瞬时的。
    """
    import fitz  # PyMuPDF

    with Session(_pipeline().engine) as s:
        j = s.get(ParseJob, job_id)
        if not j:
            raise HTTPException(404, "任务不存在")
        v = s.get(DrawingVersion, j.drawing_version_id)
        if not v or not Path(v.file_path).exists():
            raise HTTPException(404, "原始文件缺失")
        src = Path(v.file_path)

    mtime = src.stat().st_mtime
    cached = _PAGE_CACHE.get(job_id)
    if not cached or cached[0] != mtime or cached[1] != dpi:
        if len(_PAGE_CACHE) >= _PAGE_CACHE_MAX // max(1, dpi // 10):
            _PAGE_CACHE.clear()
        cached = (mtime, dpi, {})
        _PAGE_CACHE[job_id] = cached

    pages = cached[2]
    if page in pages:
        return Response(pages[page], media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    try:
        doc = fitz.open(src)
    except Exception as e:  # 损坏文件不应让整个预览崩掉
        raise HTTPException(422, f"无法打开 PDF: {e}") from e
    try:
        if page < 1 or page > doc.page_count:
            raise HTTPException(404, f"页码越界（共 {doc.page_count} 页）")
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        data = pix.tobytes("png")
        # 内存保护：超大页只缓存小图，避免 35 页高DPI 撑爆内存
        if len(data) < 4_000_000:
            pages[page] = data
        return Response(data, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    finally:
        doc.close()


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
                    "image_path": v.image_path,
                    "image_url": (f"/api/thumbs/{Path(v.image_path).name}"
                                  if v.image_path else None),
                    "size_source": v.size_source,
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
                # 用户自定义显示名（PATCH /api/variants/{id}/name 写入
                # meter_counts['label']）。没有就用下面的默认描述。
                # 前端树与标题优先取这个，所以改名刷新后还在。
                "label": (v.meter_counts or {}).get("label"),
                "name": f"{v.layout_cols}表位/层 × {v.layout_rows}层",
                "w": v.w, "h": v.h, "d": v.d,
                "spacing_h": v.meter_spacing_h,
                "spacing_v": v.meter_spacing_v,
                "description": v.description,
                "source": v.source,
                "meter_combo": v.meter_combo,
                # 来源 DWG 名—— 左树根节点用它分组（显示名可改，
                # 改名只动前端 sessionStorage，不改这里的值）
                "source_name": v.source_name,
                # 柜型缩略图（需求 2：柜型库可点击查看）
                # image_url 是给 <img src> 直接用的完整路径，
                # image_path 保留库里的相对值便于排查
                "image_path": v.image_path,
                "image_url": (f"/api/thumbs/{Path(v.image_path).name}"
                              if v.image_path else None),
                "image_dpi": v.image_dpi,
                "size_source": v.size_source,
            }
            for v in vs
        ]


# ---------------------------------------------------------------- 删除


def _delete_variant_row(s: Session, v: CupboardVariant) -> None:
    """删变体并顺手清掉它的缩略图文件。

    只删库不删文件的话，var/thumbs/ 会越堆越大；反过来只删文件
    不删库则会留下 404 图片。两者必须一起做。
    """
    if v.image_path:
        f = (VAR_DIR / v.image_path).resolve()
        if str(f).startswith(str(THUMB_DIR_PATH.resolve())) and f.is_file():
            try:
                f.unlink()
            except OSError as exc:
                log.warning("缩略图删除失败 %s: %s", f, exc)
    s.delete(v)


def _assert_not_in_use(s: Session, vid: int) -> None:
    """已被单户选用的柜型不许直接删 —— 保护已生成的清单。

    这是既有业务规则（原 test_variant_in_use_cannot_be_deleted 守着）：
    柜型一旦被 unit 引用（经由 selections.variant_id），删掉会让历史
    排布清单出现悬空引用，导出的表格也对不上。所以返回 409 而不是硬删。

    注意关联字段在 ``selections.variant_id``，Unit 上并没有
    ``variant_id`` 列（那是 relationship 的名字，不是数据库字段）。
    """
    n = s.scalar(select(func.count()).where(Selection.variant_id == vid))
    if n:
        raise HTTPException(
            409,
            f"该柜型已被 {n} 个单元选用，不能直接删除。"
            "请先到工作台把相关单元改选其它柜型。",
        )


@app.delete("/api/variants/{variant_id}")
def delete_variant(variant_id: int) -> dict[str, Any]:
    """删除单个柜型。用户诉求：「里面不需要的数据，我需要可以删除」。"""
    with Session(_pipeline().engine) as s:
        v = s.get(CupboardVariant, variant_id)
        if v is None:
            raise HTTPException(404, f"柜型 {variant_id} 不存在")
        _assert_not_in_use(s, variant_id)
        code = v.variant_code
        _delete_variant_row(s, v)
        s.commit()
    return {"deleted": 1, "code": code}


class DeleteBody(BaseModel):
    ids: list[int] = []


@app.post("/api/variants/delete")
def delete_variants(body: DeleteBody) -> dict[str, Any]:
    """批量删除。逐条跳过不存在的，避免用户重复点击时整批失败。

    被单元选用的柜型**跳过而非报错**（返回 ``in_use``）——
    批量操作里一个受保护的条目不该让其余白删。
    """
    if not body.ids:
        return {"deleted": 0, "missing": [], "in_use": []}
    deleted, missing, in_use = 0, [], []
    with Session(_pipeline().engine) as s:
        for vid in body.ids:
            v = s.get(CupboardVariant, vid)
            if v is None:
                missing.append(vid)
                continue
            try:
                _assert_not_in_use(s, vid)
            except HTTPException:
                in_use.append(vid)
                continue
            _delete_variant_row(s, v)
            deleted += 1
        s.commit()
    return {"deleted": deleted, "missing": missing, "in_use": in_use}


class DeleteByCodeBody(BaseModel):
    """按 block / 变体编码删除 —— 用户在图纸里看到某个柜型不想要时，
    比先查 id 再删顺手得多。"""
    codes: list[str] = []


@app.post("/api/variants/delete-by-code")
def delete_variants_by_code(body: DeleteByCodeBody) -> dict[str, Any]:
    deleted, missing = 0, []
    with Session(_pipeline().engine) as s:
        for code in body.codes:
            rows = s.scalars(
                select(CupboardVariant).where(CupboardVariant.variant_code == code)
            ).all()
            if not rows:
                missing.append(code)
                continue
            for r in rows:
                _delete_variant_row(s, r)
                deleted += 1
        s.commit()
    return {"deleted": deleted, "missing": missing}


class ClearVariantsBody(BaseModel):
    #: 只清 source='dwg' 的（自动解析入库的），保留手工录入的
    only_dwg: bool = True


@app.post("/api/variants/clear")
def clear_variants(body: ClearVariantsBody) -> dict[str, Any]:
    """清空柜型库（重新上传前用）。

    ``only_dwg=True``（默认）只清 DWG 解析进来的，种子数据与手工
    录入的保留 —— 用户通常是「这份图纸认错了，重传」而不是
    「整个库都不要了」。

    被单元选用的柜型会跳过并计入 ``kept`` —— 清空库不等于让
    已生成的排布清单出现悬空引用。
    """
    kept = 0
    with Session(_pipeline().engine) as s:
        q = select(CupboardVariant)
        if body.only_dwg:
            q = q.where(CupboardVariant.source == "dwg")
        rows = s.scalars(q).all()
        n = 0
        for r in rows:
            try:
                _assert_not_in_use(s, r.id)
            except HTTPException:
                kept += 1
                continue
            _delete_variant_row(s, r)
            n += 1
        s.commit()
    return {"deleted": n, "kept": kept, "only_dwg": body.only_dwg}


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
    #: 柜型分组键，如「2 套 water+gas」。需求 2：同一个组合可能有多种排布形式，
    #: 所以分组靠 meter_combo，排布形式是组内第二层维度。
    meter_combo: str | None = None
    #: 对应 DWG 里的 block 名 —— 用于把渲染图关联到变体
    block_name: str | None = None
    #: **套数（units）** —— 一套 = 1 个 water + 1 个 gas。
    #: 必须由解析层给出真实值；不能用 rows×cols代替（那是网格容量）。
    positions: int | None = None
    #: 排布字符串，如 "5x4"（5 列 × 4 行）
    grid: str | None = None


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


class VariantRenameIn(BaseModel):
    """只改显示名 —— 柜型改名。

    为什么不复用上面的 ``PATCH /api/variants/{id}``：
    那个端点会用 ``rows × cols`` 覆写 ``positions_total``，而**网格容量
    不是套数**（真实样本里13 Units 的柜网格是 5×4=20，只填了 13 套）。
    走它改名会顺手把套数改错，所以单开一个只碰名字的端点。
    """
    name: str


@app.patch("/api/variants/{variant_id}/name")
def rename_variant(variant_id: int, body: VariantRenameIn) -> dict[str, Any]:
    """柜型改名。用户诉求：「有编辑的功能，可以删除元素，或者重命名」。

    存进 ``meter_counts['label']``：``CupboardVariant`` 没有单独的
    显示名字段，而 ``meter_combo`` 同时被前端当分组键用，改它会把
    柜型从树里挪走。所以借用 ``meter_counts`` 里这个不参与逻辑的键。
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    with Session(_pipeline().engine) as s:
        v = s.get(CupboardVariant, variant_id)
        if not v:
            raise HTTPException(404, "变体不存在")
        mc = dict(v.meter_counts or {})
        mc["label"] = name
        v.meter_counts = mc
        s.commit()
        return {"id": v.id, "name": name}


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


class RenderOut(BaseModel):
    """单个柜型 block 的渲染结果（前端确认时原样回传）。"""

    block_name: str
    ok: bool
    image_name: str | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    depth_mm: float | None = None
    entity_count: int = 0
    layers: list[str] = []
    layer_counts: dict[str, int] = {}
    texts: list[str] = []
    error: str | None = None
    #: 往临时文档搬实体时的失败记录（块定义缺失等）。
    clone_errors: list[str] = []
    #: 标注文字渲染失败、已降级为纯几何渲染。
    text_render_failed: bool = False
    #: 降级时被剥离的带文字实体数量。
    stripped_count: int = 0


class RenderRequest(BaseModel):
    """对已上传的 DWG/DXF 做柜型渲染。

    ``file`` 用上传时返回的 stored_name，而不是让前端再传一遍路径 ——
    前端能改参数，服务端必须自己从库里取路径，否则就是任意文件读取漏洞。
    """

    file: str
    width: int = Query(1400, ge=400, le=3000)
    only_blocks: list[str] = []


@app.post("/api/cupboards/render")
def render_cupboards(body: RenderRequest) -> dict[str, Any]:
    """把 DWG/DXF 里的每个**柜体**渲染成 JPG，供柜型库点击查看。

    需求：「读取了 dwg 文件，将里面的不同的柜型都导出成 jpg 格式，
    放在 library 里，可以点击查看」。

    **粒度（用户实图纠正）**：一张图 = 一个**柜子**，柜内含若干套
    water+gas，**表位数量就是该层的 units 数**。用户原话：
    「不是解析单个 water 或者 gas 的表，而是一个 cupboard，
    里面包含了多套的 water+gas」。

    旧实现按命名 block 逐个渲染，渲出来的是柜内单个表符号 —— 前两轮
    翻车的根因都是「一个 block = 一个柜型」这个错误前提。
    详见 :mod:`app.parsers.cupboard_geometry` 的模块文档。
    """
    # 路径只能来自上传目录，不接受外部路径
    src = (UPLOAD_DIR / Path(body.file).name).resolve()
    if not str(src).startswith(str(UPLOAD_DIR.resolve())) or not src.is_file():
        raise HTTPException(404, "文件不存在或不在上传目录内")

    if src.suffix.lower() not in (".dwg", ".dxf"):
        raise HTTPException(400, "只支持 DWG / DXF")

    dxf, err = _to_dxf(src)
    if dxf is None:
        raise HTTPException(422, f"DWG 转换失败：{err}")

    results, parsed = render_cupboard_regions(
        dxf,
        THUMB_DIR_PATH,
        only_codes=body.only_blocks or None,
        width=body.width,
    )
    ok = [r for r in results if r.ok]
    return {
        "total": len(results),
        "rendered": len(ok),
        "failed": len(results) - len(ok),
        "source": src.name,
        # 柜型几何明细 —— 前端按表位数（= units 数）分组展示
        "cupboards": [
            {**_cup_out(c, i), "code": _cab_code(c, i)} for i, c
            in enumerate(parsed.cupboards)
        ],
        # 被丢弃的区域（含理由）—— 用户抱怨「为啥有这些」时要能解释
        "discarded": [
            {"w": round(r.width, 1), "h": round(r.height, 1),
             "size": [round(r.width, 1), round(r.height, 1)],
             "layer": r.layer, "reason": why}
            for r, why in parsed.discarded[:120]
        ],
        "diag": {
            "entity_total": parsed.entity_total,
            "rect_total": parsed.rect_total,
            "cabinet_frame_pairs": parsed.cabinet_frame_pairs,
            "cabinet_layer_hits": parsed.cabinet_layer_hits,
            "units_total": sum(c.positions_total for c in parsed.cupboards),
        },
        "items": [RenderOut(**r.__dict__) for r in results],
    }


def _cab_code(c, idx: int) -> str:
    """柜型编码 —— 必须与 :func:`_cabinet_code` 完全一致。

    两边不一致会让前端按 code 关联柜型明细时全部落空（卡片退回
    显示「实体数 × 尺寸」，套数信息丢失），所以这里复用渲染侧的
    同一个函数，而不是各写一份 f-string。
    """
    from app.parsers.cupboard_render import _cabinet_code
    return _cabinet_code(c, idx)


def _cup_out(c, idx: int = 0) -> dict[str, Any]:
    """柜型几何信息 → 前端可消费的 dict。

    ``positions_total`` 是**套数 = max(gas 表数, water 表数)**，
    前端按它分组显示「N 套 · CxR 排布」。
    """
    d = c.as_dict()
    d["notes"] = c.notes
    d["layer"] = c.layer
    d["glyph_count"] = c.glyph_count
    d["code"] = _cab_code(c, idx)
    return d


@app.get("/api/thumbs/{name}")
def get_thumb(name: str):
    """柜型缩略图。前端柜型库点击时用 <img src="/api/thumbs/xxx.jpg">。"""
    p = (THUMB_DIR_PATH / Path(name).name).resolve()
    if not str(p).startswith(str(THUMB_DIR_PATH.resolve())):
        raise HTTPException(403, "非法路径")
    if not p.is_file():
        raise HTTPException(404, "缩略图不存在")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


class DwgCommitIn(BaseModel):
    """需求 2：用户在预览确认后提交入库。"""

    source_file: str
    #: 来源 DWG 的显示名（不含扩展名）—— 左树根节点用它，可改显示名
    source_name: str | None = None
    variants: list[VariantIn] = []
    cupboard_name: str | None = None
    note: str | None = None
    #: 柜型 block 名 → 渲染结果。解析阶段产出，前端原样回传。
    renders: list[RenderOut] = []


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
        # block 名 → 渲染结果。尺寸优先用 DWG 实测值，不再是 POC 占位。
        rmap = {r.block_name: r for r in body.renders if r.ok and r.image_name}
        for v in body.variants:
            if s.scalar(select(CupboardVariant).where(CupboardVariant.variant_code == v.code)):
                continue
            # 先按 block_name 精确匹配，再退回 code（多数情况两者相同）
            r = rmap.get(v.block_name or "") or rmap.get(v.code)
            # 实测优先：v.w/v.h 若用户没填（None）就用 DWG 量出来的
            w = v.w if v.w is not None else (r.width_mm if r else None)
            h = v.h if v.h is not None else (r.height_mm if r else None)
            # 尺寸来源判定：
            #   有渲染来源 → measured（DWG 实测）。前端会把实测值回填到
            #     w/h 再提交，所以**不能只看「w/h 有没有值」**——
            #     那会把 DWG 实测误标成 manual（实测踩坑）。
            #   无渲染但填了值 → manual（真人工录入）
            #   都没有         → estimated（POC 占位，必须显式标出）
            if r is not None and (r.width_mm is not None or r.height_mm is not None):
                size_src = "measured"
            elif v.w is not None or v.h is not None:
                size_src = "manual"
            else:
                size_src = "estimated"
            lc = r.layer_counts if r else {}
            # 图纸里的标注文字就是最准确的「介绍」——
            # 比让用户手填「2套 water+gas 排布」有价值得多
            desc = v.description or (
                " / ".join(r.texts[:4]) if r and r.texts else None)
            # 套数优先用解析出的真实值（= max(gas 表数, water 表数)）
            # 绝对不能用 rows*cols —— 那只是网格容量，不是套数。
            # 真实样本实测：13 Units 的柜是 5列×4行 = 20 格，
            # 但只填了 13 套，rows*cols 会把它报成 20 套。
            if v.positions:
                positions = int(v.positions)
            else:
                positions = max(1, int(v.rows) * int(v.cols))
            grid = v.grid or f"{v.rows}x{v.cols}"
            s.add(
                CupboardVariant(
                    cupboard_id=c.id,
                    variant_code=v.code,
                    source_name=body.source_name or body.source_file,
                    layout_rows=v.rows,
                    layout_cols=v.cols,
                    positions_total=positions,
                    grid_aspect=grid,
                    w=w, h=h, d=v.d,
                    meter_spacing_h=v.spacing_h,
                    meter_spacing_v=v.spacing_v,
                    description=desc,
                    source="dwg",
                    meter_combo=v.meter_combo or v.code,
                    meter_counts={"layers": lc} if lc else {},
                    image_path=(f"{THUMB_DIR}/{r.image_name}"
                                if r and r.image_name else None),
                    image_dpi=DEFAULT_THUMB_DPI,
                    size_source=size_src,
                )
            )
            created += 1
        # 先commit 再取 id —— commit 会让 session 里所有对象过期，
        # 此后访问 c.id 会抛 DetachedInstanceError。
        # 原代码在 Cupboard 已存在时不 commit 前访问，侥幸没触发。
        s.commit()
        cid = c.id
    return {"created": created, "cupboard_id": cid}


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
        #必须 no-cache —— 前端是**单文件 HTML**，浏览器一旦缓存就再也
        # 拿不到新代码，用户看到的还是旧界面 + 新后端：
        #   ·旧 JS 调旧字段 → 后端 500
        #   · 旧弹窗布局 → 「点了没反应」
        # 实测踩坑：用户 git pull 到新版、也重启了后端，但浏览器缓存的
        # 还是旧 index.html，于是柜型库点不开、入库报 500。
        resp = FileResponse(str(fe), media_type="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp
    raise HTTPException(404, "前端未构建")