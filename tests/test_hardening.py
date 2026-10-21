"""v0.2 加固与企业功能的回归测试。

每条断言对应一个真实的坑：
- 分片数超过格子数时的重复计数；
- 合并结果依赖输入顺序（破坏"确定性"承诺）；
- 旧加密接口无法发现篡改；
- 审计链删一条 / 改一条必须被抓；
- 企业功能无证可用 = 白送。
"""

import os

import pytest

from weft import Weft, Layer
from weft.audit import AuditLog
from weft.codec import serialize
from weft.enterprise import (
    diagnose,
    merge_journals,
    open_cell,
    plan_shards,
    seal_cell,
    shard_of,
)
from weft.license import License, LicenseError, issue

SK = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
PK_HEX = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"


def _lic(*features):
    return License(issue(SK, "ACME", 10, 365, list(features)), PK_HEX)


def _weft(n=4):
    w = Weft(400, 400, [Layer(name="p", kind="polygon", columns=("owner",))])
    for i in range(n):
        x = i * 50
        w.add_polygon("p", [[[x, 0], [x + 40, 0], [x + 40, 40], [x, 40], [x, 0]]],
                      {"owner": f"o{i}"})
    w.commit()
    return w


# ---------------------------------------------------------------- 分片
def test_shards_never_exceed_cells():
    """分片数 > 格子数：按格子数封顶，计数不重复。"""
    w = _weft(3)
    w.commit()
    n = len(w.cells)
    shards = plan_shards(w, n + 5, lic=_lic("shard"))
    assert len(shards) == n
    assert sum(s["n_cells"] for s in shards) == n
    # 区间不重叠
    for a, b in zip(shards, shards[1:]):
        assert a["key_hi"] < b["key_lo"]


def test_shard_of_matches_plan_exactly():
    w = _weft(6)
    w.commit()
    shards = plan_shards(w, 3, lic=_lic("shard"))
    counted = {}
    for k in w.cells:
        sid = shard_of(k, shards)
        counted[sid] = counted.get(sid, 0) + 1
    for s in shards:
        assert counted.get(s["shard"], 0) == s["n_cells"]


def test_shard_of_outside_returns_minus_one():
    w = _weft(2)
    w.commit()
    shards = plan_shards(w, 2, lic=_lic("shard"))
    assert shard_of(-1, shards) == -1
    assert shard_of(1 << 50, shards) == -1


# ---------------------------------------------------------------- 合并
def test_merge_is_order_independent():
    """同一批日志，换个顺序合并，结果必须一致（确定性承诺）。"""
    a = [{"version": 1, "op": "update", "fid": 1, "keys": [1]}]
    b = [{"version": 1, "op": "delete", "fid": 1, "keys": []}]
    lic = _lic("merge")
    r1 = merge_journals([a, b], lic=lic)
    r2 = merge_journals([b, a], lic=lic)
    assert r1 == r2


def test_merge_last_writer_wins_on_higher_version():
    a = [{"version": 1, "op": "insert", "fid": 1, "keys": [1]}]
    b = [{"version": 9, "op": "delete", "fid": 1, "keys": []}]
    out = merge_journals([a, b], lic=_lic("merge"))
    assert out[0]["op"] == "delete"


# ---------------------------------------------------------------- 认证加密
def test_seal_roundtrip_and_detects_tamper():
    lic = _lic("encrypt")
    key = b"k" * 32
    data = b"integral-raster-field" * 10
    blob = seal_cell(data, key, lic=lic)
    assert blob != data
    assert open_cell(blob, key, lic=lic) == data

    # 翻转密文任意一位 → 完整性校验必须失败
    bad = bytearray(blob)
    bad[40] ^= 0x01
    with pytest.raises(ValueError, match="完整性"):
        open_cell(bytes(bad), key, lic=lic)

    # 换密钥也失败
    with pytest.raises(ValueError):
        open_cell(blob, b"z" * 32, lic=lic)


def test_seal_is_randomized():
    """同一明文两次加密结果不同（随机 nonce），但都能解回原文。"""
    lic = _lic("encrypt")
    key = b"k" * 32
    a = seal_cell(b"x" * 64, key, lic=lic)
    b = seal_cell(b"x" * 64, key, lic=lic)
    assert a != b
    assert open_cell(a, key, lic=lic) == open_cell(b, key, lic=lic)


def test_seal_requires_licence():
    with pytest.raises(LicenseError):
        seal_cell(b"data", b"k" * 32, lic=None)


# ---------------------------------------------------------------- 审计链
def test_audit_chain_detects_edit_and_deletion(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    log = AuditLog(p, lic=_lic("audit"))
    log.append("agent:1", "weft.build", "site.weft", {"bytes": 453})
    log.append("agent:1", "weft.merge", "site.weft", {"journals": 2})
    assert log.verify()["ok"] is True

    # 改一条
    raw = open(p, encoding="utf-8").read().splitlines()
    raw[0] = raw[0].replace("weft.build", "weft.hacked")
    open(p, "w", encoding="utf-8").write("\n".join(raw) + "\n")
    assert log.verify()["ok"] is False


def test_audit_chain_detects_truncation(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    log = AuditLog(p, lic=_lic("audit"))
    log.append("agent:1", "weft.build")
    log.append("agent:1", "weft.build")
    lines = open(p, encoding="utf-8").read().splitlines()
    open(p, "w", encoding="utf-8").write(lines[0] + "\n")   # 删掉最后一条
    report = log.verify()
    assert report["ok"] is False
    assert "锚点" in report["reason"]


def test_audit_requires_licence(tmp_path):
    with pytest.raises(LicenseError):
        AuditLog(str(tmp_path / "a.jsonl"), lic=None)


# ---------------------------------------------------------------- 诊断
def test_diagnose_reports_structure():
    w = _weft(4)
    w.commit()
    r = diagnose(w, lic=_lic("diagnose"))
    assert r["cells"] >= 1
    assert "cells_by_depth" in r
    assert isinstance(r["warnings"], list)


def test_diagnose_requires_licence():
    w = _weft(2)
    w.commit()
    with pytest.raises(LicenseError):
        diagnose(w, lic=None)


# ---------------------------------------------------------------- 可复现性
def test_serialize_roundtrip_is_bitwise_stable():
    w = _weft(5)
    w.commit()
    once = serialize(w)
    twice = serialize(w)
    assert once == twice
    from weft.codec import deserialize

    assert serialize(deserialize(once)) == once
