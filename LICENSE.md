# License Overview 双许可总览

**一句话：个人免费用，商业用付费。**

Weft 采用**双许可（Dual Licensing）**发布。使用前按你的身份选一条即可：

| 你是谁 | 适用的许可 | 费用 |
|---|---|---|
| **个人**：学习、研究、试验、个人项目、课程作业 | [PolyForm Noncommercial 1.0.0](LICENSE-POLYFORM.md) | **永久免费** |
| 非营利组织：高校院系、公共研究机构、慈善、教育、政府 | [PolyForm Noncommercial 1.0.0](LICENSE-POLYFORM.md) | **免费** |
| **公司 / 个体经营者 / 任何营利实体**（内部管线、产品内嵌、对外交付） | [商业许可](COMMERCIAL-LICENSE.md) | **需购买授权** |

## 1. PolyForm Noncommercial —— 个人与非营利组织免费

完整法律文本见 [LICENSE-POLYFORM.md](LICENSE-POLYFORM.md)。覆盖**全部本地能力**：

- 建格、插入/删除/更新、几何重建
- 全部查询：点查询、bbox、KNN、积分场（含误差界）、栅格导出
- 确定性序列化 + 内容哈希、`weft verify` 完整性校验
- CLI、基准套件、完整测试

## 2. 商业许可 —— 企业与营利组织付费

任何**营利性实体**出于任何商业目的使用 Weft（生产管线、嵌入自有产品、
对外交付、SaaS 化），都需要一份商业许可。
条款、档位与联系方式见 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。

## 与企业功能的关系

| 层 | 内容 | 个人/非营利 | 营利组织 |
|---|---|---|---|
| 核心 | 建格、查询、积分场、序列化、校验 | 免费 | 需商业许可 |
| 团队 | `diagnose` 运维诊断报告 | — | 需商业许可 + 许可证 |
| 企业 | `shard` 分片 / `merge` 合并 / `encrypt` 认证加密 / `audit` 审计链 / `sla` | — | 需商业许可 + 许可证 |

企业功能通过 `WEFT_LICENSE` 指向的 Ed25519 令牌解锁
（格式 `WEFT1.<payload>.<sig>`），公钥随包分发（`weft/license_public.pem`），
私钥只在签发方。**核心能力永不加锁**——这是 open-core 的信任基础。

## 判断不了？看这两个问题

1. **使用 Weft 的行为，最终是否服务于营利？** 是 → 需要商业许可。
2. **你是法律意义上的非商业组织吗？**（PolyForm "Noncommercial Organizations" 一节
   有逐字定义）是 → 免费许可覆盖你。

---

Copyright (c) 2026 yuyu2929. All rights reserved.
