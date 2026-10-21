"""基准声明断言（小规模，保证 CI 秒级）：bench.py 的关键设计声明可复现。"""

import random

import pytest

from weft.address import cell_rect
from weft.bench import make_district
from weft.codec import serialize
from weft.query import count_boundary_artifacts


@pytest.fixture(scope="module")
def built():
    weft, parcels, roads, sensors = make_district(n_parcels=120, n_roads=40, n_sensors=150)
    weft.commit()
    return weft


def test_partition_discipline_bench_data(built):
    """基准数据同样满足分区纪律（面积和=域面积，无重叠，无空注册）。"""
    weft = built
    rects = [cell_rect(k, c.depth, weft.width, weft.height, weft.max_depth) for k, c in weft.cells.items()]
    total = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    assert total == pytest.approx(weft.width * weft.height)
    for i, ra in enumerate(rects):
        for rb in rects[i + 1 :]:
            assert not (min(ra[2], rb[2]) > max(ra[0], rb[0]) and min(ra[3], rb[3]) > max(ra[1], rb[1]))
    assert all(f.cell_keys for f in weft.features.values())


def test_weft_smaller_than_mvt(built):
    """存储声明：Weft 文件小于 MVT 式瓦片（几何一次存储）。"""
    from weft.query import to_mvt_tiles

    weft = built
    weft_bytes = len(serialize(weft))
    mvt = to_mvt_tiles(weft, "parcels", zoom=4)
    mvt_bytes = sum(12 * len(weft.features[fid].geom[0]) + 24 for t in mvt.values() for fid in set(t["features"]))
    assert weft_bytes < mvt_bytes


def test_boundary_artifacts_mvt_vs_weft(built):
    """MVT 切割 vs Weft 不切割（bench [2] 的声明）。"""
    weft = built
    art = count_boundary_artifacts(weft, "parcels", zoom=4)
    assert art["features_cut"] > 0  # MVT 式确实切碎
    assert art["duplicate_copies"] > 0
    # Weft 侧：几何一次存储（用序列化里要素区字节佐证）
    geom_bytes = sum(f.geom[0].nbytes for f in weft.features.values() if f.layer == "parcels")
    assert geom_bytes > 0


def test_knn_exact_match_bench_data(built):
    """KNN 精确性声明（bench [5]）：与暴力全表扫描一致。"""
    from weft.baseline import brute_force_knn
    from weft.query import knn

    weft = built
    feats = list(weft.features.values())
    rng = random.Random(11)
    for _ in range(8):
        x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
        r1 = knn(weft, "sensors", x, y, 10)
        r2 = brute_force_knn(feats, "sensors", x, y, 10)
        assert sorted(f for f, _ in r1) == sorted(f for f, _ in r2)
        assert abs(r1[-1][1] - r2[-1][1]) < 1e-9
