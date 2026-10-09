"""DWG 解析链路：ODA File Converter → DXF → ezdxf。

这是报告认定的**风险最高的一环**（P0 验证项）。
关键设计：
  · ODA 是外部进程，不自己写 DWG 解析器
  · LibreDWG 作为兜底
  · **代理实体校验**：ODA 转出的 DXF 可能丢失 proxy/custom entity，
    必须显式检查并在报告中暴露（报告第 03 章硬要求）
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import ezdxf


class DwgBackend(str, Enum):
    ODA = "oda"
    LIBREDWG = "libredwg"
    NONE = "none"


@dataclass
class DwgParseResult:
    ok: bool
    backend: DwgBackend
    dxf_path: str | None = None
    layers: list[str] = field(default_factory=list)
    entity_counts: dict[str, int] = field(default_factory=dict)
    blocks: list[str] = field(default_factory=list)
    # block 定义内的实体统计（柜型库的几何主体在这里，不在 modelspace）
    block_entity_counts: dict[str, int] = field(default_factory=dict)
    # 每个 block 内的图层清单 —— Module B 靠它区分 water / gas 分组
    block_layers: dict[str, list[str]] = field(default_factory=dict)
    # 代理实体是否丢失 —— 必须在诊断中暴露
    proxy_entity_lost: bool | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    # 转换完整性 —— 静默失败比报错危险得多，必须显式分级
    integrity: str = "unknown"          # ok | degraded | corrupt
    integrity_notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total_entities(self) -> int:
        """全部实体数 = modelspace + block 定义。"""
        return sum(self.entity_counts.values()) + sum(self.block_entity_counts.values())


# ---------------------------------------------------------------- 后端探测


def detect_backends() -> dict[str, str | None]:
    """探测可用的 DWG 转换后端。"""
    found: dict[str, str | None] = {"oda_file_converter": None, "dwgread": None, "oda_bin": None}

    for exe in ("ODAFileConverter", "ODAFileConverter.exe"):
        p = shutil.which(exe)
        if p:
            found["oda_file_converter"] = p
            break

    dwgread = shutil.which("dwgread")
    if dwgread:
        found["dwgread"] = dwgread

    return found


def convert_with_oda(dwg_path: Path, out_dir: Path) -> tuple[Path | None, str | None]:
    """用 ODA File Converter 转 DXF。

    ODA CLI 用法：
        ODAFileConverter <in_dir> <out_dir> <out_ver> <out_type> <recurse> <audit>
      例：ODAFileConverter in/ out/ ACAD2018 DXF 0 1
    """
    exe = shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe")
    if not exe:
        return None, "未找到 ODAFileConverter（需从 open-design-alliance.com 免费注册下载）"

    in_dir = out_dir / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
    target = in_dir / dwg_path.name
    if dwg_path.resolve() != target.resolve():
        shutil.copy2(dwg_path, target)

    cmd = [exe, str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "ODA 转换超时（300s）"

    dxfs = sorted(out_dir.glob("*.dxf"))
    if not dxfs:
        return None, f"ODA 未产出 DXF。stdout={proc.stdout[:200]} stderr={proc.stderr[:200]}"
    return dxfs[0], None


def convert_with_libredwg(dwg_path: Path, out_dir: Path) -> tuple[Path | None, str | None]:
    """LibreDWG 兜底。注意 GPLv3，仅作独立进程调用。"""
    exe = shutil.which("dwgread")
    if not exe:
        return None, "未找到 dwgread（LibreDWG 命令行）"
    out = out_dir / f"{dwg_path.stem}.dxf"
    try:
        proc = subprocess.run([exe, "-O", "DXF", "-o", str(out), str(dwg_path)],
                              capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "LibreDWG 转换超时"
    if not out.exists() or out.stat().st_size == 0:
        return None, f"LibreDWG 未产出有效 DXF。stderr={proc.stderr[:200]}"
    return out, None


# ---------------------------------------------------------------- 完整性校验

#: DWG 里名字被截成单字母的图层（如 `WATER_METER` → `W`）几乎只出现在
#: **转换器把字符串写坏**的情况。AutoCAD 原生 DWG 不允许图层名重复到
#: 这种程度（同一 drawing 内 `A`/`C`/`D`/`G`/`W` 五个单字母层几乎不可能
#: 是真实设计意图）。命中即判定 corrupt。
_SUSPECT_LAYER_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")

#: 低于此实体数基本等于「什么都没解析出来」
_MIN_PLAUSIBLE_ENTITIES = 5


@dataclass
class IntegrityReport:
    """完整性评估结果。

    ``notes`` 是说明性信息（不降级），``issues`` 是真正的质量问题。
    分开存放是必要的：早期版本把所有 notes 拼进 error，产出过
    「…属正常。；图层名疑似被截断…」这种自相矛盾的用户可见消息。
    """

    level: str                       # ok | degraded | corrupt
    notes: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.level == "ok"

    def message(self) -> str:
        """给用户看的一句话结论。"""
        if self.level == "corrupt":
            return "转换产物完整性校验失败（corrupt）：" + "；".join(self.issues)
        if self.level == "degraded":
            return "转换产物可用但存疑（degraded）：" + "；".join(self.issues)
        return "；".join(self.notes)


def assess_integrity(
    layers: list[str],
    entity_counts: dict[str, int],
    blocks: list[str],
    block_entity_counts: dict[str, int] | None = None,
) -> IntegrityReport:
    """判断转换结果是否可信。

    实测踩坑（本函数的存在理由）：
      用 LibreDWG 自带的 ``dxf2dwg`` 造测试 DWG 时，**写出端**就把
      ``WATER_METER``/``CUPBOARD``/``CP-TRI-1x3`` 全部截断成首字母
      （``W``/``C``/``C``），modelspace 直接为空。转换退出码是 0，
      ezdxf 也能「成功」读出文件 —— 于是旧版 ``inspect_dxf`` 返回
      ``ok=True``，上层拿一堆垃圾数据当柜型库入库，没有任何报错。
      **静默失败比失败本身危险得多**，所以必须在这里拦住。

    ``block_entity_counts`` 必须计入：柜型库 DWG 的几何主体在 block 定义里，
    只看 modelspace 会把完整图纸误判为空图纸。
    """
    notes: list[str] = []
    issues: list[str] = []
    msp_total = sum(entity_counts.values())
    blk_total = sum((block_entity_counts or {}).values())
    total = msp_total + blk_total
    named = [ly for ly in layers if ly not in ("0", "Defpoints") and not ly.startswith("*")]

    if total == 0:
        return IntegrityReport("corrupt", issues=[
            "modelspace 与 block 定义内实体数均为 0：转换产物不含任何几何。"
            "转换器退出码可能仍是 0 —— 不要相信退出码，要验证产物内容。"
        ])

    if total < _MIN_PLAUSIBLE_ENTITIES:
        issues.append(f"仅解析出 {total} 个实体，低于合理下限 {_MIN_PLAUSIBLE_ENTITIES}")

    if msp_total == 0 and blk_total > 0:
        # 这是**说明**不是问题：柜型库 DWG 的变体以 block 组织，
        # modelspace 只有几个 INSERT。若算进 issues 会把正常柜型库
        # 误判成 degraded（实测踩坑）。
        notes.append(
            f"modelspace 为空但 block 定义内有 {blk_total} 个实体 —— "
            "柜型库通常如此（变体以 block 组织），属正常。"
        )

    # 症状 1：图层名全被截成单字母
    single = [ly for ly in named if len(ly) == 1 and ly.upper() in _SUSPECT_LAYER_CHARS]
    if named and len(single) == len(named) and len(named) >= 3:
        issues.append(
            f"图层名疑似被截断：{named} 全部为单字母。"
            "这是转换器写出端字符串处理错误的特征（实测 LibreDWG dxf2dwg 复现）。"
        )
        return IntegrityReport("corrupt", notes, issues)

    # 症状 2：block 名被截断
    real_blocks = [b for b in blocks if not b.startswith("*") and b not in ("_", "__")]
    if real_blocks and all(len(b) == 1 for b in real_blocks) and len(real_blocks) >= 2:
        issues.append(f"block 名疑似被截断：{real_blocks} 全部为单字符")
        return IntegrityReport("corrupt", notes, issues)

    level = "degraded" if issues else "ok"
    notes.append(f"完整性通过：{total} 个实体 / {len(layers)} 图层 / {len(blocks)} block")
    return IntegrityReport(level, notes, issues)


# ---------------------------------------------------------------- DXF 解析


def inspect_dxf(dxf_path: str | Path) -> DwgParseResult:
    """解析 DXF，提取柜体库入库所需的结构信息。

    这是 Module B「分解预览」的数据来源：
      · 图层清单 → 用于识别 water / gas 分组
      · 实体类型统计 → 判断是平面图还是详图
      · block 名清单 → 柜型变体往往是一个 block
    """
    dxf_path = Path(dxf_path)
    res = DwgParseResult(ok=False, backend=DwgBackend.NONE, dxf_path=str(dxf_path))
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        res.error = f"ezdxf 无法读取 DXF: {exc}"
        return res

    try:
        msp = doc.modelspace()
    except Exception as exc:
        res.error = f"无法访问 modelspace: {exc}"
        return res

    res.layers = sorted({ly.dxf.name for ly in doc.layers})
    res.blocks = sorted({b.name for b in doc.blocks if not b.name.startswith("*")})

    counts: dict[str, int] = {}
    for e in msp:
        t = e.dxftype()
        counts[t] = counts.get(t, 0) + 1

    # 柜型库 DWG 的几何几乎全部藏在 block 定义里 —— 只统计 modelspace 会把
    # 一份内容完整的柜型库判成「空图纸」（实测：3 个柜型 block、18 个
    # 冷热水表圆，modelspace 只有 4 个 INSERT/TEXT）。
    # 因此额外遍历 block 内容并单独计数，不与 modelspace 混算。
    block_counts: dict[str, int] = {}
    for blk in doc.blocks:
        if blk.name.startswith("*"):
            continue  # 匿名块 = 标注/线型等系统定义，不是设计内容
        for e in blk:
            t = e.dxftype()
            block_counts[t] = block_counts.get(t, 0) + 1
    res.block_entity_counts = dict(sorted(block_counts.items(), key=lambda kv: -kv[1]))

    # 每个 block 内的图层分布 —— Module B 靠它区分 water / gas 分组
    block_layers: dict[str, list[str]] = {}
    for blk in doc.blocks:
        if blk.name.startswith("*"):
            continue
        lys = sorted({
            e.dxf.layer for e in blk
            if e.dxf.hasattr("layer") and e.dxf.layer
        })
        if lys:
            block_layers[blk.name] = lys
    res.block_layers = dict(sorted(block_layers.items()))

    res.entity_counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    # 代理实体检查：ODA 对 ACAD 代理对象支持有限，需显式暴露
    try:
        has_proxy = any(
            e.dxftype() in ("ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT")
            for e in msp
        )
        # 若 DXF 中完全没有 ACAD_PROXY_ENTITY 但DWG 源很大，提示可能丢失
        res.proxy_entity_lost = None if has_proxy else None
        if not has_proxy:
            res.warnings.append(
                "DXF 中未发现 ACAD_PROXY_ENTITY。若源 DWG 大量使用代理对象"
                "（天正/理正/探索者等），几何可能已丢失 —— 需人工核对。"
            )
    except Exception as exc:
        res.warnings.append(f"代理实体检查失败: {exc}")

    report = assess_integrity(
        res.layers, res.entity_counts, res.blocks, res.block_entity_counts
    )
    res.integrity = report.level
    res.integrity_notes = [*report.notes, *report.issues]
    res.warnings.extend(res.integrity_notes)
    if report.level == "corrupt":
        # 关键：解析「成功」但数据不可信时，绝不能返回 ok=True。
        # 否则上层会拿截断的图层名 / 空 modelspace 当作真实柜型库入库。
        res.ok = False
        res.error = report.message()
        return res

    res.ok = True
    return res


def parse_dwg(dwg_path: str | Path, work_dir: str | Path | None = None) -> DwgParseResult:
    """完整 DWG → 结构化结果。后端自动降级。

    优先级：ODA → LibreDWG → 报错（并给出明确安装指引）
    """
    dwg_path = Path(dwg_path)
    if dwg_path.suffix.lower() == ".dxf":
        return inspect_dxf(dwg_path)

    work_dir = Path(work_dir or tempfile.mkdtemp(prefix="dwg_"))
    work_dir.mkdir(parents=True, exist_ok=True)

    backends = detect_backends()
    attempts: list[str] = []

    # 优先 ODA
    dxf, err = convert_with_oda(dwg_path, work_dir)
    used = DwgBackend.ODA
    if dxf is None:
        attempts.append(f"ODA: {err}")
        dxf, err = convert_with_libredwg(dwg_path, work_dir)
        used = DwgBackend.LIBREDWG
        if dxf is None:
            attempts.append(f"LibreDWG: {err}")

    if dxf is None:
        res = DwgParseResult(ok=False, backend=DwgBackend.NONE)
        res.error = "所有 DWG 后端均不可用或转换失败"
        res.warnings = attempts
        res.integrity = "corrupt"
        res.integrity_notes = ["无任何后端产出可读 DXF"]
        res.stats = {"detected_backends": {k: v for k, v in backends.items()}}
        return res

    res = inspect_dxf(dxf)
    res.backend = used
    res.stats["attempts"] = attempts
    res.stats["detected_backends"] = backends
    if used == DwgBackend.LIBREDWG:
        res.warnings.append("使用 LibreDWG 兜底：高版本 DWG 支持不全，代理实体可能丢失")
    if res.integrity == "corrupt":
        # corrupt 时把降级过程也带出去，便于定位是哪个后端写坏的
        res.warnings.append(f"降级链：{' -> '.join(attempts) or '首个后端即产出 corrupt 产物'}")
    return res
