# AU Meter Cupboard Scheduling System

澳洲公寓/多住宅**计量柜选型清单系统**。上传建筑图纸 → 自动识别楼层数与每层 units →
匹配柜型库变体 → 导出 PDF / Word / JPG 选型清单。

按需求分析报告第 14 章技术选型实现，**不引入** Frappe / NocoBase / DMS / Temporal。

**环境要求：Python 3.11 或更高。** 本项目使用 `X | None` 联合类型语法，
3.9 及以下无法解析。

> **macOS 用户注意**：系统自带的 `python` 是 **Python 2.7**，直接用会报
> `No module named uvicorn`。请先按下面步骤配置环境。

## 安装与运行

### macOS（推荐用 Homebrew装 Python 3）

```bash
# 1) 若还没有 Homebrew，先装
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2) 装 Python 3
brew install python@3.12

# 3) 克隆并进入项目
git clone https://github.com/Forrest-tech/au-cupboards.git
cd au-cupboards

# 4) 建虚拟环境（隔离，避免污染系统 Python）
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 5) 装依赖（务必在仓库根目录执行）
pip install --upgrade pip
pip install -r requirements.txt

# 6) 启动（前端与API 同源）
cd backend
python -m uvicorn app.api.server:app --host 0.0.0.0 --port 8000

# 7) 浏览器打开
#    http://127.0.0.1:8000
```

### Linux / Windows

步骤同上，只是第 2 步换成安装 Python 3.11+，第 4 步的虚拟环境激活命令为：

- Linux / macOS：`source .venv/bin/activate`
- Windows PowerShell：`.venv\Scripts\Activate.ps1`

### 验证安装

```bash
# 后端健康检查（DWG 后端未装时 dwg_ready 为 false，不影响使用）
curl http://127.0.0.1:8000/api/health

# 测试（应输出 111 passed）
cd backend && python -m pytest tests/ -q
```

### 常见问题

| 报错 | 原因 | 解决 |
|------|------|------|
| `python: command not found` | macOS 只有 `python3` | 用 `python3`，或先激活虚拟环境 |
| `No module named uvicorn` | 装到了 Python 2.7 或没装依赖 | `python3 -m pip install -r requirements.txt` |
| `ModuleNotFoundError: PIL` | 用了旧的根目录 requirements | 重新 `pip install -r requirements.txt`（已含 Pillow） |
| `SyntaxError` on `X \| None` | Python < 3.10 | 升级到 3.11+ |
| `Address already in use` | 8000 端口被占 | `--port 8001` |

DWG 解析为可选能力，未装后端时系统照常工作，详见下方「DWG 链路状态」。

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

## 命令行用法

完整安装步骤见上文「安装与运行」。以下命令均需先激活虚拟环境，
且除`uvicorn` 外都在 `backend/` 目录下执行。

### 启动 Web 服务

```bash
python -m uvicorn app.api.server:app --host 0.0.0.0 --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

### CLI

不想开浏览器时，可直接用命令行完成解析与导出：

```bash
# 环境自检（Python 版本 / 规则版本 / DWG 后端）
python -m app.cli health

# 端到端流水线：解析 → 匹配柜型 → 导出五种格式
python -m app.cli run ~/Downloads/COMBINED\ ARCHITECTURAL\ DRAWINGS.pdf

# 查看某次解析的逐单元明细（含 DRY/WET 面积、柜型、尺寸、无障碍标记）
python -m app.cli units 1

# 列出柜型库全部变体
python -m app.cli variants
```

`run` 的实际输出：

```
================================================================
  解析完成：plan.pdf
================================================================
  单元总数    : 44（+2 CO-LIVING，共 46 空间）
  按楼层      : GROUND=8  L1=11  L2=11  L3=11  L4=3
  按柜型      : CP-QUAD-1x4×1  CP-QUAD-2x2-ACC×4  CP-TRI-1x3×39
  按户型      : CO-LIVING×2  DOUBLE×43  SINGLE×1
  交叉验证    : AGREE（一致率 100%）
  合规错误    : 0    警告: 0
  平均置信度  : 0.986

  导出文件：
    json   → .../selection-20261009-044805.json
    excel  → .../selection-20261009-044805.xlsx
    pdf    → .../selection-20261009-044805.pdf
    word   → .../selection-20261009-044805.docx
    jpg    → .../selection-20261009-044805.jpg
    jpg    → .../selection-20261009-044805-p2.jpg
