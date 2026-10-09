# AU Meter Cupboard Scheduling System

澳洲公寓/多住宅**计量柜选型清单系统**。上传建筑图纸 → 自动识别楼层数与每层 units →
匹配柜型库变体 → 导出 PDF / Word / JPG 选型清单。

按需求分析报告第 14 章技术选型实现，**不引入** Frappe / NocoBase / DMS / Temporal。

---

## 权威口径：44 户（已锁定）

以 `74 KEELER ST CARLINGFORD`（35 页 A3，ArchiCAD/PDFTron 出图）实测：

| 楼层 | 单元数 | CO-LIVING | 编号 |
|------|-------|----------|------|
| GROUND | 8 | 1 | `G01`–`G08` + `CLA1` |
| LEVEL 1 | 11 | 0 | `101`–`111` |
| LEVEL 2 | 11 | 0 | `201`–`211` |
| LEVEL 3 | 11 | 0 | `301`–`311` |
| LEVEL 4 | 3 | 1 | `401`–`403` + `CLA2` |
| **合计** | **44** | **2** | 空间总数 46 |

**三重独立验证**：

1. **A008 单元表**：GROUND 8 + L1 11 + L2 11 + L3 11 + L4 3 = 44
2. **封面 ROOM MIX**：`43 DOUBLE ROOMS + 1 SINGLE ROOM + 2 COMMUNAL LIVING AREA`
3. **`MIN DRY AREA: SINGLE ROOM 12SQM, DOUBLE ROOM 16SQM` 规则**：GROUND `G01` 的
   DRY = 12.18m² < 16m²，是全楼唯一 SINGLE，其余 43 户均 ≥16m²

早期流传的 28 / 32 口径均已作废：

- **28** = 平面图 `UNIT n` 连续序号的最大值（不是户数）
- **32** = 早期正则统计误差（漏了 GROUND 8 户 + 每层漏 110/111）
- **38** = 漏了 GROUND，且每层少算 2 户

---

## 双源交叉验证

系统不信任单一数据源。**两个独立来源逐户对账，`verdict=AGREE` 才算通过**：

| 数据源 | 角色 | 实测|
|--------|------|------|
| 逐张平面图坐标法解析 | 主信号 | 46 个空间 |
| `UNIT SCHEDULE` 单元表 A008 | 最硬证据（一次给出全部楼层/面积/序位） | 46 个空间 |

当前结果：`AGREE`、一致率 `1.0`、面积矛盾 `0` 条、仅平面图 `0`、仅单元表 `0`。

---

## 三/四信号交叉验证 + 置信度分诊

| 信号 | 权重 | 含义 |
|------|------|------|
| `w_text` | 0.60 | 矢量文本层编号 |
| `w_schedule` | 0.40 | 单元表一致率 |
| `w_seq` | 0.25 | 序号连续性 / 缺号 |
| `w_geo` | 0.15 | 几何 |

阈值：`auto_accept >= 0.85`、`needs_review >= 0.55`。当前平均置信度 **0.986**。

---

## 运行

```bash
cd backend
pip install -r ../requirements.txt

# 启动服务（含前端 http://127.0.0.1:8000）
python -m uvicorn app.api.server:app --host 0.0.0.0 --port 8000

# 端到端流水线（命令行）
python -m app.cli run /workspace/var/extract/plan.pdf

# 测试（68 项）
python -m pytest tests/ -q
```

---

## 技术栈（严格按报告第 08 章）

| 层 | 选型 |
|---|---|
| 后端 | FastAPI 0.128 + SQLAlchemy 2.1 + SQLite |
| STAGE 1 解析 | PyMuPDF 1.26（主路径）+ pdfplumber 0.11 + **坐标法配对** |
| DWG | ezdxf 1.4（ODA/LibreDWG 作为外部转换进程） |
| 几何/查询 | Shapely 2.1 / DuckDB 1.5 / NumPy |
| STAGE 2 | 自研匹配器 + 澳洲规范内建校验 |
| STAGE 3 导出 | ReportLab（PDF）/ python-docx（Word）/ Pillow（JPG）/ openpyxl（Excel） |
| 前端 | 单文件 HTML + 原生 JS（无构建步骤） |

