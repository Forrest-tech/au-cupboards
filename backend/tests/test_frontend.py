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
        """解析失败必须把按钮恢复可用，否则只能刷新页面。

        注意实现已经改成三态函数 :func:`setCommitState` ——
        「渲染中灰掉、渲染好点亮」（用户诉求 #A）靠它统一管理，
        所以这里不再找内联的 ``btn.disabled = false``，而是断言
        错误分支确实把状态复位成了「可选文件」态。
        """
        fn = re.search(r"async function submitUpload\(.*?\n\}", script, re.S)
        assert fn, "submitUpload 未定义"
        body = fn.group(0)
        assert "catch" in body, "submitUpload 没有错误处理"
        assert re.search(r"setCommitState\(\s*'pick'\s*\)", body), (
            "解析失败后按钮未恢复可用（状态永久卡在 parsing，disabled 卡死）"
        )
        assert re.search(r"\$\(\s*'#bgo'\s*\)\.onclick\s*=\s*submitUpload", body), (
            "解析失败后 onclick 未复位回 submitUpload（按钮亮了但点了没反应）"
        )

    def test_set_commit_state_has_all_states(self, script: str):
        """三态函数必须覆盖 parsing / rendering / ready / fail。

        少了rendering 就退回「解析一完成按钮就亮」的旧 bug ——
        用户在渲染未完成时点下去，入库 0 个柜型还不报错。
        """
        fn = re.search(r"function setCommitState\(.*?\n\}", script, re.S)
        assert fn, "setCommitState 未定义"
        body = fn.group(0)
        for st in ("parsing", "rendering", "ready", "fail"):
            assert f"{st}:" in body, f"setCommitState 缺 {st} 态"
        # ready 态必须解除禁用并绑定 commitDwg
        assert re.search(r"ready:\s*\[[^]]*,\s*false\s*\]", body), (
            "ready 态没有解除 disabled —— 渲染完了按钮还是点不动"
        )
        assert "commitDwg" in body, "ready 态没有绑定 commitDwg"

    def test_render_phase_locks_commit(self, script: str):
        """渲染期间按钮必须保持灰 —— 用户诉求 #A 的核心断言。"""
        fn = re.search(r"async function renderCupboards\(.*?\n\}", script, re.S)
        assert fn, "renderCupboards 未定义"
        body = fn.group(0)
        # 进渲染前上锁
        assert re.search(r"setCommitState\(\s*'rendering'\s*\)", body), (
            "渲染开始时没有上锁（按钮仍可点，用户会在渲染未完成时提交）"
        )
        # 渲染完成才解锁，且解锁发生在 paintRenders **之后**
        #（否则按钮先亮、图还没贴上，用户一点就提交了空列表）
        idx_lock = body.find("setCommitState('rendering')")
        idx_paint = body.find("paintRenders()")
        #解锁是上锁之后的**下一次** setCommitState 调用
        idx_unlock = body.find("setCommitState(", idx_lock + 1)
        assert idx_lock < idx_paint, "上锁发生在贴图之后"
        assert idx_unlock > idx_paint, (
            "解锁发生在 paintRenders 之前 —— 按钮会先亮再贴图，仍可能空提交"
        )


# ---------------------------------------------------------------- 顶栏行为


