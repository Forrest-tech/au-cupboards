"""CLI 冒烟测试。

为什么需要：`python -m app.cli` 写进了 README，用户会照着敲。
实测踩坑（本文件存在的原因）——CLI 初版引用了一批**不存在的字段**：
  · `Unit.area_dry_m2`→ 实际是 `Unit.area_m2`
  · `CupboardVariant.code` → 实际是 `variant_code`
  · `CupboardVariant.width/height/depth` → 实际是 `w/h/d`
  · `CupboardVariant.cols/rows` → 实际是 `layout_cols/layout_rows`
  · `Selection.job_id` → 该列不存在，Selection 只经 `unit_id` 关联
  · `ParseJob.file_path`→ 该列不存在（路径在 DrawingVersion 上）
  · `JobStatus.DONE` → 枚举实际是 QUEUED/RUNNING/NEEDS_REVIEW/CONFIRMED/FAILED

这些错误只在运行时才暴露，因此必须逐个命令实跑验证。
"""
from __future__ import annotations

import pytest

from app.cli import main


class TestHelp:
    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as e:
            main(["--help"])
        assert e.value.code == 0

    def test_no_subcommand_is_an_error(self):
        with pytest.raises(SystemExit) as e:
            main([])
        assert e.value.code != 0


class TestHealth:
    def test_health_runs(self, capsys):
        assert main(["health"]) == 0
        out = capsys.readouterr().out
        assert "Python" in out
        assert "DWG" in out

    def test_health_reports_rule_version(self, capsys):
        main(["health"])
        assert "解析规则版本" in capsys.readouterr().out


class TestVariants:
    """``variants`` 必须真的把库里的柜型列出来。

    实测踩坑：这两个用例原先直接 ``main(["variants"])`` 然后断言
    ``CP-TRI-1x3`` 在输出里。本地开发时``var/aucup.db`` 早就被灌过种子，
    所以一直是绿的；但 ``var/`` 在 .gitignore 里，**干净 clone / CI 上
    数据库根本不存在** —— 断言必然失败。这里改成用例自己先把种子灌进
    一个临时库，让断言不再依赖机器状态。
    """

    @pytest.fixture(autouse=True)
    def _seeded(self):
        from app.cli import _pipeline

        # 必须走 pipeline.ensure_variants() —— seed_variants() 只返回内存对象，
        # 真正落库的是 ensure_variants（幂等，已存在则跳过）。
        _pipeline().ensure_variants()
        yield

    def test_variants_lists_seed_data(self, capsys):
        assert main(["variants"]) == 0
        out = capsys.readouterr().out
        # 种子柜型库必然包含这几个（若字段名再变，这里会先崩）
        assert "CP-TRI-1x3" in out
        assert "CP-QUAD-2x2" in out
        assert "表位" in out

    def test_variants_renders_dimensions(self, capsys):
        """尺寸列必须用 w/h/d（不是 width/height/depth）。"""
        main(["variants"])
        out = capsys.readouterr().out
        assert "×" in out# 形如 1200×800×200
        assert "None" not in out   # 字段错会导致 None 混入表格

    def test_variants_on_empty_library_prints_guidance(self, capsys):
        """空库不该崩，且必须告诉用户怎么灌种子。"""
        from app.models.entities import CupboardVariant

        from app.cli import _pipeline
        from sqlalchemy.orm import Session

        with Session(_pipeline().engine) as s:
            s.query(CupboardVariant).delete()
            s.commit()
        assert main(["variants"]) == 0
        out = capsys.readouterr().out
        assert "柜型库为空" in out and "种子" in out


class TestRun:
    def test_missing_file_returns_error_code(self, capsys):
        assert main(["run", "/nonexistent/plan.pdf"]) == 2
        assert "不存在" in capsys.readouterr().err

    def test_non_pdf_is_rejected_with_guidance(self, capsys, tmp_path):
        f = tmp_path / "plan.dwg"
        f.write_bytes(b"AC1032")
        assert main(["run", str(f)]) == 2
        err = capsys.readouterr().err
        assert "PDF" in err
        assert "kind=cupboard" in err      # 必须告诉用户 DWG 该走哪条路


class TestUnits:
    def test_unknown_job_returns_error(self, capsys):
        assert main(["units", "999999"]) == 2
        assert "找不到" in capsys.readouterr().err
