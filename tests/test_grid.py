"""主结构测试：分区纪律、边界点注册、分裂、删除剪枝、增量编辑、缝合账本。"""

import numpy as np
import pytest

from weft.address import cell_rect, key_at, xy_of
from weft.feature import KIND_POINT, Layer
from weft.grid import Weft, WeftError


def _layers():
    return [
        Layer(name="parcels", kind="polygon", columns=("owner",)),
        Layer(name="roads", kind="line", columns=("name",)),
        Layer(name="sensors", kind=KIND_POINT, columns=("kind",)),
    ]


def _district(seed: int = 7):
    """小规模合成工程区（与 bench 同构，规模压小保证测试秒级）。"""
    import random

    rng = random.Random(seed)
    weft = Weft(1000.0, 1000.0, _layers())
    gx = gy = 12
    cw, ch = 1000.0 / gx, 1000.0 / gy
    for i in range(gx):
        for j in range(gy):
            if rng.random() < 0.1:
                continue
            x0, y0 = i * cw + 4, j * ch + 4
            x1, y1 = (i + 1) * cw - 4, (j + 1) * ch - 4
            ring = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])
            weft.add_polygon("parcels", [ring], {"owner": rng.choice("ABCD")})
    for _ in range(40):
        x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
        weft.add_point("sensors", x, y, {"kind": "temp"})
    for i in range(gx + 1):
        x = i * cw
        weft.add_line("roads", [[x, 0], [x, 500], [x + 12, 500], [x + 12, 1000]], {"name": f"V{i}"})
    return weft


# ------------------------------------------------------------------ 分区纪律
def test_partition_invariants():
    """(I) 叶子矩形互不相交且面积和=域面积；(II) 无要素空注册。"""
    weft = _district()
    rects = {k: cell_rect(k, c.depth, weft.width, weft.height, weft.max_depth) for k, c in weft.cells.items()}
    total = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects.values())
    assert total == pytest.approx(weft.width * weft.height)
    keys = list(rects)
    for i, ka in enumerate(keys):
        for kb in keys[i + 1 :]:
            ra, rb = rects[ka], rects[kb]
            assert not (min(ra[2], rb[2]) > max(ra[0], rb[0]) and min(ra[3], rb[3]) > max(ra[1], rb[1]))
    assert all(f.cell_keys for f in weft.features.values())
    # 每要素的注册叶都真实存在
    for f in weft.features.values():
        for k in f.cell_keys:
            assert k in weft.cells
            assert f.fid in weft.cells[k].feature_ids


def test_point_belongs_to_exactly_one_leaf():
    """不变式 (I)：点至多属一叶（半开规则 + cell_of 一致）。"""
    weft = _district()
    for f in list(weft.features.values()):
        if f.kind != KIND_POINT:
            continue
        px, py = float(f.geom[0, 0]), float(f.geom[0, 1])
        assert len(f.cell_keys) == 1
        cell = weft.cell_of(px, py)
        assert cell is not None and cell.key == f.cell_keys[0]


def test_boundary_points_registered():
    """DEVLOG 修复：x=0 / y=0 边界的点必须注册（曾因半开剪枝全部丢失）。"""
    weft = Weft(100.0, 100.0, _layers())
    fids = [
        weft.add_point("sensors", 0.0, 50.0),
        weft.add_point("sensors", 50.0, 0.0),
        weft.add_point("sensors", 0.0, 0.0),
        weft.add_point("sensors", 99.999, 99.999),
    ]
    for fid in fids:
        assert weft.features[fid].cell_keys, f"fid={fid} 未注册"
        cell = weft.cell_of(*map(float, weft.features[fid].geom[0]))
        assert cell is not None and fid in cell.feature_ids


def test_right_edge_line_registered():
    """贴域右边界的线必须注册（含边界接触相交规则）。"""
    weft = Weft(100.0, 100.0, _layers())
    fid = weft.add_line("roads", [[100.0, 0], [100.0, 100]], {"name": "edge"})
    assert weft.features[fid].cell_keys


def test_point_outside_domain_rejected():
    weft = Weft(100.0, 100.0, _layers())
    with pytest.raises(WeftError):
        weft.add_point("sensors", -1.0, 50.0)
    with pytest.raises(WeftError):
        weft.add_point("sensors", 50.0, 100.0)  # 半开域不含右边界


