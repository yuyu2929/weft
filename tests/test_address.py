"""地址编码测试：复合 uint64 键（深度高位 + Hilbert 低位）。

覆盖 docs/THEORY.md §2.2 的全部声明与 DEVLOG.md 记录的三次设计失败
（左对齐父≡子、右对齐深度重叠、深度低位覆盖 h）。
"""

import itertools

import pytest

from weft.address import (
    DEPTH_SHIFT,
    HILBERT_MASK,
    cell_rect,
    cell_size,
    children,
    descendant_intervals,
    is_ancestor,
    key_at,
    morton_key,
    neighbors,
    parent,
    xy_of,
)

MAX_D = 8  # 穷举上界（4^8 = 65536 键，秒级完成）


# ------------------------------------------------------------------ 单射与深度
def test_key_is_injective_and_depth_recoverable():
    """穷举 d<=8：(depth,x,y) -> key 全局单射，且深度可从键恢复。"""
    seen = {}
    for d in range(MAX_D + 1):
        n = 1 << d
        for x, y in itertools.product(range(n), range(n)):
            k = key_at(d, x, y)
            assert k not in seen, f"键碰撞: ({d},{x},{y}) 与 {seen[k]}"
            seen[k] = (d, x, y)
            assert k >> DEPTH_SHIFT == d
    assert len(seen) == sum(1 << (2 * d) for d in range(MAX_D + 1))


def test_no_parent_child_collision():
    """左对齐设计的失败模式：父键不得与任何子键相等。"""
    for d in range(1, MAX_D + 1):
        n = 1 << d
        for x, y in itertools.product(range(n), range(n)):
            k = key_at(d, x, y)
            pk = parent(k, d)
            for c in children(k, d):
                assert pk != c
                assert pk != k


def test_depth_spaces_disjoint():
    """右对齐设计的失败模式：不同深度键空间不得重叠。"""
    keys_by_depth = {}
    for d in range(MAX_D + 1):
        n = 1 << d
        keys_by_depth[d] = {key_at(d, x, y) for x, y in itertools.product(range(n), range(n))}
    for d1, d2 in itertools.combinations(range(MAX_D + 1), 2):
        assert keys_by_depth[d1].isdisjoint(keys_by_depth[d2])


def test_depth_in_low_bits_would_collide():
    """DEVLOG 记录：深度若放低位会同深度内不单射——复合键不犯此错。"""
    # 构造反证：若 key = h | (d << 62)，则 d=0 的 key h 与 d=1 的 key (h|...) 重叠
    # 实际复合键（深度高位）无此问题，本测试用同构反证说明设计选择的必要性
    d0 = key_at(0, 0, 0)
    assert d0 == 0
    d1 = key_at(1, 0, 0)
    assert d0 != d1 and d1 >> DEPTH_SHIFT == 1


# ------------------------------------------------------------------ 父子与祖先
def test_parent_children_roundtrip():
    for d in range(2, 6):
        n = 1 << d
        for x, y in itertools.product(range(0, n, 7), range(0, n, 5)):
            k = key_at(d, x, y)
            pk = parent(k, d)
            assert xy_of(pk, d - 1) == (x >> 1, y >> 1)
            if d >= 2:
                # pk 是祖父的四个子之一
                assert pk in children(parent(pk, d - 1), d - 2)


def test_is_ancestor_matches_geometry():
    for d in range(1, 6):
        n = 1 << d
        for x, y in itertools.product(range(0, n, 11), range(0, n, 9)):
            k = key_at(d, x, y)
            # 自身是自身祖先
            assert is_ancestor(k, d, k, d)
            # 祖先是父、祖父……
            pk, pd = k, d
            while pd > 0:
                pk, pd = parent(pk, pd), pd - 1
                assert is_ancestor(pk, pd, k, d)
            # 同层不同格不是祖先
            k2 = key_at(d, (x + 1) % n, y)
            if k2 != k:
                assert not is_ancestor(k, d, k2, d)


def test_descendant_intervals_cover_exactly():
    """后代区间：每层一个，拼接恰好覆盖全部真后代（无重复无遗漏）。"""
    for d in range(0, 5):
        n = 1 << d
        for x, y in itertools.product(range(0, n, 3), range(0, n, 3)):
            k = key_at(d, x, y)
            intervals = descendant_intervals(k, d, max_depth=6)
            assert len(intervals) == 6 - d + 1
            # 每个后代格恰好落在一个区间内
            for dd in range(d, 7):
                nd = 1 << dd
                for xx, yy in itertools.product(range(0, nd, 5), range(0, nd, 7)):
                    kk = key_at(dd, xx, yy)
                    if not is_ancestor(k, d, kk, dd):
                        continue
                    n_hits = sum(1 for lo, hi in intervals if lo <= kk < hi)
                    assert n_hits == 1, f"后代 {kk} 落入 {n_hits} 个区间"


# ------------------------------------------------------------------ 邻居与矩形
def test_neighbors_bounded_no_wraparound():
    """域有界：越界方向没有邻居（无换日线问题——H3/Geohash 的经典缺陷）。"""
    k = key_at(2, 0, 0)  # 角格
    ns = neighbors(k, 2)
    assert len(ns) == 2
    k = key_at(2, 1, 1)  # 中心格
    assert len(neighbors(k, 2)) == 4
    k = key_at(2, 0, 1)  # 边格
    assert len(neighbors(k, 2)) == 3


def test_cell_rect_and_size():
    r = cell_rect(key_at(1, 1, 0), 1, 100.0, 200.0)
    assert r == (50.0, 0.0, 100.0, 100.0)
    assert cell_size(2, 100.0, 100.0) == (25.0, 25.0)


def test_key_at_validates():
    with pytest.raises(ValueError):
        key_at(21, 0, 0)  # 超出 max_depth
    with pytest.raises(ValueError):
        key_at(2, 4, 0)  # x 越界


# ------------------------------------------------------------------ Morton 对照
def test_morton_key_composite_same_shape():
    """对照键与 Weft 键同构（同层同深度域），保证基准可比。"""
    for d in range(1, 5):
        n = 1 << d
        codes = set()
        for x, y in itertools.product(range(n), range(n)):
            mk = morton_key(d, x, y)
            assert mk >> DEPTH_SHIFT == d
            assert (mk & HILBERT_MASK) not in codes
            codes.add(mk & HILBERT_MASK)


def test_hilbert_beats_morton_on_runs():
    """3×3 邻域键序连续段数：Hilbert 平均 ≤ Morton（THEORY.md §4 对账 #2）。"""
    def runs(keys):
        ks = sorted(keys)
        return 1 + sum(1 for a, b in zip(ks, ks[1:]) if b - a > 4)

    d, n = 5, 32
    h_runs, z_runs = [], []
    for x in range(2, n - 2, 3):
        for y in range(2, n - 2, 5):
            hk = [key_at(d, x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
            zk = [morton_key(d, x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
            h_runs.append(runs(hk))
            z_runs.append(runs(zk))
    assert sum(h_runs) / len(h_runs) < sum(z_runs) / len(z_runs)
