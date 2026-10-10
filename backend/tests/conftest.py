"""pytest 全局配置。

**唯一数据源的真实 DWG fixture 在这里注册**
---------------------------------------------
``tests/fixtures/real_dwg.py`` 里定义了 ``real_dxf`` / ``real_dwg_path``
两个 session 级 fixture，以及 Ground Truth 常量。fixture 必须通过
``conftest.py``（或 ``pytest_plugins``）注册才会生效 —— 直接
``from tests.fixtures.real_dwg import real_dxf`` 只是拿到一个函数对象，
pytest 并不会把它当作 fixture，所以测试会全部 skip。

要用真实 DWG 的测试文件里既 import 常量，也 import 这两个 fixture 名。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让``from tests.fixtures.real_dwg import ...`` 在所有测试文件里可用
_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
for _p in (str(_ROOT), str(_TESTS.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 把真实 DWG fixture 注册进 pytest
pytest_plugins = ["tests.fixtures.real_dwg"]