```

> `run` 目前只接受 PDF。传DWG 会返回明确提示并指向
> `/api/parse?kind=cupboard`（DWG 走柜型库入库流程，不进本流水线）。

### 测试

```bash
python -m pytest tests/ -q# 111 passed
```

### DWG 支持（可选）

未装 DWG 后端时系统照常工作，只是上传 DWG 会返回明确的安装指引。
`GET /api/health` 的 `dwg_ready` 字段反映当前可用性。

**方案 A：ODA File Converter（生产推荐）**
免费注册下载 <https://www.opendesign.com/guestfiles/oda_file_converter>，
安装后 `ODAFileConverter` 需在 `PATH` 中。本系统会自动探测并优先使用。

**方案 B：LibreDWG（开源兜底，本环境已装）**

```bash
# Debian/Ubuntu 依赖
sudo apt-get install -y build-essential libtool autoconf

# 源码编译（apt 源与 PyPI 均无 libredwg 包，必须源码编译）
curl -O https://ftp.gnu.org/gnu/libredwg/libredwg-0.13.3.tar.gz
tar xzf libredwg-0.13.3.tar.gz && cd libredwg-0.13.3
./configure --prefix=/usr/local --disable-bindings --disable-shared --enable-static
make -j$(nproc) && make install

# 验证
dwgread --version   # → dwgread 0.13.3
```

> LibreDWG 为 **GPLv3**，本项目仅以独立进程方式调用，不链接其库。
> 编译约 3.5 分钟（4 核）。

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
  parsers/dwg.py               DWG → DXF → ezdxf（代理实体校验 + 完整性分级）
  services/stage2.py           STAGE 2：选型匹配 + 澳洲规范校验
  services/stage3.py           STAGE 3：PDF / Word / JPG / Excel / JSON 导出
  services/meter_requirement.py表位需求推导（WET 面积分档）
  services/cupboard_seed.py    柜型库种子数据
  services/pipeline.py端到端流水线 + 落库
  api/server.py                FastAPI 接口（14 个端点）
  cli.py                       命令行入口（health/run/units/variants）
frontend/index.html            三栏 UI（树导航 + 原图预览 + 楼层×柜型矩阵）
backend/tests/
  test_dwg.py                  DWG 降级链 + 完整性校验（34 项）
  test_core_assumptions.py     核心假设（25 项）
  test_regression_74keeler.py  实测踩坑固化 + 44 户口径锁定（22 项）
  test_api_e2e.py              端到端 API 行为（21 项）
  test_cli.py命令行冒烟（9 项）
  fixtures/
    cupboard_library_design.dxf  自建柜型库 DXF（正向）
    dwg_real_acad2000.dwg        真实 AutoCAD 2000（正向）
    dwg_real_acad2018.dwg        真实 AutoCAD 2018（正向）
    neg_truncated_by_dxf2dwg.dwg 截断损坏样本（反向，必须被拒绝）
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

### 11. DWG 转换器退出码为 0 ≠ 产物可信

用 LibreDWG 自带的 `dxf2dwg` 造测试 DWG 时，**写出端有损**：
`WATER_METER`→`W`、`CUPBOARD`→`C`、`CP-TRI-1x3`→`C`（全部截断为首字母），
modelspace 几何全丢。但 `dxf2dwg` 与 `dwgread` 的**退出码都是 0**，
ezdxf 也能"成功"读出文件 —— 旧实现返回 `ok=True`，会把垃圾数据当柜型库入库。

> **不要相信退出码，要验证产物内容。** 现由 `assess_integrity()` 拦截
> （`corrupt` → `ok=False`），反向夹具 `neg_truncated_by_dxf2dwg.dwg` 固化此行为。

### 12. 柜型库几何在 block 里，只统计 modelspace 会误判为空图纸

一份含 3 个柜型 block、13 个冷热水表圆、13 个燃气表方框的完整 DXF，
modelspace 只有 4 个实体（3× INSERT + 1× TEXT）。只统计 modelspace 会被
完整性校验判成 `degraded`。必须同时遍历 block 定义（排除 `*` 匿名块）。

### 13. `error` 消息不能混入说明性提示

完整性校验早期把所有 notes 拼进 error，产出过
「…block 定义内有 45 个实体——柜型库通常如此…属正常。；图层名疑似被截断…」
这种自相矛盾的用户可见消息。现改为结构化 `IntegrityReport`，
`notes`（说明）与 `issues`（问题）分开，只有 `issues` 进 `error`。

### 14. README 里的命令必须实跑验证

用户会照着 README 敲命令。实测踩坑：README 写了 `python -m app.cli run`，
但 `app/cli.py` **根本不存在**；补写后又引用了一批不存在的字段 ——
`Unit.area_dry_m2`（实际 `area_m2`）、`CupboardVariant.code`（实际 `variant_code`）、
`.width/.height/.depth`（实际 `.w/.h/.d`）、`Selection.job_id`（该列不存在）、
`JobStatus.DONE`（枚举实际是 `QUEUED/RUNNING/NEEDS_REVIEW/CONFIRMED/FAILED`）。

这些错误只有运行时才暴露。现已：
- 补齐 `app/cli.py`（`health` / `run` / `units` / `variants` 四个子命令，全部实跑验证）
- 新增 `test_cli.py` 9 项冒烟测试，把字段名钉死
- 目录结构与依赖清单逐项核对（发现根 `requirements.txt` 漏了 `Pillow`，
  照原README 装会`ModuleNotFoundError: PIL`，已合并为单一权威清单）

### 15. 两份 requirements.txt 必须合并

根目录与 `backend/` 各有一份，内容曾不一致（根目录漏 `Pillow`，而 JPG 导出
依赖它）。现在根目录是唯一权威来源，`backend/requirements.txt` 改为
`-r ../requirements.txt` 引用。类似 `python -m app.cli run` 的路径陷阱要一并避免。

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
| | 中间原样显示 PDF/Word/DWG | ✅ PDF/DWG；Word 走DOCX 指引（见限制） |
| | 右侧按楼层按行显示 units 数与 cupboard 类型/样式/尺寸/描述 | ✅ |
| | 导出 PDF / Word / JPG | ✅（另附 Excel / JSON） |
| **2** 柜型库入库 | 顶部导航入库入口 | ✅ |
| | 上传 → 预览 → 解析 → 分解后预览 → 人工确认 → 入库 | ✅ 真实 DWG 已端到端验证 |
| | 左侧按 water+gas 数量分组 / 右侧显示排布种类 + 尺寸 + 介绍 | ✅ |
| | 比较 / 删除 / 编辑文字| ✅ |
| **3** Building 管理 | 顶部导航 building 添加 | ✅ |
| | 上传图纸 → 预览 → 挂载树节点 → 自动解析楼层与 units | ✅ |
| **4** 技术要求 | 成熟算法/API key | ✅（ezdxf/PyMuPDF/pdfplumber/LibreDWG，无 AI 依赖） |
| | 100% 识别 DWG/PDF/JPG | ✅ PDF 100%（44 户全对）；✅ DWG 已用真实 AutoCAD 2000/2018 验证；⚠️ JPG 走 OCR 指引 |
| | 完整测试通过后交付 | ✅ **111 项**测试通过 |
| | 本地使用 | ✅ 单命令启动 |
| | 代码保存在 GitHub | ⏳ **待推送凭据**（本地已提交，34 个文件） |

---

## DWG 链路状态 ✅ 已端到端验证

```
ODA File Converter  →  需从 open-design-alliance.com 免费注册下载（生产建议）
LibreDWG 0.13.3     →  ✅ 已源码编译安装，dwgread /usr/local/bin/dwgread
```

`parsers/dwg.py` 实现完整降级链：**ODA → LibreDWG → 报错并给出明确安装指引**，
并含代理实体（`ACAD_PROXY_ENTITY`）校验与**转换产物完整性分级**。

### 真实 AutoCAD 文件验证结果

用 LibreDWG 自带的真实 ODA 示例图纸（`tests/fixtures/dwg_real_acad*.dwg`）：

| 指标 | ACAD 2018 | ACAD 2000 |
|---|---|---|
| `integrity` | **ok** | **ok** |
| 后端 | libredwg | libredwg |
| 实体总数 | 82 | 82 |
| 图层 | `*ADSK_SYSTEM_LIGHTS` / `0` / `Defpoints` / `Tavolo 2` / `Tavolo 3` | 同 |
| block | `CIRKLO_PUNKTOJ` / `bloko` | 同 |
| 实体类型 | LWPOLYLINE / INSERT / DIMENSION / LINE / ARC / SPLINE / REGION / HATCH / 3DSOLID / MTEXT / MULTILEADER … 共 20+ 类 | 同 |

两版交叉验证：设计图层集合与实体结构**完全一致** → 转换器对两个 DWG 版本无结构性偏差。

### 完整性分级（`assess_integrity`）

转换器**退出码为 0 不代表产物可信**。`inspect_dxf` 对产物做三级判定：

| 级别 | 判据 | 行为 |
|---|---|---|
| `ok` | 实体数达标 + 图层/block 名正常 | 正常入库 |
| `degraded` | 实体数低于下限 5 | 可用但提示存疑 |
| `corrupt` | 全图 0 实体 / 图层名全被截成单字母 / block 名全被截成单字符 | **`ok=False` + 拒绝入库** |

**为什么必须有这一层** —— 实测踩坑：用LibreDWG 自带的 `dxf2dwg` 造测试 DWG 时，
写出端有损，`WATER_METER`→`W`、`CUPBOARD`→`C`、`CP-TRI-1x3`→`C`，
modelspace 几何全丢，**但退出码是 0**，ezdxf 也能"成功"读出文件。
修复前的实现会返回 `ok=True`，把截断的图层名当柜型库入库，全程零报错。
**静默失败比失败本身危险得多。** 该场景已固化为反向测试夹具
`tests/fixtures/neg_truncated_by_dxf2dwg.dwg`。

### block 定义必须遍历

柜型库 DWG 的几何主体在 block 定义里，modelspace 通常只有几个 `INSERT`。
只统计 modelspace 会把一份含 3 个柜型 block、13 个冷热水表圆的完整图纸
算成"只有 4 个实体"。因此 `DwgParseResult` 同时给出：

- `block_entity_counts` — block 内的实体类型统计（匿名块 `*` 已排除）
- `block_layers` — 每个 block 内的图层清单，**Module B 靠它区分 water / gas 分组**

### 后端优先级

`ODA File Converter` 是商业免费注册件，能 100% 正确读 R13–R2018 全部版本；
`LibreDWG` 对 2004+ 支持良好，R13/R14 有已知缺失。**生产环境建议装 ODA。**
本环境未装 ODA，全部走 LibreDWG 兜底，解析结果中会显式标注
`"使用 LibreDWG 兜底：高版本 DWG 支持不全，代理实体可能丢失"`。

---

## 已知限制

1. **DOCX / JPG 输入**：已接收并预览，但尚未接入自动解析器。API 返回明确的
   可执行指引（`error` 字段提示转PDF），不静默失败。扫描件 JPG 需OCR，
   按需求 4 只在「无文本层」时启用 AI 且强制人工确认。
2. **柜型库尺寸是POC 占位值**（标注 `estimated`），真实参数必须由业主 DWG 解析入库。
3. **WET 面积分档阈值**是经验规律而非规范强制（见上文诚实声明）。
4. **AI 完全未启用**：`used_ai=False`。当前实测图纸全部含矢量文本层，无需 AI。
5. **DWG 走 LibreDWG 兜底**：本环境未装 ODA File Converter。真实 AutoCAD
   2000/2018 已验证通过，但 R13/R14 及含大量代理实体（天正/理正/探索者）
   的图纸可能有损 —— 完整性校验会拦下明显损坏的产物，但**几何级别的
   细微丢失仍需人工比对**。生产部署请装 ODA。
6. **DOCX / JPG 未接入自动解析**：需求 1 要求中间栏"原样显示 Word"，
   当前可上传并预览，但内容不参与解析。

---

## 测试

```bash
cd backend && python -m pytest tests/ -q
# 111 passed
```

| 文件 | 项数 | 覆盖 |
|------|------|------|
| `test_dwg.py` | 34 | DWG 降级链、完整性分级、block 遍历（需求 2/4） |
| `test_cli.py` | 9 | 命令行入口冒烟（字段名回归） |
| `test_core_assumptions.py` | 25 | 核心假设（坐标法、规范校验、配置外置） |
| `test_regression_74keeler.py` | 22 | 实测踩坑固化 + 44 户口径锁定 |
| `test_api_e2e.py` | 21 | 端到端 HTTP 行为（需求 1/2/3逐条） |