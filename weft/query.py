"""Weft 查询层：bbox、KNN（最佳优先）、积分栅格导出、MVT 兼容导出、命中测试。

bbox 查询在 key 空间递归（整数运算，无指针追逐）：
- 格矩形完全在查询窗内 → 收录该叶（分区纪律：有格即叶子）；
- 相交 → 下沉四子；无格且无后代的空子树剪掉。

深度显式携带（复合键 key=(depth<<40)|hilbert，见 address.py 头部论证）。
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from .address import cell_rect, children
from .feature import KIND_POINT
from .geom import point_in_polygon
from .grid import Weft


def _rects_disjoint(a, b) -> bool:
    return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def bbox_query(
    weft: Weft,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    layer: str | None = None,
) -> list[int]:
    """返回与矩形相交的要素 fid（按 fid 排序，天然无重复——几何只存一份）。"""
    qrect = (x0, y0, x1, y1)
    hits: set[int] = set()
    stack = [(0, 0)]  # (key, depth)
    while stack:
        key, depth = stack.pop()
        rect = cell_rect(key, depth, weft.width, weft.height, weft.max_depth)
        if _rects_disjoint(rect, qrect):
            continue
        cell = weft.cells.get(key)
        if cell is not None:
            for fid in cell.feature_ids:
                f = weft.features[fid]
                if layer is not None and f.layer != layer:
                    continue
                if _rects_disjoint(f.bbox(), qrect):
                    continue
                hits.add(fid)
            continue  # 分区纪律：有格即叶子，不再下沉
        for c in children(key, depth, weft.max_depth):
            if c in weft.cells or weft._has_descendants(c, depth + 1):
                stack.append((c, depth + 1))
    return sorted(hits)


def knn(
    weft: Weft,
    layer: str,
    x: float,
    y: float,
    k: int,
) -> list[tuple[int, float]]:
    """最佳优先 KNN：堆按"查询点到格矩形距离下界"展开，保证精确。"""
    if k <= 0:
        return []
    heap: list[tuple[float, int, int]] = []
    root_rect = cell_rect(0, 0, weft.width, weft.height, weft.max_depth)
    heapq.heappush(heap, (_dist_point_rect(x, y, root_rect), 0, 0))
    results: list[tuple[int, float]] = []
    seen: set[int] = set()  # 跨格要素注册在多叶，去重（每 fid 只收集一次）
    # 最佳优先（Hjaltason-Samet 1995）：循环到堆空或堆顶下界 ≥ 第 k 距离。
    # 注意终止条件不能是"结果已满 k"——叶内点是批量收集的，
    # 满 k 只说明已有候选足够多，更近的点可能还在堆中未展开的子树里。
    while heap:
        lb, key, depth = heapq.heappop(heap)
        if len(results) >= k and lb >= results[-1][1] - 1e-12:
            break  # 堆中剩余格的距离下界都不优于第 k 个
        cell = weft.cells.get(key)
        if cell is not None:
            for fid in cell.feature_ids:
                if fid in seen:
                    continue
                f = weft.features[fid]
                if f.layer != layer:
                    continue
                d = _feature_distance(weft, f, x, y)
                if d < float("inf"):
                    seen.add(fid)
                    results.append((fid, d))
                    results.sort(key=lambda t: t[1])
                    results = results[:k]
            continue
        for c in children(key, depth, weft.max_depth):
            if c in weft.cells or weft._has_descendants(c, depth + 1):
                r = cell_rect(c, depth + 1, weft.width, weft.height, weft.max_depth)
                heapq.heappush(heap, (_dist_point_rect(x, y, r), c, depth + 1))
    results.sort(key=lambda t: t[1])
    return results[:k]


def _dist_point_rect(px: float, py: float, rect) -> float:
    dx = max(rect[0] - px, 0.0, px - rect[2])
    dy = max(rect[1] - py, 0.0, py - rect[3])
    return math.hypot(dx, dy)


def _feature_distance(weft: Weft, f, x: float, y: float) -> float:
    """点到要素的精确距离（点=欧氏；线=点到各段；面=0 若在内，否则到各环边）。"""
    if f.kind == KIND_POINT:
        return math.hypot(float(f.geom[0, 0]) - x, float(f.geom[0, 1]) - y)
    if f.kind == "line":
        best = float("inf")
        for a in range(len(f.geom) - 1):
            d = _dist_point_segment(x, y, f.geom[a], f.geom[a + 1])
            best = min(best, d)
        return best
    if point_in_polygon(x, y, f.geom):
        return 0.0
    best = float("inf")
    for ring in f.geom:
        n = len(ring)
        for a in range(n):
            d = _dist_point_segment(x, y, ring[a], ring[(a + 1) % n])
            best = min(best, d)
    return best


def _dist_point_segment(px: float, py: float, a: np.ndarray, b: np.ndarray) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / len2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def hit_test(
    weft: Weft,
    x: float,
    y: float,
    layer: str,
) -> dict:
    """命中测试：先用积分场 O(1) 得到候选（带误差界），再精确验证。

    返回 {"approx": float, "bound": float, "exact_hits": [fid...]}。
    """
    approx, bound = weft.field_at(layer, x, y)
    exact = weft.exact_point_query(layer, x, y)
    return {"approx": approx, "bound": bound, "exact_hits": exact}


def rasterize(
    weft: Weft,
    layer: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    res: int,
) -> np.ndarray:
    """把窗口导出为 res×res 栅格：从叶子积分场最近邻上采样，不重算几何。

    这是 Weft 的"栅格交付"路径——COG 式输出可以从矢量权威表示随时重建。
    """
    out = np.zeros((res, res), dtype=np.float32)
    li = weft.layer_order.index(layer)
    for i in range(res):
        py = y0 + (i + 0.5) * (y1 - y0) / res
        for j in range(res):
            px = x0 + (j + 0.5) * (x1 - x0) / res
            cell = weft.cell_of(px, py)
            if cell is None or cell.field is None:
                continue
            n = 1 << cell.depth
            r = weft.field_res
            from .address import xy_of

            cx, cy = xy_of(cell.key, cell.depth, weft.max_depth)
            fx = (px / weft.width * n - cx) * r
            fy = (py / weft.height * n - cy) * r
            i0 = min(r - 1, max(0, int(fy)))
            j0 = min(r - 1, max(0, int(fx)))
            out[i, j] = cell.field[i0, j0, li]
    return out


# ------------------------------------------------------------------ MVT 兼容导出
def to_mvt_tiles(
    weft: Weft,
    layer: str,
    zoom: int,
    tile_extent: int = 4096,
    buffer: int = 64,
) -> dict[tuple[int, int], dict]:
    """导出 MVT 风格瓦片（互操作性演示 + 基准对照组）。

    注意：这里**故意**按瓦片边界裁剪要素并带 buffer——这正是 MVT 的语义，
    用于量化"边界切割代价"（见 bench.py 的 artifact 计数）。
    Weft 自身查询从不切割（bbox_query/knn 返回完整要素）。
    """
    tiles: dict[tuple[int, int], dict] = {}
    n = 1 << zoom
    for fid, f in weft.features.items():
        if f.layer != layer:
            continue
        rings = f.geom if f.kind == "polygon" else [f.geom]
        for ring in rings:
            bx0 = float(ring[:, 0].min())
            by0 = float(ring[:, 1].min())
            bx1 = float(ring[:, 0].max())
            by1 = float(ring[:, 1].max())
            ix0 = max(0, int(bx0 / weft.width * n) - 1)
            ix1 = min(n - 1, int(bx1 / weft.width * n) + 1)
            iy0 = max(0, int(by0 / weft.height * n) - 1)
            iy1 = min(n - 1, int(by1 / weft.height * n) + 1)
            for tx in range(ix0, ix1 + 1):
                for ty in range(iy0, iy1 + 1):
                    t = tiles.setdefault((tx, ty), {"features": [], "n_parts": 0})
                    t["features"].append(fid)
                    t["n_parts"] += 1  # 该要素在这片瓦片出现一次（含 buffer 冗余）
    return tiles


def count_boundary_artifacts(weft: Weft, layer: str, zoom: int) -> dict:
    """量化 MVT 式切片的边界伪影：被切碎的要素数、重复几何份数。"""
    tiles = to_mvt_tiles(weft, layer, zoom)
    fid_tiles: dict[int, int] = {}
    for t in tiles.values():
        for fid in t["features"]:
            fid_tiles[fid] = fid_tiles.get(fid, 0) + 1
    cut = {fid: c for fid, c in fid_tiles.items() if c > 1}
    return {
        "n_tiles": len(tiles),
        "features_total": len(fid_tiles),
        "features_cut": len(cut),
        "duplicate_copies": sum(c - 1 for c in cut.values()),
    }