# ------------------------------------------------------------------ 分裂与剪枝
def test_split_on_overflow():
    """超限分裂：叶内 >32 要素触发四分，且保持分区纪律。"""
    weft = Weft(100.0, 100.0, _layers())
    for i in range(80):
        weft.add_point("sensors", 10.0 + (i % 8), 10.0 + (i // 8))
    assert len(weft.cells) > 1
    assert all(len(c.feature_ids) <= 32 for c in weft.cells.values())
    # 纪律保持
    rects = [cell_rect(k, c.depth, 100.0, 100.0, weft.max_depth) for k, c in weft.cells.items()]
    total = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    assert total == pytest.approx(10000.0)


def test_delete_prunes_and_keeps_partition():
    weft = _district()
    fid = next(iter(weft.features))
    weft.delete(fid)
    assert fid not in weft.features
    for k, c in weft.cells.items():
        assert fid not in c.feature_ids
    rects = [cell_rect(k, c.depth, weft.width, weft.height, weft.max_depth) for k, c in weft.cells.items()]
    total = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    assert total == pytest.approx(weft.width * weft.height)


def test_delete_nonexistent_raises():
    weft = Weft(10.0, 10.0, _layers())
    with pytest.raises(WeftError):
        weft.delete(999)


def test_internal_set_consistent():
    """_internal 与 cells 一致：内部节点的子键必有叶或内部后代。"""
    weft = _district()
    for k in weft._internal:
        d = k >> 40
        assert any(c in weft.cells or c in weft._internal for c in weft._child_keys(k, d))


# ------------------------------------------------------------------ 编辑与版本
def test_update_geometry_reregisters():
    weft = Weft(100.0, 100.0, _layers())
    fid = weft.add_polygon("parcels", [[10, 10], [30, 10], [30, 30], [10, 30], [10, 10]], {"owner": "A"})
    old_keys = set(weft.features[fid].cell_keys)
    # 移到远处
    weft.update_geometry(fid, [[70, 70], [90, 70], [90, 90], [70, 90], [70, 70]])
    new_keys = set(weft.features[fid].cell_keys)
    assert old_keys.isdisjoint(new_keys) or old_keys == new_keys  # 位置变了键应不同
    assert new_keys
    for k in old_keys - new_keys:
        assert fid not in weft.cells[k].feature_ids


def test_dirty_cells_and_commit():
    weft = Weft(100.0, 100.0, _layers())
    for i in range(50):
        weft.add_point("sensors", 5.0 + i * 0.1, 5.0)
    n_dirty = len(weft.dirty_cells())
    assert n_dirty > 0
    v0 = weft.logical_version
    n_recomputed = weft.commit()
    assert n_recomputed == n_dirty
    assert weft.logical_version == v0 + 1
    assert not weft.dirty_cells()
    # 非空格都有场（split 产生的空格无要素、无场，语义为 0）
    assert all(c.field is not None for c in weft.cells.values() if c.feature_ids)


def test_field_recompute_deterministic():
    """场是矢量的确定性函数：重算两次逐字节一致（THEORY.md §4 #8）。"""
    weft = _district()
    weft.commit()
    before = {k: c.field.tobytes() for k, c in weft.cells.items() if c.field is not None}
    for k in weft.cells:
        weft.cells[k].field = weft._build_field(k)
    after = {k: c.field.tobytes() for k, c in weft.cells.items() if c.field is not None}
    assert before == after


# ------------------------------------------------------------------ 缝合计账本
def test_ledger_records_cross_cell_edges():
    """跨格要素的共边事件必须记账（工程审计一等查询）。"""
    weft = Weft(100.0, 100.0, _layers())
    # 先加足量点触发分裂（树有多叶后跨格线才会穿过多格）
    for i in range(60):
        weft.add_point("sensors", 5.0 + (i % 10) * 9.0, 5.0 + (i // 10) * 9.0)
    fid = weft.add_line("roads", [[0, 50], [100, 50]], {"name": "cross"})
    assert len(weft.cells) > 1
    assert weft.ledger, "跨格线应产生账本记录"
    recorded = {f for s in weft.ledger.values() for f in s}
    assert fid in recorded


def test_crossings_query():
    weft = Weft(100.0, 100.0, _layers())
    for i in range(60):
        weft.add_point("sensors", 5.0 + (i % 10) * 9.0, 5.0 + (i // 10) * 9.0)
    fid = weft.add_line("roads", [[0, 50], [100, 50]], {"name": "cross"})
    pair = next(iter(weft.ledger))
    assert fid in weft.crossings(*pair)
    assert weft.crossings(key_at(0, 0, 0), key_at(3, 7, 7)) == set()  # 不相邻格对


def test_reconstruct_returns_full_geometry():
    weft = _district()
    fid = next(iter(weft.features))
    f = weft.reconstruct(fid)
    assert f.fid == fid and f.geom is not None


# ------------------------------------------------------------------ 场查询
def test_field_at_point_layer_exact():
    """点层场是精确计数（无误差界）。"""
    weft = Weft(100.0, 100.0, _layers())
    for i in range(4):
        weft.add_point("sensors", 10.0, 10.0)  # 同一点 4 次
    weft.commit()
    val, bound = weft.field_at("sensors", 10.0, 10.0)
    assert val == 4.0
    assert bound == 0.0  # 点层精确


def test_field_at_polygon_layer_has_bound():
    weft = Weft(100.0, 100.0, _layers())
    weft.add_polygon("parcels", [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]], {"owner": "A"})
    weft.commit()
    val, bound = weft.field_at("parcels", 50.0, 50.0)
    assert abs(val - 1.0) <= bound
    assert bound == pytest.approx(1.0 / 16)  # sub=4


def test_exact_point_query_matches_brute_force():
    from weft.baseline import brute_force_point_query

    weft = _district()
    weft.commit()
    feats = list(weft.features.values())
    import random

    rng = random.Random(5)
    for _ in range(40):
        x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
        assert weft.exact_point_query("parcels", x, y) == brute_force_point_query(feats, "parcels", x, y)


def test_stats():
    weft = _district()
    st = weft.stats()
    assert st["n_features"] == len(weft.features)
    assert st["n_cells"] == len(weft.cells)
    assert sum(st["cells_by_depth"].values()) == len(weft.cells)


# ------------------------------------------------------------------ 域与层校验
def test_invalid_construction():
    from weft.grid import WeftError as E

    with pytest.raises(E):
        Weft(-1, 100, _layers())
    with pytest.raises(E):
        Weft(100, 100, [])
    with pytest.raises(ValueError):  # 层类型非法（Layer 自身校验）
        Weft(100, 100, [Layer("a", "bogus")])
    with pytest.raises(E):
        Weft(100, 100, [Layer("dup", KIND_POINT), Layer("dup", "line")])


def test_layer_kind_mismatch():
    weft = Weft(100.0, 100.0, _layers())
    with pytest.raises(WeftError):
        weft.add_point("roads", 5, 5)  # roads 是线层
    with pytest.raises(WeftError):
        weft.add_point("nonexistent", 5, 5)
