# Weft（纬纱）

<div align="left">

[![CI](https://github.com/yuyu2929/weft/actions/workflows/ci.yml/badge.svg)](https://github.com/yuyu2929/weft/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Dual_(NC_%2F_Commercial)-blue)](LICENSE.md)

</div>

> 💰 **个人免费，商业付费**：个人学习/研究/个人项目永久免费；高校与公共研究机构免费；
> 公司等营利实体的任何业务用途需购买[商业许可](COMMERCIAL-LICENSE.md)。

**一种统一的矢量-栅格数据结构**：一次编码，同时回答矢量问题（这块地是谁的、
这条管线穿哪些格）与栅格问题（这格被覆盖多少、这里有多少传感器）。

> 命名：网格骨架是**经线**（warp，固定、可寻址），跨格要素是**纬线**
> （weft，一次织入、纵横穿过多个格而不被剪断），叶子里的积分栅格场是
> 织成的**布面**——从经纬中自然生出、可拆了重织。

```
pip install -e .          # 或直接把 weft/ 加进 PYTHONPATH（零依赖，纯 Python + numpy）
python3 -m weft demo       # 一键演示：合成工程区 → 建格 → 查询 → 基准
python3 -m pytest tests/   # 111 项测试
```

## 解决什么问题

GIS 领域矢量与栅格长期是两套 stack：MVT/GeoParquet 管矢量交付，
COG/栅格金字塔管栅格服务。由此产生四个工程顽疾（详见
[docs/RESEARCH.md](docs/RESEARCH.md) 的 12 结构横向对照）：

1. **边界切割**：MVT 把跨瓦片地块切成多段、属性重复，hover 拾取只能靠 id 去重；
2. **栅格不可编辑**：COG 改一个像素要重生整个文件；
3. **两套表示漂移**：矢栅各存一份，一致性靠人工同步；
4. **不可复现**：浮点/字典序不确定 → 同数据两次导出字节不同，无法 diff/审计。

Weft 用单一结构同时消除这四个：**要素一次存储绝不切割**（格内只存 fid 引用）、
**叶子积分栅格场可随时从矢量重算**（编辑只重算脏格）、
**确定性序列化 + 内容哈希**（同输入逐字节同输出）。

## 快速开始

```python
from weft import Weft, Layer
from weft.codec import serialize

weft = Weft(1000, 1000, [
    Layer(name="parcels", kind="polygon", columns=("owner",)),
    Layer(name="sensors", kind="point", columns=("kind",)),
])
weft.add_polygon("parcels", [[[0,0],[500,0],[500,500],[0,500],[0,0]]], {"owner": "ACME"})
weft.add_point("sensors", 250, 250, {"kind": "temp"})
weft.commit()

weft.exact_point_query("parcels", 250, 250)   # [1] 精确矢量查询
weft.field_at("sensors", 250, 250)            # (1.0, 0.0) 栅格场 + 误差界
weft.field_at("parcels", 250, 250)            # (≈1.0, 0.0625) 带误差界的近似
blob = serialize(weft)                        # 确定性字节流，blake2b 内容哈希
```

CLI：`python3 -m weft build in.geojson -o out.weft && python3 -m weft info out.weft`

## 结构一览

| 组件 | 职责 |
|---|---|
| 分区四叉骨架 | 叶子构成域的分区（非 overlap）；超限（32）分裂重分发 |
| 复合地址键 | `key = (depth<<40) \| hilbert`，单一 uint64，纯整数父子/祖先运算 |
| 要素块 | 几何一次存储，**绝不按格裁剪**；格内只存 fid 引用 |
| 缝合计账本 | 跨格共边事件 `(key_a, key_b, fid)`：拾取完备、边界审计、脏格推导 |
| 叶子积分场 | 每叶 8×8×层数 float32：面=面积占比（有误差界）、点=计数（精确）、线=长度密度 |
| 版本 + 脏格 | 编辑只重算受影响叶；日志 + 内容哈希 = 可审计快照 |
| 企业门控 | 分片 / 合并 / 认证加密 / 审计链 / 诊断（Ed25519 许可证，`enterprise.py`） |

理论细节（引理与证明、复杂度表）见 [docs/THEORY.md](docs/THEORY.md)；
设计过程中的三次键编码失败、分区纪律修复、KNN 终止条件错误、边界退化
等踩坑全记录见 [DEVLOG.md](DEVLOG.md)。

## 基准（1000×1000m 合成工程区：574 地块 + 71 道路 + 800 传感器）

`python3 -m weft bench` 完整输出，固定种子可复现：

| 指标 | Weft | 对照 | 结论 |
|---|---|---|---|
| 存储 | 184 KB | GeoJSON 173 KB / MVT 瓦片 769 KB / 栅格金字塔 328 KB | Weft 不是更小，是**一次编码同读矢栅** |
| 边界切割 | **0** 个要素被切 | MVT：574/574 地块被切碎、重复 8575 份 | 几何一次存储 |
| 点查询延迟 | 109 µs | 暴力全表 6509 µs | **60×**（叶内精确） |
| 场查询延迟 | 15 µs | 栅格金字塔 0.8 µs | **Weft 慢**（诚实记录：优势在矢量权威+误差界+增量） |
| KNN (k=10) | 0.17 ms | 暴力 0.39 ms | **2.3×**，结果与暴力逐一致 |
| 增量编辑 | 24 脏格 / +2.7 KB / 112 ms | MVT 重切 18 瓦片 / COG 重生 1.3 MB | 局部重算 vs 整文件重生 |
| 局部性 | 3×3 邻域 1.91 段 | Morton 2.25 段 | Hilbert 聚类保持更优 |

## 诚实的能力边界

1. **单文件单有界矩形域**。不做全球球面（那是 S2/H3 的领域）；多域用多文件，v1 不做。
2. **纯 2D**。无高程/时间维。
3. **面层场是估计值**（误差界 1/sub²，默认 0.0625）。精确答案必须回落几何测试——
   Weft 提供开关，不承诺"栅格即精确"。
4. **裸场查询比纯栅格慢**（算 key + dict 查找）。Weft 的赌注是"矢量权威 +
   可增量"比"裸查快"更值钱——读多写少、要审计的工程管线。
5. **原型规模**：单机内存版。企业级分片/多写者合并走 `enterprise` 门控路径。
6. **不是 PostGIS 替代品**：SQL 生态、任意拓扑运算不在范围。

## 商业模式（open-core 三层）

| 层 | 内容 | 许可 |
|---|---|---|
| 社区版 | 完整单机能力：建格、查询、积分场、序列化、校验、CLI、基准 | 个人/非营利免费；商业需授权 |
| 团队版 | 多文件目录、并发只读快照、诊断报告 | 订阅制 |
| 企业版 | 分片、合并、认证加密、审计链、诊断、SLA | 订阅制（Ed25519 许可证门控） |

细节见 [docs/BUSINESS.md](docs/BUSINESS.md)。

## 开发

```bash
python3 -m pytest tests/ -q     # 111 项测试（地址穷举/纪律/往返/向量/门控/CLI）
python3 -m weft bench           # 基准套件
python3 scripts/gen_keys.py     # 生成 Ed25519 签发密钥对（本地，勿提交私钥）
python3 scripts/issue_license.py --org ACME --seats 25 --days 365 --features shard,merge,encrypt
```

## 目录

```
weft/            # 包：address/geom/grid/codec/query/license/enterprise/baseline/bench/cli
docs/            # RESEARCH.md（12 结构调研）/ THEORY.md（理论）/ BUSINESS.md（商业）
tests/           # 111 项测试
scripts/         # gen_keys.py / issue_license.py / demo.py
DEVLOG.md        # 设计踩坑全记录
```

## 许可

**个人免费，商业付费**（见 [LICENSE.md](LICENSE.md)）：个人与非营利组织用
PolyForm Noncommercial 永久免费；公司等营利实体需[商业许可](COMMERCIAL-LICENSE.md)。

> ⚠️ 本项目**不是 OSI 开源项目**，准确说法是 **source-available**（源码可见）。
> open-core 红线不变：许可证只锁多节点运维能力，不锁本地使用。