---

## 目录

```
backend/app/
  config.py规则加载（外部化，不硬编码）
  config/parse_rules.json      ← 所有解析规则，改这里不改代码
  models/entities.py           SQLAlchemy 数据模型
  parsers/stage1.py            STAGE 1：图纸 → 楼层 + units（含单元表坐标法解析器）
  parsers/dwg.py               DWG → DXF → ezdxf（含代理实体校验）
  services/stage2.py           STAGE 2：选型匹配 + 澳洲规范校验
  services/stage3.py           STAGE 3：PDF / Word / JPG / Excel / JSON 导出
  services/meter_requirement.py表位需求推导（WET 面积分档）
  services/cupboard_seed.py    柜型库种子数据
  services/pipeline.py         端到端流水线 + 落库
  api/server.pyFastAPI 接口
frontend/index.html            三栏 UI（树导航 + 原图预览 + 楼层×柜型矩阵）
backend/tests/
  test_core_assumptions.py     核心假设
  test_regression_74keeler.py  实测踩坑固化
  test_api_e2e.py              端到端 API 行为
```

---

## 三段式流水线

```
STAGE 1  图纸 → 楼层数 + 每层 units（含 DRY/WET 面积、无障碍标记、CO-LIVING）
   ↓
STAGE 2  units → 按 WET 面积分档推导表位数 → 匹配柜型变体 + 澳洲规范校验
   ↓
STAGE 3  选型结果 → PDF / Word / JPG / Excel / JSON（每个数字可溯源）
```

### STAGE 2 的表位需求推导

初版对所有单元硬编码同一套表位，导致 44 户全部匹配同一个柜型 —— 与需求 1
「一楼 5 units → 对应 2 种 cupboard」直接矛盾。

现改为依据 A008 单元表实测的 **WET 面积**分档（澳洲公寓的表位数由湿区数量决定）：

| 档位 | WET 面积 m² | water | hot_water | gas | 表位合计 | 柜型 |
|------|------------|-------|-----------|-----|---------|------|
| W1 | ≤ 3.5 | 1 | 0 | 1 | 2 | `CP-DBL-1x2` |
| W2 | ≤ 7.2 | 1 | 1 | 1 | 3 | `CP-TRI-1x3` / `CP-TRI-3x1` |
| W3 | ≤ 9.2 | 1 | 1 | 2 | 4 | `CP-QUAD-2x2` / `CP-QUAD-1x4` / `CP-QUAD-2x2-ACC` |
| W4 | > 9.2 | 2 | 2 | 2 | 6 | `CP-SIX-2x3` |

实测 46 个空间的 WET 面积只有 9 个离散取值
（`{5.63, 6.21, 6.42, 6.43, 6.44, 6.50, 6.95, 7.01, 8.50, 8.65, 8.66, 10.68, 12.31}`），
正好落在这些档位内。

> **诚实声明**：档位阈值是从本项目 46 个样本反推的经验规律，**不是规范强制的**。
> 真实项目必须由业主提供的计量点清单覆盖（Module B 入库流程）。
> 每个推导结果在UI 上标注 `derived` 来源与 `caveat`，可人工改写。

### 澳洲规范内建校验

| 规范 | 校验项 |
|------|--------|
| Jemena ADG-002 (NSW) | 单户600×100×100mm 下限、净距 ≥100mm（集中式 ≥150mm）、最高点 ≤2200mm |
| ATCO Gas | 表面 300–1000mm / 嵌入式 200–1500mm |
| Water Corporation WA | 水平间距 ≥300mm、垂直间距 ≥100mm（仅多行排布） |

当前 46 个单元：**0 错误 0 警告**。

---

## 实测踩坑清单（全部固化为回归测试）

### 1. 楼层来自 `LEVEL n PLAN`，不是图号