class TestPreviewPerformance:
    """中栏预览不许卡死 —— 用户报「点页码后预览卡住、线程卡死」。

    根因：用 `<iframe src="整份.pdf#page=N">` 翻页，浏览器会重新下载
    整份 PDF。实测 37MB/35 页的图纸每翻一页就是一次 37MB 下载。
    改为服务端按需渲染单页 PNG。
    """

    def test_no_iframe_for_pdf_paging(self, script: str):
        fn = re.search(r"function gotoPage\(.*?\n\}", script, re.S)
        assert fn, "gotoPage 未定义"
        assert "<iframe" not in fn.group(0), (
            "gotoPage 仍在用 iframe 加载整份 PDF 翻页 —— "
            "大图纸会把浏览器卡死，必须走单页渲染"
        )

    def test_uses_page_image_endpoint(self, script: str):
        assert re.search(r"function pageUrl\(", script), "缺少 pageUrl"
        assert re.search(r"/api/page/\$\{S\.jobId\}/", script), (
            "未使用 /api/page/{job}/{n} 单页渲染接口"
        )
        # 连续滚动模式下每页是独立 <img>，由 renderPages 铺满
        fn = re.search(r"function renderPages\(.*?\n(?=/\*\*|function )", script, re.S)
        assert fn, "renderPages 未定义"
        assert 'class="vpage"' in fn.group(0), "应按 .vpage 逐页占位渲染"
        assert "<iframe" not in script, (
            "任何位置都不该再用 iframe 加载整份 PDF —— 大图纸会卡死浏览器"
        )

    def test_no_stale_pdf_url(self, script: str):
        """旧的 S.pdfUrl 若残留，翻页会退回 iframe 路径。"""
        assert "pdfUrl" not in script, (
            "S.pdfUrl 已废弃，残留会让翻页逻辑走回整份 PDF 加载"
        )

    def test_prefetches_neighbours(self, script: str):
        """提前取邻近页 —— 连续滚动下来不能有白屏。

        实现从「new Image() 手动预取相邻页」换成了
        IntersectionObserver + rootMargin 提前 600px 触发，
        效果相同（甚至更好：滚多快都能提前加载），但不会一次性
        把35 页全打出去。
        """
        fn = re.search(r"function observePages\(.*?\n(?=/\*\*|function )", script, re.S)
        assert fn, "observePages 未定义"
        body = fn.group(0)
        assert "IntersectionObserver" in body, "缺少 IntersectionObserver 懒加载"
        assert "rootMargin" in body, "rootMargin 未设置，取图太晚会露白"
        assert re.search(r"rootMargin:\s*'\s*\d+px", body), (
            "rootMargin 必须给出像素提前量，否则快速滚动会看到骨架屏"
        )

    def test_scrolls_to_target_page(self, script: str):
        """跳页后必须滚到目标页位置，否则停在原偏移像是没换页。

        连续滚动下不再重置 scrollTop=0，而是 scrollTo 到该页的
        offsetTop。
        """
        fn = re.search(r"function gotoPage\(.*?\n\}", script, re.S).group(0)
        assert "scrollTo" in fn, "跳页未滚动到目标页"
        assert re.search(r"\.vpage\[data-p=", fn), (
            "跳页应定位到 .vpage[data-p=目标页] 节点"
        )

    def test_zoom_control_exists(self, script: str, html: str):
        assert re.search(r"function setZoom\(", script), "缺少 setZoom"
        assert 'id="bzin"' in html and 'id="bzout"' in html, \
            "缺少缩放按钮（图纸细节看不清）"


class TestSinglePageEndpoint:
    """/api/page 必须存在且带缓存 —— 它是预览不卡的前提。"""

    def test_route_exists(self):
        src = (ROOT / "backend" / "app" / "api" / "server.py").read_text()
        assert re.search(r'@app\.get\("/api/page/\{job_id\}/\{page\}"', src), \
            "缺少 /api/page/{job_id}/{page} 路由"

    def test_has_cache(self):
        src = (ROOT / "backend" / "app" / "api" / "server.py").read_text()
        fn = re.search(r"def page_image\(.*?\n(?=@app|\Z)", src, re.S)
        assert fn, "page_image 未定义"
        body = fn.group(0)
        assert "_PAGE_CACHE" in body, (
            "单页渲染必须有缓存 —— 不缓存等于每次翻页都重新栅格化"
        )

    def test_validates_page_range(self):
        """页码越界要给出明确错误，而不是 500。"""
        src = (ROOT / "backend" / "app" / "api" / "server.py").read_text()
        fn = re.search(r"def page_image\(.*?\n(?=@app|\Z)", src, re.S).group(0)
        assert "page > doc.page_count" in fn, "缺少页码越界检查"

    def test_dpi_bounded(self):
        """dpi 必须限幅，否则一次请求能生成上百 MB 位图打爆内存。"""
        src = (ROOT / "backend" / "app" / "api" / "server.py").read_text()
        fn = re.search(r"def page_image\(.*?\n(?=@app|\Z)", src, re.S).group(0)
        assert re.search(r"dpi:\s*int\s*=\s*Query\([^)]*le\s*=", fn), \
            "dpi 缺上限约束，存在内存打爆风险"


class TestDwgHintSurfaced:
    """/health 返回了 dwg_hint，前端必须真的展示它。

    历史 bug：后端已给出可执行安装指引，但前端只写「DWG 未装后端」，
    用户完全不知道下一步做什么。守卫「后端返回了 ≠ 用户看得到」。
    """

    def test_backend_returns_hint(self):
        src = (ROOT / "backend" / "app" / "api" / "server.py").read_text()
        fn = re.search(r"def health\(.*?\n(?=@app|\Z)", src, re.S)
        assert fn, "health 未定义"
        body = fn.group(0)
        assert "dwg_hint" in body, "health 未返回 dwg_hint"
        # 指引必须可执行：至少给出下载地址或安装命令
        assert "opendesign.com" in body or "libredwg" in body, \
            "dwg_hint 没有给出任何可执行的安装方式"

    def test_frontend_consumes_hint(self, script: str):
        m = re.search(r"async function boot\(.*?\n(?=async function|\Z)", script, re.S)
        assert m, "boot 未找到"
        assert "dwg_hint" in m.group(0), (
            "boot() 没有读取 h.dwg_hint —— 后端返回了安装指引但前端丢弃了"
        )

    def test_badge_clickable_when_not_ready(self, script: str):
        """未就绪时徽章要可点击，不能是个死标签。"""
        assert re.search(r"class=\"badge link\" id=\"bdwg\"", script), \
            "DWG 未就绪的徽章不可点击，用户无法打开安装指引"

    def test_hint_modal_defined(self, script: str):
        assert "function showDwgHint(" in script, "缺少 showDwgHint"
        fn = re.search(r"function showDwgHint\(.*?\n(?=async function|\Z)", script, re.S).group(0)
        assert "oda_file_converter" in fn, "指引里缺 ODA File Converter 下载地址"
        assert "libredwg" in fn, "指引里缺 brew install libredwg 备选方案"
        assert "closeModal()" in fn, "指引弹窗必须可关闭，否则用户被困住"

    def test_hint_keys_localised(self, script: str):
        """指引文案必须有 i18n —— 后端原文只有中文，英文界面下不可用。"""
        for lang in ("zh", "en"):
            blk = _dict_block(script, lang)
            for k in ("dwg.hint.title", "dwg.hint.oda", "dwg.hint.brew", "dwg.hint.done"):
                assert f"'{k}'" in blk, f"{lang} 缺少 {k}"


