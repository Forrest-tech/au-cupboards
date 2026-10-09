"""命令行入口：不经Web UI，直接跑端到端流水线。

    python -m app.cli run <图纸.pdf>              # 解析 + 匹配 + 导出
    python -m app.cli run <图纸.pdf> --project "XX"
    python -m app.cli health                      # 检查后端可用性
    python -m app.cli units<job_id>               # 列出某次解析的单元
    python -m app.cli variants                    # 列出柜型库

设计原则：**零额外依赖**，只用标准库argparse —— 不想装click/typer
就够用了，且 CLI 挂了不该影响 Web 服务。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 与 api/server.py 保持一致的默认路径：<repo>/var
VAR_DIR = Path(__file__).resolve().parents[2] / "var"
DB_PATH = VAR_DIR / "aucup.db"
EXPORT_DIR = VAR_DIR / "export"

#: 44 户口径（A008 单元表 / 封面 ROOM MIX / MIN DRY AREA 规则三重验证）
COVER_TOTAL_UNITS = 44


def _pipeline():
    from app.services.pipeline import Pipeline

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return Pipeline(DB_PATH, EXPORT_DIR)


# ---------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"✗ 文件不存在：{pdf}", file=sys.stderr)
        return 2
    if pdf.suffix.lower() != ".pdf":
        print(f"✗ 目前仅支持 PDF（收到 {pdf.suffix}）。"
              f"DWG/DXF 请用 Web UI 或 /api/parse?kind=cupboard", file=sys.stderr)
        return 2

    pl = _pipeline()
    result = pl.run(pdf, project=args.project)

    s = result.bundle.summary()
    cv = result.bundle.cross_validation

    print(f"\n{'=' * 64}")
    print(f"  解析完成：{pdf.name}")
    print(f"{'=' * 64}")
    print(f"  单元总数    : {s['total_units']}"
          + (f"（+{s['communal_units']} CO-LIVING，共 {s['total_spaces']} 空间）"
             if s.get("communal_units") else ""))
    print(f"  按楼层      : " + "  ".join(
        f"{k}={v}" for k, v in (s.get("by_floor") or {}).items()))
    print(f"  按柜型      : " + "  ".join(
        f"{k}×{v}" for k, v in (s.get("by_variant") or {}).items()))
    print(f"  按户型      : " + "  ".join(
        f"{k}×{v}" for k, v in (s.get("by_unit_type") or {}).items()))
    print(f"  交叉验证    : {cv.get('verdict', 'N/A')}"
          f"（一致率 {cv.get('consistency', 0):.0%}）")
    print(f"  合规错误    : {s.get('errors', 0)}    警告: {s.get('warnings', 0)}")
    print(f"  平均置信度  : {s.get('avg_confidence', 0):.3f}")

    if s.get("total_units") != COVER_TOTAL_UNITS:
        print(f"\n  ⚠️ 预期 {COVER_TOTAL_UNITS} 户，实得 {s['total_units']} 户 —— "
              f"请检查图纸是否完整")

    print(f"\n  导出文件：")
    for kind, paths in (result.out_files or {}).items():
        if isinstance(paths, str):
            paths = [paths]
        for p in paths:
            print(f"    {kind:6} → {p}")

    print(f"\n  JSON 摘要：")
    print("    " + json.dumps(s, ensure_ascii=False, indent=2).replace("\n", "\n    "))
    return 0


# ---------------------------------------------------------------- health


def cmd_health(_args: argparse.Namespace) -> int:
    import shutil

    from app.config import load_config
    from app.parsers.dwg import detect_backends

    cfg = load_config()
    backends = detect_backends()
    dwg_ready = any(backends.values())

    print("AU Meter Cupboard Scheduling System — 环境自检")
    print(f"  Python           : {sys.version.split()[0]}")
    print(f"  解析规则版本      : {getattr(cfg, 'version', '?')}")
    print(f"  数据库           : {DB_PATH}")
    print(f"  导出目录         : {EXPORT_DIR}")
    print(f"  DWG 后端 ODA     : {backends.get('oda_file_converter') or '未安装'}")
    print(f"  DWG 后端 dwgread : {backends.get('dwgread') or '未安装'}")
    print(f"  DWG 可用         : {'是' if dwg_ready else '否（不影响 PDF 使用）'}")

    if sys.version_info < (3, 11):
        print(f"\n  ⚠️ 当前 Python {sys.version.split()[0]}，"
              f"本项目需要 3.11+（使用了 `X | None` 语法）")
        return 1
    return 0


# ---------------------------------------------------------------- units


def cmd_units(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.entities import (
        CupboardVariant,
        JobStatus,
        ParseJob,
        Selection,
        Unit,
    )

    pl = _pipeline()
    with Session(pl.engine) as s:
        job = s.get(ParseJob, args.job_id)
        if job is None:
            print(f"✗ 找不到 job {args.job_id}", file=sys.stderr)
            return 2
        print(f"Job {job.id}  status={job.status.value}  "
              f"rules=v{job.config_version or '?'}")
        if job.status == JobStatus.FAILED:
            print(f"  ✗ 该任务失败：{job.error_message or '（无错误信息）'}", file=sys.stderr)
            return 1
        if job.status == JobStatus.NEEDS_REVIEW:
            print(f"  ⚠️ 状态为 needs_review —— 有单元需人工确认"
                  f"（这通常是正常的：置信度 0.55~0.85 区间）")

        rows = s.execute(
            select(Unit, Selection)
            .join(Selection, Selection.unit_id == Unit.id)
            .where(Unit.parse_job_id == args.job_id)
            .order_by(Unit.floor_no, Unit.seq)
        ).all()

        if not rows:
            print("（无单元记录）")
            return 0

        print(f"\n  {'单元':<8}{'楼层':<8}{'DRY':>8}{'WET':>8}  "
              f"{'柜型':<20}{'尺寸 mm':<20}{'备注'}")
        print("  " + "-" * 92)
        for u, sel in rows:
            v = s.get(CupboardVariant, sel.variant_id) if sel.variant_id else None
            size = f"{v.w:.0f}×{v.h:.0f}×{v.d:.0f}" if v else "—"
            code = v.variant_code if v else "—"
            flags = []
            if u.is_communal:
                flags.append("CO-LIVING")
            if u.accessible:
                flags.append("无障碍")
            if u.corrected:
                flags.append("人工修正")
            print(f"  {u.label:<8}{u.floor_code:<8}"
                  f"{(u.area_m2 or 0):>8.2f}{(u.area_wet_m2 or 0):>8.2f}  "
                  f"{code:<20}{size:<20}{'/'.join(flags)}")
        print(f"\n  共 {len(rows)} 行")
    return 0


# ---------------------------------------------------------------- variants


def cmd_variants(_args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.entities import CupboardVariant

    pl = _pipeline()
    with Session(pl.engine) as s:
        vs = s.scalars(
            select(CupboardVariant).order_by(CupboardVariant.positions_total,
                                             CupboardVariant.variant_code)
        ).all()
        if not vs:
            print("柜型库为空 —— 运行一次 `run` 后会自动灌入种子数据。")
            return 0
        print(f"{'编码':<20}{'表位':<6}{'排布':<8}{'尺寸 mm':<20}{'分组'}")
        print("-" * 86)
        for v in vs:
            size = f"{v.w:.0f}×{v.h:.0f}×{v.d:.0f}"
            print(f"{v.variant_code:<20}{v.positions_total:<6}"
                  f"{f'{v.layout_cols}×{v.layout_rows}':<8}{size:<20}"
                  f"{v.meter_combo or '—'}")
        print(f"\n  共 {len(vs)} 个变体")
    return 0


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="AU Meter Cupboard Scheduling System 命令行入口",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="解析图纸并导出选型清单")
    p_run.add_argument("pdf", help="建筑图纸 PDF 路径")
    p_run.add_argument("--project", default="74 KEELER ST CARLINGFORD",
                       help="项目名（用于树状导航归类）")
    p_run.set_defaults(func=cmd_run)

    p_health = sub.add_parser("health", help="检查环境与 DWG 后端可用性")
    p_health.set_defaults(func=cmd_health)

    p_units = sub.add_parser("units", help="列出某次解析的单元明细")
    p_units.add_argument("job_id", type=int)
    p_units.set_defaults(func=cmd_units)

    p_var = sub.add_parser("variants", help="列出柜型库变体")
    p_var.set_defaults(func=cmd_variants)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
