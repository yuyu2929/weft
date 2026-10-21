"""企业功能测试：分片规划/定位、日志合并、单元格加密、许可证状态、社区版门控。"""

import pytest

from weft.enterprise import (
    encrypt_cell,
    license_status,
    merge_journals,
    plan_shards,
    shard_of,
)
from weft.license import License, LicenseError, issue
from helpers import district as _district

SK = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
PK_HEX = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"


def _ent_lic():
    token = issue(SK, "ACME", 25, 365, ["shard", "encrypt", "merge"])
    return License(token, PK_HEX)


# ------------------------------------------------------------------ 分片
def test_plan_shards_partitions_all_keys():
    """分片规划：键区间不重不漏覆盖全部格子（Hilbert 序 → 区间连续）。"""
    weft = _district()
    weft.commit()
    shards = plan_shards(weft, 4, lic=_ent_lic())
    assert len(shards) == 4
    keys = sorted(weft.cells.keys())
    for s in shards:
        assert s["key_lo"] <= s["key_hi"]
        assert s["n_cells"] == sum(1 for k in keys if s["key_lo"] <= k <= s["key_hi"])
    # 区间拼接不重不漏
    assert shards[0]["key_lo"] == keys[0]
    assert shards[-1]["key_hi"] == keys[-1]
    for a, b in zip(shards, shards[1:]):
        assert a["key_hi"] < b["key_lo"]
    assert sum(s["n_cells"] for s in shards) == len(keys)


def test_shard_of_consistent_with_plan():
    weft = _district()
    weft.commit()
    shards = plan_shards(weft, 4, lic=_ent_lic())
    counts = {}
    for k in weft.cells:
        sid = shard_of(k, shards)
        assert 0 <= sid < len(shards)
        counts[sid] = counts.get(sid, 0) + 1
    assert sum(counts.values()) == len(weft.cells)
    # 与规划声明的 n_cells 一致
    for i, s in enumerate(shards):
        assert counts.get(i, 0) == s["n_cells"]


def test_plan_shards_requires_license():
    weft = _district()
    with pytest.raises(LicenseError):
        plan_shards(weft, 4, lic=None)


# ------------------------------------------------------------------ 日志合并
def test_merge_journals_last_writer_wins():
    j1 = [
        {"version": 1, "op": "insert", "fid": 1, "keys": [10]},
        {"version": 2, "op": "insert", "fid": 2, "keys": [20]},
    ]
    j2 = [
        {"version": 3, "op": "update", "fid": 1, "keys": [11]},  # 更新 fid 1
        {"version": 4, "op": "delete", "fid": 2, "keys": []},  # 删除 fid 2
    ]
    merged = merge_journals([j1, j2], lic=_ent_lic())
    by_fid = {e["fid"]: e for e in merged}
    assert by_fid[1]["op"] == "update" and by_fid[1]["version"] == 3
    assert by_fid[2]["op"] == "delete" and by_fid[2]["version"] == 4


def test_merge_journals_requires_license():
    with pytest.raises(LicenseError):
        merge_journals([[]], lic=None)


# ------------------------------------------------------------------ 加密
def test_encrypt_cell_roundtrip():
    key = b"k" * 32
    data = b"\x00\x01\x02\x03field-bytes"
    enc = encrypt_cell(data, key, lic=_ent_lic())
    assert enc != data
    # 计数器流 XOR：再加密一次得回原文（演示语义）
    dec = encrypt_cell(enc, key, lic=_ent_lic())
    assert dec == data


def test_encrypt_cell_requires_license():
    with pytest.raises(LicenseError):
        encrypt_cell(b"data", b"k" * 32, lic=None)


# ------------------------------------------------------------------ 状态
def test_license_status_community_vs_enterprise():
    st_free = license_status(None)
    assert st_free["tier"] == "community"
    assert not st_free["valid"]
    st_ent = license_status(_ent_lic())
    assert st_ent["tier"] == "enterprise"
    assert "shard" in st_ent["features"]