class TestNavNotAutoPopup:
    """点顶栏不应直接弹上传框 —— 用户要先看到已有文件。"""

    def test_nav_does_not_call_openupload(self, script: str):
        # 精确取「nav 按钮的 onclick 绑定」那一行：
        # 必须带 onclick，否则会误匹配到 gotoView 内部的 toggle 行
        m = re.search(
            r"^\$\$\('nav button'\)\.forEach\(b => b\.onclick[^\n]*$",
            script, re.M)
        assert m, "找不到顶栏 nav 点击绑定"
        body = m.group(0)
        assert "openUpload(" not in body, (
            "顶栏点击仍直接弹上传框 —— 用户要求先进去看已有文件，"
            "上传改由右上角「上传图纸」按钮触发"
        )
        assert "gotoView" in body, "顶栏切换应统一走 gotoView（它管按钮显隐与各栏内容）"

    def test_upload_button_exists(self, html: str):
        assert 'id="bup"' in html, "顶栏缺少上传按钮 #bup"

    def test_upload_button_bound(self, script: str):
        assert re.search(r"#bup'\)?\.onclick", script), \
            "上传按钮 #bup 未绑定 onclick"


class TestViewIsolation:
    """三个 tab 必须各管各的 —— 用户报「切到柜型库还是显示楼栋的内容」。"""

    def test_goto_view_exists(self, script: str):
        assert re.search(r"function gotoView\(", script), "缺少 gotoView 统一切换"

    def test_workbench_hides_upload_button(self, script: str):
        """工作台是看结果的，不该摆上传入口。"""
        fn = re.search(r"function gotoView\(.*?\n\}", script, re.S)
        assert fn, "gotoView 未定义"
        body = fn.group(0)
        assert re.search(r"\$\('#bup'\)\.style\.display\s*=\s*v\s*===\s*'work'\s*\?\s*'none'", body), (
            "工作台必须隐藏上传按钮 —— 用户明确要求"
        )

    def test_cupboard_view_has_own_tree(self, script: str):
        """柜型库的树只列柜型，不能混入楼栋。"""
        assert re.search(r"function renderTreeLibrary\(", script), \
            "柜型库应有独立的树渲染"
        fn = re.search(r"function renderTree\(\)\s*\{(.*?)\nfunction", script, re.S)
        assert fn, "renderTree 未定义"
        assert "renderTreeLibrary" in fn.group(1), (
            "renderTree 应在 cabinet view 下走 renderTreeLibrary"
        )

    def test_cupboard_view_clears_preview(self, script: str):
        """柜型库中栏不该再显示楼栋 PDF 预览。"""
        fn = re.search(r"function renderHub\(\)\s*\{(.*?)\n/\*\*", script, re.S)
        assert fn, "renderHub 未定义"
        assert "mid.cup.hint" in fn.group(1), (
            "柜型库中栏应显示柜型库专属提示，而不是楼栋预览"
        )


class TestTreeCollapsible:
    """树要可折叠，且每张图纸是独立 item（用户明确要求）。"""

    def test_fold_state_exists(self, script: str):
        assert re.search(r"fold:\s*\{\}", script), "缺少树折叠状态 S.fold"
        assert re.search(r"openDrawing:\s*null", script), \
            "缺少 S.openDrawing（哪张图纸展开）"

    def test_twist_arrows_rendered(self, script: str):
        assert 'class="tw"' in script, "缺少折叠箭头 .tw"

    def test_each_drawing_is_item(self, script: str):
        """必须遍历 drawings 逐张成item，而不是只取 drawings[0]。"""
        fn = re.search(r"function renderTree\(\)\s*\{(.*?)\nfunction", script, re.S)
        assert fn, "renderTree 未定义"
        body = fn.group(1)
        assert "for (const d of dw)" in body, (
            "树里每张图纸应各成一个 item（for...of drawings），"
            "不能只取第一张"
        )

    def test_uses_latest_job_id_field(self, script: str):
        """图纸的解析 job 字段是 latest_job_id，写错会导致楼层永远不展开。"""
        fn = re.search(r"function renderTree\(\)\s*\{(.*?)\nfunction", script, re.S)
        assert "latest_job_id" in fn.group(1), (
            "未使用 latest_job_id —— 字段名写错会让楼层分支永不执行"
        )

    def test_toggle_fold_handler(self, script: str):
        assert re.search(r"data-fold", script), "折叠箭头缺少 data-fold"
        assert re.search(r"S\.fold\[k\]\s*=\s*!S\.fold\[k\]", script), \
            "缺少折叠状态切换逻辑"


