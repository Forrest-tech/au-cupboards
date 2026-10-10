"""共享测试夹具：**只用真实 DWG，不再用合成样本**。

历史教训（写在这里，免得以后有人又走回头路）
------------------------------------------------
早期版本有个 ``make_cabinet_sample.py``，用 ezdxf 画「闭合矩形柜体 +
闭合矩形表位框」的假 DWG，然后所有柜型识别逻辑都在这份假数据上调参。
结果假数据的结构和真实图纸差得很远：

===================  =====================  =====================
                     合成样本（假）          真实图纸
===================  =====================  =====================
柜体外框             闭合 LWPOLYLINE         **4 条跨图层 LINE**
表位                 闭合矩形                INSERT block（块内
                                             顶点用绝对坐标，
                                             ``insert`` 点偏 6500mm）
柜框对齐             上下边与侧墙严格对齐    侧墙在上下边端点**内侧**
上下边长度           上下完全一致            上下差几十毫米
相邻柜               各自分开               **共用横线**
===================  =====================  =====================

在假数据上「配对成功率 100%」的规则，真实图纸上一个都配不出来。

所以现在：**柜型识别的所有测试都跑在真实 DWG 上**，Ground Truth 是
用户人工统计并复核确认的 23 套柜（见 :data:`GROUND_TRUTH`）。

真实 DWG 太大（1.4MB DWG → 6.2MB DXF）不适合直接进 git，所以：
- ``samples/`` 存DWG（已入库）；
- 测试运行时调``dwg2dxf`` 转成 DXF，缓存在临时目录，只转一次。

若机器上没有 LibreDWG，依赖真实 DWG 的测试会 **skip**（不fail），
保证 CI 上没有 DWG 工具链也能跑其余测试。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

#: 仓库根目录
#:
#: 本文件位于 ``<repo>/backend/tests/fixtures/real_dwg.py``，
#: 所以 parents[0]=fixtures, [1]=tests, [2]=backend, [3]=<repo>。
REPO_ROOT = Path(__file__).resolve().parents[3]

#: **唯一**真实数据源：用户提供的柜型库图纸
SAMPLE_DWG = REPO_ROOT / "samples" / (
    "Cold and hot water and gs meter cupboard detail 1.dwg"
)

#: 缓存目录：真实 DXF 有6MB，放进 git 太重，运行时转换
_CACHE = REPO_ROOT / ".pytest_cache" / "real_dxf"


#: **Ground Truth —— 用户人工统计的 23 套柜**
#:
#: 用户原话（2026-10）：图纸里共 23 套柜，按户数分布如下。
#: 关键约束：**所有水表、气表外观样式完全一样**，不能靠表的外观特征
#: 区分柜型，只能靠数量 + 空间排布。
#:
#: 历史修正：最初人工统计记的是 22 柜 / 191 套（7 Units 记 2 套），
#: 解析器实测到 23 柜 / 198 套且多出的那个 7 Units 柜有独立上下边线、
#: 真实 DIMENSION 标注、7 gas + 7 water，不与另外两个 7 Units 柜重叠。
#: 用户复核原始 DWG 后确认 **7 Units 确实是 3 套**，人工统计漏数一柜。
GROUND_TRUTH: dict[int, int] = {
    3: 2, 4: 1, 5: 2, 6: 2, 7: 3, 8: 1,
    9: 2, 10: 2, 11: 2, 12: 2, 13: 3, 14: 1,
}

#: Ground Truth 的柜型总数与总套数（用于汇总断言）
GT_CABINETS = sum(GROUND_TRUTH.values())          # 23
GT_UNITS = sum(n * k for n, k in GROUND_TRUTH.items())  # 198


def _dwg2dxf() -> str | None:
    """找dwg2dxf（LibreDWG）。找不到返回 None。"""
    return shutil.which("dwg2dxf")


@pytest.fixture(scope="session")
def real_dwg_path() -> Path:
    """真实 DWG 路径。不存在则 skip。"""
    if not SAMPLE_DWG.exists():
        pytest.skip(f"缺少真实 DWG 样本：{SAMPLE_DWG}")
    return SAMPLE_DWG


@pytest.fixture(scope="session")
def real_dxf(real_dwg_path: Path) -> Path:
    """把真实 DWG 转成 DXF 并缓存，供多个测试共享。

    只转一次：1.4MB DWG → 6.2MB DXF，转换约 3 秒。
    """
    if not _dwg2dxf():
        pytest.skip("未安装 dwg2dxf（LibreDWG），跳过真实 DWG 相关测试")
    _CACHE.mkdir(parents=True, exist_ok=True)
    out = _CACHE / (real_dwg_path.stem.replace(" ", "_") + ".dxf")
    if out.exists() and out.stat().st_size > 1_000_000:
        return out
    res = subprocess.run(
        [_dwg2dxf(), "-o", str(out), str(real_dwg_path)],
        capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0 or not out.exists():
        pytest.skip(f"dwg2dxf 转换失败：{res.stderr[:300]}")
    return out