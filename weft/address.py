"""Weft 地址空间：Hilbert 序四叉键（quadkey）。

设计（见 docs/THEORY.md §2.1-2.2）：
- 有界矩形域 [0,W)×[0,H)，最大深度 D（默认 20）。
- 深度 d 的均匀细分是 2^d × 2^d 网格；同层格子面积严格相等。
- 键是单一 uint64，复合打包：**深度占高位、Hilbert 序号占低位**：
      key(d,x,y) = (d << HILBERT_BITS) | h_d(x,y),  HILBERT_BITS = 40
  （h_d < 4^20 = 2^40，深度占其余 24 位。）

为什么这样打包（DEVLOG.md 有完整踩坑记录——前两版都撞了鸽笼原理）：
- 左对齐（h 左移到 D 层）：父键是子键前缀 → **父与第 0 子同键**
  （4 子+父=5 个串挤进下一尺度 4 个槽位）；
- 右对齐（key=h_d）：不同深度的键空间重叠 → **深度 1 的键 0..3
  撞上深度 2 格的儿子 0..3**；
- 深度放低位：会覆盖 h 的低位 → 同深度内不再单射。
深度-高位 + Hilbert-低位是唯一同时满足 单射/可除/可算 的打包。
代价（如实声明）：存储按深度分层（depth-major），层内是纯 Hilbert 序；
跨层后代不是单一整数区间，而是"每层一个区间"（至多 D 个，
见 descendant_intervals）——范围扫描按层枚举，bbox 查询本就走树下探。

父/子/祖先（全部整数运算）：
- parent(key, depth)  = ((depth-1) << 40) | (h >> 2)
- children(key, depth)= [((depth+1) << 40) | ((h << 2) | t), t∈{0,1,2,3}]
- is_ancestor(a,da,b,db) ⟺ da ≤ db 且 (h_b >> 2(db-da)) == h_a

xy2d/d2xy 采用教科书 Hilbert 算法（纯整数，互为逆变换，
tests/test_address.py 对 d ≤ 8 穷举校验）。
"""

from __future__ import annotations

DEFAULT_MAX_DEPTH = 20
HILBERT_BITS = 40  # 支持 max_depth ≤ 20（4^20 = 2^40）
HILBERT_MASK = (1 << HILBERT_BITS) - 1
DEPTH_SHIFT = HILBERT_BITS
UINT64_MAX = (1 << 64) - 1


