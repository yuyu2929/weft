"""Weft 几何内核：点-in-多边形（支持孔）、线段长度密度、覆盖率估计。

全部为确定性浮点运算（IEEE 754 双精度），不依赖 shapely——
原型自包含、跨平台逐位可复现是硬需求（docs/THEORY.md §2.5）。
"""

from __future__ import annotations

import math

import numpy as np

Point = tuple[float, float]
Ring = np.ndarray  # (N,2) 浮点
Polygon = list[Ring]  # rings[0] 外环，其余为孔


def bbox_of(coords: np.ndarray) -> tuple[float, float, float, float]:
    xs = coords[:, 0]
    ys = coords[:, 1]
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def bbox_union(*boxes: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return x0, y0, x1, y1


def bbox_hits_rect(fb: tuple[float, float, float, float], rect, point: bool = False) -> bool:
    """要素包围盒 fb 与格子矩形 rect 是否相交（写入侧注册判定）。

    两种语义，对应分区纪律的两类不变式：
    - 点要素（bbox 退化为点）：半开规则 rect[0] <= px < rect[2]，
      与 cell_of 的 int(px/w*n) 归一化逐位一致 → 保证不变式 (I)
      "任意点至多属于一个叶子"；贴域左/下边界的点也正确归属。
    - 面/线要素（bbox 有面积）：含边界接触即相交（fb[0]==rect[2] 等
      接触情形算相交）→ 贴域右/上边界、跨格压线的要素正确注册，
      压线双注册正是缝合计账本要记录的跨格事件。
    """
    if point:
        return rect[0] <= fb[0] < rect[2] and rect[1] <= fb[1] < rect[3]
    return not (fb[2] < rect[0] or rect[2] < fb[0] or fb[3] < rect[1] or rect[3] < fb[1])


def point_in_ring(px: float, py: float, ring: np.ndarray) -> bool:
    """射线法 point-in-polygon。边界上的点按半开规则处理（确定性）。

    实现细节：用 (px,py) 向右的水平射线，统计交点奇偶。顶点/边穿越用
    半开区间 (y_i, y_i+1] 避免双重计数，保证同一查询点结果确定。
    """
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > py) != (yj > py):
            # x 方向交点
            t = (py - yi) / (yj - yi)
            xint = xi + t * (xj - xi)
            if px < xint:
                inside = not inside
        j = i
    return inside


def point_in_polygon(px: float, py: float, rings: Polygon) -> bool:
    """在内环则不在多边形内（孔）。"""
    if not point_in_ring(px, py, rings[0]):
        return False
    return not any(point_in_ring(px, py, r) for r in rings[1:])


def ring_bbox(ring: np.ndarray) -> tuple[float, float, float, float]:
    return bbox_of(ring)


def segment_rect_length(
    ax: float, ay: float, bx: float, by: float, rect: tuple[float, float, float, float]
) -> float:
    """线段在矩形内的长度（Liang-Barsky 裁剪，解析精确）。"""
    x0, y0, x1, y1 = rect
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return 0.0
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return 0.0
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return 0.0
                if r < t1:
                    t1 = r
    if t1 <= t0:
        return 0.0
    return math.hypot(dx, dy) * (t1 - t0)


def ring_signed_area(ring: np.ndarray) -> float:
    x = ring[:, 0]
    y = ring[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def coverage_grid(
    rings: Polygon,
    rect: tuple[float, float, float, float],
    res: int,
    sub: int,
) -> "np.ndarray":
    """整个 rect 的 res×res 子格覆盖率：一次批量计算（每子格 sub×sub 个采样点）。

    返回 (res, res) float64 数组。比逐子格调用 coverage_subsample 快 ~res 倍
    （每要素每次调用只做一次 numpy 批量运算）。
    """
    x0, y0, x1, y1 = rect
    w = (x1 - x0) / res
    h = (y1 - y0) / res
    if w <= 0 or h <= 0:
        return np.zeros((res, res))
    # 子格中心 + 子采样偏移
    cx = np.linspace(x0 + w * 0.5, x1 - w * 0.5, res)
    cy = np.linspace(y0 + h * 0.5, y1 - h * 0.5, res)
    off = (np.arange(sub) + 0.5) / sub - 0.5
    PX = (cx[:, None] + off[None, :] * w).ravel()  # (res*sub,)
    PY = (cy[:, None] + off[None, :] * h).ravel()
    GX, GY = np.meshgrid(PX, PY)  # (res*sub, res*sub)
    px = GX.ravel()
    py = GY.ravel()
    inside = _ring_contains_batch(px, py, rings[0])
    for hole in rings[1:]:
        inside &= ~_ring_contains_batch(px, py, hole)
    # 行 = y 采样索引 (i*sub+b)，列 = x 采样索引 (j*sub+a)；
    # 每个子格 (i,j) 的命中数 = 在 (b,a) 两个子采样轴上的和
    hits = inside.reshape(res, sub, res, sub).sum(axis=(1, 3))
    return hits / (sub * sub)


def coverage_subsample(
    rings: Polygon,
    rect: tuple[float, float, float, float],
    sub: int,
) -> float:
    """多边形在 rect 内的覆盖率估计：rect 内 sub×sub 规则采样点在多边形内
    的比例。确定性最坏误差 ≤ 1/sub²，期望误差 ≤ 1/(2·sub)（引理 3）。

    为速度用 numpy 批量测试（每子格 16~64 个点，全格批量一次算完）。
    """
    x0, y0, x1, y1 = rect
    w = (x1 - x0) / sub
    h = (y1 - y0) / sub
    if w <= 0 or h <= 0:
        return 0.0
    # 采样点取子格中心，避免落在格线上
    gx = np.linspace(x0 + w * 0.5, x1 - w * 0.5, sub)
    gy = np.linspace(y0 + h * 0.5, y1 - h * 0.5, sub)
    PX, PY = np.meshgrid(gx, gy)
    px = PX.ravel()
    py = PY.ravel()
    inside = np.zeros(px.shape, dtype=bool)
    outer = rings[0]
    inside |= _ring_contains_batch(px, py, outer)
    for hole in rings[1:]:
        inside &= ~_ring_contains_batch(px, py, hole)
    return float(inside.mean())


def _ring_contains_batch(px: np.ndarray, py: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """向量化射线法。与 point_in_ring 的半开规则一致。"""
    n = len(ring)
    inside = np.zeros(px.shape, dtype=bool)
    j = n - 1
    xi = ring[:, 0]
    yi = ring[:, 1]
    for i in range(n):
        yj = yi[j]
        cond = (yi[i] > py) != (yj > py)
        if cond.any():
            # 只在 cond 为真的子集上算交点，避免 0/0 的 inf/nan 污染
            idx = np.nonzero(cond)[0]
            t = (py[idx] - yi[i]) / (yj - yi[i])
            xint = xi[i] + t * (xi[j] - xi[i])
            inside[idx] ^= px[idx] < xint
        j = i
    return inside


def polyline_length(coords: np.ndarray) -> float:
    if len(coords) < 2:
        return 0.0
    d = np.diff(coords, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())
