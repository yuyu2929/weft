"""Weft 主结构：分区四叉骨架 + 要素块 + 缝合计账本 + 脏格版本。

关键设计决策（docs/DEVLOG.md 有完整踩坑记录）：
叶子格构成域的**分区**（partition），不是"每要素自选深度"的 overlap 集合。
分区纪律保证不变式：

    (I) 任意点 p 至多属于一个已存叶子；
    (II) 包围盒覆盖 p 的每个要素都注册在 p 所在的叶子里。

(I) 使 cell_of/field_at/exact_point_query 只需看唯一叶子；
(II) 使叶子积分场不漏任何几何上覆盖 p 的要素（场是全部相关要素的聚合）。
二者由插入时的"沿 bbox 裁剪的递归覆盖 + 超限分裂重分发"归纳维持。

地址为复合 uint64 键 key=(depth<<40)|hilbert（address.py 头部有完整论证：
左对齐/右对齐/深度低位三版都失败，DEVLOG.md 有记录）；Cell 与 Feature.cell_keys
配套携带/查询深度。内部节点（有后代的已分裂格）登记在 self._internal，
使 _has_descendants 为 O(1)——只看一层直接子键曾导致陈旧父键"复活"
与深层叶子 overlap（DEVLOG.md 分区纪律修复记录）。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

import numpy as np

from .address import (
    DEFAULT_MAX_DEPTH,
    cell_rect,
    cells_adjacent,
    children,
    key_at,
    parent,
)
from .feature import (
    KIND_LINE,
    KIND_POINT,
    KIND_POLYGON,
    Feature,
    Layer,
)
from .geom import (
    Polygon,
    bbox_hits_rect,
    coverage_grid,
    point_in_polygon,
    segment_rect_length,
)

MAX_FEATS_PER_CELL = 32


@dataclass
class Cell:
    """叶子格：Hilbert 键 + 深度 + 版本 + 要素引用 + 积分栅格场。"""

    key: int
    depth: int
    version: int = 0
    feature_ids: list[int] = dc_field(default_factory=list)
    field: np.ndarray | None = None  # (field_res, field_res, n_layers) float32
    dirty: bool = False


class WeftError(Exception):
    pass


class Weft:
    """统一的矢量-栅格结构（单文件、单有界矩形域、2D）。"""

    def __init__(
        self,
        width: float,
        height: float,
        layers: list[Layer],
        max_depth: int = DEFAULT_MAX_DEPTH,
        field_res: int = 8,
        sub: int = 4,
        max_feats_per_cell: int = MAX_FEATS_PER_CELL,
    ) -> None:
        if width <= 0 or height <= 0:
            raise WeftError("域尺寸必须为正")
        if not layers:
            raise WeftError("至少需要一个层")
        if field_res < 1 or sub < 1:
            raise WeftError("field_res/sub 必须 ≥ 1")
        self.width = float(width)
        self.height = float(height)
        self.max_depth = int(max_depth)
        self.field_res = int(field_res)
        self.sub = int(sub)
        self.max_feats_per_cell = int(max_feats_per_cell)
        self.layers: dict[str, Layer] = {}
        for lay in layers:
            if lay.name in self.layers:
                raise WeftError(f"层名重复: {lay.name}")
            self.layers[lay.name] = lay
        self.layer_order: list[str] = [l.name for l in layers]
        self.cells: dict[int, Cell] = {}
        self._internal: set[int] = set()  # 有后代的中间节点键（split 产生，空枝剪除）
        self.features: dict[int, Feature] = {}
        self.ledger: dict[tuple[int, int], set[int]] = {}
        self._next_fid: int = 1
        self.logical_version: int = 0
        self.journal: list[dict] = []

    # ------------------------------------------------------------------ 树操作
    def _child_keys(self, key: int, depth: int) -> list[int]:
        return children(key, depth, self.max_depth)

    def _has_descendants(self, key: int, depth: int) -> bool:
        """子树中是否有任何叶子。

        经 _internal 集合 O(1) 判定（split 时父键登记、空枝剪除时注销）。
        只看 cells 不够：分区纪律下 split 后父键不在 cells，但其区域
        已有更细的叶子——曾因只查一层直接子键导致陈旧父键"复活"与
        深层叶子 overlap（见 docs/DEVLOG.md 分区纪律修复记录）。
        """
        return any(c in self.cells or c in self._internal for c in self._child_keys(key, depth))

    def _prune_chain(self, key: int, depth: int) -> None:
        """空叶删除后向上剪枝：把不再有任何后代的祖先从 _internal 注销。"""
        k, d = key, depth
        while d > 0:
            pk, pd = parent(k, d, self.max_depth), d - 1
            if pk not in self._internal:
                return
            if any(c in self.cells or c in self._internal for c in self._child_keys(pk, pd, self.max_depth)):
                return
            self._internal.discard(pk)
            k, d = pk, pd

    def _split(self, key: int, depth: int) -> None:
        """叶子超限 → 四分，重分发要素（bbox 与子格相交即入）。"""
        cell = self.cells.pop(key)
        self._internal.add(key)  # 父键成为中间节点（区域已由更细叶子覆盖）
        kids = self._child_keys(key, depth)
        rects = {
            c: cell_rect(c, depth + 1, self.width, self.height, self.max_depth) for c in kids
        }
        for c in kids:
            self.cells[c] = Cell(key=c, depth=depth + 1)
        for fid in cell.feature_ids:
            f = self.features[fid]
            fb = f.bbox()
            new_keys = set()
            for c, r in rects.items():
                if bbox_hits_rect(fb, r, point=f.kind == KIND_POINT):
                    self.cells[c].feature_ids.append(fid)
                    self.cells[c].dirty = True  # 重分发进来的要素需重算场
                    new_keys.add(c)
            f.cell_keys = tuple(sorted((set(f.cell_keys) - {key}) | new_keys))
        self._rebuild_ledger_for([self.features[f] for f in cell.feature_ids])

    def _rebuild_ledger_for(self, feats: list[Feature]) -> None:
        """重建受影响要素的缝合账本记录（跨格共边事件）。"""
        for f in feats:
            for pair in [p for p, s in self.ledger.items() if f.fid in s]:
                s = self.ledger[pair]
                s.discard(f.fid)
                if not s:
                    del self.ledger[pair]
            self._add_ledger_for(f)

    def _add_ledger_for(self, f: Feature) -> None:
        by_depth: dict[int, list[int]] = {}
        for k in f.cell_keys:
            cell = self.cells.get(k)
            if cell is None:
                continue
            by_depth.setdefault(cell.depth, []).append(k)
        for group in by_depth.values():
            for i, ka in enumerate(group):
                for kb in group[i + 1 :]:
                    da = self.cells[ka].depth
                    db = self.cells[kb].depth
                    if da == db and cells_adjacent(ka, da, kb, db, self.max_depth):
                        pair = (ka, kb) if ka < kb else (kb, ka)
                        self.ledger.setdefault(pair, set()).add(f.fid)

    # ------------------------------------------------------------------ 写入
    def _register(self, feat: Feature, op: str = "insert") -> None:
        """把要素注册进所有与其 bbox 相交的叶子（分区纪律，维持不变式 I/II）。"""
        fb = feat.bbox()
        keys: list[int] = []
        # 先登记要素本体：cover 中的 _split 会重分发 cell.feature_ids
        # （此时新要素已在某些叶的列表里），找不到会 KeyError。
        self.features[feat.fid] = feat

        if feat.kind == KIND_POINT:
            # 点要素：走 cell_of 式上溯（半开归属），与查询侧逐位一致。
            # 曾用通用 AABB 剪枝导致 x=0/y=0 边界的点被所有格子剪掉
            # （fb[2] <= rect[0] 即 0<=0 成立）——见 docs/DEVLOG.md。
            px, py = float(feat.geom[0, 0]), float(feat.geom[0, 1])
            d = self.max_depth
            n = 1 << d
            ix = min(n - 1, max(0, int(px / self.width * n)))
            iy = min(n - 1, max(0, int(py / self.height * n)))
            k = key_at(d, ix, iy, self.max_depth)
            depth = d
            while True:
                if k in self.cells:
                    cell = self.cells[k]
                    if feat.fid not in cell.feature_ids:
                        cell.feature_ids.append(feat.fid)
                        cell.feature_ids.sort()
                        cell.dirty = True
                    keys.append(k)
                    if len(cell.feature_ids) > self.max_feats_per_cell and depth < self.max_depth:
                        self._split(k, depth)
                        keys.remove(k)  # split 后旧键不再是叶子，从记账移除
                    break
                if depth == 0:  # 全域为空：建根叶
                    self.cells[0] = Cell(key=0, depth=0, feature_ids=[feat.fid], dirty=True)
                    keys.append(0)
                    break
                k = parent(k, depth, self.max_depth)
                depth -= 1
        else:

            def cover(key: int, depth: int) -> None:
                rect = cell_rect(key, depth, self.width, self.height, self.max_depth)
                # 面/线：含边界接触相交（贴域右/上边界的线也曾被误剪）
                if not bbox_hits_rect(fb, rect):
                    return
                if key in self.cells:
                    cell = self.cells[key]
                    if feat.fid not in cell.feature_ids:
                        cell.feature_ids.append(feat.fid)
                        cell.feature_ids.sort()
                        cell.dirty = True
                    keys.append(key)
                    if len(cell.feature_ids) > self.max_feats_per_cell and depth < self.max_depth:
                        self._split(key, depth)
                        keys.remove(key)  # split 后旧键不再是叶子，从记账移除
                    return
                if self._has_descendants(key, depth):
                    for c in self._child_keys(key, depth):
                        cover(c, depth + 1)
                    return
                # 空区域：在此建叶
                self.cells[key] = Cell(key=key, depth=depth, feature_ids=[feat.fid], dirty=True)
                keys.append(key)

            cover(0, 0)
        feat.cell_keys = tuple(sorted(set(keys) | set(feat.cell_keys)))
        self._add_ledger_for(feat)
        self.journal.append(
            {"version": self.logical_version, "op": op, "fid": feat.fid, "keys": list(feat.cell_keys)}
        )

    def add_point(self, layer: str, x: float, y: float, props: dict | None = None) -> int:
        if not (0.0 <= x < self.width and 0.0 <= y < self.height):
            raise WeftError(f"点 ({x}, {y}) 在域 [0,{self.width})×[0,{self.height}) 之外")
        self._layer(layer, KIND_POINT)
        feat = Feature(
            fid=self._next_fid,
            layer=layer,
            kind=KIND_POINT,
            geom=np.array([[float(x), float(y)]], dtype=np.float64),
            props=dict(props or {}),
        )
        self._register(feat)
        self._next_fid += 1
        return feat.fid

    def add_line(self, layer: str, coords, props: dict | None = None) -> int:
        self._layer(layer, KIND_LINE)
        arr = np.asarray(coords, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != 2 or len(arr) < 2:
            raise WeftError("线要素至少需要 2 个 (x,y) 顶点")
        feat = Feature(fid=self._next_fid, layer=layer, kind=KIND_LINE, geom=arr, props=dict(props or {}))
        self._register(feat)
        self._next_fid += 1
        return feat.fid

    def add_polygon(self, layer: str, rings, props: dict | None = None) -> int:
        self._layer(layer, KIND_POLYGON)
        # 便利：传裸环（[[x,y],...] 首元素是数）时按单环处理
        if rings and not isinstance(rings[0], (list, tuple, np.ndarray)):
            rings = [rings]
        elif rings and isinstance(rings[0], (list, tuple)) and rings[0] and isinstance(rings[0][0], (int, float)):
            rings = [rings]
        polys: Polygon = []
        for ring in rings:
            arr = np.asarray(ring, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[1] != 2 or len(arr) < 3:
                raise WeftError("多边形环至少需要 3 个顶点")
            polys.append(arr)
        feat = Feature(fid=self._next_fid, layer=layer, kind=KIND_POLYGON, geom=polys, props=dict(props or {}))
        self._register(feat)
        self._next_fid += 1
        return feat.fid

    def _layer(self, name: str, kind: str) -> Layer:
        lay = self.layers.get(name)
        if lay is None:
            raise WeftError(f"未定义的层 {name!r}")
        if lay.kind != kind:
            raise WeftError(f"层 {name!r} 类型为 {lay.kind}，与 {kind} 不符")
        return lay

    def delete(self, fid: int) -> None:
        feat = self.features.pop(fid, None)
        if feat is None:
            raise WeftError(f"要素不存在: fid={fid}")
        self.journal.append(
            {"version": self.logical_version, "op": "delete", "fid": fid, "keys": list(feat.cell_keys)}
        )
        for k in feat.cell_keys:
            cell = self.cells.get(k)
            if cell is None:
                continue
            if fid in cell.feature_ids:
                cell.feature_ids.remove(fid)
                cell.dirty = True
            if not cell.feature_ids:
                del self.cells[k]  # 空叶剪枝（分区纪律允许）
                self._prune_chain(k, cell.depth)
        for pair in [p for p, s in self.ledger.items() if fid in s]:
            s = self.ledger[pair]
            s.discard(fid)
            if not s:
                del self.ledger[pair]

    def update_geometry(self, fid: int, geom) -> None:
        feat = self.features.get(fid)
        if feat is None:
            raise WeftError(f"要素不存在: fid={fid}")
        for k in feat.cell_keys:
            cell = self.cells.get(k)
            if cell is not None and fid in cell.feature_ids:
                cell.feature_ids.remove(fid)
                cell.dirty = True
        for pair in [p for p, s in self.ledger.items() if fid in s]:
            s = self.ledger[pair]
            s.discard(fid)
            if not s:
                del self.ledger[pair]
        feat.cell_keys = ()
        if feat.kind == KIND_POLYGON:
            # 与 add_polygon 同样的裸环便利
            if geom and not isinstance(geom[0], (list, tuple, np.ndarray)):
                geom = [geom]
            elif (
                geom
                and isinstance(geom[0], (list, tuple))
                and geom[0]
                and isinstance(geom[0][0], (int, float))
            ):
                geom = [geom]
            polys: Polygon = [np.asarray(r, dtype=np.float64) for r in geom]
        else:
            polys = np.asarray(geom, dtype=np.float64)
        feat.geom = polys
        self._register(feat, op="update")

    # ------------------------------------------------------------------ 提交
    def dirty_cells(self) -> list[int]:
        return sorted(k for k, c in self.cells.items() if c.dirty)

    def commit(self) -> int:
        """重算全部脏格的积分场，格子版本 +1，逻辑时钟 +1。返回重算格数。"""
        dirty = self.dirty_cells()
        for k in dirty:
            cell = self.cells[k]
            cell.field = self._build_field(k)
            cell.version += 1
            cell.dirty = False
        if dirty:
            self.logical_version += 1
        return len(dirty)

    def _build_field(self, key: int) -> np.ndarray:
        r = self.field_res
        n_layers = len(self.layer_order)
        out = np.zeros((r, r, n_layers), dtype=np.float32)
        cell = self.cells[key]
        rect = cell_rect(key, cell.depth, self.width, self.height, self.max_depth)
        x0, y0, x1, y1 = rect
        sw = (x1 - x0) / r
        sh = (y1 - y0) / r
        buckets: dict[int, list[Feature]] = {}
        for fid in cell.feature_ids:
            f = self.features[fid]
            li = self.layer_order.index(f.layer)
            buckets.setdefault(li, []).append(f)
        for li, feats in buckets.items():
            kind = self.layers[self.layer_order[li]].kind
            if kind == KIND_POLYGON:
                # 面层：整格一次批量估计（每要素一次 numpy 运算）
                cov = np.zeros((r, r), dtype=np.float64)
                for f in feats:
                    cov += coverage_grid(f.geom, (x0, y0, x1, y1), r, self.sub)
                out[:, :, li] = np.clip(cov, 0.0, 1.0).astype(np.float32)
                continue
            for i in range(r):
                cy0 = y0 + i * sh
                cy1 = cy0 + sh
                for j in range(r):
                    cx0 = x0 + j * sw
                    cx1 = cx0 + sw
                    sub_rect = (cx0, cy0, cx1, cy1)
                    if kind == KIND_POINT:
                        cnt = 0
                        for f in feats:
                            px, py = float(f.geom[0, 0]), float(f.geom[0, 1])
                            if cx0 <= px < cx1 and cy0 <= py < cy1:
                                cnt += 1
                        out[i, j, li] = cnt
                    else:  # line：子格内线段总长 / 子格面积 = 线长密度
                        total = 0.0
                        for f in feats:
                            for a in range(len(f.geom) - 1):
                                total += segment_rect_length(
                                    float(f.geom[a, 0]), float(f.geom[a, 1]),
                                    float(f.geom[a + 1, 0]), float(f.geom[a + 1, 1]),
                                    sub_rect,
                                )
                        out[i, j, li] = total / (sw * sh)
        return out

    # ------------------------------------------------------------------ 查询
    def cell_of(self, x: float, y: float) -> Cell | None:
        """包含点 (x,y) 的唯一叶子（分区纪律 → 唯一确定）。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        d = self.max_depth
        n = 1 << d
        ix = min(n - 1, int(x / self.width * n))
        iy = min(n - 1, int(y / self.height * n))
        k = key_at(d, ix, iy, self.max_depth)
        depth = d
        while True:
            if k in self.cells:
                return self.cells[k]
            if depth == 0:
                return None
            k = parent(k, depth, self.max_depth)
            depth -= 1

    def field_at(self, layer: str, x: float, y: float) -> tuple[float, float]:
        """O(1) 积分场查询。返回 (估计值, 误差界)（引理 3）。"""
        cell = self.cell_of(x, y)
        if cell is None or cell.field is None:
            return 0.0, 0.0
        n = 1 << cell.depth
        r = self.field_res
        fx = (x / self.width * n - xy_of_key(cell.key, cell.depth, self.max_depth)[0]) * r
        fy = (y / self.height * n - xy_of_key(cell.key, cell.depth, self.max_depth)[1]) * r
        i = min(r - 1, max(0, int(fy)))
        j = min(r - 1, max(0, int(fx)))
        li = self.layer_order.index(layer)
        val = float(cell.field[i, j, li])
        bound = 1.0 / (self.sub * self.sub) if self.layers[layer].kind == KIND_POLYGON else 0.0
        return val, bound

    def exact_point_query(self, layer: str, x: float, y: float) -> list[int]:
        """精确查询：返回几何上包含 (x,y) 的要素 fid 列表。"""
        cell = self.cell_of(x, y)
        if cell is None:
            return []
        hits = []
        for fid in cell.feature_ids:
            f = self.features[fid]
            if f.layer != layer:
                continue
            if f.kind == KIND_POINT:
                if float(f.geom[0, 0]) == x and float(f.geom[0, 1]) == y:
                    hits.append(fid)
            elif f.kind == KIND_POLYGON:
                if point_in_polygon(x, y, f.geom):
                    hits.append(fid)
            else:
                for a in range(len(f.geom) - 1):
                    if _point_on_segment(x, y, f.geom[a], f.geom[a + 1]):
                        hits.append(fid)
                        break
        return sorted(hits)

    def crossings(self, key_a: int, key_b: int) -> set[int]:
        """穿过两格公共边界的要素集合（缝合账本，工程审计一等查询）。"""
        pair = (key_a, key_b) if key_a < key_b else (key_b, key_a)
        return set(self.ledger.get(pair, ()))

    def reconstruct(self, fid: int) -> Feature:
        """按 fid 取完整要素（跨格不切割，hover/拾取直接拿到全几何）。"""
        f = self.features.get(fid)
        if f is None:
            raise WeftError(f"要素不存在: fid={fid}")
        return f

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict:
        hist: dict[int, int] = {}
        for c in self.cells.values():
            hist[c.depth] = hist.get(c.depth, 0) + 1
        return {
            "n_features": len(self.features),
            "n_cells": len(self.cells),
            "n_ledger_pairs": len(self.ledger),
            "logical_version": self.logical_version,
            "cells_by_depth": dict(sorted(hist.items())),
        }


def xy_of_key(key: int, depth: int, max_depth: int = DEFAULT_MAX_DEPTH) -> tuple[int, int]:
    from .address import xy_of

    return xy_of(key, depth, max_depth)


def _point_on_segment(px: float, py: float, a: np.ndarray, b: np.ndarray) -> bool:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > 1e-9:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < 0:
        return False
    len2 = (bx - ax) ** 2 + (by - ay) ** 2
    return dot <= len2 + 1e-9