def _xy2d(depth: int, x: int, y: int) -> int:
    """(x,y) → 深度 depth 网格上的 Hilbert 序号 ∈ [0, 4^depth)。"""
    n = 1 << depth
    d = 0
    s = n >> 1
    xx, yy = x, y
    while s > 0:
        rx = 1 if (xx & s) > 0 else 0
        ry = 1 if (yy & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                xx = n - 1 - xx
                yy = n - 1 - yy
            xx, yy = yy, xx
        s >>= 1
    return d


def _d2xy(depth: int, d: int) -> tuple[int, int]:
    """深度 depth 网格上的 Hilbert 序号 → (x,y)。_xy2d 的逆。"""
    n = 1 << depth
    x = y = 0
    t = d
    s = 1
    while s < n:
        rx = 1 & (t >> 1)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        t >>= 2
        s <<= 1
    return x, y


def key_at(depth: int, x: int, y: int, max_depth: int = DEFAULT_MAX_DEPTH) -> int:
    """(depth,x,y) → 复合 uint64 键。"""
    if not (0 <= depth <= max_depth):
        raise ValueError(f"depth {depth} 越界 [0,{max_depth}]")
    if max_depth > 20:
        raise ValueError("max_depth ≤ 20（Hilbert 序号占 40 位）")
    n = 1 << depth
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError(f"({x},{y}) 越界 depth={depth} 的 [0,{n}) 网格")
    k = (depth << DEPTH_SHIFT) | _xy2d(depth, x, y)
    if k > UINT64_MAX:
        raise ValueError("键超出 uint64")
    return k


def hilbert_of(key: int) -> int:
    """键 → 低 40 位的 Hilbert 序号。"""
    return key & HILBERT_MASK


def depth_of_key(key: int) -> int:
    """键 → 高位深度。"""
    return key >> DEPTH_SHIFT


def xy_of(key: int, depth: int, max_depth: int = DEFAULT_MAX_DEPTH) -> tuple[int, int]:
    """(key, depth) → (x,y)。"""
    if not (0 <= depth <= max_depth):
        raise ValueError(f"depth {depth} 越界")
    if depth == 0:
        return 0, 0
    return _d2xy(depth, hilbert_of(key))


def parent(key: int, depth: int, max_depth: int = DEFAULT_MAX_DEPTH) -> int:
    """父格键（根的父是自己）。"""
    if depth <= 0:
        return 0
    return ((depth - 1) << DEPTH_SHIFT) | (hilbert_of(key) >> 2)


def children(
    key: int, depth: int, max_depth: int = DEFAULT_MAX_DEPTH
) -> list[int]:
    """四个子格键（已到最大深度时返回空）。depth 为当前格深度。"""
    if depth >= max_depth:
        return []
    h = hilbert_of(key)
    base = ((depth + 1) << DEPTH_SHIFT) | (h << 2)
    return [base | t for t in range(4)]


def is_ancestor(
    a: int, da: int, b: int, db: int, max_depth: int = DEFAULT_MAX_DEPTH
) -> bool:
    """a(深度 da) 是 b(深度 db) 的祖先或自身 ⟺ b 的高 2*da 个 Hilbert 位等于 a。"""
    if da > db:
        return False
    if da == 0:
        return True
    return (hilbert_of(b) >> (2 * (db - da))) == hilbert_of(a)


def descendant_intervals(
    key: int, depth: int, max_depth: int = DEFAULT_MAX_DEPTH
) -> list[tuple[int, int]]:
    """key(深度 depth) 的全部后代（含自身）：每层一个区间 [lo, hi)。

    复合键跨层不连续（存储 depth-major），但每层内单一区间：
    深度 d' 上为 [(d'<<40)|(h<<2(d'-depth)), (d'<<40)|((h+1)<<2(d'-depth)))。
    至多 max_depth-depth+1 个区间。
    """
    h = hilbert_of(key)
    out = []
    for d in range(depth, max_depth + 1):
        shift = 2 * (d - depth)
        lo = (d << DEPTH_SHIFT) | (h << shift)
        hi = (d << DEPTH_SHIFT) | ((h + 1) << shift)
        out.append((lo, hi))
    return out


def neighbors(
    key: int, depth: int, max_depth: int = DEFAULT_MAX_DEPTH
) -> list[int]:
    """同层四邻居（域有界：越界方向没有邻居——无换日线问题）。"""
    x, y = xy_of(key, depth, max_depth)
    n = 1 << depth
    out = []
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < n and 0 <= ny < n:
            out.append(key_at(depth, nx, ny, max_depth))
    return out


def cells_adjacent(a: int, da: int, b: int, db: int, max_depth: int = DEFAULT_MAX_DEPTH) -> bool:
    """两格是否共边（同层曼哈顿相邻；不同层不构成账本记录，见 grid.py）。"""
    if da != db:
        return False
    xa, ya = xy_of(a, da, max_depth)
    xb, yb = xy_of(b, db, max_depth)
    return abs(xa - xb) + abs(ya - yb) == 1


def cell_rect(
    key: int,
    depth: int,
    width: float,
    height: float,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> tuple[float, float, float, float]:
    """键 → 矩形 (x0, y0, x1, y1)。"""
    x, y = xy_of(key, depth, max_depth)
    n = 1 << depth
    return (
        x * width / n,
        y * height / n,
        (x + 1) * width / n,
        (y + 1) * height / n,
    )


def cell_size(depth: int, width: float, height: float) -> tuple[float, float]:
    """深度 depth 的格子尺寸（域内同层格子严格等大——等面积是构造性保证）。"""
    n = 1 << depth
    return width / n, height / n


def keys_covering_rect(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    depth: int,
    width: float,
    height: float,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[int]:
    """与矩形相交的所有 depth 层格子键（按 Hilbert 序排序）。"""
    n = 1 << depth
    cw, ch = width / n, height / n
    ix0 = max(0, min(n - 1, int(x0 / cw)))
    ix1 = max(0, min(n - 1, int((x1 - 1e-12) / cw)))
    iy0 = max(0, min(n - 1, int(y0 / ch)))
    iy1 = max(0, min(n - 1, int((y1 - 1e-12) / ch)))
    return sorted(
        key_at(depth, ix, iy, max_depth)
        for ix in range(ix0, ix1 + 1)
        for iy in range(iy0, iy1 + 1)
    )


def morton_key(depth: int, x: int, y: int, max_depth: int = DEFAULT_MAX_DEPTH) -> int:
    """Z-order（Morton）键——仅用于基准对照（局部性对比），Weft 自身不用。

    与 Weft 键同样的复合打包，保证同层可比。
    """
    code = 0
    for i in range(depth):
        code |= ((x >> i) & 1) << (2 * i)
        code |= ((y >> i) & 1) << (2 * i + 1)
    return (depth << DEPTH_SHIFT) | code
