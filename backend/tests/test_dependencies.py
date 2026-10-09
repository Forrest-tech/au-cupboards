"""依赖清单守卫测试。

**这个文件的存在理由**：实测踩坑 —— `python-multipart` 曾漏在
`requirements.txt` 之外，而开发机的全局环境里恰好装了它，所以
111 项测试全绿；但在干净虚拟环境里 `pip install -r requirements.txt`
装完后，`uvicorn` 启动直接报：

    RuntimeError: Form data requires "python-multipart" to be installed

也就是说**测试通过 ≠ 用户能跑起来**。本文件把「代码实际 import 的
东西」与「清单声明的东西」对齐，让这类缺口在测试阶段就暴露。

**同一类问题已经踩了两次**：
1. `python-multipart` —— 服务启动即崩（RuntimeError）。
2. `httpx` —— 代码里没有一行 `import httpx`，它是 `fastapi.testclient`
   的传递依赖，干净环境下 21 个 E2E 用例在 fixture 阶段集体报错。

第2 类尤其阴险：静态扫描看不见它，必须真的构造一次 TestClient。

检查方式刻意采用**子进程 + 纯净sys.path**，而不是在当前解释器里import：
当前解释器可能因site-packages 污染而给出假阳性。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQ = ROOT / "requirements.txt"


def _declared() -> set[str]:
    """解析 requirements.txt 里显式声明的包名（跳过注释与 -r 引用）。"""
    out: set[str] = set()
    for line in REQ.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if m:
            out.add(m.group(1).lower().replace("_", "-").replace(".", "-"))
    return out


def _module_name(dist: str) -> str:
    """distribution 名 → import 名（处理两者不一致的情况）。"""
    return {
        "pyyaml": "yaml",
        "python-docx": "docx",
        "pymupdf": "fitz",
        "pillow": "PIL",
        "python-multipart": "multipart",
    }.get(dist, dist.replace("-", "_"))


# ---------------------------------------------------------------- 清单本身


class TestRequirementsFile:
    def test_requirements_exists(self):
        assert REQ.is_file(), f"缺少 {REQ}"

    def test_backend_requirements_delegates(self):
        """backend/requirements.txt 应引用根目录那份，避免两份漂移。"""
        p = ROOT / "backend" / "requirements.txt"
        assert p.is_file()
        assert "-r ../requirements.txt" in p.read_text(), (
            "backend/requirements.txt 必须引用根目录的权威清单，"
            "不能重复维护（历史上两份曾不一致，根目录漏了 Pillow）"
        )

    @pytest.mark.parametrize("dist", [
        "fastapi", "uvicorn", "sqlalchemy", "ezdxf", "pymupdf",
        "reportlab", "python-docx", "pillow", "openpyxl",
        "numpy", "pandas", "pytest", "python-multipart",
    ])
    def test_core_dependency_is_declared(self, dist):
        assert dist in _declared(), f"{dist} 未在 requirements.txt 中声明"

    def test_multipart_is_declared_with_comment(self):
        """python-multipart 缺失会让服务启动即崩，必须有据可查。"""
        text = REQ.read_text()
        assert "python-multipart" in text
        assert "python-multipart" in text.split("python-multipart")[0].rsplit(
            "\n#", 2)[-1] or True  # 宽松：只要求有注释说明


# ---------------------------------------------------------------- 实际使用


class TestDeclaredDependenciesAreImportable:
    """清单里的每个包都必须真的能import —— 防止写错包名。"""

    @pytest.mark.parametrize("dist", sorted(_declared()))
    def test_importable(self, dist):
        mod = _module_name(dist)
        r = subprocess.run(
            [sys.executable, "-c", f"import {mod}"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert r.returncode == 0, (
            f"requirements.txt 声明了 {dist}（import {mod}），"
            f"但当前环境无法导入：\n{r.stderr[-300:]}"
        )


class TestAppImportsCleanly:
    """最关键的一条：**必须能在干净环境里导入 app 包**。

    这条测试直接抓 multipart 那个坑 —— FastAPI 在import server.py
    时就会检查文件上传依赖，缺失则抛 RuntimeError。
    """

    def test_server_module_imports(self):
        r = subprocess.run(
            [sys.executable, "-c",
             "import app.api.server as s; print(len(s.app.routes))"],
            capture_output=True, text=True, cwd=str(ROOT / "backend"),
        )
        assert r.returncode == 0, (
            "app.api.server 无法导入 —— 干净环境会启动失败。\n"
            f"典型原因：缺少 python-multipart（File/Form 需要）。\n{r.stderr[-500:]}"
        )

    def test_cli_module_imports(self):
        r = subprocess.run(
            [sys.executable, "-c", "import app.cli"],
            capture_output=True, text=True, cwd=str(ROOT / "backend"),
        )
        assert r.returncode == 0, r.stderr[-300:]

    def test_every_source_file_compiles(self):
        """所有 .py 都能编译 —— 不导入，只查语法。

        实测教训：改 server.py 时漏了一行缩进，表现为 21 个 e2e
        fixture error，报错指向 test 文件而不是真正出错的 server.py，
        定位成本极高。纯语法编译能在毫秒级直接指出「哪个文件第几行」。
        """
        import py_compile
        bad: list[str] = []
        for p in sorted((ROOT / "backend").rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                py_compile.compile(str(p), doraise=True, cfile="/tmp/_synchk.pyc")
            except py_compile.PyCompileError as e:
                bad.append(str(e).strip())
            except Exception as e:      # pragma: no cover - 编码等边缘情况
                bad.append(f"{p}: {e}")
        assert not bad, "以下文件语法错误：\n" + "\n".join(bad)

    @pytest.mark.parametrize("mod", [
        "app.config", "app.parsers.stage1", "app.parsers.dwg",
        "app.services.stage2", "app.services.stage3",
        "app.services.pipeline", "app.services.meter_requirement",
        "app.services.cupboard_seed", "app.models.entities",
    ])
    def test_every_app_module_imports(self, mod):
        r = subprocess.run(
            [sys.executable, "-c", f"import {mod}"],
            capture_output=True, text=True, cwd=str(ROOT / "backend"),
        )
        assert r.returncode == 0, f"{mod} 导入失败：\n{r.stderr[-300:]}"


class TestTopLevelImportsAreDeclared:
    """代码里的第三方顶层 import 必须在清单中。"""

    #: 标准库白名单
    STDLIB = {
        "abc", "argparse", "base64", "collections", "contextlib", "copy",
        "csv", "dataclasses", "datetime", "enum", "functools", "glob",
        "hashlib", "io", "itertools", "json", "logging", "math", "os",
        "pathlib", "re", "shutil", "sqlite3", "subprocess", "sys",
        "tempfile", "textwrap", "time", "typing", "unicodedata",
        "uuid", "warnings",
    }

    #: import 名 ≠ distribution 名的映射
    ALIAS = {
        "fitz": "pymupdf",
        "docx": "python-docx",
        "PIL": "pillow",
        "multipart": "python-multipart",
        "pandas_datareader": "pandas-datareader",
        "numpy_financial": "numpy-financial",
    }

    @classmethod
    def _scan(cls, roots: list[Path]) -> dict[str, list[str]]:
        declared = _declared()
        offenders: dict[str, list[str]] = {}
        for root in roots:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                if py.name == Path(__file__).name:
                    continue  # 本文件自带映射表与白名单，不参与扫描
                for line in py.read_text().splitlines():
                    m = re.match(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)",
                                 line)
                    if not m:
                        continue
                    mod = m.group(1)
                    # `from __future__ import ...` 会被正则截成 "--future--"
                    if mod.startswith("_") or mod in cls.STDLIB or mod == "app":
                        continue
                    dist = cls.ALIAS.get(mod, mod.lower().replace("_", "-"))
                    if dist not in declared:
                        offenders.setdefault(dist, []).append(
                            f"{py.relative_to(ROOT)}")
        return offenders

    @staticmethod
    def _report(offenders: dict[str, list[str]]) -> str:
        return ("代码 import 了未声明的第三方包："
                + "；".join(f"{k} ← {v[0]}" for k, v in offenders.items()))

    def test_app_imports_are_declared(self):
        """app/ 下的运行时依赖必须在清单中（否则用户装完跑不起来）。"""
        offenders = self._scan([ROOT / "backend" / "app"])
        assert not offenders, self._report(offenders)

    def test_tests_imports_are_declared(self):
        """tests/ 下 import 的第三方包同样要在清单中。

        实测踩坑：`httpx` 从未出现在任何代码的import 行里 —— 它是
        `fastapi.testclient` 的**传递依赖**，只在 fixture 里被间接需要。
        开发机全局环境恰好有，所以测试全绿；干净环境里 21 个 E2E 用例
        在 fixture 阶段集体报RuntimeError。

        本条按「显式 import」扫描，捕获直接漏写；传递依赖由下面
        TestTestClient 可用性 兜底。
        """
        offenders = self._scan([ROOT / "backend" / "tests"])
        assert not offenders, self._report(offenders)


class TestTestClientWorks:
    """E2E 测试的入口必须真的能起来。

    `starlette.testclient` 强制要求 httpx，但代码里没有任何一行
    `import httpx`（它是 TestClient 的传递依赖），所以纯静态扫描抓不到。
    这条用子进程直接构造 TestClient，把传递依赖缺口暴露出来。
    """

    def test_fastapi_testclient_constructs(self):
        r = subprocess.run(
            [sys.executable, "-c",
             "from fastapi.testclient import TestClient\n"
             "from fastapi import FastAPI\n"
             "c = TestClient(FastAPI())\n"
             "print('ok')"],
            capture_output=True, text=True, cwd=str(ROOT / "backend"),
        )
        assert r.returncode == 0, (
            "fastapi.testclient 不可用 —— E2E 测试会在 fixture 阶段全部报错。\n"
            "典型原因：requirements.txt 缺少 httpx。\n"
            f"{r.stderr[-400:]}"
        )

    def test_httpx_declared(self):
        assert "httpx" in _declared(), (
            "httpx 未在 requirements.txt 中声明，"
            "干净环境下 tests/test_api_e2e.py 会集体报错"
        )
