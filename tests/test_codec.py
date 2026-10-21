"""序列化测试：确定性往返、内容哈希、篡改检测、_internal 重建、跨深度一致性。"""

import copy

import pytest

from weft.codec import content_hash, deserialize, serialize
from helpers import district as _district


def _roundtrip(weft):
    return deserialize(serialize(weft))


def test_roundtrip_preserves_stats():
    weft = _district()
    weft.commit()
    w2 = _roundtrip(weft)
    assert w2.stats() == weft.stats()


def test_roundtrip_preserves_features():
    weft = _district()
    weft.commit()
    w2 = _roundtrip(weft)
    assert set(w2.features) == set(weft.features)
    for fid, f in weft.features.items():
        g = w2.features[fid]
        assert g.layer == f.layer and g.kind == f.kind and g.props == f.props
        assert g.cell_keys == f.cell_keys


def test_roundtrip_preserves_cells_and_fields():
    weft = _district()
    weft.commit()
    w2 = _roundtrip(weft)
    assert set(w2.cells) == set(weft.cells)
    for k, c in weft.cells.items():
        c2 = w2.cells[k]
        assert c2.depth == c.depth and c2.feature_ids == c.feature_ids
        if c.field is None:
            assert c2.field is None
        else:
            assert (c2.field == c.field).all()


def test_roundtrip_preserves_ledger_and_journal():
    weft = _district()
    weft.commit()
    w2 = _roundtrip(weft)
    assert dict(w2.ledger) == {k: set(v) for k, v in weft.ledger.items()}
    assert w2.journal == weft.journal


def test_serialize_deterministic():
    """确定性：同输入两次序列化逐字节相同（THEORY.md §2.5）。"""
    weft = _district()
    weft.commit()
    b1 = serialize(weft)
    b2 = serialize(copy.deepcopy(weft))
    assert b1 == b2
    assert content_hash(b1) == content_hash(b2)


def test_deserialize_rebuilds_internal():
    """反序列化必须重建 _internal（分区纪律查询依赖它）。"""
    weft = _district()
    weft.commit()
    w2 = _roundtrip(weft)
    assert w2._internal == weft._internal


def test_tampered_data_rejected():
    weft = _district()
    weft.commit()
    blob = bytearray(serialize(weft))
    blob[20] ^= 0xFF  # 翻转头部某字节
    with pytest.raises(ValueError, match="哈希"):
        deserialize(bytes(blob))


def test_bad_magic_rejected():
    with pytest.raises(ValueError, match="magic"):
        deserialize(b"NOTWEFT" + b"\x00" * 100)


def test_edit_after_roundtrip_consistent():
    """反序列化后可继续编辑（增量更新路径完整）。"""
    weft = _district()
    weft.commit()
    w2 = _roundtrip(weft)
    fid = w2.add_polygon("parcels", [[700, 700], [720, 700], [720, 720], [700, 720], [700, 700]], {"owner": "NEW"})
    w2.commit()
    assert w2.features[fid].cell_keys
    # 查询仍正确
    from weft.query import bbox_query

    assert fid in bbox_query(w2, 690, 690, 730, 730, layer="parcels")


def test_empty_weft_roundtrip():
    from weft.feature import Layer

    from weft.grid import Weft

    w = Weft(100.0, 100.0, [Layer("sensors", "point")])
    w2 = _roundtrip(w)
    assert w2.stats()["n_cells"] == 0
    assert w2.stats()["n_features"] == 0
