"""STAGE 3：选型结果 → 导出 PDF / Excel / JSON。

PDF 用 ReportLab（已在沙箱预装，无需额外依赖）。
导出内容包含完整溯源信息 —— 借鉴 OpenTakeoff 的 provenance 理念：
每个数字都要能回答「怎么来的」。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# 中文字体：使用 ReportLab 内置 CID 字体，无需外部字体文件
_CN = "STSong-Light"
_CN_BOLD = "STSong-Light"
for _f in (_CN, _CN_BOLD):
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_f))
    except Exception:  # pragma: no cover - 已注册
        pass


@dataclass
class ExportRow:
    floor: str
    unit_label: str
    area_m2: float | None
    variant_code: str | None
    grid: str | None
    w: float | None
    h: float | None
    d: float | None
    positions: int | None
    compliance: str
    confidence: float
    confidence_tier: str
    evidence_page: int | None
    corrected: bool
    # ---- 扩展字段（需求 1：每层 units 数与对应 cupboard 类型/样式/尺寸/描述）
    is_communal: bool = False
    unit_type: str | None = None
    accessible: bool = False
    variant_name: str | None = None
    variant_description: str | None = None
    layout_form: str | None = None   # 排布形式 1/2/3/4
    sources: list[str] = field(default_factory=list)
    # 表位需求的推导溯源：为什么这个单元要N 个表位
    requirement_derivation: dict[str, Any] = field(default_factory=dict)
    wet_area_m2: float | None = None


@dataclass
class ExportBundle:
    project: str
    generated_at: str
    rows: list[ExportRow] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    floor_summary: list[dict[str, Any]] = field(default_factory=list)
    cross_validation: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        by_floor: dict[str, int] = {}
        by_variant: dict[str, int] = {}
        for r in self.rows:
            if r.is_communal:
                continue
            by_floor[r.floor] = by_floor.get(r.floor, 0) + 1
            if r.variant_code:
                by_variant[r.variant_code] = by_variant.get(r.variant_code, 0) + 1
        communal = [r for r in self.rows if r.is_communal]
        return {
            "total_units": sum(by_floor.values()),
            "communal_units": len(communal),
            "total_spaces": len(self.rows),
            "by_floor": dict(sorted(by_floor.items())),
            "by_variant": dict(sorted(by_variant.items())),
            "by_unit_type": dict(
                sorted(
                    (
                        t,
                        sum(1 for r in self.rows if r.unit_type == t),
                    )
                    for t in {r.unit_type for r in self.rows if r.unit_type}
                )
            ),
            "errors": sum(1 for r in self.rows if r.compliance.startswith("ERROR")),
            "warnings": sum(1 for r in self.rows if r.compliance.startswith("WARN")),
            "corrected": sum(1 for r in self.rows if r.corrected),
            "avg_confidence": round(
                sum(r.confidence for r in self.rows) / len(self.rows), 3
            ) if self.rows else 0.0,
        }


# ---------------------------------------------------------------- JSON


def export_json(bundle: ExportBundle, path: str | Path) -> Path:
    p = Path(path)
    payload = {
        "project": bundle.project,
        "generated_at": bundle.generated_at,
        "summary": bundle.summary(),
        "rows": [asdict(r) for r in bundle.rows],
        "diagnostics": bundle.diagnostics,
        "warnings": bundle.warnings,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------- Excel


def export_excel(bundle: ExportBundle, path: str | Path) -> Path:
    import pandas as pd

    p = Path(path)
    df = pd.DataFrame([asdict(r) for r in bundle.rows])
    s = bundle.summary()
    with pd.ExcelWriter(p, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="选型清单", index=False)
        pd.DataFrame(
            [{"指标": k, "值": json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v}
             for k, v in s.items()]
        ).to_excel(w, sheet_name="汇总", index=False)
        if bundle.warnings:
            pd.DataFrame({"警告": bundle.warnings}).to_excel(w, sheet_name="警告", index=False)
    return p


# ---------------------------------------------------------------- PDF

_CN = "STSong-Light"


def export_pdf(bundle: ExportBundle, path: str | Path) -> Path:
    p = Path(path)
    doc = SimpleDocTemplate(
        str(p), pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle("cn", parent=styles["Normal"], fontName=_CN, fontSize=8, leading=11)
    h1 = ParagraphStyle("h1", parent=base, fontSize=15, leading=19, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base, fontSize=11, leading=15,
                        spaceBefore=10, spaceAfter=5, textColor=colors.HexColor("#1e40af"))
    cell = ParagraphStyle("cell", parent=base, fontSize=7.5, leading=10)

    story: list[Any] = []
    story.append(Paragraph(f"AU Cupboards 选型清单 — {bundle.project}", h1))
    story.append(Paragraph(
        f"生成时间 {bundle.generated_at}｜ 单位数 {bundle.summary()['total_units']} ｜ "
        f"平均置信度 {bundle.summary()['avg_confidence']} ｜ "
        f"合规错误 {bundle.summary()['errors']} ｜ 警告 {bundle.summary()['warnings']}",
        base))
    story.append(Spacer(1, 5))

    # 汇总
    s = bundle.summary()
    story.append(Paragraph("汇总", h2))
    sum_rows = [[Paragraph("<b>楼层</b>", cell), Paragraph("<b>单元数</b>", cell)]] + [[k, str(v)] for k, v in s["by_floor"].items()]
    sum_rows += [[Paragraph("<b>柜型变体</b>", cell), Paragraph("<b>数量</b>", cell)]] + [[k, str(v)] for k, v in s["by_variant"].items()]
    t1 = Table(sum_rows, colWidths=[60 * mm, 30 * mm], hAlign="LEFT", repeatRows=1)
    t1.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _CN),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2ff")),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t1)

    # 明细
    story.append(Paragraph("选型明细", h2))
    head = ["楼层", "单元", "面积 m²", "柜型变体", "排布", "宽", "高", "深",
            "表位数", "合规", "置信度", "级别", "证据页", "已修正"]
    data = [[Paragraph(f"<b>{h}</b>", cell) for h in head]]
    for r in bundle.rows:
        data.append([
            r.floor, r.unit_label,
            f"{r.area_m2:.2f}" if r.area_m2 else "—",
            r.variant_code or "未匹配",
            r.grid or "—",
            f"{r.w:.0f}" if r.w else "—",
            f"{r.h:.0f}" if r.h else "—",
            f"{r.d:.0f}" if r.d else "—",
            str(r.positions) if r.positions else "—",
            r.compliance or "OK",
            f"{r.confidence:.3f}",
            {"high": "高", "medium": "中", "low": "低"}.get(r.confidence_tier, r.confidence_tier),
            str(r.evidence_page) if r.evidence_page else "—",
            "是" if r.corrected else "",
        ])

    t2 = Table(data, colWidths=[13 * mm, 15 * mm, 17 * mm, 30 * mm, 13 * mm,
                                12 * mm, 12 * mm, 12 * mm, 13 * mm, 17 * mm,
                                17 * mm, 12 * mm, 14 * mm, 14 * mm], repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), _CN),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (-1, -1), "CENTER"),
    ]
    for i, r in enumerate(bundle.rows, start=1):
        if r.compliance.startswith("ERROR"):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fee2e2")))
        elif r.compliance.startswith("WARN"):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fef3c7")))
        elif r.confidence_tier == "high":
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f0fdf4")))
        if r.corrected:
            style.append(("TEXTCOLOR", (13, i), (13, i), colors.HexColor("#b91c1c")))
    t2.setStyle(TableStyle(style))
    story.append(t2)

    # 溯源页
    story.append(PageBreak())
    story.append(Paragraph("溯源与诊断（Provenance）", h1))
    story.append(Paragraph(
        "本清单每个数字均可回溯到解析证据。规则版本、命中统计与警告记录如下。", base))
    story.append(Paragraph("规则命中统计", h2))
    drows = [[Paragraph("<b>规则 ID</b>", cell), Paragraph("<b>说明</b>", cell),
              Paragraph("<b>命中次数</b>", cell)]]
    from app.config import load_config
    cfg = load_config()
    desc = {}
    for r in list(cfg.unit_rules) + list(cfg.floor_rules) + list(cfg.drawing_no_rules) + list(cfg.page_role_rules):
        desc[r.rule_id] = r.description
    for k, v in sorted(bundle.diagnostics.items()):
        if v:
            drows.append([k, desc.get(k, ""), str(v)])
    if len(drows) == 1:
        drows.append(["（无）", "", ""])
    t3 = Table(drows, colWidths=[45 * mm, 150 * mm, 25 * mm], repeatRows=1)
    t3.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _CN),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2ff")),
    ]))
    story.append(t3)

    if bundle.warnings:
        story.append(Paragraph("警告", h2))
        for w in bundle.warnings:
            story.append(Paragraph(f"• {w}", base))
            story.append(Spacer(1, 2))

    doc.build(story)
    return p


# ---------------------------------------------------------------- Word (DOCX)
# 需求 1 明确要求导出 PDF / Word / JPG 三种格式。
# Word 用python-docx；它对中文字体支持良好，且能生成真正的表格对象，
# 比「把 HTML 改后缀为 .doc」可靠得多（实测后者Word 会提示格式不匹配）。


def export_word(bundle: ExportBundle, path: str | Path) -> Path:
    """导出 .docx。含标题、汇总、逐层分组明细、溯源诊断。"""
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt, RGBColor

    p = Path(path)
    doc = Document()

    # 横向A4，容纳14 列明细
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Mm(297), Mm(210)
    for attr in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, attr, Mm(10))

    # 中文字体
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(9)
    try:
        from docx.oxml.ns import qn

        normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    except Exception:  # pragma: no cover
        pass

    s = bundle.summary()
    h = doc.add_heading(f"AU Cupboards 选型清单 — {bundle.project}", level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph(
        f"生成时间 {bundle.generated_at}｜ 单元 {s['total_units']} 户"
        f"｜ CO-LIVING {s['communal_units']} 处"
        f"｜ 空间总数 {s['total_spaces']}"
        f"｜ 平均置信度 {s['avg_confidence']}"
        f"｜ 合规错误 {s['errors']} ｜ 警告 {s['warnings']}"
    )

    # ---- 楼层汇总（需求 1：按楼层按行显示 units 数与对应 cupboard）
    doc.add_heading("按楼层汇总", level=2)
    t = doc.add_table(rows=1, cols=7)
    t.style = "Light Grid Accent 1"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, name in enumerate(["楼层", "单元数", "CO-LIVING", "总面积 m²", "无障碍", "单元编号", "柜型"]):
        t.rows[0].cells[i].text = name

    variants_by_floor: dict[str, set[str]] = {}
    for r in bundle.rows:
        variants_by_floor.setdefault(r.floor, set())
        if r.variant_code:
            variants_by_floor[r.floor].add(r.variant_code)

    fs_rows = bundle.floor_summary or []
    for row in fs_rows:
        cells = t.add_row().cells
        cells[0].text = str(row.get("floor_code", ""))
        cells[1].text = str(row.get("unit_count", 0))
        cells[2].text = str(row.get("communal_count", 0))
        cells[3].text = str(row.get("total_area_m2", 0))
        cells[4].text = str(row.get("accessible_count", 0))
        cells[5].text = ", ".join(row.get("labels", []))
        cells[6].text = ", ".join(sorted(variants_by_floor.get(row.get("floor_code", ""), set())))

    # ---- 逐层分组明细
    doc.add_heading("选型明细", level=2)
    by_floor: dict[str, list[ExportRow]] = {}
    for r in bundle.rows:
        by_floor.setdefault(r.floor, []).append(r)

    head = ["单元", "面积 m²", "类型", "柜型变体", "排布", "宽", "高", "深",
            "表位数", "合规", "置信度", "证据页", "描述"]
    for floor in sorted(by_floor, reverse=True):
        doc.add_heading(floor, level=3)
        t2 = doc.add_table(rows=1, cols=len(head))
        t2.style = "Light Grid Accent 1"
        for i, name in enumerate(head):
            t2.rows[0].cells[i].text = name
        for r in by_floor[floor]:
            c = t2.add_row().cells
            c[0].text = r.unit_label + ("（CO-LIVING）" if r.is_communal else "")
            c[1].text = f"{r.area_m2:.2f}" if r.area_m2 else "—"
            c[2].text = r.unit_type or "—"
            c[3].text = r.variant_code or "未匹配"
            c[4].text = r.layout_form or r.grid or "—"
            c[5].text = f"{r.w:.0f}" if r.w else "—"
            c[6].text = f"{r.h:.0f}" if r.h else "—"
            c[7].text = f"{r.d:.0f}" if r.d else "—"
            c[8].text = str(r.positions) if r.positions else "—"
            c[9].text = r.compliance or "OK"
            c[10].text = f"{r.confidence:.3f}"
            c[11].text = str(r.evidence_page) if r.evidence_page else "—"
            c[12].text = (r.variant_description or "")[:60]

    # ---- 溯源诊断
    doc.add_heading("溯源与诊断（Provenance）", level=2)
    doc.add_paragraph(
        "本清单每个数字均可回溯到解析证据。规则版本与命中统计如下，"
        "规则定义见 backend/app/config/parse_rules.json。"
    )
    from app.config import load_config

    cfg = load_config()
    desc = {}
    for r in list(cfg.unit_rules) + list(cfg.floor_rules) + list(cfg.drawing_no_rules) + list(cfg.page_role_rules):
        desc[r.rule_id] = r.description
    t3 = doc.add_table(rows=1, cols=3)
    t3.style = "Light Grid Accent 1"
    for i, name in enumerate(["规则 ID", "说明", "命中次数"]):
        t3.rows[0].cells[i].text = name
    for k, v in sorted(bundle.diagnostics.items()):
        if not v:
            continue
        c = t3.add_row().cells
        c[0].text = k
        c[1].text = desc.get(k, "")[:120]
        c[2].text = str(v)

    if bundle.cross_validation:
        cv = bundle.cross_validation
        doc.add_heading("双源交叉验证", level=2)
        doc.add_paragraph(
            f"结论：{cv.get('verdict')}｜ 平面图 {cv.get('plan_units')} 户vs "
            f"单元表 {cv.get('schedule_units')} 户 → 互证 {cv.get('agreed')} 户"
            f"｜ 一致率 {cv.get('consistency')}｜ 仅平面图 {cv.get('only_in_plan')}"
            f" ｜ 仅单元表 {cv.get('only_in_schedule')}"
            f" ｜ 面积矛盾 {len(cv.get('area_mismatch', []))} 条"
        )

    if bundle.warnings:
        doc.add_heading("警告", level=2)
        for w in bundle.warnings:
            doc.add_paragraph(f"• {w}", style="List Bullet")

    doc.save(str(p))
    return p


# ---------------------------------------------------------------- JPG
# 需求 1 要求导出 JPG。用 Pillow 直接渲染表格图元 —— 不依赖浏览器/字体安装，
# 沙箱与本地环境都能跑。

_FONT_CACHE: dict[int, Any] = {}


def _load_font(size: int):
    """按优先级加载中文字体；找不到则回退默认位图字体。"""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:  # pragma: no cover
                continue
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def export_jpg(
    bundle: ExportBundle,
    path: str | Path,
    width: int = 2400,
    max_rows_per_page: int = 40,
) -> list[Path]:
    """导出 JPG（分页）。返回生成的文件路径列表。

    实现说明：用 Pillow 直接画表格。相比「PDF 转图片」的好处是
    无需 poppler/ghostscript，且中文字体可控。
    """
    from PIL import Image, ImageDraw

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    s = bundle.summary()
    f_title = _load_font(34)
    f_sub = _load_font(19)
    f_head = _load_font(20)
    f_cell = _load_font(18)

    # 列定义：(表头, 宽度px, 取值函数, 对齐)
    cols: list[tuple[str, int, Any, str]] = [
        ("楼层", 100, lambda r: r.floor, "l"),
        ("单元", 110, lambda r: r.unit_label + ("*" if r.is_communal else ""), "l"),
        ("面积m²", 110, lambda r: f"{r.area_m2:.2f}" if r.area_m2 else "—", "r"),
        ("类型", 110, lambda r: r.unit_type or "—", "c"),
        ("柜型变体", 250, lambda r: r.variant_code or "未匹配", "l"),
        ("排布", 110, lambda r: r.layout_form or r.grid or "—", "c"),
        ("宽", 85, lambda r: f"{r.w:.0f}" if r.w else "—", "r"),
        ("高", 85, lambda r: f"{r.h:.0f}" if r.h else "—", "r"),
        ("深", 85, lambda r: f"{r.d:.0f}" if r.d else "—", "r"),
        ("表位", 85, lambda r: str(r.positions) if r.positions else "—", "r"),
        ("合规", 230, lambda r: r.compliance or "OK", "l"),
        ("置信度", 110, lambda r: f"{r.confidence:.3f}", "r"),
        ("级别", 80, lambda r: {"high": "高", "medium": "中", "low": "低"}.get(r.confidence_tier, r.confidence_tier), "c"),
        ("证据页", 90, lambda r: str(r.evidence_page) if r.evidence_page else "—", "r"),
    ]
    table_w = sum(c[1] for c in cols)
    width = max(width, table_w + 60)

    row_h = 34
    header_h = 44
    top_h = 210

    out_files: list[Path] = []
    pages = [
        bundle.rows[i:i + max_rows_per_page]
        for i in range(0, len(bundle.rows), max_rows_per_page)
    ] or [[]]

    for page_idx, rows in enumerate(pages, start=1):
        height = top_h + header_h + row_h * (len(rows) + 1) + 90
        img = Image.new("RGB", (width, height), "white")
        d = ImageDraw.Draw(img)

        # 标题区
        d.rectangle([0, 0, width, 130], fill="#1e3a8a")
        d.text((30, 22), f"AU Cupboards 选型清单 — {bundle.project}", font=f_title, fill="white")
        d.text(
            (30, 76),
            f"生成 {bundle.generated_at}｜ 单元 {s['total_units']} 户"
            f"｜ CO-LIVING {s['communal_units']} 处"
            f"｜ 空间 {s['total_spaces']}"
            f"｜ 平均置信度 {s['avg_confidence']}"
            f"｜ 错误 {s['errors']} / 警告 {s['warnings']}",
            font=f_sub, fill="#c7d2fe",
        )

        # 楼层汇总条
        fs = bundle.floor_summary or []
        x = 30
        d.text((x, 148), "楼层分布：", font=f_sub, fill="#0f172a")
        x += 92
        for row in fs:
            label = f"{row.get('floor_code','')} {row.get('unit_count',0)}户"
            if row.get("communal_count"):
                label += f"(+{row['communal_count']}C)"
            tw = d.textlength(label, font=f_sub)
            d.rounded_rectangle([x - 8, 142, x + tw + 8, 176], 6, fill="#eef2ff", outline="#c7d2fe")
            d.text((x, 148), label, font=f_sub, fill="#1e3a8a")
            x += tw + 26
        cv = bundle.cross_validation or {}
        if cv:
            txt = (
                f"双源交叉验证: {cv.get('verdict')} "
                f"(互证 {cv.get('agreed')} 户, 一致率 {cv.get('consistency')})"
            )
            d.text((width - d.textlength(txt, font=f_sub) - 30, 148), txt, font=f_sub, fill="#166534")

        # 表头
        y0 = top_h
        d.rectangle([30, y0, 30 + table_w, y0 + header_h], fill="#1e40af")
        cx = 30
        for name, cw, _fn, _al in cols:
            tw = d.textlength(name, font=f_head)
            d.text((cx + (cw - tw) / 2, y0 + 11), name, font=f_head, fill="white")
            cx += cw

        # 数据行
        y = y0 + header_h
        for i, r in enumerate(rows):
            if r.compliance.startswith("ERROR"):
                bg = "#fee2e2"
            elif r.compliance.startswith("WARN"):
                bg = "#fef3c7"
            elif r.confidence_tier == "high":
                bg = "#f0fdf4"
            else:
                bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
            d.rectangle([30, y, 30 + table_w, y + row_h], fill=bg, outline="#cbd5e1")
            cx = 30
            for _name, cw, fn, al in cols:
                val = str(fn(r))
                tw = d.textlength(val, font=f_cell)
                if al == "r":
                    tx = cx + cw - tw - 10
                elif al == "c":
                    tx = cx + (cw - tw) / 2
                else:
                    tx = cx + 10
                d.text((tx, y + 8), val, font=f_cell, fill="#0f172a")
                cx += cw
            y += row_h

        # 页脚
        d.line([30, y + 24, 30 + table_w, y + 24], fill="#cbd5e1", width=1)
        foot = (
            f"第 {page_idx}/{len(pages)} 页｜ * = CO-LIVING 集体居住区"
            f" ｜ 溯源：backend/app/config/parse_rules.json"
        )
        d.text((30, y + 36), foot, font=f_sub, fill="#64748b")

        out = p if page_idx == 1 else p.with_name(f"{p.stem}-p{page_idx}{p.suffix}")
        img.save(out, "JPEG", quality=92, optimize=True)
        out_files.append(out)

    return out_files
