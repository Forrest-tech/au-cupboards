"""前端回归守卫 —— 纯静态检查，不需要浏览器。

**存在理由**：本轮前端出了三类问题，且都不在Python 测试的覆盖范围内：

1. 「解析并确认入库」按钮从未绑定 onclick —— 点击完全无反应，
   且 submitUpload() 已定义、无任何调用点，纯 Python 测试无法发现。
2. 切换语言时漏翻译，或 i18n 字典缺键 —— 页面上直接显示原始key。
3. 深色硬编码色值残留在浅色主题里 —— 出现看不清的文字。

这三条都用「读 HTML + 断言」来钉住，成本低且能挡住回归。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "frontend" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    assert HTML.is_file(), f"缺少 {HTML}"
    return HTML.read_text()


@pytest.fixture(scope="module")
def script(html: str) -> str:
    """抽出内联 <script> 正文（不含标签）。"""
    m = re.search(r"<script>\n(.*)\n</script>", html, re.S)
    assert m, "index.html 里找不到内联 <script>"
    return m.group(1)


# ---------------------------------------------------------------- 上传按钮


class TestUploadButtonWired:
    """按钮必须有真实的点击处理 —— 用户的核心反馈是「点了没作用」。"""

    def test_go_button_exists(self, html: str):
        assert 'id="bgo"' in html, "找不到解析确认按钮 #bgo"

    def test_submit_upload_defined(self, script: str):
        assert re.search(r"(async\s+)?function\s+submitUpload\s*\(", script), \
            "submitUpload 未定义（却被绑定会直接抛 ReferenceError）"

    def test_submit_upload_is_referenced(self, script: str):
        """submitUpload 必须被**引用**，不只是定义。

        这正是本轮的真实 bug：submitUpload() 完整实现了，
        但全文件没有任何地方引用它 —— 按钮点了毫无反应。

        注意绑定写作 `btn.onclick = submitUpload`（无括号），
        所以不能只数 `submitUpload(` 的出现次数。
        """
        n = len(re.findall(r"(?<![.\w])submitUpload\s*\(?", script))
        assert n >= 2, (
            f"submitUpload 只出现 {n} 次（仅函数定义那一次）—— "
            f"没有任何引用点，这就是「点击确认按钮无作用」的根因"
        )

    def test_bgo_button_gets_onclick(self, script: str):
        """#bgo 的 onclick 必须被赋值成 submitUpload。"""
        # 写法一：btn.onclick = submitUpload（先 const btn = $('#bgo')）
        # 写法二：$('#bgo').onclick = submitUpload
        hits = re.findall(r"(?:\$\(\s*'#bgo'\s*\)|btn)\.onclick\s*=\s*(\w+)", script)
        assert hits, (
            "#bgo 没有绑定 onclick —— 点了不会有任何反应"
        )
        assert "submitUpload" in hits, (
            f"#bgo 绑定的是 {hits}，不是 submitUpload"
        )

    def test_do_file_also_binds(self, script: str):
        """选完文件后也要保证按钮可点 —— showDwgResult 会改按钮文案。"""
        fn = re.search(r"async function doFile\(.*?\n\}", script, re.S)
        assert fn, "doFile 未定义"
        assert "onclick" in fn.group(0), (
            "doFile 内未重新绑定 onclick，showDwgResult 改过按钮后可能失效"
        )

    def test_error_path_restores_button(self, script: str):
        """解析失败必须把按钮恢复可用，否则只能刷新页面。"""
        fn = re.search(r"async function submitUpload\(.*?\n\}", script, re.S)
        assert fn, "submitUpload 未定义"
        body = fn.group(0)
        assert "catch" in body, "submitUpload 没有错误处理"
        assert re.search(r"btn\.disabled\s*=\s*false", body), (
            "解析失败后按钮未恢复可用（disabled 永久卡死）"
        )


# ---------------------------------------------------------------- 顶栏行为


