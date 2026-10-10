# 测试夹具说明

## 唯一数据源：真实 DWG（正向，柜型识别）

```
samples/Cold and hot water and gs meter cupboard detail 1.dwg
```

**柜型识别的所有测试都跑在这张真实图纸上。** 由 `real_dwg.py` 提供夹具：

| 夹具 | 说明 |
|---|---|
| `SAMPLE_DWG` | 真实 DWG 路径（仓库根 `samples/`） |
| `real_dwg_path` | DWG 路径，不存在则 skip |
| `real_dxf` | 运行时用 LibreDWG 的 `dwg2dxf` 转成 DXF 并缓存 |
| `GROUND_TRUTH` | 用户人工统计的柜型分布 |
| `GT_CABINETS` / `GT_UNITS` | 柜型总数 / 总套数 |

真实 DWG 约 1.4MB，转出的 DXF 约 6.2MB —— DXF 不进 git，缓存在
`.pytest_cache/real_dxf/`，只在首次使用时转换。没有 LibreDWG 的机器上
相关测试会 **skip**（不 fail）。

### 为什么不复现一份「够用的」小样本

早期版本有个 `make_cabinet_sample.py`，用 ezdxf 画「闭合矩形柜体 + 闭合
矩形表位框」的假 DWG，所有识别逻辑都在这份假数据上调参。假数据的结构
和真实图纸差得很远：

| | 合成样本（假） | 真实图纸 |
|---|---|---|
| 柜体外框 | 闭合 LWPOLYLINE | **4 条跨图层 LINE** |
| 表位 | 闭合矩形 | INSERT block（块内顶点用绝对坐标，`insert` 点偏 6500mm） |
| 柜框对齐 | 上下边与侧墙严格对齐 | 侧墙在上下边端点**内侧** |
| 上下边长度 | 上下完全一致 | 上下差几十毫米 |
| 相邻柜 | 各自分开 | **共用横线** |

在假数据上「配对成功率 100%」的规则，真实图纸上一个都配不出来。合成
样本与生成脚本已删除，不要再重建。

### Ground Truth 与实测的差异

`GROUND_TRUTH` 是用户人工统计的结果。当前解析在该图纸上识别出
**23 个柜型 / 198 套**，与之相差一个 7 Units 柜：Ground Truth 记
`7: 2`，实测 `7: 3`。

三个 7 Units 柜已逐一核实（详见 `test_cupboard_geometry.py`）：

| 位置 | 尺寸 (mm) | gas | water | DIMENSION 标注 |
|---|---|---|---|---|
| `x=-7177..-5712, y=-10173..-7973` | 1465×2200 | 7 | 7 | 1465×2200 ✓ |
| `x=-4729..-3314, y=-10173..-7823` | 1415×2350 | 7 | 7 | 1415×2350 ✓ |
| `x=-2370..-955, y=-10173..-7873` | 1415×2300 | 7 | 7 | 1415×2350 ✓ |

三者互不重叠、各带真实尺寸标注、渲染确认各含 7 gas + 7 water。
**在用户确认之前，Ground Truth 保持原值不改** —— 测试失败是刻意保留的
信号，用来提醒这里存在未对齐的差异，而不是把它改绿。

## `neg_truncated_by_dxf2dwg.dwg` — 反向夹具（必须被拒绝）

由合成 DXF 经 LibreDWG 的 `dxf2dwg` 转换而来。**这个转换过程有损**：

| 字段 | DXF 中真实值 | 转换后 DWG 中实际值 |
|---|---|---|
| 图层名 | `WATER_METER` / `GAS_METER` / … | `W` / `G` / `C` / `A`（截断为首字母） |
| block 名 | `CP-TRI-1x3` / `CP-QUAD-2x2` / … | `C` / `C`（截断） |
| modelspace 实体 | INSERT×3 + TEXT×1 | **0 个** |

而 `dxf2dwg` 的**退出码是 0**，`dwgread` 也能生成一个 ezdxf 能读的 DXF。

**这就是本项目最危险的一类故障：静默数据损坏。** 修复前的 `inspect_dxf()`
会返回 `ok=True`，上层会把截断的图层名和空 modelspace 当成真实柜型库入库，
全程无任何报错。

现在由 `assess_integrity()` 拦截 → `integrity="corrupt"` + `ok=False`。
测试 `test_corrupt_dxf_from_dxf2dwg_is_rejected` 固化此行为。

该文件与柜型识别主流程无关（它验证的是 DWG 静默损坏防护），故保留。

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

- `samples/Cold and hot water and gs meter cupboard detail 1.dwg`：
  用户提供的真实项目图纸。
- `neg_truncated_by_dxf2dwg.dwg`：本项目自建，可自由使用。
- `dwg_real_*.dwg`：取自 LibreDWG 0.13.3 发行包，其内容为 ODA 提供的示例图形。
  **本项目未修改其内容**，仅作为解析链路的测试输入。若你的项目对第三方
  DWG 文件的再分发有顾虑，删除这两个文件即可 —— 相关测试会自动 skip。

## 重新生成

```bash
# 真实 DXF 缓存（一般无需手动执行，测试会自动转换）
dwg2dxf -o .pytest_cache/real_dxf/real.dxf \
  "samples/Cold and hot water and gs meter cupboard detail 1.dwg"
```