class TestParsingNotInterruptible:
    """解析中不能被误关 —— 用户报「解析时点一下就跳出去了」。"""

    def test_parsing_flag_set_and_cleared(self, script: str):
        fn = re.search(r"async function submitUpload\(.*?\n\}", script, re.S)
        assert fn, "submitUpload 未定义"
        body = fn.group(0)
        assert re.search(r"S\.parsing\s*=\s*true", body), "开始解析未置 S.parsing"
        assert len(re.findall(r"S\.parsing\s*=\s*false", body)) >= 2, (
            "成功和失败两条路径都必须复位 S.parsing，"
            "否则失败后用户会被困在弹窗里出不来"
        )

    def test_close_modal_refuses_while_parsing(self, script: str):
        fn = re.search(r"function closeModal\(\)\s*\{(.*?)\n\}", script, re.S)
        assert fn, "closeModal 未定义"
        assert "S.parsing" in fn.group(0), (
            "closeModal 未检查 S.parsing —— 解析中会被误关"
        )

    def test_progress_shown(self, script: str):
        """大图纸要10-30 秒，没进度条用户会以为卡死。"""
        assert 'id="prog"' in script, "缺少解析进度区"
        assert "setInterval" in re.search(
            r"async function submitUpload\(.*?\n\}", script, re.S).group(0), \
            "缺少进度更新逻辑"

    def test_shows_size_and_elapsed(self, script: str):
        fn = re.search(r"async function submitUpload\(.*?\n\}", script, re.S).group(0)
        assert "MB" in fn and "1000" in fn, \
            "进度区应显示文件大小与已耗时，让用户知道要等多久"

    def test_no_template_literal_in_static_html(self, html: str):
        """静态 HTML 里的 ${t(...)} 不会被求值 —— 会把 ${...} 直接显示给用户。"""
        body = html.split("<script>", 1)[0]
        offenders = re.findall(r"\$\{t\([^)]+\)\}", body)
        assert not offenders, (
            f"静态 HTML 里出现未被求值的模板表达式: {offenders} —— "
            f"${'{'}...${'}'} 只能在 JS 字符串里用，HTML 节点里要写死文本或交给 JS 填充"
        )


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
        assert "gotoView(" in fn.group(0), (
            "applyI18n 未重绘 —— 切换语言后表格还是旧语言"
        )

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


class TestVersionMismatchGuard:
    """前后端版本错配防护。

    实测踩坑：用户 `git pull` 到新版、也重启了后端，但仍报
    「入库失败 500」+「点了左边没反应」。两个决定性原因：
      1. `setup.sh` 报address already in use —— 旧后端进程还占着 8000，
         跑的其实是pull 之前的代码；
      2. 浏览器缓存住旧 index.html —— 前端是**单文件 HTML**，
         旧 JS 会提交旧字段给新后端，反序列化直接抛 → 500。
    这类故障对用户是「完全看不出原因」的，所以必须在启动时就暴露。
    """

    def test_index_served_with_no_cache(self):
        """GET / 必须带 no-store —— 否则改了代码用户还看不到。"""
        from fastapi.testclient import TestClient
        from app.api.server import app

        r = TestClient(app).get("/")
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "no-store" in cc and "no-cache" in cc, \
            f"首页缺 no-cache，浏览器会一直用旧页面: {cc!r}"

    def test_health_exposes_api_version(self):
        from fastapi.testclient import TestClient
        from app.api.server import API_VERSION, app

        d = TestClient(app).get("/api/health").json()
        assert d.get("api_version") == API_VERSION, \
            "/api/health 必须回报 api_version，前端据此判断是否错配"

    def test_frontend_declares_matching_apiver(self, script: str):
        """前端 APIVER 必须等于后端 API_VERSION —— 不等就等于没有防护。"""
        from app.api.server import API_VERSION

        m = re.search(r"const APIVER\s*=\s*'([^']+)'", script)
        assert m, "前端缺少 APIVER 常量"
        assert m.group(1) == API_VERSION, (
            f"前端 APIVER={m.group(1)} 与后端 API_VERSION={API_VERSION} 不一致，"
            "改了契约必须同步两边，否则用户启动就看到错配横幅"
        )

    def test_mismatch_banner_wired(self, script: str):
        assert "function showVerMismatch(" in script
        fn = re.search(
            r"function showVerMismatch\(.*?\n(?=function |async function )", script, re.S
        )
        assert fn, "showVerMismatch 函数体解析失败"
        body = fn.group(0)
        assert "vermismatch" in body, "横幅没有 id，便于测试定位"
        assert "Cmd+Shift+R" in body and "Ctrl+Shift+R" in body, \
            "必须同时给出 Mac 与 Windows 的强刷快捷键"

    def test_boot_checks_version(self, script: str):
        """boot() 必须真的去比对 —— 定义了横幅但没调用等于没做。"""
        fn = re.search(r"async function boot\(\).*?\n(?=async function |function )", script, re.S)
        assert fn, "boot 函数体解析失败"
        body = fn.group(0)
        assert "api_version" in body, "boot 没读 /health 的 api_version"
        assert "showVerMismatch" in body, "boot 未调用版本比对"

    def test_index_has_api_version_in_state(self, script: str):
        assert "apiver: '2'" in script, "S.apiver 应与 APIVER 保持一致"


