"""商业空间解析器 - 独立于住宅逻辑

用于商业建筑（0住宅单元），按楼层统计隔间（房间）数量和种类。
与 stage1.py 住宅解析器完全独立，互不影响。
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF


@dataclass
class CommercialFloor:
    """商业楼层"""
    floor_code: str
    space_count: int
    spaces: list[str] = field(default_factory=list)


@dataclass
class CommercialResult:
    """商业解析结果"""
    floors: list[CommercialFloor] = field(default_factory=list)
    total_spaces: int = 0


# 房间关键词白名单（通用，不硬编码特定项目）
_ROOM_KEYWORDS = {
    'LIFT', 'LOBBY', 'BATH', 'WC', 'KITCHEN', 'STORE', 'REFUSE', 'RECYC',
    'LINEN', 'FIRE', 'ELEC', 'COMS', 'DB', 'HT', 'CHW', 'CIRCULATION',
    'SERVICE', 'STUDIO', 'SUITE', 'AMENITY', 'SAUNA', 'CHANGE', 'BAR',
    'RETAIL', 'OFFICE', 'MEETING', 'STORAGE', 'PLANT', 'PUMP',
}

# 噪声过滤
_NOISE_EXACT = {
    'DRAWING', 'SPECIFICATION', 'SCHEDULES', 'CONSULTANTS', 'DOCUMENTATION',
    'PRELIMINARY', 'GENERAL NOTES', 'LEGEND', 'PLAN', 'SECTION', 'ELEVATION',
    'DKO', 'ABN', 'FOR COORDINATION', 'ARCHITECTURAL', 'GENERAL', 'NOTES',
}
_NOISE_CONTAINS = [
    'SPECIFICATION', 'DOCUMENTATION', 'CONJUNCTION', 'PRELIMINARY',
    'ACCORDANCE', 'INFORMATION', 'DIMENSIONS', 'PROPERTIES',
]
# 不完整的短语结尾（介词/连词）
_INCOMPLETE_ENDINGS = {'TO', 'AND', 'OR', 'OF', 'THE', 'A', 'AN', 'IN', 'ON', 'FOR', 'WITH'}

# 楼层标题识别（复用通用规则）
_LEVEL_RE = re.compile(
    r"\bLEVEL\s*\d{1,2}\s*PLAN\b"
    r"|\bLEVEL\s*\d{1,2}\b(?!\s*(PLANTER|ROOF))"
    r"|\bGROUND\s+FLOOR\s*PLAN\b|\bGROUND\s+PLAN\b"
    r"|\bGROUND\b(?=\s*-\s*)",
    re.I,
)


def _extract_floor_code(text: str) -> str | None:
    """从文本提取楼层代码"""
    # GAP Level 01 / GAP Ground 格式
    m = re.search(r'GAP\s+(Level\s*\d+|Ground)', text, re.I)
    if m:
        g = m.group(1)
        if 'ground' in g.lower():
            return "GROUND"
        n = re.search(r'\d+', g)
        if n:
            return f"L{int(n.group())}"
    # 通用 LEVEL n
    m = re.search(r'\bLEVEL\s*(\d{1,2})\b', text, re.I)
    if m:
        return f"L{int(m.group(1))}"
    if re.search(r'\bGROUND\b', text, re.I):
        return "GROUND"
    return None


def _extract_spaces(page: fitz.Page) -> list[str]:
    """从单页提取商业空间名"""
    text = page.get_text()
    # 匹配大写标签
    candidates = re.findall(r'\b([A-Z]{2,}(?:[\s\/\-]+[A-Z]{2,}){0,2})\b', text)
    spaces = set()
    for c in candidates:
        # 清理换行
        c = ' '.join(c.split())
        if not (3 <= len(c) <= 30):
            continue
        if c in _NOISE_EXACT:
            continue
        if any(n in c for n in _NOISE_CONTAINS):
            continue
        # 必须包含房间关键词
        if not any(kw in c for kw in _ROOM_KEYWORDS):
            continue
        words = c.split()
        if not (1 <= len(words) <= 3):
            continue
        # 排除不完整短语（以介词/连词结尾）
        if words[-1] in _INCOMPLETE_ENDINGS:
            continue
        # 去重合并的标签 (如 "LIFT\nLIFT" -> "LIFT")
        uniq_words = []
        for w in words:
            if not uniq_words or uniq_words[-1] != w:
                uniq_words.append(w)
        c = ' '.join(uniq_words)
        spaces.add(c)
    return sorted(spaces)


def parse_commercial(pdf_path: str) -> CommercialResult:
    """解析商业 PDF，按楼层统计空间
    
    只处理被识别为楼层平面图的页面。
    """
    doc = fitz.open(pdf_path)
    floor_spaces: dict[str, set[str]] = {}
    
    for page in doc:
        text = page.get_text()
        # 必须是楼层平面图
        if not _LEVEL_RE.search(text):
            continue
        floor_code = _extract_floor_code(text)
        if not floor_code:
            continue
        spaces = _extract_spaces(page)
        if floor_code not in floor_spaces:
            floor_spaces[floor_code] = set()
        floor_spaces[floor_code].update(spaces)
    
    floors = [
        CommercialFloor(floor_code=fc, space_count=len(s), spaces=sorted(s))
        for fc, s in sorted(floor_spaces.items())
    ]
    return CommercialResult(
        floors=floors,
        total_spaces=sum(f.space_count for f in floors),
    )


if __name__ == "__main__":
    import sys
    pdf = sys.argv[1] if len(sys.argv) > 1 else \
        "/home/hatch/workspace/user/files/c61e28f5d410da15-260119_River_Terrace_Surfers_Paradise_WIP_Drawing_Set.pdf"
    r = parse_commercial(pdf)
    print(f"Floors: {len(r.floors)}, Total spaces: {r.total_spaces}")
    for f in r.floors:
        print(f"\n{f.floor_code}: {f.space_count} spaces")
        for s in f.spaces[:15]:
            print(f"  - {s}")
