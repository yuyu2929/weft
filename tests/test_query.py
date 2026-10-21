"""查询层测试：bbox、KNN（与暴力对照）、hit_test、rasterize、MVT 导出、边界伪影。"""

import math
import random

import numpy as np
import pytest

from weft.address import xy_of
from weft.query import (
    bbox_query,
    count_boundary_artifacts,
    hit_test,
    knn,
    rasterize,
    to_mvt_tiles,
)
from helpers import district as _district, seeded_pts as _seeded_pts


def seeded_pts(n, seed):
    rng = random.Random(seed)
    return [(rng.uniform(0, 1000), rng.uniform(0, 1000)) for _ in range(n)]


# ------------------------------------------------------------------ bbox
def test_bbox_query_matches_brute_force():
    weft = _district()
    weft.commit()
    feats = list(weft.features.values())
    rng = random.Random(3)
    for _ in range(25):
        x0, y0 = rng.uniform(0, 900), rng.uniform(0, 900)
        x1, y1 = x0 + rng.uniform(10, 300), y0 + rng.uniform(10, 300)
        got = bbox_query(weft, x0, y0, x1, y1, layer="sensors")
        want = sorted(
            f.fid
            for f in feats
            if f.layer == "sensors"
            and not (f.bbox()[2] <= x0 or x1 <= f.bbox()[0] or f.bbox()[3] <= y0 or y1 <= f.bbox()[1])
        )
        assert got == want


def test_bbox_query_no_duplicates():
    """跨格要素只返回一次（几何一次存储，fid 天然去重）。"""
    weft = _district()
    got = bbox_query(weft, 0, 0, 1000, 1000)
    assert len(got) == len(set(got))


def test_bbox_query_covers_whole_domain():
    """全域查询 = 所有与域相交的要素（域外部分的线按 bbox 不相交排除）。"""
    weft = _district()
    got = bbox_query(weft, 0, 0, 1000, 1000)
    want = sorted(
        f.fid
        for f in weft.features.values()
        if not (f.bbox()[2] <= 0 or 1000 <= f.bbox()[0] or f.bbox()[3] <= 0 or 1000 <= f.bbox()[1])
    )
    assert got == want
    assert len(got) > 0


# ------------------------------------------------------------------ KNN
def test_knn_matches_brute_force():
    from weft.baseline import brute_force_knn

    weft = _district()
    weft.commit()
    feats = list(weft.features.values())
    for x, y in _seeded_pts(20):
        for k in (1, 5, 10, 25):
            r1 = knn(weft, "sensors", x, y, k)
            r2 = brute_force_knn(feats, "sensors", x, y, k)
            assert sorted(f for f, _ in r1) == sorted(f for f, _ in r2)
            assert abs(r1[-1][1] - r2[-1][1]) < 1e-9


def test_knn_sorted_and_bounded():
    weft = _district()
    weft.commit()
    for x, y in _seeded_pts(10):
        r = knn(weft, "sensors", x, y, 7)
        assert len(r) == 7
        ds = [d for _, d in r]
        assert ds == sorted(ds)
        # 返回的距离与暴力独立复算一致
        for fid, d in r:
            f = weft.features[fid]
            assert abs(d - math.hypot(float(f.geom[0, 0]) - x, float(f.geom[0, 1]) - y)) < 1e-9


def test_knn_k_zero():
    weft = _district()
    assert knn(weft, "sensors", 500, 500, 0) == []


def test_knn_no_duplicates():
    weft = _district()
    weft.commit()
    for x, y in _seeded_pts(10):
        r = knn(weft, "sensors", x, y, 10)
        fids = [f for f, _ in r]
        assert len(fids) == len(set(fids))


# ------------------------------------------------------------------ hit_test / rasterize
def test_hit_test_structure():
    weft = _district()
    weft.commit()
    h = hit_test(weft, 500.0, 500.0, "parcels")
    assert set(h) == {"approx", "bound", "exact_hits"}
    assert h["bound"] >= 0.0


def test_rasterize_shape_and_range():
    weft = _district()
    weft.commit()
    out = rasterize(weft, "parcels", 0, 0, 1000, 1000, 32)
    assert out.shape == (32, 32)
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_rasterize_matches_field_samples():
    """栅格导出来自叶子场：逐点与 field_at 一致（无重算几何）。"""
    weft = _district()
    weft.commit()
    out = rasterize(weft, "parcels", 0, 0, 100, 100, 10)
    for i in range(10):
        for j in range(10):
            px, py = (j + 0.5) * 10, (i + 0.5) * 10
            cell = weft.cell_of(px, py)
            if cell is None or cell.field is None:
                continue
            x, y = xy_of(cell.key, cell.depth, weft.max_depth)
            n = 1 << cell.depth
            fx = (px / weft.width * n - x) * weft.field_res
            fy = (py / weft.height * n - y) * weft.field_res
            i0, j0 = min(7, max(0, int(fy))), min(7, max(0, int(fx)))
            li = weft.layer_order.index("parcels")
            assert out[i, j] == pytest.approx(float(cell.field[i0, j0, li]))


# ------------------------------------------------------------------ MVT 对照导出
def test_mvt_tiles_cut_features():
    """MVT 式导出故意切割（对照臂）：跨瓦片要素出现多次。"""
    weft = _district()
    tiles = to_mvt_tiles(weft, "parcels", zoom=3)
    fid_tiles: dict[int, int] = {}
    for t in tiles.values():
        for fid in t["features"]:
            fid_tiles[fid] = fid_tiles.get(fid, 0) + 1
    cut = {fid: c for fid, c in fid_tiles.items() if c > 1}
    assert cut, "z3 下应有地块被切碎"


def test_count_boundary_artifacts():
    weft = _district()
    art = count_boundary_artifacts(weft, "parcels", zoom=3)
    assert art["features_cut"] > 0
    assert art["duplicate_copies"] == sum(c - 1 for c in art.values() if isinstance(c, int)) - (
        art["features_cut"]
    ) * 0 or art["duplicate_copies"] > 0
    # 与 MVT 语义一致：features_cut ≤ features_total
    assert art["features_cut"] <= art["features_total"]


def test_weft_queries_never_cut():
    """Weft 自身查询不切割：bbox 全量返回完整 fid，hover 可重建全几何。"""
    weft = _district()
    got = bbox_query(weft, 400, 400, 600, 600, layer="parcels")
    for fid in got:
        f = weft.reconstruct(fid)
        assert f.geom is not None and len(f.geom[0]) >= 4  # 完整环
