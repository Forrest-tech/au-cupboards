"""按用户截图结构生成一个柜型库 DXF 样本 —— **仅用于开发验证**。

结构严格照 2024-10 用户提供的 AutoCAD 截图构造：

- **青色大矩形 = 柜体外轮廓**（闭合 LWPOLYLINE，图层 ``CABINET``）
- **红色矩形 = gas 表位框**（图层 ``GAS_METER``）
- **绿色竖线 + 白色圆 = water 表管路与表头**（图层 ``WATER_METER``）
- 每张图纸里有**多个柜型空间并列**（截图里上方 2 个大柜 + 下方一排小柜）
- 柜体外侧有 DIMENSION 标注链（截图里 ``50/175/200/100/250/175/50``）

这个文件是**我构造的**，不是用户真实图纸。真实图纸到达 samples/ 后，
同一个解析器必须同样适用 —— 所以解析器只依赖通用几何规则
（闭合矩形 + 空间包含 + 图层语义），不依赖本样本的具体坐标。
"""
from __future__ import annotations

import ezdxf
from ezdxf.enums import TextEntityAlignment


def _rect(msp, layer, x, y, w, h, color):
    """画一个闭合矩形（LWPOLYLINE），返回实体。"""
    pl = msp.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
        close=True,
        dxfattribs={"layer": layer, "color": color},
    )
    return pl


def _water_meter_symbol(msp, cx, cy, color=3):
    """water 表符号：竖管 + 绿色表头圆（照截图里的画法）。"""
    # 竖管
    msp.add_line((cx, cy - 60), (cx, cy + 40),
                 dxfattribs={"layer": "WATER_METER", "color": color})
    # 表头（圆）
    msp.add_circle((cx, cy + 55), 22,
                   dxfattribs={"layer": "WATER_METER", "color": color})


def _gas_meter_symbol(msp, cx, cy):
    """gas 表符号：矩形套里的小圆 + 管件（照截图里红框内的白管件）。"""
    _rect(msp, "GAS_METER", cx - 75, cy - 55, 150, 190, color=1)
    msp.add_circle((cx, cy + 20), 26,
                   dxfattribs={"layer": "GAS_METER", "color": 1})
    msp.add_line((cx, cy + 46), (cx, cy + 110),
                 dxfattribs={"layer": "GAS_METER", "color": 1})


def _dim_chain(msp, x0, y0, total, parts, color=8):
    """画一串尺寸标注：总尺寸 + 细分链（截图里柜宽下方那排数字）。"""
    cur = x0
    for p in parts:
        msp.add_linear_dim(
            base=(cur, y0), p1=(cur, y0), p2=(cur + p, y0),
            dxfattribs={"layer": "DIM_CABINET", "color": color},
        ).render()
        cur += p
    msp.add_linear_dim(
        base=(x0, y0 - 220), p1=(x0, y0 - 220), p2=(x0 + total, y0 - 220),
        dxfattribs={"layer": "DIM_CABINET", "color": color},
    ).render()


def build(path: str) -> str:
    """生成样本 DXF。3 个柜型：6 表位(2x3)、4 表位(2x2)、2 表位(2x1)。"""
    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for name, color in [("CABINET", 4), ("GAS_METER", 1),
                        ("WATER_METER", 3), ("DIM_CABINET", 8),
                        ("TEXT_NOTE", 7)]:
        if name not in doc.layers:
            doc.layers.add(name, color=color)

    # ---------- 柜型 A：2 列 x 3 行 = 6 表位（截图左上）----------
    ax, ay, aw, ah = 0, 0, 1300, 2400
    _rect(msp, "CABINET", ax, ay, aw, ah, color=4)
    for r in range(3):
        for c in range(2):
            cx = ax + 325 + c * 650
            cy = ay + 1650 - r * 700
            _gas_meter_symbol(msp, cx, cy)
            _water_meter_symbol(msp, cx - 180, cy - 120)
    _dim_chain(msp, ax, ay - 60, aw, [50, 175, 200, 100, 250, 175, 200, 100, 50])
    msp.add_text("6 SETS - 2 COL x 3 ROW", height=45,
                 dxfattribs={"layer": "TEXT_NOTE"}).set_placement(
        (ax, ay + ah + 120))

    # ---------- 柜型 B：2 列 x 2 行 = 4 表位（截图右上）----------
    bx, by, bw, bh = 1700, 0, 1300, 2400
    _rect(msp, "CABINET", bx, by, bw, bh, color=4)
    for r in range(2):
        for c in range(2):
            cx = bx + 325 + c * 650
            cy = by + 1850 - r * 800
            _gas_meter_symbol(msp, cx, cy)
            _water_meter_symbol(msp, cx - 180, cy - 120)
    # 第 3 格是 water 表组（截图里下方那排 green 表头，没有红框）
    for i in range(4):
        _water_meter_symbol(msp, bx + 180 + i * 300, by + 260)
    _dim_chain(msp, bx, by - 60, bw, [50, 150, 400, 150, 400, 150])
    msp.add_text("4 SETS + WATER BANK", height=45,
                 dxfattribs={"layer": "TEXT_NOTE"}).set_placement(
        (bx, by + bh + 120))

    # ---------- 柜型 C：2 列 x 1 行 = 2 表位（截图下方小图）----------
    cx0, cy0, cw, ch = 0, -3200, 1300, 900
    _rect(msp, "CABINET", cx0, cy0, cw, ch, color=4)
    for c in range(2):
        cx = cx0 + 325 + c * 650
        _gas_meter_symbol(msp, cx, cy0 + 430)
        _water_meter_symbol(msp, cx - 180, cy0 + 250)
    for i in range(6):
        _water_meter_symbol(msp, cx0 + 180 + i * 190, cy0 + 200)
    _dim_chain(msp, cx0, cy0 - 60, cw, [50, 120, 130, 50, 400, 100, 150, 100, 150, 50])
    msp.add_text("2 SETS + WATER BANK", height=45,
                 dxfattribs={"layer": "TEXT_NOTE"}).set_placement(
        (cx0, cy0 + ch + 120))

    # ---------- 噪声：类似 Aect_Duct 风管零件的 block ----------
    # 用来验证解析器不会被零件库带偏（这是第 1 轮翻车的根因）
    for i, nm in enumerate([
        "Aect_Duct_Oval_L1line_Retn_EndCap_Drop_Edge",
        "Aect_Duct_Oval_L1line_Retn_EndCap_Drop_Edge_2",
        "Aect_Elbow_Duct_Oval_90",
    ]):
        b = doc.blocks.new(nm)
        for j in range(8):
            b.add_circle((j * 30, j * 20), 12,
                         dxfattribs={"layer": "MECH", "color": 8})
        msp.add_blockref(nm, (3600, 300 * i), dxfattribs={"layer": "MECH"})

    doc.saveas(path)
    return path


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake_cuplib.dxf"
    print("written:", build(out))