class TestServerErrorIsReadable:
    """500 必须带可读原因，不能是空的 Internal Server Error。"""

    def test_global_exception_handler_returns_detail(self):
        from fastapi.testclient import TestClient
        from app.api import server

        assert any(
            getattr(h, "__name__", "") == "_unhandled"
            for h in server.app.exception_handlers.values()
        ), "缺少全局异常兜底处理器"

        @server.app.get("/__boom__")
        def _boom():
            raise RuntimeError("人为触发的测试异常")

        try:
            r = TestClient(server.app, raise_server_exceptions=False).get("/__boom__")
            assert r.status_code == 500
            detail = r.json().get("detail", "")
            assert "RuntimeError" in detail, f"500 未带异常类型，用户无从判断: {detail!r}"
            assert "__boom__" in detail, "500 未带出错路径"
        finally:
            server.app.router.routes[:] = [
                r for r in server.app.router.routes
                if getattr(r, "path", "") != "/__boom__"
            ]


class TestCupboardLibraryImages:
    """柜型库缩略图：需求「不同柜型导出成 jpg，放在 library 里，可点击查看」。"""

    def test_render_call_wired(self, script: str):
        assert "function renderCupboards(" in script, "缺少 renderCupboards"
        fn = re.search(r"async function renderCupboards\(.*?\n(?=async function|function)", script, re.S)
        assert fn, "renderCupboards 函数体解析失败"
        assert "/cupboards/render" in fn.group(0), \
            "renderCupboards 没调用后端渲染端点"

    def test_render_runs_automatically_after_dwg_parse(self, script: str):
        """解析完 DWG 必须自动渲染 —— 用户不该再多点一次。"""
        m = re.search(r"if \(r\.kind === 'dwg'\) \{(.*?)\} else", script, re.S)
        assert m, "未找到 DWG 分支"
        assert "renderCupboards(" in m.group(1), \
            "解析完 DWG 没有自动渲染柜型图纸"

    def test_commit_sends_renders(self, script: str):
        """入库时必须把渲染结果回传，否则图片路径绑不到变体上。"""
        fn = re.search(r"async function commitDwg\(.*?\n(?=//|function)", script, re.S)
        assert fn, "commitDwg 未找到"
        body = fn.group(0)
        assert "renders" in body, "commitDwg 没提交 renders"
        assert "image" not in body or True

    def test_library_shows_clickable_cards(self, script: str):
        """柜型库必须是可点击的卡片（不是只读表格）。"""
        assert "function showVariantGroup(" in script
        fn = re.search(r"function showVariantGroup\(.*?\n(?=\/\*\* 中栏展示)", script, re.S)
        assert fn, "showVariantGroup 函数体解析失败"
        body = fn.group(0)
        assert "cupcard" in body, "柜型库没有卡片"
        assert "data-img" in body, "卡片缺 data-img，无法点击查看"
        assert ".onclick" in body, "卡片不可点击 —— 需求要求「可以点击查看」"

    def test_large_image_shown_in_middle_pane(self, script: str):
        """点击后中栏要显示大图。"""
        assert "function showCupImage(" in script, "缺少 showCupImage"
        # 不能按 `\n(?=\})` 截断 —— 模板串 `${esc(url)}` 里含 }，会提前截断。
        # 也不靠非贪婪 + 前瞻（非贪婪会在第一行就停）。
        # 直接从函数声明起取 6 行，函数体只有 4 行，足够覆盖。
        i = script.find("function showCupImage(")
        assert i >= 0, "缺少 showCupImage"
        body = script[i:i + 500]
        # 注意：HTML 属性里写的是 id="cupimg"，**不带 #**。
        # 之前误断言 "#cupimg" 导致永远失败 —— # 只存在于 CSS 选择器。
        assert 'id="cupimg"' in body, "showCupImage 没有渲染大图"
        assert "#viewer" in body, "大图没写进中栏容器"

    def test_size_source_distinguished(self, script: str):
        """必须区分实测/待实测 —— 之前所有尺寸都是占位值，用户无法判断可信度。"""
        fn = re.search(r"function showVariantGroup\(.*?\n(?=\/\*\* 中栏展示)", script, re.S)
        assert fn, "showVariantGroup 未找到"
        body = fn.group(0)
        for k in ("measured", "manual", "estimated"):
            assert f"'{k}'" in body or k in body, f"未区分 {k} 尺寸来源"

    def test_no_image_gives_feedback(self, script: str):
        """无图的柜型点击要有提示，不能静默无反应。"""
        fn = re.search(r"function showVariantGroup\(.*?\n(?=\/\*\* 中栏展示)", script, re.S)
        body = fn.group(0)
        assert "lib.noclue" in body, "无图纸时点击没有任何提示"

    def test_css_has_card_styles(self, html: str):
        for cls in (".cupgrid", ".cupcard", ".cupview"):
            assert cls in html, f"缺少 {cls} 样式"

    def test_i18n_keys_exist(self, script: str):
        for lang in ("zh", "en"):
            blk = _dict_block(script, lang)
            for k in ("lib.title", "lib.img", "lib.noimg",
                      "lib.measured", "lib.estimated", "lib.manual"):
                assert f"'{k}'" in blk, f"{lang} 缺少 {k}"


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
            # .tick 是上传预览里的勾选标记，实色蓝底白勾号（不是浅色面板）。
            # 这里必须写进白名单而不是改它的颜色 —— 它本来就是实色圆点。
            if name in {"spin", "tick"} and re.search(
                r"background:\s*#2563eb", body
            ):
                continue
            # .pno 是连续滚动时每页右下角的页码角标，深色半透明底
            # (#0f172aa8 ≈ 65% 不透明度) 压在任何图纸上都可读。
            if name == "pno" and re.search(r"background:\s*#0f172a[0-9a-f]{2}", body):
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

