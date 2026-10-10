# samples/ — 柜型库真实样本

这个目录用来放**真实的柜型库 DWG**，让我（开发侧）能用真实几何验证
「柜体 / 表位分解」规则，而不是从截图猜。

## 为什么要放真实文件

需求文档 `docs/需求分析报告-AU-Cupboards.md` 2.2 节规定的解析规则是：

1. 闭合多段线 / LWPOLYLINE → **柜体外轮廓**
2. 其内部的闭合矩形 → **表位框**，数量即 units 数
3. 按图层 / 块名区分 gas 表与 water 表
4. 读 DIMENSION 实体 → 精确柜体尺寸

同时 **一个 DWG 内并列画了多个柜型**，需要**空间聚类**切分，不能遍历 block。

已翻车的两次，都是因为没拿到真实文件：

|轮次|我的做法 | 错在哪 |
|---|---|---|
| 第 1 次 | 遍历命名 block，每个渲染一张 JPG | 141 个 block 里 130+ 个是 `Aect_Duct_*` 风管零件 |
| 第 2 次 | 按 block 名/图层打分筛选 | 粒度错了 —— 把**柜内单个表**当成了柜型 |

第 2 次的教训尤其关键：**block 名不是柜型**。真实图纸里 block 是按
零件组织的（一个 water 表符号一个 block），柜子反而是 modelspace 里
若干实体的**空间组合**。所以「一个 block = 一个柜型」这个前提从根上就是错的。

## 怎么放进来

`samples/` 默认**不入库**（`.gitignore` 里已忽略图纸，防止商业图纸意外外泄）。
需要显式强制添加：

```bash
cp "/Users/jadenfly/Desktop/Cold and hot water and gs meter cupboard detail 1.dwg" samples/
git add -f samples/*.dwg
git commit -m "chore(samples): 加入柜型库真实样本用于几何分解验证"
git push
```

如果不想进 git 历史，也可以只把解析诊断报告给我（见下）。

## 不想上传图纸的话：给我诊断报告

代码里内置了诊断模式，会把解析过程逐条打印：

- 找到多少个闭合矩形、按什么依据判为柜体
- 每个柜体内数出多少个表位
- 每个表位落在哪些图层、对应 water 还是 gas
- 哪些实体没被归类、为什么被丢弃

```bash
python -m backend.app.cli diagnose <你的图纸路径>
```

把输出贴给我，我可以照着调阈值 —— 精度不如给文件，但通常 1~2 轮能收敛。
