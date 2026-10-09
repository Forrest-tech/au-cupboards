# 测试夹具说明

## `cupboard_library_design.dxf` — 柜型库设计源（正向）

用 `ezdxf` 生成的**结构真实**柜型库 DXF，代表需求 2 的入库对象：

| 图层 | 含义 | 几何 |
|---|---|---|
| `CUPBOARD` | 柜体外框 | LWPOLYLINE 闭合矩形 |
| `WATER_METER` | 冷热水表 | CIRCLE r=45 |
| `GAS_METER` | 燃气表 | 80×80 闭合方框 |
| `ANNOTATION` | 标注 | TEXT，如 `CP-TRI-1x3-00` |

3 个 block（即 3 个柜型变体）：`CP-TRI-1x3`(1×3)、`CP-QUAD-2x2`(2×2)、`CP-SIX-2x3`(2×3)。

> ⚠️ 这个文件**故意只保留 DXF 形态**。原因见下。

## `neg_truncated_by_dxf2dwg.dwg` — 反向夹具（必须被拒绝）

由 `cupboard_library_design.dxf` 经 LibreDWG 的 `dxf2dwg` 转换而来。
**这个转换过程有损**，实测：

| 字段 | DXF 中真实值 | 转换后 DWG 中实际值 |
|---|---|---|
| 图层名 | `WATER_METER` / `GAS_METER` / `CUPBOARD` / `ANNOTATION` | `W` / `G` / `C` / `A`（截断为首字母） |
| block 名 | `CP-TRI-1x3` / `CP-QUAD-2x2` / `CP-SIX-2x3` | `C` / `C`（截断） |
| modelspace 实体 | INSERT×3 + TEXT×1 | **0 个** |

而 `dxf2dwg` 的**退出码是 0**，`dwgread` 也能生成一个 ezdxf 能读的 DXF。

**这就是本项目最危险的一类故障：静默数据损坏。** 修复前的 `inspect_dxf()`
会返回 `ok=True`，上层会把截断的图层名和空 modelspace 当成真实柜型库入库，
全程无任何报错。

现在由 `assess_integrity()` 拦截 → `integrity="corrupt"` + `ok=False`。
测试 `test_corrupt_dxf_from_dxf2dwg_is_rejected` 固化此行为。

## `dwg_real_acad2000.dwg` / `dwg_real_acad2018.dwg` — 真实 AutoCAD DWG（正向）

来自 LibreDWG 0.13.3 自带测试数据 `test/test-data/example_*.dwg`，
由 ODA 手工绘制。覆盖 AutoCAD 2000 与 2018 两个 DWG 版本，验证
DWG → DXF → ezdxf 降级链在**真实文件**上正常工作。

实测解析结果（两版一致）：

```
integrity   : ok
layers      : 0 / *ADSK_SYSTEM_LIGHTS / Defpoints / Tavolo 2 / Tavolo 3
blocks      : CIRKLO_PUNKTOJ / bloko
entities    : 64  (LWPOLYLINE 11, INSERT 10, DIMENSION 9, LINE 8,
                  ARC 2, SPLINE 2, REGION 2, 3DFACE 2, HATCH, 3DSOLID …)
```

## 许可说明

- `cupboard_library_design.dxf`、`neg_truncated_by_dxf2dwg.dwg`：本项目自建，可自由使用。
- `dwg_real_*.dwg`：取自 LibreDWG 0.13.3 发行包，其内容为 ODA 提供的示例图形。
  **本项目未修改其内容**，仅作为解析链路的测试输入。若你的项目对第三方
  DWG 文件的再分发有顾虑，删除这两个文件即可 —— 相关测试会自动 skip
  （见 `test_dwg.py` 的 `_requires_real_dwg` 标记）。

## 重新生成

```bash
# 正向 DXF（脚本见 tests/fixtures/ 内的 make 逻辑，或直接手工用 ezdxf 画）
python3 - <<'PY'
import ezdxf
doc = ezdxf.new("R2018", setup=True)
for name, color in (("WATER_METER",5),("GAS_METER",1),("CUPBOARD",7),("ANNOTATION",3)):
    doc.layers.add(name, color=color)
doc.saveas("cupboard_library_design.dxf")
PY

# 负向 DWG（需要 libredwg）
dxf2dwg -y cupboard_library_design.dxf -o neg_truncated_by_dxf2dwg.dwg
```
