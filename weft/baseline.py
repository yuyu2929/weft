"""参照实现（基准对照组）：Geohash 索引、扁平栅格金字塔、暴力扫描。

这些不是 Weft 的组成部分，而是 bench.py 的"对照臂"——
用同一套合成数据回答同一个问题，量化 Weft 的设计选择收益。
"""

from __future__ import annotations

import math

import numpy as np

from .feature import KIND_POINT, KIND_POLYGON
from .geom import point_in_polygon

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(x: float, y: float, precision: int = 9) -> str:
    """标准 geohash（base32，经纬度二分交错）。"""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    bits = []
    is_lon = True
    while len(bits) < precision * 5:
        if is_lon:
            mid = (lon_lo + lon_hi) / 2
            if x >= mid:
                bits.append(1)
                lon_lo = mid
            else:
                bits.append(0)
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if y >= mid:
                bits.append(1)
                lat_lo = mid
            else:
                bits.append(0)
                lat_hi = mid
        is_lon = not is_lon
    gh = []
    for i in range(0, len(bits), 5):
        idx = 0
        for b in bits[i : i + 5]:
            idx = (idx << 1) | b
        gh.append(_BASE32[idx])
    return "".join(gh)


class GeohashIndex:
    """点层 geohash 索引：dict[str, list[fid]]，前缀范围扫描。"""

    def __init__(self, precision: int = 9) -> None:
        self.precision = precision
        self.index: dict[str, list[int]] = {}

    def add(self, fid: int, x: float, y: float) -> None:
        self.index.setdefault(geohash_encode(x, y, self.precision), []).append(fid)

    def prefix_scan(self, x: float, y: float, radius: float) -> set[int]:
        """9-cell 扩展：查中心格及 8 邻居格（geohash 经典做法）。"""
        cx, cy = x, y
        out: set[int] = set()
        cells = [(cx, cy)]
        for dx in (-radius, 0, radius):
            for dy in (-radius, 0, radius):
                cells.append((cx + dx, cy + dy))
        for (px, py) in cells:
            out.update(self.index.get(geohash_encode(px, py, self.precision), []))
        return out


class RasterPyramid:
    """COG 式扁平栅格金字塔：每 zoom 一层全分辨率栅格，编辑=整层重渲染。"""

    def __init__(self, width: float, height: float, max_zoom: int = 6) -> None:
        self.width = width
        self.height = height
        self.max_zoom = max_zoom
        self.levels: dict[int, np.ndarray] = {}

    def build(self, polygons: list, zoom_grid: int = 256) -> int:
        """把面层渲染成每 zoom 一张 zoom_grid×zoom_grid 的覆盖率栅格。

        向量化实现：每个多边形对整张网格做一次批量 PIP（而非逐像素）。
        """
        from .geom import _ring_contains_batch

        total_bytes = 0
        for z in range(self.max_zoom + 1):
            n = zoom_grid
            grid = np.zeros((n, n), dtype=np.float32)
            xs = (np.arange(n) + 0.5) * self.width / n
            ys = (np.arange(n) + 0.5) * self.height / n
            GX, GY = np.meshgrid(xs, ys)
            px = GX.ravel()
            py = GY.ravel()
            for rings in polygons:
                inside = _ring_contains_batch(px, py, rings[0])
                for hole in rings[1:]:
                    inside &= ~_ring_contains_batch(px, py, hole)
                grid.ravel()[inside] = 1.0
            self.levels[z] = grid
            total_bytes += grid.nbytes
        return total_bytes

    def query(self, x: float, y: float, zoom: int) -> float:
        g = self.levels[zoom]
        n = g.shape[0]
        i = min(n - 1, int(y / self.height * n))
        j = min(n - 1, int(x / self.width * n))
        return float(g[i, j])

    def edit_cost(self) -> int:
        """COG 的编辑代价：全部层级重渲染（字节数）。"""
        return sum(g.nbytes for g in self.levels.values())


def brute_force_point_query(features: list, layer: str, x: float, y: float) -> list[int]:
    """无索引全表扫描（点-in-面）。"""
    hits = []
    for f in features:
        if f.layer != layer:
            continue
        if f.kind == KIND_POINT:
            if float(f.geom[0, 0]) == x and float(f.geom[0, 1]) == y:
                hits.append(f.fid)
        elif f.kind == KIND_POLYGON:
            if point_in_polygon(x, y, f.geom):
                hits.append(f.fid)
    return sorted(hits)


def brute_force_knn(features: list, layer: str, x: float, y: float, k: int) -> list[tuple[int, float]]:
    out = []
    for f in features:
        if f.layer != layer:
            continue
        if f.kind == KIND_POINT:
            d = math.hypot(float(f.geom[0, 0]) - x, float(f.geom[0, 1]) - y)
            out.append((f.fid, d))
    out.sort(key=lambda t: t[1])
    return out[:k]