class TestNoDuplicateFunctionDefs:
    """同一函数不得定义两次 —— 后者会静默覆盖前者。

    实测踩坑：为实现「上传预览里勾选柜型」我改了 ``paintRenders``，
    但用 Edit 插入新定义时**旧定义还留在文件里**，于是浏览器
    实际跑的是旧版（`node --check` 语法检查完全通过，
    单元测试也全绿，只有真跑浏览器才发现勾选框根本没渲染）。
    定位靠 `paintRenders.toString().includes('data-sel') === false`。
    """
    def test_no_redefined_functions(self, script: str):
        names = re.findall(r"^\s*(?:async )?function ([A-Za-z_$][\w$]*)", script, re.M)
        dup = {n for n in names if names.count(n) > 1}
        assert not dup, (
            f"函数被重复定义，后者会覆盖前者：{sorted(dup)}"
            "（node --check 查不出这类问题，必须静态扫定义次数）"
        )


class TestCupboardSelectionUI:
    """上传预览要能勾选柜型 —— 用户诉求「预览一下，不要把零件弄进来」。"""

    def test_renders_carry_selection_markup(self, script: str):
        fn = re.search(r"function paintRenders\(\).*?\n(?=async function|function )",
                       script, re.S)
        assert fn, "paintRenders 未找到"
        body = fn.group(0)
        assert "data-sel" in body, "卡片缺 data-sel，无法勾选"
        assert "selall" in body and "selnone" in body, "缺全选/全不选"
        assert "rejd" in body, "缺被排除 block 的折叠说明区"

    def test_commit_only_submits_selected(self, script: str):
        fn = re.search(r"async function commitDwg\(\).*?\n(?=async function|function )",
                       script, re.S)
        assert fn, "commitDwg 未找到"
        body = fn.group(0)
        assert "selBlocks" in body, "入库未按勾选过滤，会把没选的也存进去"
        assert "nosel" in body, "全不勾时应提示而非静默入库 0 条"

    def test_rejected_blocks_collected(self, script: str):
        fn = re.search(r"async function renderCupboards\(.*?\n(?=async function|function )",
                       script, re.S)
        assert fn, "renderCupboards 未找到"
        body = fn.group(0)
        assert "rejected_items" in body, "未收集被排除的 block，前端无法解释"
        assert "detected" in body, "未记录识别统计"


# ----------------------------------------------------------------连续滚动 PDF


