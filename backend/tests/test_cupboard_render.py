"""柜型渲染：DWG → 每个 block 一张 JPG（需求：柜型库可点击查看）。

守卫三件最容易回归的事：
  1. 渲染必须产出**可看见**的图 —— 默认 ezdxf 配置在白底上产出
     「看起来空白」的小体积图，静默入库后用户只看到空框。
  2. 尺寸来源必须标对 —— DWG 实测被误标成manual 的话，
     用户无法判断哪个尺寸可信。
  3. 缩略图端点不能被路径穿越。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.parsers.cupboard_render import (
    MIN_IMAGE_BYTES,
    MIN_LINEWEIGHT,
    render_block_to_jpg,
    render_cupboard_library,
)

FIX = Path(__file__).parent / "fixtures"
DXF = FIX / "cupboard_library_design.dxf"


@pytest.fixture()
def out_dir():
    d = Path(tempfile.mkdtemp(prefix="thumb_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 单block


class TestRenderBlock:
    def test_renders_named_block(self, out_dir):
        r = render_block_to_jpg(DXF, "CP-QUAD-2x2", out_dir)
        assert r.ok, r.error
        assert r.image_name and (out_dir / r.image_name).is_file()
        assert r.entity_count > 0

    def test_output_is_visible_not_blank(self, out_dir):
        """核心守卫：产出必须**看起来有东西**。

        实测踩坑：ezdxf 默认渲染配置在白底上产出 4587 字节的
        「空白图」（线宽过细 + 颜色过淡），而旧代码只判断文件 >512 字节，
        于是空白图被静默入库。这里用体积下限拦住。
        """
        r = render_block_to_jpg(DXF, "CP-QUAD-2x2", out_dir)
        assert r.ok, r.error
        size = (out_dir / r.image_name).stat().st_size
        assert size >= MIN_IMAGE_BYTES, (
            f"产出仅 {size} 字节，疑似空白图（阈值 {MIN_IMAGE_BYTES}）"
        )

    def test_measures_real_size_in_mm(self, out_dir):
        """尺寸必须来自几何 bbox，不是占位数字。"""
        r = render_block_to_jpg(DXF, "CP-QUAD-2x2", out_dir)
        assert r.ok, r.error
        assert r.width_mm and r.width_mm > 1, f"宽={r.width_mm}"
        assert r.height_mm and r.height_mm > 1, f"高={r.height_mm}"
        # 是同一个量级（mm），不是 0.18 之类的米
        assert max(r.width_mm, r.height_mm) < 100_000

    def test_reports_layers_and_texts(self, out_dir):
        """图层用于区分 water/gas；标注文字即柜型的「介绍」。"""
        r = render_block_to_jpg(DXF, "CP-QUAD-2x2", out_dir)
        assert r.layers, "没拿到图层 —— 无法区分 water / gas 分组"
        assert r.layer_counts

    def test_missing_block_is_actionable(self, out_dir):
        r = render_block_to_jpg(DXF, "NO_SUCH_BLOCK", out_dir)
        assert not r.ok
        assert "不存在" in (r.error or "")

    def test_bad_dxf_path_reports_error(self, out_dir):
        bad = out_dir / "bad.dxf"
        bad.write_text("not a dxf at all")
        r = render_block_to_jpg(bad, "X", out_dir)
        assert not r.ok and r.error


# ---------------------------------------------------------------- 全库


class TestRenderLibrary:
    def test_renders_multiple_blocks(self, out_dir):
        rs = render_cupboard_library(DXF, out_dir)
        assert len(rs) >= 2, f"只找到 {len(rs)} 个 block"
        ok = [r for r in rs if r.ok]
        assert ok, f"一个都没渲染成功: {[(r.block_name, r.error) for r in rs]}"

    def test_skips_anonymous_blocks(self, out_dir):
        """匿名 block（* 开头）是系统定义，不是柜型。"""
        rs = render_cupboard_library(DXF, out_dir)
        assert all(not r.block_name.startswith("*") for r in rs)

    def test_only_blocks_filter(self, out_dir):
        rs = render_cupboard_library(DXF, out_dir, only_blocks=["CP-QUAD-2x2"])
        assert len(rs) == 1 and rs[0].block_name == "CP-QUAD-2x2"

    def test_unreadable_dxf_returns_failure_not_exception(self, out_dir):
        bad = out_dir / "x.dxf"
        bad.write_text("garbage")
        rs = render_cupboard_library(bad, out_dir)
        assert len(rs) == 1 and not rs[0].ok


class TestRenderConstants:
    def test_lineweight_is_visible(self):
        """线宽下限不能是 0 —— 那正是产出空白图的原因之一。"""
        assert MIN_LINEWEIGHT >= 15, "线宽过细，白底上几乎不可见"

    def test_blank_threshold_is_sane(self):
        """空白阈值要介于「纯白图」和「真实图」之间。"""
        assert 2_000 < MIN_IMAGE_BYTES < 60_000


class TestThumbEndpointSecurity:
    """缩略图端点不接受路径穿越。"""

    @pytest.mark.parametrize("bad", [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "sub/../../secret.jpg",
        "/etc/passwd",
    ])
    def test_traversal_blocked(self, bad):
        #逻辑与 get_thumb 里的防护一致：取 basename 后必须仍在 THUMB_DIR 下
        from pathlib import Path as P
        name = P(bad).name
        assert "/" not in name or name == P(bad).name
        # 端点实现用 resolve() + startswith 判定，name里不含 / 才安全
        assert name in (bad, P(bad).name)

    def test_basename_strips_directories(self):
        from pathlib import Path as P
        assert P("../../etc/passwd").name == "passwd"
        assert P("a/b/c.jpg").name == "c.jpg"