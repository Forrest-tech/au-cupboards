"""STAGE 1 解析器：图纸 → 楼层 + 每层 units。

方法论依据（报告第 04/06 章 + AECV-Bench 实测 + 74KEELER ST 实测）
--------------------------------------------------------------
· 矢量文本层是主路径，零AI、成本为零、准确率 95–98%
· **坐标法配对**，不用文本流顺序 —— 实测证明文本流顺序会漏配
· **双数据源交叉验证**：逐张平面图解析（主）+ UNIT SCHEDULE 单元表（校验）
  实测 A008 单元表一次给出全部 44 户 + 面积 + 序位，是最硬的证据
· 四信号交叉验证：文本编号 + 序号连续性 + 几何连通域 + 单元表
· 置信度分诊：高自动通过 / 中需人工确认 / 低标记待查
· AI 仅在无文本层（扫描件）时启用，且强制人工确认

实测踩坑记录（全部已固化为回归测试）
------------------------------------
1. 图号第二位是分区号恒为 1（A103/A104/A105/A106 第2位都是 1），
   图号不能作为楼层信号 →降级为 FR-001-zoning 兜底
2. 单元编号不只有 101–109，还有 110/111（实测每层 11 户不是 9 户）
   正则写成 [1-4]0[1-9] 会静默漏掉 110/111 → 必须写成 [1-9](0[1-9]|1[0-9])
3. GROUND 层用字母编码 G01~G08，与数字编码完全不同 → 独立规则 UR-002
4. 设计师电话 +61 449 984 889 被拆成 449/984/889 三个独立词，
   必须列入noise_labels排除
5. A004是 Site 图不是楼层平面图，被 marker 计数误判 → 图号前缀排除表
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import fitz  # PyMuPDF

from app.config import ParseConfig


# ---------------------------------------------------------------- 结果结构


@dataclass
class UnitCandidate:
    label: str
    floor_no: int
    seq: int | None
    floor_code: str
    area_m2: float | None = None
    area_required_m2: float | None = None
    accessible: bool = False
    is_communal: bool = False
    label_scheme: str = "floor_coded"
    confidence: float = 0.0
    confidence_tier: str = "medium"
    sources: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def merge_evidence(self, other: "UnitCandidate") -> None:
        """把另一数据源（单元表）的证据合并进来，置信度取高者。"""
        if other.area_m2 is not None and self.area_m2 is None:
            self.area_m2 = other.area_m2
        if other.area_required_m2 is not None and self.area_required_m2 is None:
            self.area_required_m2 = other.area_required_m2
        self.accessible = self.accessible or other.accessible
        self.is_communal = self.is_communal or other.is_communal
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)
        self.confidence = max(self.confidence, other.confidence)
        self.confidence_tier = other.confidence_tier
        self.evidence.setdefault("cross_ref", []).append(other.evidence)


@dataclass
class PageResult:
    page_no: int
    drawing_no: str | None
    title: str | None
    role: str
    floor_no: int | None
    text_layer_present: bool
    units: list[UnitCandidate] = field(default_factory=list)
    confidence: float = 0.0
    signals: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    source_path: str
    page_count: int
    pages: list[PageResult] = field(default_factory=list)
    config_version: str = "1.0"
    diagnostics: dict[str, int] = field(default_factory=dict)
    used_ai: bool = False
    warnings: list[str] = field(default_factory=list)
    # 单元表交叉验证结论
    cross_validation: dict[str, Any] = field(default_factory=dict)

    @property
    def total_units(self) -> int:
        """去重后的单元总数（不含 communal）。"""
        return sum(
            1
            for _ in self.deduped_units()
            if not _.is_communal
        )

    @property
    def total_communal(self) -> int:
        return sum(1 for u in self.deduped_units() if u.is_communal)

    def all_units(self) -> list[UnitCandidate]:
        out: list[UnitCandidate] = []
        for p in self.pages:
            out.extend(p.units)
        return out

    def deduped_units(self) -> list[UnitCandidate]:
        """按 (floor_no, label) 去重合并多来源/多页结果。

        实测踩坑：同一份GROUND 平面图在A007/A009/A010/A011/A014/A102
        多个图号下重复出现（同一套图被拆进不同专业分册），逐页累加会
        得到 GROUND=77 户的荒谬结果。必须按楼层+编号去重。
        """
        merged: dict[tuple[int, str], UnitCandidate] = {}
        for u in self.all_units():
            key = (u.floor_no, u.label)
            prev = merged.get(key)
            if prev is None:
                merged[key] = u
            else:
                prev.merge_evidence(u)
        out = sorted(
            merged.values(),
            key=lambda u: (u.floor_no, u.is_communal, u.seq or 0),
        )
        return out

    def by_floor(self) -> dict[int, list[UnitCandidate]]:
        out: dict[int, list[UnitCandidate]] = {}
        for u in self.deduped_units():
            out.setdefault(u.floor_no, []).append(u)
        return out

    def floor_summary(self) -> list[dict[str, Any]]:
        """需求 1 的核心输出：按楼层按行，每层 units 数 + 柜型。"""
        by_fl = self.by_floor()
        rows: list[dict[str, Any]] = []
        for floor_no in sorted(by_fl, reverse=True):
            us = by_fl[floor_no]
            real = [u for u in us if not u.is_communal]
            communal = [u for u in us if u.is_communal]
            rows.append(
                {
                    "floor_no": floor_no,
                    "floor_code": floor_code(floor_no),
                    "unit_count": len(real),
                    "communal_count": len(communal),
                    "labels": [u.label for u in real],
                    "communal_labels": [u.label for u in communal],
                    "total_area_m2": round(sum(u.area_m2 for u in real if u.area_m2), 2),
                    "accessible_count": sum(1 for u in real if u.accessible),
                    "sources": sorted({s for u in real for s in u.sources}),
                }
            )
        return rows


# ---------------------------------------------------------------- 工具


def floor_code(no: int | None) -> str:
    if no is None:
        return "?"
    if no < 0:
        return f"B{abs(no)}"
    if no == 0:
        return "GROUND"
    return f"L{no}"


# 页面身份标识（实测 74KEELER ST 全部图纸）
_LEVEL_PLAN_RE = re.compile(
    r"\bLEVEL\s*\d\s*PLAN\b|\bGROUND\s+FLOOR\s*PLAN\b|\bGROUND\s+PLAN\b",
    re.I,
)
# 剖面图/立面图标识 —— 这些页面会在索引条里列出各层编号，须否决
_SECTION_RE = re.compile(r"\bSECTION\s+[A-Z0-9]{1,3}\b|\bELEVATION\b", re.I)


def detect_text_layer(page: fitz.Page) -> tuple[bool, int]:
    """判断页面是否有可用矢量文本层。返回 (有无, 字符数)。

    阈值 200 字符：低于此值基本可判定为扫描件/纯图。
    """
    n = len(page.get_text().strip())
    return (n >= 200), n


# ---------------------------------------------------------------- 主解析器


class Stage1Parser:
    def __init__(self, cfg: ParseConfig) -> None:
        self.cfg = cfg
        self._stats: dict[str, int] = cfg.rule_stats()

    def _hit(self, rule_id: str, n: int = 1) -> None:
        self._stats[rule_id] = self._stats.get(rule_id, 0) + n

    # ---------------------------------------------------------- 页面角色

    def _page_role(self, page: fitz.Page, text: str, drawing_no: str | None) -> str:
        rule = self.cfg.page_role_rules[0]
        upper = text.upper()

        # 强信号：明确的单元表标题
        if rule.unit_schedule_title.upper() in upper:
            self._hit(rule.rule_id)
            return "unit_schedule"

        # 排除表：Site 图/ 总图等不承载单元
        if drawing_no and any(drawing_no.startswith(p) for p in rule.excluded_drawing_prefixes):
            self._hit(rule.rule_id)
            return "other"

        if any(m.upper() in upper for m in rule.unit_schedule_markers):
            self._hit(rule.rule_id)
            return "unit_schedule"

        # **主判据**：标题栏 'LEVEL n PLAN' / 'GROUND FLOOR PLAN' —— 实测
        #   A103=LEVEL 1 PLAN, A104=LEVEL 2, A105=LEVEL 3, A106=LEVEL 4，
        #   GROUND 平面图（A010/A011/A014/A102）虽无 LEVEL 字样但有 GROUND FLOOR。
        #   这是唯一 100% 可靠的平面图身份标识。
        if _LEVEL_PLAN_RE.search(text):
            self._hit(rule.rule_id)
            return "floor_plan"

        # **否决项（先于编号判据）**：剖面图/立面图 —— 实测 A301/A302/A404
        # 等剖面图会在剖面索引条里列出各层单元编号（105/205/305...），
        # 若不否决会被误判为平面图。判据是页面无 LEVEL/GROUND 平面标题。
        if _SECTION_RE.search(upper) and not _LEVEL_PLAN_RE.search(text):
            self._hit(rule.rule_id)
            return "other"

        # 次判据：页面矢量文本层里存在单元编号
        # 实测踩坑：仅靠 marker 词计数会漏判 —— p11/p12/p15 这些
        # GROUND 平面图只含 2 个 'LIVING'，达不到阈值 3，
        # 但它们确实标注了 G01~G08，是真正的楼层平面图。
        if self._has_unit_labels(page):
            self._hit(rule.rule_id)
            return "floor_plan"

        hits = sum(1 for m in rule.floor_plan_markers if m.upper() in upper)
        if hits >= rule.floor_plan_marker_threshold:
            self._hit(rule.rule_id)
            return "floor_plan"
        return "other"

    @staticmethod
    def _has_unit_labels(page: fitz.Page) -> bool:
        """页面是否含单元编号（矢量文本层坐标法）。

        比关键词计数可靠得多：编号是解析器真正要找的东西，
        出现编号就说明这页承载单元信息。
        """
        try:
            words = page.get_text("words")
        except Exception:  # pragma: no cover
            return False
        h = page.rect.height
        code = re.compile(r"^(G0[1-9]|CLA\d{1,2}|[1-9](0[1-9]|1[0-9]))$")
        for w in words:
            if w[1] > h * 0.86:
                continue  # 标题栏带
            if code.match(w[4]):
                return True
        return False

    # ---------------------------------------------------------- 图号/标题

    def _drawing_no(self, page: fitz.Page, text: str) -> str | None:
        """识别标题栏图号。

        关键修正（实测踩坑）：**不能对全页做最短匹配**。
        全页会命中封面/说明页上的 A000，导致 p20(A104) 被误判为 A000
        进而楼层识别为 1。正确做法是**限定在标题栏带内**（页面底部 14%）。
        """
        h = page.rect.height
        footer = fitz.Rect(0, h * 0.86, page.rect.width, h)
        band_text = page.get_text(clip=footer)
        scope = band_text if len(band_text.strip()) >= 10 else text

        for rule in self.cfg.drawing_no_rules:
            best: str | None = None
            for m in rule.compiled.finditer(scope):
                val = m.group(1)
                if best is None or len(val) < len(best):
                    best = val
            if best:
                self._hit(rule.rule_id)
                return best
        return None

    def _title(self, text: str) -> str | None:
        m = re.search(r"DRAWING TITLE\s*:\s*\n?([^\n]{2,80})", text)
        if m:
            return m.group(1).strip()
        m = re.search(r"^(Site:|A\d{3})\s*$", text, re.M)
        return m.group(1).strip() if m else None

    # ---------------------------------------------------------- 楼层

    def _detect_floor(
        self, text: str, drawing_no: str | None, role: str, words: list
    ) -> tuple[int | None, list[str]]:
        """返回 (楼层号, 命中规则列表)。多信号投票。"""
        votes: dict[int, list[str]] = {}
        page_has_ground_labels = any(
            re.fullmatch(r"G0[1-9]", w[4]) for w in words
        )

        for rule in self.cfg.floor_rules:
            # 角色闸门：单元表页面会同时出现 LEVEL 1..4，不能用 FR-002
            if rule.applies_to_roles and role not in rule.applies_to_roles:
                continue

            # 信号 A：图号 A1xx → 楼层（兜底，第二位是分区号不可靠）
            if rule.rule_id == "FR-001-zoning" and drawing_no:
                for pat in rule.compiled:
                    m = pat.search(drawing_no)
                    if m and m.groupdict().get(rule.floor_group):
                        fl = int(m.group(rule.floor_group))
                        votes.setdefault(fl, []).append(rule.rule_id)
                        self._hit(rule.rule_id)
                        break

            # 信号 B：正文 LEVEL n / GROUND FLOOR
            elif rule.rule_id in ("FR-002-text-level", "FR-004-ground"):
                for pat in rule.compiled:
                    for m in pat.finditer(text):
                        gd = m.groupdict()
                        if not gd.get(rule.floor_group):
                            continue
                        # LEVEL n需要 role=floor_plan 才可信
                        if rule.rule_id == "FR-002-text-level" and role != "floor_plan":
                            continue
                        fl = int(gd[rule.floor_group])
                        votes.setdefault(fl, []).append(rule.rule_id)
                        self._hit(rule.rule_id)

            # 信号 C：页面含 G0X 编码 → GROUND
            elif rule.rule_id == "FR-005-ground-label":
                if page_has_ground_labels:
                    votes.setdefault(0, []).append(rule.rule_id)
                    self._hit(rule.rule_id)

        if not votes:
            return None, []

        # 票数最多者胜出；同票取最小楼层绝对值（保守）
        best = max(votes.items(), key=lambda kv: (len(kv[1]), -abs(kv[0])))
        return best[0], sorted(set(best[1]))

    # ---------------------------------------------------------- 单元抽取（坐标法）

    def _extract_units(
        self, page: fitz.Page, floor_no: int
    ) -> tuple[list[UnitCandidate], dict[str, Any]]:
        """核心：坐标法提取单元编号 + 面积配对。

        关键教训：不能用纯文本正则流。实测 p19 的
            104 \\n ACCESSIBLE \\n 17.32 m2
            110 \\n 17.49 m2
        在文本流正则下会漏配，必须按词坐标在邻近窗口内配对。

        多规则支持：GROUND 层走 UR-002（G0X），其他层走 UR-001（数字编码），
        communal（CLA）走 UR-003 且不限制楼层。
        """
        words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,wordno)
        found: dict[str, UnitCandidate] = {}
        area_re = re.compile(r"^\d{1,3}\.\d{1,2}$")
        h = page.rect.height

        rules = [r for r in self.cfg.unit_rules if "floor_plan" in r.applies_to]
        for rule in rules:
            if not rule.accepts_floor("", floor_no) and rule.floor_prefix_mode != "any_floor":
                pass  # accepts_floor 需要 token，此处只做规则筛选
            for i, w in enumerate(words):
                token = w[4]
                if token in rule.noise_set:
                    continue
                if not rule.compiled_code.fullmatch(token):
                    continue
                # 标题栏内的数字是图号/电话，不是单元
                if w[1] > h * 0.86:
                    continue
                # 楼层隔离
                if not rule.accepts_floor(token, floor_no):
                    continue

                self._hit(rule.rule_id)
                x0, y0, x1, y1 = w[0], w[1], w[2], w[3]

                area: float | None = None
                accessible = False
                win = rule.pair_window
                for j in range(i + 1, min(i + win, len(words))):
                    tj = words[j][4]
                    if rule.compiled_code.fullmatch(tj) and tj not in rule.noise_set:
                        break  # 撞上下一个编号，停止
                    if tj.upper() in ("ACCESSIBLE", "ACCESSIBLE."):
                        accessible = True
                    if tj.lower() in ("m2", "m²", "m2."):
                        for k in range(j - 1, i, -1):
                            if area_re.fullmatch(words[k][4]):
                                area = float(words[k][4])
                                break
                        break

                seq = rule.seq_of(token)
                communal = rule.is_communal(token)
                prev = found.get(token)
                if prev is not None and prev.area_m2 is not None:
                    continue  # 同标签重复出现，保留已配到面积的

                found[token] = UnitCandidate(
                    label=token,
                    floor_no=floor_no,
                    seq=seq,
                    floor_code=floor_code(floor_no),
                    area_m2=area,
                    accessible=accessible,
                    is_communal=communal,
                    label_scheme=rule.label_scheme,
                    sources=["floor_plan"],
                    evidence={
                        "rule_id": rule.rule_id,
                        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                        "page": page.number + 1,
                        "token": token,
                    },
                )

        units = sorted(found.values(), key=lambda u: (u.is_communal, u.seq or 0))
        signals = {
            "text_signal": {
                "count": len([u for u in units if not u.is_communal]),
                "communal": len([u for u in units if u.is_communal]),
                "labels": [u.label for u in units if not u.is_communal],
                "communal_labels": [u.label for u in units if u.is_communal],
                "with_area": sum(1 for u in units if u.area_m2 is not None),
            }
        }
        return units, signals

    # ---------------------------------------------------------- 数据源 2：单元表

    def _parse_unit_schedule(
        self, page: fitz.Page
    ) -> tuple[list[UnitCandidate], dict[str, Any]]:
        """解析 UNIT SCHEDULE 单元表 —— 最硬的证据源。

        实测 A008 的结构（文本流是**乱序**的，必须用坐标法）：
            5 个表格块平铺在页面上，2 行× 3 列的网格布局：
              STOREY列 x=96/515/939,  y=572/658/658
            每块结构：STOREY | UNIT No. | DRY/WET AREA | REQUIRED AREA
                      | PROVIDED AREA | GROSS AREA
            编号在 UNIT No. 列下方成对出现（DRY 一个、WET 一个）。

        为什么必须用坐标：文本流把5 个块的内容**交错**在一起
        （GROUND 的编号后面跟着 LEVEL 2 的编号），纯文本流解析会把
        全部 44 户都算到 GROUND 层去。实测踩坑：文本流法得出
        GROUND=77 户 / L3=22 户的荒谬结果。

        正确做法：先定位 5 个 STOREY 表头的坐标，再把每个编号按
        **x 落在哪个块、y 在该块下方**归属到对应楼层。
        """
        if not self.cfg.schedule_rules:
            return [], {}
        rule = self.cfg.schedule_rules[0]
        words = page.get_text("words")

        # 1) 定位所有 STOREY 表头块
        #    实测踩坑：楼层名**不在** STOREY 右侧的连续词流里，而在
        #    **同一 y 带（±6pt）内的其它列**。例如 p9(A008)：
        #      STOREY@(96,572)   ... GROUND@(249,572) FLOOR@(271,572)  → GROUND FLOOR
        #      STOREY@(515,573)  ... LEVEL 1@(709,573)                → LEVEL 1
        #      STOREY@(96,658)   ... LEVEL 2@(290,658)                → LEVEL 2
        #      STOREY@(515,658)  ... LEVEL 3@(709,658)                → LEVEL 3
        #      STOREY@(939,658)  ... LEVEL 4@(1027,658)               → LEVEL 4
        #    因此必须按 y 带 + x 距离扫描，不能按词序。
        blocks: list[dict[str, Any]] = []
        for w in words:
            if w[4].upper() != "STOREY":
                continue
            sx, sy = w[0], w[1]
            # 同行（±8pt）扫描，按 x 升序找最近的楼层名。
            # 关键：'LEVEL' 与层号是两个独立词（LEVEL@709 + '1'@725），
            # 必须按 x 顺序配对，不能只找单个词。
            row = sorted(
                (kw[0], kw[1], kw[4].upper())
                for kw in words
                if abs(kw[1] - sy) <= 8 and kw[0] > sx + 5
            )
            storey: str | None = None
            for k, (kx, _ky, ktext) in enumerate(row):
                if ktext in rule.storey_floor_map and ktext != "GROUND":
                    storey = ktext
                    break
                if ktext == "GROUND":
                    storey = "GROUND FLOOR"
                    break
                if ktext == "LEVEL" and k + 1 < len(row):
                    nxt_x, _nxt_y, nxt_t = row[k + 1]
                    if nxt_t.isdigit() and nxt_x - kx <= 40:
                        cand = f"LEVEL {nxt_t}"
                        if cand in rule.storey_floor_map:
                            storey = cand
                            break
            if storey and storey in rule.storey_floor_map:
                blocks.append(
                    {
                        "storey": storey,
                        "floor_no": rule.storey_floor_map[storey],
                        "x0": sx,
                        "y0": sy,
                        "row_key": sy,
                    }
                )

        if not blocks:
            return [], {"schedule_signal": {"count": 0, "reason": "no_storey_header_found"}}

        self._hit(rule.rule_id)

        # 2) 切分每块的 x/y 范围。
        #    实测 p9(A008) 布局（5 块，2 个 y 带）：
        #      带 y=572: STOREY@95(GROUND)  STOREY@514(LEVEL 1)
        #      带 y=658: STOREY@95(LEVEL 2) STOREY@515(LEVEL 3) STOREY@938(LEVEL 4)
        #    x 边界 = 同带内相邻块的中点；y 下界 = 下一带的y
        band_tol = 8.0
        bands: dict[float, list[dict]] = {}
        for b in blocks:
            key = round(b["y0"] / band_tol)
            bands.setdefault(key, []).append(b)

        for b in blocks:
            b["x0_raw"] = b["x0"]
        for b in blocks:
            # 按原始表头 x 排序：x0 会被就地放宽，用它排序会引入顺序依赖
            same_band = sorted(bands[round(b["y0"] / band_tol)], key=lambda o: o["x0_raw"])
            i = next(k for k, o in enumerate(same_band) if o is b)

            # x 左界：自身 STOREY 表头 x 往左放宽（表头在列组左侧，
            # 首个数据列可能紧贴表头，甚至略偏左）
            b["x0"] = b["x0_raw"] - 45
            # x 右界：同带内右邻块的 **STOREY 原始表头 x** - 1
            #   关键：必须用 x0_raw。踩坑：若用已放宽的 x0，
            #   LEVEL 1 块右界会变成 1190（跨越到 LEVEL 4），
            #   导致 L1 的编号被错误归属或面积串块。
            b["x1"] = (
                same_band[i + 1]["x0_raw"] - 1 if i + 1 < len(same_band) else page.rect.width
            )

            # y 下界：下一 y 带的最小 y（表头行之前）
            lower_y = [o["y0"] for k, obs in bands.items() for o in obs if k > round(b["y0"] / band_tol)]
            b["y1"] = min(lower_y) - 2 if lower_y else page.rect.height
            # 关键：y1 不能只到下一**表头行**，还要覆盖其下方的数据行
            #（实测 L1 块的 GROSS/POS 数据在 y=633，而 L3 表头在 y=658，
            #  若y1=656 则 L1 数据仍在内；但 GROUND 块与 L2 表头同 x，
            #  必须靠跨度最小化区分，见下方 owner 选择）
            # 同时确保 y1 至少覆盖本块自身数据（表头下 80pt）
            b["y1"] = max(b["y1"], b["y0"] + 90)

        # 3) 收集编号并按空间归属
        found: dict[tuple[int, str], UnitCandidate] = {}
        for w in words:
            token = w[4]
            if not rule.compiled_code.fullmatch(token):
                continue
            cx, cy = (w[0] + w[2]) / 2, w[1]

            # 命中所有 x/y 均落在其范围内的块
            owner = None
            best_span = None
            for b in blocks:
                # 用词的**中心 x**判定归属（实测编号与面积的列中心完全对齐 dx=0）
                if not (b["x0"] <= cx <= b["x1"]):
                    continue
                if not (b["y0"] - 2 <= cy <= b["y1"]):
                    continue
                # 多个块的 x 区间可能重叠（GROUND 与 LEVEL 2 的 STOREY 同在 x=96），
                # 必须选**纵向跨度最小**的那个，即最贴合数据的块
                span = b["y1"] - b["y0"]
                if best_span is None or span < best_span:
                    best_span, owner = span, b
            if owner is None:
                continue

            area, area_col = self._column_area(words, w, owner)

            # 编号成对出现（DRY 列 + WET 列，x 相差约 15pt）：
            # 用「楼层 + 编号 + DRY/WET 序号」作key，两列都保留，
            # 面积优先取 DRY 列（净使用面积，柜型选型依据）
            idx_in_row = self._pair_index(words, w, owner)
            key = (owner["floor_no"], token, idx_in_row)
            prev = found.get(key)
            if prev is not None:
                # 同一位置重复：保留有面积者
                if prev.area_m2 is None and area is not None:
                    prev.area_m2 = area
                    prev.evidence["area_column"] = area_col
                continue

            seq_m = self._seq_of(token, owner["floor_no"])
            found[key] = UnitCandidate(
                label=token,
                floor_no=owner["floor_no"],
                seq=seq_m,
                floor_code=floor_code(owner["floor_no"]),
                area_m2=area,
                label_scheme=(
                    "communal" if token.startswith("CLA")
                    else "floor_coded" if token[0].isdigit()
                    else "ground_coded"
                ),
                is_communal=token.startswith("CLA"),
                confidence=0.95,
                confidence_tier="high",
                sources=["unit_schedule"],
                evidence={
                    "rule_id": rule.rule_id,
                    "page": page.number + 1,
                    "storey": owner["storey"],
                    "area_column": area_col,
                    "pair_index": idx_in_row,
                    "token": token,
                    "bbox": [round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1)],
                },
            )

        # 同一编号的 DRY/WET 两列折叠为一个单元。
        #关键：编号行内**偶数索引 = DRY 列**（面积大，柜型选型依据），
        #奇数索引 = WET 列（湿区/干湿分离后的小面积）。
        # 因此显式按 pair_index 判定，不靠 min/max 猜。
        collapsed: dict[tuple[int, str], UnitCandidate] = {}
        for (fl, token, idx), u in sorted(
            found.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])
        ):
            key2 = (fl, token)
            is_dry = idx % 2 == 0
            prev = collapsed.get(key2)
            if prev is None:
                u.evidence["wet_area_m2"] = None if is_dry else u.area_m2
                if is_dry:
                    u.area_m2 = u.area_m2
                collapsed[key2] = u
                continue
            # 已有记录：新的是 WET 列则记为 wet_area，不覆盖 dry
            if u.area_m2 is not None:
                if is_dry:
                    prev.area_m2 = u.area_m2
                    prev.evidence["area_column"] = u.evidence.get("area_column")
                else:
                    prev.evidence["wet_area_m2"] = u.area_m2
            prev.merge_evidence(u)
            # 若原记录缺 DRY 面积而新记录有，用新记录补
            if prev.area_m2 is None and u.area_m2 is not None and is_dry:
                prev.area_m2 = u.area_m2

        units = sorted(
            collapsed.values(), key=lambda u: (u.floor_no, u.is_communal, u.seq or 0)
        )
        by_storey: dict[str, list[str]] = {}
        for u in units:
            by_storey.setdefault(u.evidence["storey"], []).append(u.label)

        signals = {
            "schedule_signal": {
                "blocks": len(blocks),
                "storeys": sorted(by_storey),
                "count": len([u for u in units if not u.is_communal]),
                "communal": len([u for u in units if u.is_communal]),
                "by_storey": {k: len(v) for k, v in by_storey.items()},
                "labels_by_storey": by_storey,
                "with_area": sum(1 for u in units if u.area_m2 is not None),
            }
        }
        return units, signals

    @staticmethod
    def _seq_of(token: str, floor_no: int) -> int | None:
        """提取层内序号（1起）。

        关键修正：不能直接抓编号末尾数字 —— '101' 会得到 101，
        而正确答案是 1（去掉楼层前缀 '1'）。
        """
        m = re.fullmatch(r"[A-Za-z](\d+)", token)
        if m:
            return int(m.group(1))
        m = re.fullmatch(r"[1-9](\d{1,2})", token)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)$", token)
        return int(m.group(1)) if m else None

    @staticmethod
    def _pair_index(words: list, token_w, owner: dict) -> int:
        """判断该编号是 DRY 列还是 WET 列（同一行内的第几个）。

        实测编号行：101 101 102 102 ... 成对出现，
        第一个是 DRY（面积大），第二个是 WET（面积小）。
        """
        ty = token_w[1]
        code_re = re.compile(r"^(G0[1-9]|CLA\d{1,2}|[1-9](0[1-9]|1[0-9]))$")
        # 关键：只统计**编号词**的序号，不能把 UNIT / No. 等表头词算进去
        # （实测 L1 行首是 'UNIT'@515 'No.'@527，若不排除会让 101 拿到索引 2，
        #  被误判成 WET 列）
        same_row = sorted(
            (w[0], w[4])
            for w in words
            if abs(w[1] - ty) <= 2
            and owner["x0"] <= (w[0] + w[2]) / 2 <= owner["x1"]
            and code_re.match(w[4])
        )
        for i, (x, _t) in enumerate(same_row):
            if abs(x - token_w[0]) < 3:
                return i
        return 0

    @staticmethod
    def _column_area(words: list, token_w, owner: dict) -> tuple[float | None, str]:
        """按 x 列对齐取面积（单元表是横向列布局）。

        实测 p9(A008) GROUND 块：
            y=582  UNIT No.  CLA1 CLA1 G01 G01 G02 G02 ... G08 G08
            y=592  DRY/WETAREA  DRY WET  DRY WET ...
            y=613  PROVIDED AREA  50.75 10.68 12.18 6.21 17.31 8.66 ...
            y=623  GROSS AREA     61.43 18.39 25.97 ...
        面积与编号**共享 x 坐标**（±2pt）。

        返回 (最干面积, 列名)：
          · PROVIDED AREA = 单元的净使用面积（DRY 面积），这是柜型选型的依据
          · GROSS AREA= DRY+WET 含湿区，不用于柜型选型
        因此取**所有匹配列中最小的非零面积**（PROVIDED < GROSS）。
        """
        area_re = re.compile(r"^\d{1,3}\.\d{1,2}$")
        #关键修正：ty 必须是编号词的**y 坐标**，不是 x 中心。
        # 原写成 (token_w[0]+token_w[2])/2 是 x 中心 —— 由于面积在编号
        # 下方约 30pt，只有当「编号 x 中心≈面积 y」时才偶然落进窗口。
        # 实测踩坑：L1 块编号 x≈557 / 面积 y≈613 → dy=56 巧合通过；
        # GROUND 块编号 x≈165 / 面积 y≈613 → dy=448 超窗 → 整块面积丢失
        #（8 户 G01~G08 + CLA1 的 DRY/WET 面积全部为 None）。
        tx, ty = (token_w[0] + token_w[2]) / 2, token_w[1]

        # 先识别列标题所在行（y=613 PROVIDED / y=624 GROSS / y=633 POS）
        # 关键：列标题必须在块的**左侧表头区**（表头列 x ≈ 块左界 + 35~45），
        # 否则数据区里的 'POS' 单元（指储藏间，不是列名）会抢占 col_rows。
        # 实测踩坑：POS@512 被当成列名，导致 103 的 16.21 被判成 POS 列而丢弃。
        col_rows: dict[str, float] = {}
        header_x0 = owner["x0"] + 30
        header_x1 = owner["x0"] + 60
        for w in words:
            t = w[4].upper()
            if t not in ("PROVIDED", "GROSS", "POS"):
                continue
            if not (header_x0 <= w[0] <= header_x1):
                continue
            dy = w[1] - ty
            if not (1 < dy <= 60):
                continue
            # 关键：列标题必须落在**块自身的 y 范围**内，
            # 否则会串到相邻块（本块 GROSS 与下一块 PROVIDED 极易混淆）
            if not (owner["y0"] <= w[1] <= owner["y1"]):
                continue
            prev = col_rows.get(t)
            col_rows[t] = w[1] if prev is None else min(prev, w[1])

        if not col_rows:
            return None, ""

        cands: list[tuple[float, str]] = []
        for w in words:
            dy = w[1] - ty
            # 注意：words 的 y 并非全局有序（多栏布局），不能用 break 提前退出
            if not (1 < dy <= 60):
                continue
            if not (owner["x0"] <= (w[0] + w[2]) / 2 <= owner["x1"]):
                continue
            # 关键：必须用**块自身的表头行**，否则会串到相邻块的数据。
            # 实测踩坑：104 在 L1 块，本块 col_rows 因窗口过宽收进了 L3 的
            # PROVIDED 行(y=698.9, dy=115)，导致取到 L3 的 16.81 而非L1 的 17.32。
            if not (owner["y0"] <= w[1] <= owner["y1"]):
                continue
            if not area_re.fullmatch(w[4]):
                continue
            dx = abs((w[0] + w[2]) / 2 - tx)
            if dx > 6:
                continue
            # 判定所在列：取 y 最接近的列标题行
            col = min(col_rows, key=lambda cn: abs(w[1] - col_rows[cn]))
            cands.append((float(w[4]), col))

        if not cands:
            return None, ""

        # 优先 PROVIDED（净面积），其次 POS，最后 GROSS
        rank = {"PROVIDED": 0, "POS": 1, "GROSS": 2, "UNKNOWN": 3}
        cands.sort(key=lambda c: (rank.get(c[1], 3), c[0]))
        return cands[0][0], cands[0][1]

    @staticmethod
    def _nearby_area(
        words: list, token_w, owner: dict
    ) -> float | None:
        """备用：同行右侧查找（兼容竖排表格）。"""
        area_re = re.compile(r"^\d{1,3}\.\d{1,2}$")
        tx, ty = token_w[0], token_w[1]
        for w in words:
            if w[1] > ty + 3:
                break
            if abs(w[1] - ty) <= 3 and w[0] > tx + 1:
                if not (owner["x0"] - 5 <= w[0] <= owner["x1"]):
                    continue
                if area_re.fullmatch(w[4]):
                    return float(w[4])
        return None

    # ---------------------------------------------------------- 交叉验证

    def _cross_validate(self, plan_units: list[UnitCandidate], sched_units: list[UnitCandidate]) -> dict[str, Any]:
        """平面图解析结果 vs 单元表，交叉验证。

        这是本系统最硬的质量保障：两个**独立数据源**互证。
        """
        plan_map = {u.label: u for u in plan_units}
        sched_map = {u.label: u for u in sched_units}

        only_plan = sorted(set(plan_map) - set(sched_map))
        only_sched = sorted(set(sched_map) - set(plan_map))
        both = sorted(set(plan_map) & set(sched_map))

        # 面积一致性
        area_mismatch = []
        for lab in both:
            a, b = plan_map[lab].area_m2, sched_map[lab].area_m2
            if a is not None and b is not None and abs(a - b) > 0.01:
                area_mismatch.append({"label": lab, "plan": a, "schedule": b})

        return {
            "plan_units": len(plan_units),
            "schedule_units": len(sched_units),
            "agreed": len(both),
            "only_in_plan": only_plan,
            "only_in_schedule": only_sched,
            "area_mismatch": area_mismatch,
            "consistency": round(len(both) / max(1, len(set(plan_map) | set(sched_map))), 4),
            "verdict": (
                "AGREE" if not only_sched and not only_plan
                else "PARTIAL" if both
                else "DISJOINT"
            ),
        }

    # ---------------------------------------------------------- 信号 B：序号连续性

    @staticmethod
    def _seq_signal(units: list[UnitCandidate], seq_max: int = 30) -> tuple[dict[str, Any], float]:
        """缺号检测。100% 可靠的交叉验证信号。"""
        real = [u for u in units if not u.is_communal and u.seq is not None]
        communal = [u for u in units if u.is_communal]
        if not real:
            return (
                {
                    "seq_signal": {
                        "expected": 0,
                        "found": 0,
                        "gaps": [],
                        "continuous": False,
                        "communal_skipped": len(communal),
                    }
                },
                0.0,
            )
        seqs = sorted(u.seq for u in real)
        expected = list(range(1, max(seqs) + 1))
        gaps = [s for s in expected if s not in seqs]
        score = len(seqs) / len(expected) if expected else 0.0
        return (
            {
                "seq_signal": {
                    "expected": len(expected),
                    "found": len(seqs),
                    "gaps": gaps,
                    "continuous": not gaps,
                    "coverage": round(score, 3),
                    "communal_skipped": len(communal),
                }
            },
            score,
        )

    # ---------------------------------------------------------- 信号 C：几何

    @staticmethod
    def _geo_signal(page: fitz.Page, n_units: int) -> dict[str, Any]:
        """几何信号：连通域/填充色块计数。

        实测结论（74KEELER ST）：粉色 #FFBFBF 仅存在于底部图例条带，
        不代表单元填充，因此本项目图纸上该信号权重最低，仅用于
        「文本信号与几何信号严重不符」时降级为待人工复核。
        """
        info: dict[str, Any] = {"geo_signal": {"available": False, "reason": ""}}
        try:
            pix = page.get_pixmap(dpi=100)
        except Exception as exc:  # pragma: no cover
            info["geo_signal"]["reason"] = f"render_failed: {exc}"
            return info

        import numpy as np

        n = pix.n
        buf = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, n)
        rgb = buf[:, :, :3].astype(int)
        mask = (
            (np.abs(rgb[:, :, 0] - 255) < 14)
            & (np.abs(rgb[:, :, 1] - 191) < 16)
            & (np.abs(rgb[:, :, 2] - 191) < 16)
        )
        frac = float(mask.sum()) / float(pix.width * pix.height)
        ys = np.where(mask)[0]
        in_legend = bool(ys.size and ys.min() > pix.height * 0.55)
        info["geo_signal"] = {
            "available": True,
            "pink_fraction": round(frac, 5),
            "in_legend_band": in_legend,
            "regions": 0,
            "agrees_with_text": None,
            "note": "粉色仅见于图例带，不可用于单元计数" if in_legend else "存在疑似单元填充",
        }
        return info

    # ---------------------------------------------------------- 置信度

    def _score(
        self,
        units: list[UnitCandidate],
        seq_score: float,
        geo: dict,
        sched_agree: float | None = None,
    ) -> float:
        """四信号加权置信度。

        sched_agree为单元表一致率（None = 本项目无单元表）。
        """
        w = self.cfg.confidence
        real = [u for u in units if not u.is_communal]
        n = len(real)
        if n == 0:
            return 0.0

        text_score = sum(1 for u in real if u.area_m2 is not None) / n
        raw = w.w_text * text_score + w.w_seq * seq_score
        total_w = w.w_text + w.w_seq

        if sched_agree is not None:
            raw += w.w_schedule * sched_agree
            total_w += w.w_schedule
        else:
            raw += w.w_geo * (0.5 if geo.get("available") else 0.0)
            total_w += w.w_geo

        # 样本量修正：单元数越多越可信（L1 的 11 户比 L4 的 3 户证据更足）
        sample_factor = 0.85 + 0.15 * min(1.0, n / 9)
        return round(min(1.0, (raw / total_w) * sample_factor), 4)

    def _tier(self, score: float) -> str:
        w = self.cfg.confidence
        if score >= w.auto_accept:
            return "high"
        if score >= w.needs_review:
            return "medium"
        return "low"

    # ---------------------------------------------------------- 入口

    def parse(self, pdf_path: str, use_ai_fallback: bool = True) -> ParseResult:
        doc = fitz.open(pdf_path)
        result = ParseResult(
            source_path=pdf_path,
            page_count=doc.page_count,
            config_version=self.cfg.version,
        )

        # 先扫一遍拿到单元表（数据源 2）
        sched_all: list[UnitCandidate] = []
        sched_pages: list[PageResult] = []
        for page in doc:
            text = page.get_text()
            if self._page_role(page, text, self._drawing_no(page, text)) != "unit_schedule":
                continue
            us, sig = self._parse_unit_schedule(page)
            # 只有真正识别出 STOREY 表格块的才是单元表；
            # 封面/说明页虽含 'UNIT SCHEDULE' 字样但无表格，必须排除
            sig_blocks = sig.get("schedule_signal", {}).get("blocks", 0)
            if not sig_blocks:
                continue
            sched_all.extend(us)
            pr_s = PageResult(
                page_no=page.number + 1,
                drawing_no=self._drawing_no(page, text),
                title=self._title(text),
                role="unit_schedule",
                floor_no=None,
                text_layer_present=True,
                units=us,
                signals={"schedule_signal": sig["schedule_signal"]},
            )
            sched_pages.append(pr_s)

        sched_by_floor: dict[int, list[UnitCandidate]] = {}
        for u in sched_all:
            sched_by_floor.setdefault(u.floor_no, []).append(u)

        for page in doc:
            text = page.get_text()
            has_layer, nchars = detect_text_layer(page)
            words = page.get_text("words")
            dn = self._drawing_no(page, text)
            role = self._page_role(page, text, dn)
            floor_no, floor_rules = self._detect_floor(text, dn, role, words)

            pr = PageResult(
                page_no=page.number + 1,
                drawing_no=dn,
                title=self._title(text),
                role=role,
                floor_no=floor_no,
                text_layer_present=has_layer,
                signals={"text_chars": nchars, "floor_rules": floor_rules},
            )

            if role == "unit_schedule":
                # 单元表横跨全部楼层，不能用页面级floor_no 表示
                pr.floor_no = None
                pr.signals["floor_rules"] = []
                # 已在预扫描中处理（只有真正含 STOREY 表格块的才算单元表）
                match = next((sp for sp in sched_pages if sp.page_no == pr.page_no), None)
                if match is not None:
                    pr.units = match.units
                    pr.signals.update(match.signals)
                    pr.signals["note"] = "单元表为权威数据源，不在此页计算平面图置信度"
                else:
                    pr.role = "other"
                    pr.signals["note"] = (
                        "含 'UNIT SCHEDULE' 字样但无 STOREY 表格块，判为说明页"
                    )
                result.pages.append(pr)
                continue

            if role == "floor_plan" and floor_no is not None:
                if has_layer:
                    units, sig1 = self._extract_units(page, floor_no)
                    pr.units = units
                    pr.signals.update(sig1)

                    sig2, seq_score = self._seq_signal(units)
                    pr.signals.update(sig2)

                    sig3 = self._geo_signal(page, len(units))
                    pr.signals.update(sig3)

                    # 与单元表交叉验证
                    sched_floor = sched_by_floor.get(floor_no, [])
                    agree: float | None = None
                    if sched_floor:
                        cv = self._cross_validate(units, sched_floor)
                        pr.signals["cross_validation"] = cv
                        agree = cv["consistency"]
                        # 用单元表补齐平面图漏掉的单元（如 110/111 若某页未标）
                        plan_labels = {u.label for u in units}
                        for su in sched_floor:
                            if su.label not in plan_labels:
                                merged = UnitCandidate(**{**su.__dict__})
                                merged.sources = ["unit_schedule", "backfill"]
                                units.append(merged)
                                pr.signals.setdefault("backfilled", []).append(su.label)
                        units.sort(key=lambda u: (u.is_communal, u.seq or 0))
                        pr.units = units

                    pr.confidence = self._score(units, seq_score, sig3["geo_signal"], agree)
                    for u in pr.units:
                        if "unit_schedule" in u.sources and u.confidence == 0.0:
                            u.confidence = pr.confidence
                            u.confidence_tier = self._tier(pr.confidence)
                        elif u.confidence == 0.0:
                            u.confidence = pr.confidence
                            u.confidence_tier = self._tier(pr.confidence)
                else:
                    # 无文本层 → 扫描件。AI 强制人工确认（需求硬约束）
                    pr.signals["ai_required"] = True
                    result.warnings.append(
                        f"p{pr.page_no}: 无矢量文本层（{nchars} 字符），标记为扫描件，AI 结果强制人工确认"
                    )
                    if use_ai_fallback:
                        result.used_ai = True
                        pr.signals["ai_status"] = "not_implemented"
                        result.warnings.append(
                            f"p{pr.page_no}: AI 兜底未启用（实测精度不足，见报告 06 章）"
                        )
            result.pages.append(pr)

        doc.close()

        # 全局交叉验证汇总
        plan_units = [u for p in result.pages if p.role == "floor_plan" for u in p.units]
        if sched_all:
            result.cross_validation = self._cross_validate(plan_units, sched_all)
            if result.cross_validation["verdict"] == "AGREE":
                result.warnings.append(
                    f"平面图与单元表完全一致（{result.cross_validation['agreed']} 户互证）"
                )
            else:
                cv = result.cross_validation
                result.warnings.append(
                    f"平面图/单元表差异：仅平面图 {cv['only_in_plan']}，"
                    f"仅单元表 {cv['only_in_schedule']}"
                )

        result.diagnostics = self._stats
        return result