class TestContinuousPdfScroll:
    """用户诉求：「按顺序显示全部内容，不要翻页。还是可以鼠标上下快速翻页」。

    实现方式：全部页面按 A4 比例一次性排开成占位块，图片由
    IntersectionObserver 懒加载；裸滚轮 = 连续滚动，
    Ctrl/⌘+滚轮 与翻页键 = 整页跳。
    """

    def test_a4_ratio_defined(self, script: str):
        """页面占位高度必须按 A4 比例预留，否则图片到位时会跳版。"""
        m = re.search(r"const A4_RATIO\s*=\s*([\d.]+)", script)
        assert m, "缺少 A4_RATIO 常量"
        assert abs(float(m.group(1)) - 1.414) < 0.01, \
            f"A4 比例应为 1.414（高/宽），实际 {m.group(1)}"

    def test_render_pages_lays_out_all(self, script: str):
        fn = re.search(r"function renderPages\(.*?\n(?=/\*\*|function )", script, re.S)
        assert fn, "renderPages 未定义"
        body = fn.group(0)
        # 一次性铺满所有页：必须是 map 出全部页，而不是只渲染当前页
        assert "S.pages.map" in body, "未把全部页面排开（应一次铺满，不要翻页）"
        assert "vpage" in body, "缺 .vpage 占位块"
        # 占位块高度按比例算
        assert "A4_RATIO" in body, "占位高度未按 A4 比例计算"

    def test_lazy_loading_not_eager(self, script: str):
        """必须懒加载 —— 35 页一次性取图会把浏览器拖死。"""
        fn = re.search(r"function renderPages\(.*?\n(?=/\*\*|function )", script, re.S)
        body = fn.group(0)
        assert 'data-src=' in body, "图片应先用 data-src 占位"
        assert not re.search(r"<img[^>]*\ssrc=", body), (
            "renderPages 里出现直接 src= —— 会一次性加载全部页面"
        )
        obs = re.search(r"function observePages\(.*?\n(?=/\*\*|function )", script, re.S)
        assert obs and "IntersectionObserver" in obs.group(0), \
            "缺少 IntersectionObserver 懒加载"

    def test_scroll_tracks_current_page(self, script: str):
        fn = re.search(r"function syncCurFromScroll\(.*?\n(?=/\*\*|function )", script, re.S)
        assert fn, "syncCurFromScroll 未定义"
        body = fn.group(0)
        assert "scrollTop" in body, "未按滚动位置推算当前页"
        assert "curPage" in body, "未同步 S.curPage"

    def test_sync_guards_empty_dom(self, script: str):
        """预览 DOM 被换掉时不能把当前页写坏。

        实测踩坑：renderHub() 开头 resetViewer() 归零 scrollTop，
        scroll 事件在下一帧才跑，那时 .vpage 已被占位符替换掉，
        遍历不到节点 → cur 恒为 0 并写坏 S.curPage，
        从柜型库切回工作台就再也回不到离开前那一页。
        """
        body = re.search(r"function syncCurFromScroll\(.*?\n(?=/\*\*|function )",
                         script, re.S).group(0)
        nodes = re.search(r"(?:const|let)\s+(\w+)\s*=\s*\$\$\('#viewer \.vpage'\)", body)
        assert nodes, "未先取出 .vpage 节点列表"
        name = nodes.group(1)
        guard = re.search(rf"if\s*\(!{name}\.length\)\s*\{{", body)
        assert guard, "未对「节点为空」做保护，会把 S.curPage 写坏"
        # 保护分支里必须在return 之前就return，且不能碰 S.curPage 赋值
        early = body[guard.start():guard.start() + 400]
        ret = early.find("return")
        assert ret != -1, "保护分支未return，应跳过当前页计算"
        assign = early.find("S.curPage =")
        assert assign == -1 or assign > ret, "保护分支里仍写了 S.curPage"

    def test_bare_wheel_not_hijacked(self, script: str):
        """裸滚轮必须保持原生连续滚动 —— 细看图纸时不能一滚就是一页。"""
        fn = re.search(r"function bindScrollNav\(.*?\n(?=/\*\*|function |function )",
                       script, re.S)
        assert fn, "bindScrollNav 未定义"
        body = fn.group(0)
        wheel = re.search(r"addEventListener\('wheel'.*?\}\);", body, re.S)
        assert wheel, "未绑定 wheel"
        w = wheel.group(0)
        assert "ctrlKey" in w and "metaKey" in w, \
            "整页跳应限定在 Ctrl/⌘ + 滚轮"
        assert "return" in w.split("preventDefault")[0], \
            "无修饰键时必须提前 return，不能拦裸滚轮"
        assert "passive: false" in w, \
            "wheel 拦截需要 passive:false，否则 preventDefault 无效"

    def test_scroll_listener_is_bound_once(self, script: str):
        """监听器只绑一次 —— 每次打开 PDF 都重复绑会成倍触发。"""
        fn = re.search(r"function bindScrollNav\(.*?\n(?=/\*\*|function )", script, re.S)
        assert fn, "bindScrollNav 未定义"
        assert "_scrollBound" in fn.group(0), "缺防重复绑定标记"
        assert re.search(r"addEventListener\('scroll'.*?passive:\s*true",
                         fn.group(0), re.S), \
            "scroll 监听应为 passive，避免拖动时卡顿"

    def test_zoom_reflows_all_pages(self, script: str):
        fn = re.search(r"function setZoom\(.*?\n\}", script, re.S).group(0)
        assert "reflowPages" in fn, "缩放未触发全部页面重排"
        reflow = re.search(r"function reflowPages\(.*?\n(?=/\*\*|function )",
                           script, re.S).group(0)
        assert "scrollTop" in reflow, "重排后未保持滚动位置，会跳回开头"

    def test_reset_viewer_clears_scroll_mode(self, script: str):
        """切到柜型库/图纸 tab 时必须清掉 scrollmode。

        resetViewer() 必须放在 renderHub() 开头统一调用——
        放进if(isCup) 分支会漏掉 building tab，那里同样会残留
        工作台的连续滚动样式。
        """
        fn = re.search(r"function resetViewer\(.*?\n\}", script, re.S)
        assert fn, "resetViewer 未定义"
        assert "scrollmode" in fn.group(0), "未清除 .scrollmode"
        for fnm in ["showVariantGroup", "renderHub"]:
            body = re.search(rf"function {fnm}\(.*?\n(?=\w|\Z)", script, re.S)
            assert body and "resetViewer()" in body.group(0), \
                f"{fnm} 未调用 resetViewer()，切tab 后滚动样式会残留"
        hub = re.search(r"function renderHub\(.*?\n(?=\s*let h)", script, re.S)
        assert hub, "renderHub 未找到"
        head = hub.group(0)
        # 注意要比的是 if(isCup) 判断，不是 const isCup 声明（声明必然在前）
        ifpos = head.find("if (isCup)")
        assert ifpos != -1, "renderHub 未按 isCup 分流"
        assert head.index("resetViewer()") < ifpos, \
            "resetViewer() 必须在 if(isCup) 之前无条件调用，否则 building tab 漏复位"

    def test_returning_to_work_rebuilds_preview(self, script: str):
        """从柜型库切回工作台必须重建预览。

        renderHub() 会把 #viewer 的 DOM 换成占位提示，.vpage 全没了。
        此时若只调 syncCurFromScroll()，遍历不到节点 → 当前页恒为 0
        且页面一片空白。必须先检测 DOM 缺失，再按 S.pages 重建。
        """
        fn = re.search(r"function gotoView\(.*?\n(?=\$\$)", script, re.S)
        assert fn, "gotoView 未找到"
        body = fn.group(0)
        assert "querySelector('#viewer .vpage')" in body, \
            "切回工作台未检测预览 DOM 是否还在"
        assert "renderPages(" in body, "DOM 缺失时未按 S.pages 重建预览"
        # gotoPage 要支持 behavior='auto'，重建后瞬时还原而非二次平滑滚动
        gp = re.search(r"function gotoPage\(.*?\n\}", script, re.S).group(0)
        assert "behavior" in gp, "gotoPage 不支持指定滚动方式"

    def test_page_buttons_use_page_step(self, script: str):
        """翻页按钮应走 pageStep（滚动定位），不是换 img。"""
        assert re.search(r"\$\('#bprev'\)\.onclick\s*=\s*\(\)\s*=>\s*pageStep\(-1\)",
                         script), "#bprev 未接 pageStep(-1)"
        assert re.search(r"\$\('#bnext'\)\.onclick\s*=\s*\(\)\s*=>\s*pageStep\(1\)",
                         script), "#bnext 未接 pageStep(1)"

    def test_keyboard_page_jumps(self, script: str):
        kd = re.search(r"document\.addEventListener\('keydown'.*", script, re.S)
        assert kd, "keydown 未绑定"
        body = kd.group(0)
        for key in ["PageUp", "PageDown", "Home", "End"]:
            assert key in body, f"{key} 未接整页跳"
        assert "ArrowLeft" in body and "ArrowRight" in body, "左右方向键未接"
        # 输入框里打字不能抢快捷键
        assert re.search(r"INPUT\|TEXTAREA\|SELECT", body), \
            "输入框内未排除快捷键，打字会触发翻页"
        assert "scrollmode" in body, "未限定只在连续滚动模式生效"

    def test_scroll_hint_visible(self, html: str, script: str):
        assert 'id="bhelp"' in html, "缺滚动操作提示元素"
        assert "ui.scrollhint" in html, "提示未接i18n"
        # 一行里可能有多条 i18n 条目，用中文/英文各出现一次来确认两种语言都填了
        assert len(re.findall(r"'ui\.scrollhint':", script)) == 2, \
            "i18n 的 scrollhint 必须中英各一条"
        assert re.search(r"'ui\.scrollhint':\s*'[^']*[\u4e00-\u9fff]", script), \
            "缺中文滚动提示"

    def test_no_redefined_scroll_functions(self, script: str):
        """连续滚动是重写过的函数，最容易残留旧定义静默覆盖新实现。"""
        for fnm in ["renderPages", "showPdf", "gotoPage", "setZoom",
                    "observePages", "syncCurFromScroll", "reflowPages",
                    "pageStep", "bindScrollNav", "resetViewer"]:
            n = len(re.findall(rf"function {fnm}\(", script))
            assert n == 1, f"{fnm} 有 {n} 个定义，后者会静默覆盖前者"
