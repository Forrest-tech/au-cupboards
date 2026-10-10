"""DWG 解析链路测试（需求 4 的 P0 风险项）。

验证三件事：
  1. 真实 AutoCAD DWG 能走通 DWG → DXF → ezdxf 降级链
  2. **静默数据损坏会被拦截**（本文件存在的主要理由）
  3. 后端探测与降级顺序符合报告约定

背景：本轮 LibreDWG 0.13.3 源码编译成功（dwgread 0.13.3），
DWG 从「无法验证」变成「已用真实文件端到端验证」。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.parsers import dwg
from app.parsers.dwg import (
    DwgBackend,
    LOCAL_BIN,
    ODA_APP_PATHS,
    _dwgread_exe,
    _oda_candidates,
    _oda_exe,
    assess_integrity,
    convert_with_libredwg,
    convert_with_oda,
    detect_backends,
    inspect_dxf,
    parse_dwg,
)

FIX = Path(__file__).parent / "fixtures"
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


class TestMacosBackendDiscovery:
    """macOS 上「明明装了却报未安装」的回归守卫。

    实测坑：ODA File Converter 在 macOS 装完是 .app 包，真实可执行文件在
    /Applications/ODAFileConverter.app/Contents/MacOS/ —— 该目录不在 PATH，
    shutil.which 必然返回 None，于是装好的 ODA 被当成没装。
    """

    def test_oda_candidates_include_app_bundle(self):
        """候选表必须含 macOS .app 包内路径。

        不能断言 _oda_candidates() 的返回值 —— 它只返回真实存在的文件，
        在没装 ODA 的机器上返回空列表是正确的。
        """
        assert any("ODAFileConverter.app/Contents/MacOS" in p for p in ODA_APP_PATHS), \
            "候选表漏了 macOS .app 包内路径 —— 装了 ODA 也会报未安装"

    def test_oda_app_found_outside_path(self, tmp_path, monkeypatch):
        """核心回归：.app 装好但不在 PATH，也必须能被发现。"""
        app = tmp_path / "Applications" / "ODAFileConverter.app" / "Contents" / "MacOS" / "ODAFileConverter"
        app.parent.mkdir(parents=True)
        app.write_text("#!/bin/sh\n")
        app.chmod(0o755)

        monkeypatch.setattr(dwg, "ODA_APP_PATHS", (str(app),))
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        got = dwg._oda_exe()
        assert got == str(app), f".app 内的 CLI 未被探测到（返回 {got!r}）"
        assert detect_backends()["oda_file_converter"] == str(app)

    def test_oda_candidates_are_absolute(self):
        """候选必须是绝对路径 —— 靠 which 拿到的相对路径在 subprocess 里会失效。"""
        for c in _oda_candidates():
            assert c.startswith("/"), f"候选路径不是绝对路径: {c}"

    def test_oda_candidates_empty_when_nothing_installed(self, tmp_path, monkeypatch):
        """什么都没装时返回空列表 —— 不能把不存在的路径当成已安装。"""
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        monkeypatch.setattr(Path, "is_file", lambda _s: False)
        assert _oda_candidates() == []
        assert _oda_exe() is None

    def test_detect_backends_reports_none_when_absent(self, tmp_path, monkeypatch):
        """detect_backends 必须给出 None，而不是空串/假路径。"""
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        monkeypatch.setattr(Path, "is_file", lambda _s: False)
        b = detect_backends()
        assert set(b) == {"oda_file_converter", "dwgread", "oda_bin"}
        assert all(v is None for v in b.values()), f"未安装时不应报路径: {b}"

    def test_dwgread_falls_back_to_local_bin(self, tmp_path, monkeypatch):
        """免 brew 脚本装到 ~/.local/bin，不在 PATH 也必须能找到。"""
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        fake = bin_dir / "dwgread"
        fake.write_text("#!/bin/sh\necho dwgread 0.13.3\n")
        fake.chmod(0o755)

        monkeypatch.setattr(dwg, "LOCAL_BIN", str(bin_dir))
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        got = _dwgread_exe()
        assert got == str(fake), f"~/.local/bin/dwgread 未被探测到（返回 {got!r}）"

    def test_dwgread_prefers_path_over_local(self, tmp_path, monkeypatch):
        """PATH 里若有 dwgread，应优先用它（用户自己装的版本）。"""
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "dwgread").write_text("#!/bin/sh\n")
        (bin_dir / "dwgread").chmod(0o755)

        monkeypatch.setattr(dwg, "LOCAL_BIN", str(bin_dir))
        monkeypatch.setattr(shutil, "which",
                            lambda n: "/usr/local/bin/" + n if n == "dwgread" else None)
        assert _dwgread_exe() == "/usr/local/bin/dwgread"

    def test_no_false_positive_when_absent(self, tmp_path, monkeypatch):
        """什么都没装时必须返回 None —— 不能把不存在的路径当成已安装。"""
        monkeypatch.setattr(dwg, "LOCAL_BIN", str(tmp_path / "nope"))
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        assert _dwgread_exe() is None

    def test_convert_error_mentions_script(self, tmp_path, monkeypatch):
        """报错文案要指向免 brew 方案 —— 用户的 brew 就是坏的。"""
        monkeypatch.setattr(dwg, "LOCAL_BIN", str(tmp_path / "nope"))
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        dxf, err = convert_with_libredwg(
            Path("/nonexistent.dwg"), Path(tempfile.mkdtemp()))
        assert dxf is None
        assert err and "install_dwg_backend.sh" in err, \
            f"dwgread 缺失的报错没有给出可执行的替代方案: {err!r}"

    def test_oda_convert_error_mentions_app_path(self, tmp_path, monkeypatch):
        """ODA 缺失的报错要点明 .app 位置，否则用户反复检查 PATH 也没用。"""
        monkeypatch.setattr(dwg, "ODA_APP_PATHS", (str(tmp_path / "x"),))
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        monkeypatch.setattr(Path, "is_file", lambda _s: False)
        dxf, err = convert_with_oda(Path("/nonexistent.dwg"), Path(tempfile.mkdtemp()))
        assert dxf is None
        assert err and "/Applications/" in err, f"报错未提示 .app 实际位置: {err!r}"

    def test_install_script_exists_and_is_valid_bash(self):
        """免 brew 安装脚本必须存在且语法正确 —— 报错文案指向它，
        它不能是空头支票。"""
        p = (Path(__file__).resolve().parents[2] / "scripts" / "install_dwg_backend.sh")
        assert p.is_file(), "缺少 scripts/install_dwg_backend.sh"
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"脚本语法错误: {r.stderr}"
        body = p.read_text()
        assert "libredwg" in body.lower()
        assert "ODAFileConverter.app" in body, "脚本没处理 macOS .app 已装的情况"

    def test_install_script_disables_werror(self):
        """必须关掉 -Werror —— 这是 macOS 专属坑。

        实测：用户在 macOS 11 编译 LibreDWG 0.13.3 报
          error: format specifies type 'unsigned short' but the
          argument has type 'BITCODE_BL' (aka 'unsigned int')
          [-Werror,-Wformat]      ← src/dwg.spec 的宏展开导致
        Apple clang 的 -Wformat 比 GCC 严格，LibreDWG 默认又开
        -Werror，于是警告升级为错误，print.c 直接编不过。
        Linux/gcc 上不报，所以开发机测不出来。
        """
        p = (Path(__file__).resolve().parents[2] / "scripts" / "install_dwg_backend.sh")
        body = p.read_text()
        assert "--disable-werror" in body, \
            "configure 没加 --disable-werror —— Apple clang 上必然编译失败"
        # make 阶段还要再兜一层，因为 Makefile 可能另行追加 -Werror
        assert "-Wno-error" in body, \
            "make 阶段没有 -Wno-error 兜底"
        assert "-Wno-format" in body, \
            "没有 -Wno-format —— 报错全是 printf 格式串类型不匹配"

    def test_install_script_reuses_download(self):
        """重跑不应重新下载 20MB —— 编译失败时用户最烦的是重下。"""
        p = (Path(__file__).resolve().parents[2] / "scripts" / "install_dwg_backend.sh")
        body = p.read_text()
        # 只看代码行 —— 注释里解释「为什么不用 mktemp」也会命中关键词
        code = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("#"))
        assert "mktemp" not in code, \
            "工作目录不应每次新建，否则重跑要重下 20MB 源码"
        assert "-C -" in code, "curl 缺断点续传（-C -），中断后重跑要从头下"

    def test_install_script_vars_have_explicit_braces(self):
        """变量紧邻中文/全角标点时必须写成 ${VAR}。

        实测：用户在 macOS 跑脚本 12 行后崩在
          line99: TARBALL?: unbound variable
        原因是 macOS 自带 bash 3.2 在中文 locale 下会把
        `$TARBALL（` 里的全角（ 算进变量名，于是去找一个叫
        `TARBALL（` 的变量 —— 自然 unbound。
        `bash -n` 查不出这个错（语法合法），只有真跑才炸，
        而真跑要先下 20MB，所以在 Linux CI 上完全测不出来。
        """
        p = (Path(__file__).resolve().parents[2] / "scripts" / "install_dwg_backend.sh")
        code = "\n".join(
            l for l in p.read_text().splitlines() if not l.lstrip().startswith("#")
        )
        bad = re.findall(r"\$[A-Za-z_][A-Za-z_0-9]*[^\x00-\x7f]", code)
        assert not bad, (
            "变量引用紧邻非 ASCII 字符，macOS bash 3.2 会把中文算进变量名"
            f"（应改用 ${{VAR}} 显式边界）: {bad}"
        )


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
#
# 这一组全部跑在**真实 DWG 转出来的 DXF** 上。
# 早先这里用的是一份自建合成 DXF（``cupboard_library_design.dxf``），
# 断言的是「4 个自造图层 + 3 个自造 block」—— 那些结构和真实图纸差得很远，
# 留着只会给人「已经在图纸上验证过」的错觉。合成样本已删除。


class TestInspectDxf:
    def test_real_dxf_reads_ok(self, real_dxf):
        """真实 DXF 必须能直读，且完整性判定为 ok。"""
        res = inspect_dxf(real_dxf)
        assert res.ok
        assert res.integrity == "ok", f"完整性判定异常：{res.integrity_notes}"

    def test_real_dxf_exposes_cabinet_layers(self, real_dxf):
        """真实图纸的柜框图层必须能被读出。

        实测柜体长LINE分布在 ``1CO`` / ``2CONC`` / ``HWAT___5CS`` 等图层，
        柜型识别正是靠这些图层的长线配对出柜体矩形，所以读不出来就等于
        柜型识别无从下手。
        """
        res = inspect_dxf(real_dxf)
        assert res.ok
        joined = " ".join(res.layers).upper()
        for kw in ("1CO", "2CONC", "HWAT"):
            assert kw in joined, f"缺柜框图层关键字 {kw}：{res.layers[:20]}"

    def test_real_dxf_counts_modelspace_entities(self, real_dxf):
        """modelspace 必须有足量实体（真实样本约 3420 个）。"""
        res = inspect_dxf(real_dxf)
        assert res.entity_counts.get("INSERT", 0) > 100, res.entity_counts
        assert res.entity_counts.get("LINE", 0) > 100, res.entity_counts
        assert res.total_entities > 1000

    def test_real_dxf_counts_entities_inside_blocks(self, real_dxf):
        """必须遍历 block 定义才能数全表位。

        真实样本的``gas meter 1`` / ``water meter v`` 都是 block，
        只统计 modelspace 会把柜内表位全部漏掉。
        """
        res = inspect_dxf(real_dxf)
        assert res.block_entity_counts, "未统计到任何 block 内容"
        assert sum(res.block_entity_counts.values()) > 100

    def test_block_layers_expose_water_gas_split(self, real_dxf):
        """block 清单里必须能区分 water / gas 表。"""
        res = inspect_dxf(real_dxf)
        joined = " ".join(res.blocks).lower()
        assert "gas" in joined or "water" in joined, res.blocks[:20]

    def test_anonymous_blocks_are_excluded(self, real_dxf):
        """匿名块（*Model_Space 等）是系统定义，不该计入设计内容。"""
        res = inspect_dxf(real_dxf)
        assert not any(
            k in res.block_entity_counts for k in ("*Model_Space", "*Paper_Space")
        )

    def test_dxf_bypasses_dwg_backends(self, real_dxf):
        """.dxf 直接送 parse_dwg，不该调外部进程。"""
        res = parse_dwg(real_dxf)
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


class TestSetupScriptPortGuard:
    """setup.sh 必须在启动前检出端口占用。

    实测踩坑：用户 pull 到新版后跑 setup.sh，末尾报
      ERROR: [Errno 48] address already in use
    但脚本前面所有自检都正常、整体看起来"成功"退出了，
    于是浏览器仍访问**旧进程** —— 旧 JS 提交旧字段给新代码，
    表现为「入库 500」+「柜型库点了没反应」，
    而真正的提示语淹没在日志最后一行，极难定位。
    """
    @staticmethod
    def _setup() -> str:
        return (Path(__file__).resolve().parents[2] / "setup.sh").read_text()

    def test_setup_checks_port_before_start(self):
        body = self._setup()
        seg = body.split("# ---- 启动 ----")[-1]
        assert "lsof" in seg, "启动段缺少端口占用检测"
        i_check = seg.index("lsof")
        i_start = seg.index("exec python -m uvicorn")
        assert i_check < i_start, "端口检测必须发生在启动之前，否则仍会静默失败"

    def test_setup_explains_how_to_fix(self):
        """光说'被占用'没用，必须给出可执行的 kill 命令。"""
        seg = self._setup().split("# ---- 启动 ----")[-1]
        assert "kill" in seg, "未给出 kill 命令"
        assert "lsof -nP" in seg, "未给出查询占用者的命令"
        assert "PORT=" in seg, "未给出换端口的替代方案"