图号第二位是**分区号恒为 1**：

| 页 | 图号 | 第 2 位 | 实际楼层 |
|---|---|---|---|
| p19 | A103 | 1 | LEVEL 1 |
| p20 | A104 | 1 | **LEVEL 2** |
| p21 | A105 | 1 | **LEVEL 3** |
| p22 | A106 | 1 | **LEVEL 4** |

主信号 `FR-002-text-level`；`FR-001-zoning` 降级为兜底。

### 2. 必须用坐标法，不能用文本流正则

A008 单元表 5 个表格块的文本流是**交错**的。纯文本流解析得到
`GROUND=77 户 / L3=22 户 / total=179` 的荒谬结果。必须先定位 5 个 `STOREY`
表头坐标（2 行 × 3 列网格），再按 x 区间 + 纵向跨度最小归属。

### 3. `_column_area` 的 `ty` 曾被写成编号的 x 中心

```python
tx, ty = token_w[0], (token_w[0] + token_w[2]) / 2   # ty 是 x 中心，不是 y！
```

L1 块编号 x≈557、面积 y≈613 → `dy=56` 恰好落进 `(1,60]` 窗口，**纯属巧合**；
GROUND 块编号 x≈165 → `dy=448` 超窗，导致 8 户 + CLA1 的 DRY/WET 面积全部丢失。
修正后 **46/46 面积 100% 覆盖**。

### 4. 块x 右界用了被放宽后的 `x0`

`b["x0"] -= 45` 就地改写后又被用来算右界，使 LEVEL 1 块右界错成 `1190`（跨越到
LEVEL 4）。修正：预先存`x0_raw`，右界与排序都用原始表头 x。

### 5. 剖面图否决必须放在「存在单元编号」判断之前

A301/A302/A404 剖面图的索引条会列出各层编号（105/205/305…）。
`_SECTION_RE` 否决项若排在 `_has_unit_labels()` 之后会失效。

### 6. 横排柜不应报垂直间距警告

`1x3`/`1x4` 横排柜的 `meter_spacing_v` 恒为 0（本就没有垂直间距）。
曾对所有 `positions_total>1` 的柜型都查垂直间距，44 户刷出 **40 条无意义警告**，
把真正该看的警告淹没。

### 7. 无障碍变体不能靠「间距大」抢位

曾用「表位间距大者优先」排序，导致 `-ACC` 变体（间距 350）排在普通变体（300）
之前，**39/44 户被错配成无障碍柜**。改为显式 `is_acc` 布尔项排序。

### 8. 序号提取必须先剥离楼层前缀

`seq_of('101')` 曾返回 `101` 而非 `1`，导致序号连续性信号出现 1~101 的假缺口。

### 9. `words` 的 y 并非全局有序

循环里用 `break` 提前退出会漏数据，必须 `continue` 全扫。

### 10. 标题栏数字与电话是噪声

设计师电话 `449 984 889` 会被误认为单元编号，已加入 `noise_labels`；
标题栏带（含`DRG NO` / `PROJECT NO` / `REVISION NO` / `DRAWING NO`）整体排除。

---

## API

```bash
GET    /api/health                     # 后端可用性 + DWG 工具探测
GET    /api/projects                   # Building 列表（需求 3）
POST   /api/buildings# 新建 Building 节点
POST   /api/parse?kind=building        # 上传并解析建筑图纸
POST   /api/parse?kind=cupboard        # 上传柜型 DWG 并分解预览
GET    /api/file/{job_id}              # 原图预览（回传原始 PDF 字节）
GET    /api/jobs/{id}                  # 任务详情（楼层矩阵/单元明细/溯源）
GET    /api/jobs/{id}/exports          # 导出文件路径
GET    /api/units?job_id=N             # 单元 + 选型结果
GET    /api/cupboards                  # 柜型按 meter 组合分组（需求 2）
GET    /api/variants                   # 柜型变体列表
POST   /api/variants                   # 新增柜型
PATCH  /api/variants/{id}              # 编辑柜型文字/尺寸
DELETE /api/variants/{id}              # 删除柜型
POST   /api/variants/compare           # 柜型对比
POST   /api/cupboards/from-dwg         # DWG 分解结果确认入库
GET    /api/download?path=...          # 下载导出文件
POST   /api/jobs/{id}/corrections      # 人工修正字典
```

