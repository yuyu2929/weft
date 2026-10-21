# Weft 商业模型：三层 open-core

> 红线原则：**许可证只锁多节点运维能力，不锁本地使用。** 个人开发者、
> 学生、小团队用社区版可以做任何事（含商业咨询交付），永久免费。

## 1. 为什么 open-core 适合这个结构

Weft 的价值分布极不均匀：

- **本地能力**（单文件、单机、查询、积分场、序列化）自包含，
  锁它只会挡住传播——而传播是数据结构类项目唯一的增长引擎；
- **运维能力**（多节点分片、多写者合并、加密、SLA）天然属于
  "有基础设施团队的企业"，他们本来就要为这些付钱。

这和 PostGIS（功能全免费、支持收费）、DuckDB（核心免费、云版收费）
是同一逻辑：**基础设施收费，能力不收费**。

## 2. 三层划分

### 社区版（Community）—— 个人/非营利免费（PolyForm Noncommercial），商业需授权

包含：

- 完整单机结构：建格、插入/删除/更新、全部查询（bbox/KNN/点查询/场查询/栅格导出）
- 确定性序列化 + 内容哈希
- CLI（build/info/query/bench/demo/license）
- 完整测试套件与基准
- 缝合账本、脏格增量、Ed25519 验证器（**验证**免费，**签发**在企业侧）
- `weft verify` 完整性校验（字节可复现 + 内容哈希）——**免费，因为信任不该收费**

不包含：多节点分片、多写者合并、格级加密、SLA。

### 团队版（Team）—— 订阅制（建议 ¥3,600/年/团队）

面向"多人共用一份工程数据"的场景：

- 多文件目录层（多域拼接、统一寻址）
- 并发只读快照（快照隔离，无锁读）
- 诊断报告（场误差统计、格分布健康度、序列化体积分析）
- 邮件/工单支持（下一工作日）

### 企业版（Enterprise）—— 订阅制（建议 ¥60,000/年起，按席位）

面向生产管线：

| 功能 | 门控名 | 实现位置 |
|---|---|---|
| 按键区间多节点分片（规划 + 路由） | `shard` | `enterprise.plan_shards/shard_of` |
| 多写者编辑日志合并（last-writer-wins） | `merge` | `enterprise.merge_journals` |
| 格级**认证加密**（encrypt-then-MAC，篡改即报错） | `encrypt` | `enterprise.seal_cell/open_cell` |
| 编辑审计链（append-only 哈希链，可导出） | `audit` | `weft/audit.py` |
| 运维诊断报告（格分布/场误差/告警） | `diagnose` | `enterprise.diagnose` |
| 支持与赔付 | `sla` | 标记位 |

企业版附带：私有部署支持、字段级权限集成咨询、SLA（99.9% 响应）。

## 3. 许可证机制

- **Ed25519 签名令牌**：`WEFT1.<payload_b64url>.<sig_b64url>`，
  payload = `{org, seats, exp, features}`；
- **纯 Python 实现**（RFC 8032，零依赖）——与 `cryptography` 库
  双向互操作（tests/test_license.py 用 RFC 向量 + 库交叉验证）；
- **运行时只验证**：私钥只在签发方（`scripts/issue_license.py`），
  发布包内嵌公钥，令牌可离线验证、可嵌入 CI；
- **过期不锁数据**：过期/伪造/缺功能都抛 `LicenseError`，但核心读写查询不受影响；
  明确提示获取方式，不静默降级、也不做数据劫持。

```python
from weft.enterprise import plan_shards
from weft.license import License

lic = License(open("acme.weft-license").read(), PUBLIC_HEX)
shards = plan_shards(weft, 8, lic=lic)   # 无许可证 → LicenseError
```

## 4. 定价逻辑

- 团队版按**团队**不按席位（数据结构是共享资产，按席位会激励少让人用——
  与产品利益相反）；
- 企业版按**席位 + 年限**，含分片节点数协商；
- 不做一次性买断：数据结构的价值在持续兼容（格式版本升级、
  安全补丁），买断制两边都难受。

## 5. 与竞品的位置

|  | Weft 社区版 | Weft 企业版 |
|---|---|---|
| vs PostGIS | 轻 100×、确定性序列化、矢栅同存；无 SQL/拓扑 | + 多节点分片 |
| vs MVT + COG 双栈 | 单结构单文件、无边界切割、可增量 | + 多写者合并 |
| vs H3/S2 | 有界工程域、等面积是构造性保证、无球面变换开销 | — |

**战场选择**：不抢"全球分析平台"（那是 H3+PostGIS+云数仓的战场），
抢"工程管线的本地权威数据层"——城市更新、地籍变更、厂区数字孪生、
矿区监测：**读多写少、要可复现交付物、要矢栅一致**的团队。

## 6. 版本与兼容承诺

- 格式版本在 magic 里（`WEFT1`），破坏性变更升版本号，旧版读取器保留两年；
- 社区版功能只增不减（open-core 信任的基石——已免费的能力永不收回）；
- 企业功能接口先以社区版"桩"形式发布（`enterprise.py` 的 `_require` 即桩），
  企业版只是把桩换成实现——API 形状提前冻结，企业客户可先集成后采购。
