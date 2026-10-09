"""DWG 解析链路测试（需求 4 的 P0 风险项）。

验证三件事：
  1. 真实 AutoCAD DWG 能走通 DWG → DXF → ezdxf 降级链
  2. **静默数据损坏会被拦截**（本文件存在的主要理由）
  3. 后端探测与降级顺序符合报告约定

背景：本轮 LibreDWG 0.13.3 源码编译成功（dwgread 0.13.3），
DWG 从「无法验证」变成「已用真实文件端到端验证」。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.parsers.dwg import (
    DwgBackend,
    assess_integrity,
    detect_backends,
    inspect_dxf,
    parse_dwg,
)

FIX = Path(__file__).parent / "fixtures"
DXF_DESIGN = FIX / "cupboard_library_design.dxf"
DWG_NEGATIVE = FIX / "neg_truncated_by_dxf2dwg.dwg"
DWG_REAL_2018 = FIX / "dwg_real_acad2018.dwg"
DWG_REAL_2000 = FIX / "dwg_real_acad2000.dwg"


def _need_dwgread() -> None:
    if not shutil.which("dwgread"):
        pytest.skip("未安装 dwgread（LibreDWG），跳过 DWG 链路测试")


def _need_real(name: str) -> Path:
    _need_dwgread()
    p = FIX / name
    if not p.exists():
        pytest.skip(f"缺少真实 DWG 夹具 {name}")
    return p


# ---------------------------------------------------------------- 后端探测


def test_dwgread_backend_is_detected():
    """LibreDWG 编译后，dwgread 必须被探测到。"""
    backends = detect_backends()
    assert "dwgread" in backends
    if shutil.which("dwgread"):
        assert backends["dwgread"], "dwgread 已在 PATH 中但探测失败"
        assert Path(backends["dwgread"]).is_file()


def test_backend_detection_always_returns_stable_keys():
    """探测结果的键必须稳定，前端 /api/health 依赖它。"""
    b = detect_backends()
    assert set(b) == {"oda_file_converter", "dwgread", "oda_bin"}


# ---------------------------------------------------------------- 完整性校验


class TestAssessIntegrity:
    """完整性分级：ok / degraded / corrupt。"""

    def test_healthy_drawing_is_ok(self):
        r = assess_integrity(
            layers=["0", "WATER", "GAS"],
            entity_counts={"LWPOLYLINE": 30, "INSERT": 10, "TEXT": 5},
            blocks=["CP-TRI-1x3", "CP-QUAD-2x2"],
        )
        assert r.level == "ok"
        assert any("完整性通过" in n for n in r.notes)
        assert r.issues == []

    def test_empty_modelspace_is_corrupt(self):
        """最危险的症状：什么几何都没有，但转换退出码是 0。"""
        r = assess_integrity(["A", "B"], {}, [])
        assert r.level == "corrupt"
        assert any("实体数均为 0" in n for n in r.notes + r.issues)
        assert any("不要相信退出码" in n for n in r.notes + r.issues)

    def test_single_letter_layers_are_corrupt(self):
        """实测踩坑：dxf2dwg 把 WATER_METER → W、CUPBOARD → C。"""
        r = assess_integrity(
            layers=["0", "D", "W", "G", "C", "A"],
            entity_counts={"LWPOLYLINE": 5, "CIRCLE": 3, "TEXT": 2},
            blocks=["C", "_"],
        )
        assert r.level == "corrupt"
        assert any("截断" in n for n in r.notes + r.issues)

    def test_truncated_blocks_are_corrupt(self):
        r = assess_integrity(
            layers=["WATER", "GAS"],
            entity_counts={"LWPOLYLINE": 20, "INSERT": 4},
            blocks=["C", "C"],
        )
        assert r.level == "corrupt"

    def test_tiny_drawing_is_degraded_not_corrupt(self):
        """有内容但太少 —— 应为 degraded，不能误判成 corrupt 拦掉合法文件。"""
        r = assess_integrity(
            layers=["WATER", "GAS"], entity_counts={"POINT": 2}, blocks=[]
        )
        assert r.level == "degraded"
        assert any("低于合理下限" in n for n in r.notes + r.issues)

    def test_system_layers_do_not_trigger_truncation_heuristic(self):
        """AutoCAD 系统图层（Defpoints/*ADSK_*）不应被当成截断。"""
        r = assess_integrity(
            layers=["0", "Defpoints", "*ADSK_SYSTEM_LIGHTS", "A-1", "A-2"],
            entity_counts={"LWPOLYLINE": 40, "LINE": 20},
            blocks=["BLOCK-A"],
        )
        assert r.level == "ok"

    def test_single_letter_layer_alone_is_not_corrupt(self):
        """只有一个单字母命名图层时证据不足，不能误杀。"""
        r = assess_integrity(
            layers=["0", "W"], entity_counts={"LWPOLYLINE": 20, "LINE": 5}, blocks=["B1"]
        )
        assert r.level != "corrupt"

    def test_block_entities_count_toward_total(self):
        """只统计 modelspace 会把柜型库误判成空图纸 —— block 必须计入。"""
        r = assess_integrity(
            layers=["WATER_METER", "GAS_METER"],
            entity_counts={},                      # modelspace 空
            blocks=["CP-TRI-1x3", "CP-QUAD-2x2"],
            block_entity_counts={"CIRCLE": 13, "LWPOLYLINE": 26},
        )
        assert r.level == "ok"

    def test_empty_modelspace_with_blocks_gets_informational_note(self):
        """柜型库典型形态：msp 空、block 满 —— 应为 ok 而非 degraded。"""
        r = assess_integrity(
            layers=["WATER_METER", "GAS_METER"], entity_counts={},
            blocks=["CP-TRI-1x3", "CP-QUAD-2x2"],
            block_entity_counts={"CIRCLE": 7, "LWPOLYLINE": 9, "TEXT": 7},
        )
        assert r.level == "ok"
        assert any("柜型库通常如此" in n for n in r.notes + r.issues)
        assert any("完整性通过" in n for n in r.notes + r.issues)

    def test_both_empty_is_corrupt(self):
        r = assess_integrity(
            layers=["0"], entity_counts={}, blocks=[], block_entity_counts={}
        )
        assert r.level == "corrupt"


# ---------------------------------------------------------------- DXF 直读


class TestInspectDxf:
    def test_design_dxf_reads_all_four_layers(self):
        """本项目自建的柜型库 DXF：4 个图层必须全部读出（验证无回归）。"""
        res = inspect_dxf(DXF_DESIGN)
        assert res.ok
        assert res.integrity == "ok"
        for layer in ("WATER_METER", "GAS_METER", "CUPBOARD", "ANNOTATION"):
            assert layer in res.layers, f"缺图层 {layer}：{res.layers}"

    def test_design_dxf_exposes_cupboard_blocks(self):
        """柜型变体在 DWG 里是 block —— 这是 Module B 入库的主要抓手。"""
        res = inspect_dxf(DXF_DESIGN)
        for code in ("CP-TRI-1x3", "CP-QUAD-2x2", "CP-SIX-2x3"):
            assert code in res.blocks, f"缺 block {code}：{res.blocks}"

    def test_design_dxf_counts_modelspace_entities(self):
        res = inspect_dxf(DXF_DESIGN)
        assert res.entity_counts.get("INSERT", 0) == 3   # 3 个柜型引用
        assert res.entity_counts.get("TEXT", 0) == 1

    def test_design_dxf_counts_entities_inside_blocks(self):
        """柜型库几何全在 block 里 —— 必须遍历 block 定义。

        实测踩坑：只统计 modelspace 时，一份含 3 个柜型 block、13 个
        冷热水表圆的完整图纸被算成「只有 4 个实体」，进而被完整性校验
        误判为 degraded。

        逐 block 实测构成（外框 1 + 每表位燃气方框 1）：
          CP-TRI-1x3   LWPOLYLINE 4  CIRCLE 3  TEXT 3
          CP-QUAD-2x2  LWPOLYLINE 5  CIRCLE 4  TEXT 4
          CP-SIX-2x3   LWPOLYLINE 7  CIRCLE 6  TEXT 6
        """
        res = inspect_dxf(DXF_DESIGN)
        assert res.block_entity_counts.get("CIRCLE", 0) == 13      # 冷热水表
        assert res.block_entity_counts.get("TEXT", 0) == 13        # 表位标注
        # 16 = 3 个柜体���框 + 13 个燃气方框；另 2 个来自 ACAD 预定义块
        assert res.block_entity_counts.get("LWPOLYLINE", 0) == 18
        assert res.total_entities > 40

    def test_block_layers_expose_water_gas_split(self):
        """Module B 靠 block 内的图层清单区分 water / gas 分组。"""
        res = inspect_dxf(DXF_DESIGN)
        for code in ("CP-TRI-1x3", "CP-QUAD-2x2", "CP-SIX-2x3"):
            layers = res.block_layers[code]
            assert "WATER_METER" in layers
            assert "GAS_METER" in layers
            assert "CUPBOARD" in layers

    def test_block_with_empty_modelspace_is_not_degraded(self):
        """modelspace 空但 block 有内容 → 正常，不该报 degraded。"""
        res = inspect_dxf(DXF_DESIGN)
        assert res.entity_counts.get("LWPOLYLINE", 0) == 0   # modelspace 确实空
        assert res.integrity == "ok"

    def test_anonymous_blocks_are_excluded(self):
        """匿名块（*Model_Space 等）是系统定义，不该计入设计内容。"""
        res = inspect_dxf(DXF_DESIGN)
        assert not any(k in res.block_entity_counts for k in ("*Model_Space", "*Paper_Space"))

    def test_dxf_bypasses_dwg_backends(self):
        """.dxf 直接送 inspect_dxf，不该调外部进程。"""
        res = parse_dwg(DXF_DESIGN)
        assert res.ok
        assert res.backend == DwgBackend.NONE

    def test_missing_file_reports_error(self):
        res = parse_dwg("no_such_file.dxf")
        assert not res.ok
        assert res.error


# ---------------------------------------------------------------- 反向夹具


class TestCorruptDwgIsRejected:
    """本文件最重要的部分。

    `neg_truncated_by_dxf2dwg.dwg` 是用 LibreDWG 的 `dxf2dwg` 从
    `cupboard_library_design.dxf` 转出来的，**写出端有损**：图层名被截断成
    首字母、modelspace 几何全丢，但**退出码是 0**。

    修复前 `inspect_dxf()` 对它返回 `ok=True`，会把垃圾数据当柜型库入库。
    """

    def test_corrupt_dxf_from_dxf2dwg_is_rejected(self, tmp_path):
        _need_dwgread()
        res = parse_dwg(DWG_NEGATIVE, tmp_path)
        assert not res.ok, "静默损坏的 DWG 必须被拒绝，不能返回 ok=True"
        assert res.integrity == "corrupt"
        assert res.error and "完整性校验失败" in res.error

    def test_corrupt_result_carries_diagnostic_notes(self, tmp_path):
        _need_dwgread()
        res = parse_dwg(DWG_NEGATIVE, tmp_path)
        assert res.integrity_notes
        joined = " ".join(res.integrity_notes + res.warnings)
        # 拦截原因：block 遍历生效后，该样本的 modelspace 为空但 block 内
        # 仍有 45 个实体，因此不是被「空图纸」规则拦下，而是被
        # 「图层名全部截断成单字母」规则拦下 —— 这正是 dxf2dwg 的签名症状。
        assert "截断" in joined

    def test_corrupt_result_never_reports_ok(self, tmp_path):
        """无论走哪个后端，corrupt 产物一律 ok=False。"""
        _need_dwgread()
        for target in (DWG_NEGATIVE,):
            res = parse_dwg(target, tmp_path)
            assert res.integrity != "ok"
            assert res.ok is False

    def test_corrupt_error_excludes_informational_notes(self, tmp_path):
        """error 里不能混入「属正常」这类说明性提示。

        实测踩坑：早期版本把所有 notes 拼进 error，产出过
        「…block 定义内有 45 个实体 —— 柜型库通常如此…属正常。；
        图层名疑似被截断…」这种自相矛盾的用户可见消息。
        """
        _need_dwgread()
        res = parse_dwg(DWG_NEGATIVE, tmp_path)
        assert "属正常" not in res.error
        assert "截断" in res.error
        # 说明性提示仍保留在 notes 里，只是不进 error
        assert any("柜型库通常如此" in n for n in res.integrity_notes)


# ---------------------------------------------------------------- 真实 DWG


class TestRealAutoCadDwg:
    """真实 AutoCAD 文件端到端 —— 这是 DWG 链路第一次有真样本验证。"""

    @pytest.mark.parametrize(
        "name", ["dwg_real_acad2018.dwg", "dwg_real_acad2000.dwg"]
    )
    def test_real_dwg_passes_integrity(self, name, tmp_path):
        res = parse_dwg(_need_real(name), tmp_path)
        assert res.ok
        assert res.integrity == "ok"
        assert res.error is None

    def test_real_dwg_yields_layers_and_blocks(self, tmp_path):
        res = parse_dwg(_need_real("dwg_real_acad2018.dwg"), tmp_path)
        assert "Tavolo 2" in res.layers
        assert res.blocks

    def test_real_dwg_yields_geometry(self, tmp_path):
        res = parse_dwg(_need_real("dwg_real_acad2018.dwg"), tmp_path)
        assert res.total_entities >= 30
        assert res.entity_counts.get("LWPOLYLINE", 0) > 0
        assert res.entity_counts.get("INSERT", 0) > 0

    def test_dwg_2018_and_2000_agree_on_structure(self, tmp_path):
        """同一图纸的 2000/2018 两版应解析出同样的设计图层与实体结构。

        这是交叉验证：若两版结果差异巨大，说明转换器对某版本支持不良。
        比较时排除系统图层（AutoCAD 各版本对 `ADSK_SYSTEM_LIGHTS` 的
        `*` 前缀处理不同，这是源文件差异而非转换器缺陷）。
        """
        def design_layers(r):
            return {
                ly for ly in r.layers
                if not ly.startswith("*") and "ADSK" not in ly and ly != "Defpoints"
            }

        a = parse_dwg(_need_real("dwg_real_acad2018.dwg"), tmp_path / "a")
        b = parse_dwg(_need_real("dwg_real_acad2000.dwg"), tmp_path / "b")
        assert design_layers(a) == design_layers(b)
        assert "Tavolo 2" in design_layers(a) and "Tavolo 2" in design_layers(b)
        assert a.entity_counts == b.entity_counts
        assert a.block_entity_counts == b.block_entity_counts

    def test_real_dwg_uses_libredwg_backend(self, tmp_path):
        """本环境无 ODA，应落到 LibreDWG 兜底并明确告知用户。"""
        res = parse_dwg(_need_real("dwg_real_acad2018.dwg"), tmp_path)
        if shutil.which("ODAFileConverter"):
            assert res.backend == DwgBackend.ODA
        else:
            assert res.backend == DwgBackend.LIBREDWG
            assert any("LibreDWG" in w for w in res.warnings)

    def test_proxy_entity_warning_is_always_emitted(self, tmp_path):
        """代理实体检查必须始终留下痕迹（报告第 03 章硬要求）。"""
        res = parse_dwg(_need_real("dwg_real_acad2018.dwg"), tmp_path)
        assert res.proxy_entity_lost is not None or any(
            "ACAD_PROXY_ENTITY" in w for w in res.warnings
        )


# ---------------------------------------------------------------- 降级链


def test_no_backend_yields_actionable_error(tmp_path, monkeypatch):
    """所有后端都不可用时，必须给出可执行指引而不是静默失败。"""
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    dummy = tmp_path / "x.dwg"
    dummy.write_bytes(b"AC1032" + b"\0" * 64)
    res = parse_dwg(dummy, tmp_path / "wd")
    assert not res.ok
    assert res.backend == DwgBackend.NONE
    assert res.integrity == "corrupt"
    assert any("ODAFileConverter" in a for a in res.warnings)
    assert any("dwgread" in a for a in res.warnings)


def test_garbage_file_is_rejected_not_crashed(tmp_path):
    """随机字节不能把解析器打崩，也不能返回 ok。"""
    junk = tmp_path / "junk.dwg"
    junk.write_bytes(b"\x00\x01\x02not-a-dwg-at-all" * 40)
    res = parse_dwg(junk, tmp_path / "wd")
    assert not res.ok
    assert res.integrity == "corrupt"
