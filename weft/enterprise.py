"""Weft 企业功能门控（open-core 第三层）。

免费核心（MIT 已改为双许可：个人/非营利免费，商业需授权）包含完整单机能力：
建格、写入、查询、积分场、序列化、校验。

企业功能面向生产部署的运维与规模场景：

  shard    —— 按 Hilbert 键区间把格分片到多节点（含分片规划与路由）
  merge    —— 多写者编辑日志的合并（冲突检测 + 确定性归并）
  encrypt  —— 格级**认证加密**（seal_cell/open_cell，加密后防篡改）
  audit    —— 编辑审计链（append-only 哈希链，可导出给合规审计）
  diagnose —— 运维诊断报告（格分布、场误差、体积构成）
  sla      —— 支持与赔付（仅标记位）

门控规则：功能需要 有效许可证 且令牌 features 含对应名。
没有许可证时抛出 LicenseError，提示获取方式——不静默降级。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

from .license import License, LicenseError

# 企业层功能清单（签发许可证时校验用）
FEATURES = ("shard", "merge", "encrypt", "audit", "diagnose", "sla")


def _require(lic: License | None, feature: str) -> None:
    if lic is None or not lic.valid:
        raise LicenseError(
            f"企业功能 {feature!r} 需要有效许可证。"
            "个人/开发使用免费核心即可；企业采购见 docs/BUSINESS.md。"
        )
    if not lic.has_feature(feature):
        raise LicenseError(f"许可证不含功能 {feature!r}（可用: {lic.info.get('features')}）")


# ------------------------------------------------------------------ 分片
def plan_shards(weft, n_shards: int, lic: License | None = None) -> list[dict]:
    """按 Hilbert 键区间把全部格子划分为 n_shards 个分片（键有序 → 区间连续）。

    返回 [{"shard": i, "key_lo":..., "key_hi":..., "n_cells":...}, ...]

    边界语义：
    - 请求的分片数超过格子数时，**按格子数封顶**。否则会出现多个分片
      覆盖同一个键，`n_cells` 累加后大于真实格子数（v0.1 的 bug）。
    - 分片区间严格不重不漏，且从 O(n×shards) 的扫描改成 O(shards) 的整除切分。
    """
    _require(lic, "shard")
    if n_shards < 1:
        raise ValueError("n_shards ≥ 1")
    keys = sorted(weft.cells.keys())
    total = len(keys)
    if total == 0:
        return []
    n_shards = min(n_shards, total)

    out = []
    for i in range(n_shards):
        start = (i * total) // n_shards
        end = ((i + 1) * total) // n_shards - 1      # 闭区间
        out.append({
            "shard": i,
            "key_lo": keys[start],
            "key_hi": keys[end],
            "n_cells": end - start + 1,
        })
    return out


def shard_of(key: int, shards: list[dict]) -> int:
    """键 → 分片号（二分查找区间）；不在任何分片返回 -1。"""
    if not shards:
        return -1
    lo, hi = 0, len(shards) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s = shards[mid]
        if key < s["key_lo"]:
            hi = mid - 1
        elif key > s["key_hi"]:
            lo = mid + 1
        else:
            return s["shard"]
    return -1


# ------------------------------------------------------------------ 合并
def _entry_rank(entry: dict) -> str:
    """给条目一个稳定的内容指纹，用于打破 (version, fid) 相同的平局。"""
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def merge_journals(journals: list[list[dict]], lic: License | None = None) -> list[dict]:
    """多写者日志合并：按 (version, fid) 排序后做 last-writer-wins 归并。

    **确定性**：v0.1 在 (version, fid) 相同时依赖输入列表顺序，
    换个顺序合并结果会变——与"确定性输出"的承诺矛盾。
    v0.2 在平局时用条目内容哈希做最终裁决，因此任意输入顺序结果一致。
    """
    _require(lic, "merge")
    flat: list[tuple[int, int, str, dict]] = []
    for j in journals:
        for e in j:
            flat.append((int(e["version"]), int(e["fid"]), _entry_rank(e), e))
    flat.sort(key=lambda t: (t[0], t[1], t[2]))

    last: dict[int, dict] = {}
    for _, fid, _, e in flat:
        last[fid] = e
    return [last[fid] for fid in sorted(last)]


# ------------------------------------------------------------------ 加密
_MAGIC_SEAL = b"WEFTS1"
_TAG_LEN = 32


def _derive(key: bytes, label: bytes) -> bytes:
    return hashlib.sha256(key + b"|" + label).digest()


def _keystream(key: bytes, n: int, nonce: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "little")).digest()
        counter += 1
    return bytes(out[:n])


def _seal_tag(mac_key: bytes, nonce: bytes, ct: bytes) -> bytes:
    return hmac.new(mac_key, _MAGIC_SEAL + nonce + ct, hashlib.sha256).digest()


def seal_cell(plaintext: bytes, key: bytes, lic: License | None = None) -> bytes:
    """格级**认证加密**（encrypt-then-MAC）。

    输出布局：`WEFTS1 | nonce(16) | ciphertext | tag(32)`
    - 密钥派生：enc/mac 两把子密钥，避免同密钥两用；
    - 篡改任一字节，`open_cell` 抛 ValueError（纯 XOR 做不到这一点）。

    为什么不用 AES-GCM：本项目坚持零依赖（纯 Python + numpy）。
    若环境里有 `cryptography`，建议改用 AES-GCM/ChaCha20-Poly1305；
    这里的构造是标准 encrypt-then-MAC，安全性依赖 SHA-256/HMAC，
    但**没有硬件加速**，吞吐远低于 AES。
    """
    _require(lic, "encrypt")
    if len(key) < 16:
        raise ValueError("密钥至少 16 字节（建议 32）")
    enc_key = _derive(key, b"enc")
    mac_key = _derive(key, b"mac")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc_key, len(plaintext), nonce)))
    return _MAGIC_SEAL + nonce + ct + _seal_tag(mac_key, nonce, ct)


def open_cell(blob: bytes, key: bytes, lic: License | None = None) -> bytes:
    """解密并校验完整性；标签不符直接报错。"""
    _require(lic, "encrypt")
    if len(blob) < len(_MAGIC_SEAL) + 16 + _TAG_LEN:
        raise ValueError("密文长度不足")
    if not blob.startswith(_MAGIC_SEAL):
        raise ValueError("不是 Weft 封装的密文（magic 不符）")
    nonce = blob[len(_MAGIC_SEAL):len(_MAGIC_SEAL) + 16]
    ct = blob[len(_MAGIC_SEAL) + 16:-_TAG_LEN]
    tag = blob[-_TAG_LEN:]
    enc_key = _derive(key, b"enc")
    mac_key = _derive(key, b"mac")
    if not hmac.compare_digest(tag, _seal_tag(mac_key, nonce, ct)):
        raise ValueError("完整性校验失败：密文或元数据被篡改")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, len(ct), nonce)))


def encrypt_cell(field_bytes: bytes, key: bytes, lic: License | None = None) -> bytes:
    """**旧接口，不认证**：SHA-256 计数器流 XOR。

    保留它只为兼容：它无法发现篡改，等价于把数据裸奔（攻击者可翻转任意位）。
    新代码请用 `seal_cell/open_cell`。
    """
    _require(lic, "encrypt")
    if not key:
        raise ValueError("key 不能为空")
    out = bytearray()
    counter = 0
    while len(out) < len(field_bytes):
        out += hashlib.sha256(key + counter.to_bytes(8, "little")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(field_bytes, out))


# ------------------------------------------------------------------ 诊断
def diagnose(weft, lic: License | None = None) -> dict:
    """运维诊断报告（团队版功能）：格分布、场误差抽样、体积构成。"""
    _require(lic, "diagnose")
    stats = weft.stats()
    depths = {int(k): int(v) for k, v in stats.get("cells_by_depth", {}).items()}
    total = sum(depths.values()) or 1

    # 在域内均匀抽样，估计各层的积分场误差上界（不扫全库，诊断本身要便宜）
    samples = []
    for name in getattr(weft, "layers", {}):
        worst_for_layer = 0.0
        for i in range(5):
            for j in range(5):
                x = weft.width * (i + 0.5) / 5
                y = weft.height * (j + 0.5) / 5
                try:
                    _, bound = weft.field_at(name, x, y)
                except Exception:
                    continue
                worst_for_layer = max(worst_for_layer, float(bound))
        samples.append({"layer": name, "worst_bound": round(worst_for_layer, 6)})

    worst = max((s["worst_bound"] for s in samples), default=0.0)
    share = [v / total for v in depths.values()] if depths else []
    imbalance = max(share) if share else 0.0

    warnings = []
    if imbalance > 0.8 and total > 1:
        warnings.append("格分布极度不均（单层占比 > 80%），考虑调整分裂阈值")
    if worst > 0.25:
        warnings.append(f"场误差上界 {worst:.4f} 偏大，建议提高叶子分辨率")

    return {
        "domain": [weft.width, weft.height],
        "cells": stats.get("n_cells"),
        "features": stats.get("n_features"),
        "cells_by_depth": depths,
        "depth_imbalance": round(imbalance, 4),
        "field_samples": samples,
        "worst_field_bound": round(worst, 6),
        "ledger_pairs": stats.get("n_ledger_pairs"),
        "logical_version": stats.get("logical_version"),
        "warnings": warnings,
    }


def license_status(lic: License | None) -> dict:
    if lic is None or not lic.valid:
        return {"valid": False, "tier": "community", "features_available": list(FEATURES)}
    return {
        "valid": True,
        "tier": "enterprise",
        "org": lic.info.get("org"),
        "seats": lic.info.get("seats"),
        "days_left": lic.days_left(),
        "features": lic.info.get("features"),
    }