---

## 需求对照

| 需求 | 条目 | 状态 |
|------|------|------|
| **1** 主界面三栏 | 左侧树状导航 | ✅ |
| | 中间原样显示 PDF/Word/DWG | ✅ |
| | 右侧按楼层按行显示 units 数与 cupboard 类型/样式/尺寸/描述 | ✅ |
| | 导出 PDF / Word / JPG | ✅（另附 Excel / JSON） |
| **2** 柜型库入库 | 顶部导航入库入口 | ✅ |
| | 上传 → 预览 → 解析 → 分解后预览 → 人工确认 → 入库 | ✅（DWG 后端依赖见下） |
| | 左侧按 water+gas 数量分组 / 右侧显示排布种类 + 尺寸 + 介绍 | ✅ |
| | 比较 / 删除 / 编辑文字| ✅ |
| **3** Building 管理 | 顶部导航 building 添加 | ✅ |
| | 上传图纸 → 预览 → 挂载树节点 → 自动解析楼层与 units | ✅ |
| **4** 技术要求 | 成熟算法/API key | ✅（ezdxf/PyMuPDF/pdfplumber，无 AI 依赖） |
| | 100% 识别 DWG/PDF/JPG | ⚠️ PDF 100%；DWG 需外部转换后端（见下） |
| | 完整测试通过后交付 | ✅ 68 项测试通过 |
| | 本地使用 | ✅ 单命令启动 |
| | 代码保存在 GitHub | ⏳ 待推送凭据 |

---

## DWG 链路状态

```
ODA File Converter  →  需从 open-design-alliance.com 免费注册下载
LibreDWG (dwgread)  →  可从 GNU FTP 源码编译（本机已内置编译工具链）
```

`parsers/dwg.py` 已实现完整降级链：**ODA → LibreDWG → 报错并给出明确安装指引**，
并含代理实体（`ACAD_PROXY_ENTITY`）校验。

已用 `ezdxf` 构造等价 DXF 验证下游解析逻辑：

```
layers   = ['0', 'Defpoints', 'GAS', 'HOT_WATER', 'WALL', 'WATER']
blocks   = ['CP-A-2X2', ...]
entities = {'LWPOLYLINE': 5, 'INSERT': 1}
代理实体校验 → 已实现，会在解析结果中显式警告
```

**DWG 优先级说明**：`ODA File Converter` 是商业免费注册件，能100% 正确读 R13–R2018
全部版本；`LibreDWG` 对 2004+ 版本支持良好，R13/R14 有已知缺失。**生产环境建议装ODA。**

---

## 已知限制

1. **DOCX / JPG 输入**：已接收并预览，但尚未接入自动解析器。API 返回明确的
   可执行指引（`error` 字段提示转PDF），不静默失败。扫描件 JPG 需OCR，
   按需求 4 只在「无文本层」时启用 AI 且强制人工确认。
2. **柜型库尺寸是POC 占位值**（标注 `estimated`），真实参数必须由业主 DWG 解析入库。
3. **WET 面积分档阈值**是经验规律而非规范强制（见上文诚实声明）。
4. **AI 完全未启用**：`used_ai=False`。当前实测图纸全部含矢量文本层，无需 AI。

---

## 测试

```bash
cd backend && python -m pytest tests/ -q
# 68 passed
```

| 文件 | 项数 | 覆盖 |
|------|------|------|
| `test_core_assumptions.py` | 25 | 核心假设（坐标法、规范校验、配置外置） |
| `test_regression_74keeler.py` | 22 | 实测踩坑固化 + 44 户口径锁定 |
| `test_api_e2e.py` | 21 | 端到端 HTTP 行为（需求 1/2/3逐条） |