class TestNavNotAutoPopup:
    """点顶栏不应直接弹上传框 —— 用户要先看到已有文件。"""

    def test_nav_does_not_call_openupload(self, script: str):
        fn = re.search(r"\$\$\('nav button'\)\.forEach\(.*?\n\}\);", script, re.S)
        assert fn, "找不到顶栏 nav 绑定"
        body = fn.group(0)
        assert "openUpload(" not in body, (
            "顶栏点击仍直接弹上传框 —— 用户要求先进去看已有文件，"
            "上传改由右上角「上传图纸」按钮触发"
        )
        assert "renderHub()" in body, "顶栏切到柜型库/建筑图纸时应渲染列表"

    def test_upload_button_exists(self, html: str):
        assert 'id="bup"' in html, "顶栏缺少上传按钮 #bup"

    def test_upload_button_bound(self, script: str):
        assert re.search(r"#bup'\)?\.onclick", script), \
            "上传按钮 #bup 未绑定 onclick"


# ---------------------------------------------------------------- 多语言


class TestI18n:
    """中英文必须成对：漏一条就会在界面上裸露 key 字符串。"""

    def test_both_dicts_exist(self, script: str):
        assert "zh:" in script and "en:" in script, "缺少 zh / en 字典"

    def test_keys_are_identical(self, script: str):
        zh = _keys(_dict_block(script, "zh"))
        en = _keys(_dict_block(script, "en"))
        assert zh, "zh 字典解析为空"
        only_zh = zh - en
        only_en = en - zh
        assert not only_zh, f"只有中文没有英文翻译: {sorted(only_zh)}"
        assert not only_en, f"只有英文没有中文翻译: {sorted(only_en)}"

    def test_switch_controls_bound(self, script: str):
        assert re.search(r"#lzh'\)?\.onclick", script), "中文切换按钮未绑定"
        assert re.search(r"#len'\)?\.onclick", script), "英文切换按钮未绑定"

    def test_apply_i18n_rerenders(self, script: str):
        fn = re.search(r"function applyI18n\(.*?\n\}", script, re.S)
        assert fn, "applyI18n 未定义"
        assert "renderTree()" in fn.group(0), \
            "applyI18n 未重绘 —— 切换语言后表格还是旧语言"

    def test_no_shadowed_i18n_fn(self, script: str):
        """函数参数名 t 会遮蔽全局 t()，导致翻译失效。"""
        bad = re.findall(r"function\s+\w+\([^)]*\bt\s*[,)]", script)
        assert not bad, (
            f"这些函数的参数名 t 遮蔽了 i18n 函数 t(): {bad}；"
            f"请改名为 _t"
        )


def _dict_block(script: str, lang: str) -> str:
    """截取 I18N[lang] = {...} 的正文。"""
    m = re.search(rf"\n\s*{lang}:\s*\{{(.*?)\n\s*\}},", script, re.S)
    assert m, f"未找到 {lang} 字典块"
    return m.group(1)


def _keys(block: str) -> set[str]:
    r"""提取字典 key。

    不能按行首匹配 —— 字典里大量是 `'a': 'x', 'b': 'y',` 同行写法，
    按 ^\s* 只能捞到每行第一个 key，会漏掉大量条目并误报「缺翻译」。
    """
    return set(re.findall(r"['\"]([a-z][a-z0-9_.]*)['\"]\s*:", block))


# ---------------------------------------------------------------- 白色基调


class TestLightTheme:
    """深色硬编码色值残留在浅色主题里会导致文字看不清。"""

    #: 只针对「用作背景」的深色。#0f172a 作为**文字色**（--txt）和
    #: **阴影色**（--shadow）在浅色主题下完全合法，不能一并拦掉。
    DARK_BG = [
        "#0f172a", "#1e293b", "#334155", "#475569", "#111827", "#0b1220",
        "#052e16", "#422006", "#450a0a", "#0c1e3f", "#14532d", "#78350f",
        "#7f1d1d", "#4c1d95", "#1e3a8a",
    ]

    def test_light_css_vars(self, html: str):
        m = re.search(r":root\{(.*?)\n\}", html, re.S)
        assert m, "找不到 :root 变量块"
        root = m.group(1)
        assert re.search(r"--bg:\s*#f", root), \
            "--bg 应为浅色（当前仍是深色主题）"
        assert re.search(r"--panel:\s*#fff", root), \
            "--panel 应为白色（当前仍是深色主题）"

    def test_no_dark_backgrounds_in_css(self, html: str):
        """深色只允许出现在 --txt /阴影 透明度修饰里，不许当背景用。"""
        css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        offenders = []
        for decl in re.findall(r"background(?:-color)?\s*:\s*([^;}]+)", css):
            for c in self.DARK_BG:
                # #0f172a59 是遮罩（合法）；纯 #0f172a 才是问题
                if c in decl.lower() and not re.search(
                        rf"{re.escape(c)}[0-9a-f]{{2}}\b", decl.lower()):
                    offenders.append(decl.strip())
        assert not offenders, (
            f"CSS 里把深色用作背景: {sorted(set(offenders))} —— "
            f"浅色主题下会导致文字对比度不足"
        )

    def test_alert_bg_is_light(self, html: str):
        """alert 四种语义底色都必须回到浅色变量。"""
        css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        for kind, var in [("ok", "--ok-bg"), ("warn", "--warn-bg"),
                          ("err", "--err-bg"), ("info", "--info-bg")]:
            m = re.search(rf"\.alert\.{kind}\{{([^}}]+)\}}", css)
            assert m, f"缺少 .alert.{kind}"
            assert f"var({var})" in m.group(1), (
                f".alert.{kind} 未使用 {var} —— 深色主题残留"
            )

    def test_tags_use_light_backgrounds(self, html: str):
        """tag 类徽标在浅色主题下需浅底深字。"""
        css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        for name in ["high", "medium", "low", "acc", "com", "err", "warn"]:
            m = re.search(rf"\.tag\.{name}\{{([^}}]+)\}}", css)
            assert m, f"缺少 .tag.{name}"
            body = m.group(1)
            assert not re.search(r"background:\s*#(?:14532d|78350f|7f1d1d|"
                                 r"4c1d95|1e3a8a)", body), (
                f".tag.{name} 仍是深色底"
            )

    def test_no_color_fff_as_text_on_light(self, html: str):
        """白色文字只应出现在实色按钮上，不能用在浅色面板上。"""
        css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        # .btn.pri / .btn.ok / .hupload 等实色按钮用 #fff 是正确的
        for rule in re.findall(r"\.(\w+)((?:\{[^}]*\})*)\{([^}]*)\}", css):
            name, _, body = rule
            if "color:#fff" not in body.replace(" ", ""):
                continue
            if re.search(rf"background:\s*var\(--accent\)|background:\s*var\(--ok\)|"
                         rf"background:\s*var\(--accent2\)", body):
                continue
            assert name in {"spin"}, (
                f"类 .{name} 用 #fff 作文字色但背景是浅色，可能看不清"
            )

    def test_semantic_alert_bg_defined(self, html: str):
        root = re.search(r":root\{(.*?)\n\}", html, re.S).group(1)
        for v in ["--ok-bg", "--warn-bg", "--err-bg", "--info-bg"]:
            assert f"{v}:" in root, \
                f"缺少 {v} —— 浅色主题下 alert 需要浅底色才可读"


# ---------------------------------------------------------------- 结构完整


class TestStructure:
    def test_three_panes_present(self, html: str):
        for i in ["tree", "mid", "right"]:
            assert f'id="{i}"' in html, f"缺少 #{i} 面板"

    def test_rerender_functions_exist(self, script: str):
        for fn in ["renderTree", "renderPane", "renderHub", "applyI18n"]:
            assert re.search(rf"function {fn}\(", script), f"{fn} 未定义"

    def test_script_is_valid_js(self, script: str):
        """用 node --check 校验语法（node 不可用时跳过）。"""
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            pytest.skip("node 不可用，跳过 JS 语法检查")
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as fh:
            fh.write(script)
            path = fh.name
        r = subprocess.run([node, "--check", path],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"JS 语法错误:\n{r.stderr[-600:]}"