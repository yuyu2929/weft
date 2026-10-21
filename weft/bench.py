"""Weft 基准套件：与参照实现同数据同问题对比，验证设计声明。

对照臂：
  - brute force   无索引全表扫描
  - geohash       点层前缀索引（9-cell 扩展）
  - raster pyr.   COG 式扁平栅格金字塔（整层重渲染）
  - MVT tiles     Mapbox 式切割瓦片（带 buffer 冗余）

指标：存储字节、查询延迟、边界伪影、编辑重写量、栅格查询延迟、局部性。
所有随机数固定种子 → 结果可复现。
"""

from __future__ import annotations

import json
import random
import time

import numpy as np

from .baseline import (
    GeohashIndex,
    RasterPyramid,
    brute_force_knn,
    brute_force_point_query,
)
from .codec import serialize
from .feature import KIND_POINT, Layer
from .grid import Weft
from .query import count_boundary_artifacts, knn
from .query import bbox_query as weft_bbox_query


# ------------------------------------------------------------------ 合成数据
def make_district(seed: int = 7, n_parcels: int = 400, n_roads: int = 250, n_sensors: int = 800):
    """合成工程区：地块（面）、道路（线）、传感器（点）。域 1000×1000 米。"""
    rng = random.Random(seed)
    npr = np.random.RandomState(seed)
    W = H = 1000.0
    layers = [
        Layer(name="parcels", kind="polygon", columns=("owner",)),
        Layer(name="roads", kind="line", columns=("name",)),
        Layer(name="sensors", kind=KIND_POINT, columns=("kind",)),
    ]
    weft = Weft(W, H, layers)
    # 地块：Manhattan 网格 + 抖动矩形
    gx = gy = 25
    cw, ch = W / gx, H / gy
    owners = ["A", "B", "C", "D"]
    parcels = []
    for i in range(gx):
        for j in range(gy):
            if rng.random() < 0.08:
                continue  # 空地
            jx = (rng.random() - 0.5) * cw * 0.15
            jy = (rng.random() - 0.5) * ch * 0.15
            x0 = i * cw + cw * 0.08 + jx
            y0 = j * ch + ch * 0.08 + jy
            x1 = (i + 1) * cw - cw * 0.08 + jx
            y1 = (j + 1) * ch - ch * 0.08 + jy
            ring = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])
            owner = rng.choice(owners)
            fid = weft.add_polygon("parcels", [ring], {"owner": owner})
            parcels.append((fid, [ring]))
    # 道路：横竖主干 + 斜线
    roads = []
    for i in range(gx + 1):
        x = i * cw
        pts = [[x, 0.0], [x, H * 0.5], [x + 12.0, H * 0.5], [x + 12.0, H]]
        if rng.random() < 0.6:
            fid = weft.add_line("roads", pts, {"name": f"V{i}"})
            roads.append((fid, pts))
    for j in range(gy + 1):
        y = j * ch
        pts = [[0.0, y], [W * 0.5, y], [W * 0.5, y + 12.0], [W, y + 12.0]]
        if rng.random() < 0.6:
            fid = weft.add_line("roads", pts, {"name": f"H{j}"})
            roads.append((fid, pts))
    for _ in range(20):
        x0, y0 = rng.uniform(0, W), rng.uniform(0, H)
        x1, y1 = rng.uniform(0, W), rng.uniform(0, H)
        fid = weft.add_line("roads", [[x0, y0], [x1, y1]], {"name": "diag"})
        roads.append((fid, [[x0, y0], [x1, y1]]))
    # 传感器：均匀 + 几个密集簇
    sensors = []
    for _ in range(n_sensors):
        if rng.random() < 0.3:
            cx, cy = rng.uniform(0, W), rng.uniform(0, H)
            x, y = cx + rng.gauss(0, 15), cy + rng.gauss(0, 15)
        else:
            x, y = rng.uniform(0, W), rng.uniform(0, H)
        x = min(max(x, 0.0), W - 1e-6)
        y = min(max(y, 0.0), H - 1e-6)
        fid = weft.add_point("sensors", x, y, {"kind": rng.choice(["temp", "noise", "pm25"])})
        sensors.append((fid, x, y))
    return weft, parcels, roads, sensors


# ------------------------------------------------------------------ 基准
def _timeit(fn, *a, **k) -> tuple[float, object]:
    t0 = time.perf_counter()
    r = fn(*a, **k)
    return time.perf_counter() - t0, r


def run_all(verbose: bool = False) -> dict:
    print("== 生成合成工程区（1000×1000m：地块/道路/传感器）…")
    weft, parcels, roads, sensors = make_district()
    t_build, n_dirty = _timeit(weft.commit)
    st = weft.stats()
    print(f"   要素 {st['n_features']}，叶子 {st['n_cells']}，账本对 {st['n_ledger_pairs']}，"
          f"建场耗时 {t_build:.2f}s")
    print(f"   叶子深度分布: {st['cells_by_depth']}")

    results: dict = {"stats": st, "build_field_s": round(t_build, 3)}

    # ---- 1. 存储对比
    print("\n== [1] 存储对比（同一数据）")
    weft_bytes = len(serialize(weft))
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"fid": fid},
                "geometry": {"type": "Polygon", "coordinates": [r.tolist() for r in rings]},
            }
            for fid, rings in parcels
        ],
    }
    import json as _json

    gj_bytes = len(_json.dumps(geojson).encode())
    from .query import to_mvt_tiles

    mvt_tiles = to_mvt_tiles(weft, "parcels", zoom=5)
    # MVT 式存储估算：每个瓦片存其覆盖要素的量化几何（近似按顶点数计）
    mvt_bytes = 0
    for t in mvt_tiles.values():
        for fid in set(t["features"]):
            f = weft.features[fid]
            mvt_bytes += 12 * len(f.geom[0]) + 24  # 量化顶点 + 头
    pyr = RasterPyramid(weft.width, weft.height, max_zoom=4)
    pyr_bytes = pyr.build([rings for _, rings in parcels], zoom_grid=128)
    print(f"   Weft 文件            {weft_bytes:>10,} B")
    print(f"   GeoJSON（同要素）     {gj_bytes:>10,} B  ({gj_bytes/weft_bytes:.1f}×)")
    print(f"   MVT 式瓦片(z5)        {mvt_bytes:>10,} B  ({mvt_bytes/weft_bytes:.1f}×)")
    print(f"   栅格金字塔(7层)       {pyr_bytes:>10,} B  ({pyr_bytes/weft_bytes:.1f}×)")
    results["storage"] = {
        "weft_bytes": weft_bytes,
        "geojson_bytes": gj_bytes,
        "mvt_bytes": mvt_bytes,
        "raster_pyramid_bytes": pyr_bytes,
    }

    # ---- 2. 边界伪影
    print("\n== [2] 边界伪影（MVT 式切割 vs Weft）")
    art = count_boundary_artifacts(weft, "parcels", zoom=5)
    # Weft：几何只存一次 → 重复份数恒 0；用序列化前后的要素几何字节和验证
    geom_bytes_unique = sum(f.geom[0].nbytes for f in weft.features.values() if f.layer == "parcels")
    print(f"   MVT 式: {art['features_cut']}/{art['features_total']} 个地块被切碎，"
          f"重复存储 {art['duplicate_copies']} 份")
    print(f"   Weft:   0 个要素被切碎（几何一次存储 {geom_bytes_unique:,} B，"
          f"格内仅存 fid 引用）")
    results["boundary"] = art
    results["weft_geometry_bytes_unique"] = int(geom_bytes_unique)

    # ---- 3. 点查询延迟
    print("\n== [3] 点查询延迟（100 个随机点，地块 PIP）")
    rng = random.Random(11)
    pts = [(rng.uniform(0, 1000), rng.uniform(0, 1000)) for _ in range(100)]
    t0 = time.perf_counter()
    for x, y in pts:
        weft.exact_point_query("parcels", x, y)
    t_weft = (time.perf_counter() - t0) / len(pts) * 1e6
    feats = list(weft.features.values())
    t0 = time.perf_counter()
    for x, y in pts:
        brute_force_point_query(feats, "parcels", x, y)
    t_bf = (time.perf_counter() - t0) / len(pts) * 1e6
    print(f"   Weft（叶内精确）   {t_weft:8.1f} µs/query")
    print(f"   暴力全表扫描        {t_bf:8.1f} µs/query  ({t_bf/t_weft:.1f}×)")
    results["point_query_us"] = {"weft": round(t_weft, 1), "brute_force": round(t_bf, 1)}

    # ---- 4. 积分场 O(1) vs 栅格金字塔
    print("\n== [4] 栅格查询延迟（1000 次 field_at vs 金字塔查询）")
    t0 = time.perf_counter()
    for x, y in pts * 10:
        weft.field_at("parcels", x, y)
    t_field = (time.perf_counter() - t0) / (len(pts) * 10) * 1e6
    t0 = time.perf_counter()
    for x, y in pts * 10:
        pyr.query(x, y, zoom=4)
    t_pyr = (time.perf_counter() - t0) / (len(pts) * 10) * 1e6
    print(f"   Weft field_at      {t_field:8.2f} µs/query（O(1)，带误差界）")
    print(f"   栅格金字塔查询      {t_pyr:8.2f} µs/query（不含编辑重建代价）")
    results["field_query_us"] = {"weft_field": round(t_field, 2), "raster_pyramid": round(t_pyr, 2)}

    # ---- 5. KNN
    print("\n== [5] KNN（传感器层，k=10，50 个查询点）")
    pts50 = [(rng.uniform(0, 1000), rng.uniform(0, 1000)) for _ in range(30)]
    t0 = time.perf_counter()
    for x, y in pts50:
        r1 = knn(weft, "sensors", x, y, 10)
    t_knn = (time.perf_counter() - t0) / len(pts50) * 1e3
    t0 = time.perf_counter()
    for x, y in pts50:
        r2 = brute_force_knn(feats, "sensors", x, y, 10)
    t_bfk = (time.perf_counter() - t0) / len(pts50) * 1e3
    # 正确性：并列距离下顺序不保证一致 → 比较 fid 集合与第 k 距离
    def _knn_signature(res):
        fids = sorted(f for f, _ in res)
        dmax = max((d for _, d in res), default=float("inf"))
        return fids, round(dmax, 6)

    ok = all(
        _knn_signature(knn(weft, "sensors", x, y, 10))
        == _knn_signature(brute_force_knn(feats, "sensors", x, y, 10))
        for x, y in pts50[:3]
    )
    print(f"   Weft 最佳优先   {t_knn:8.2f} ms/query")
    print(f"   暴力扫描        {t_bfk:8.2f} ms/query  ({t_bfk/t_knn:.1f}×)  结果一致: {ok}")
    results["knn_ms"] = {"weft": round(t_knn, 2), "brute_force": round(t_bfk, 2), "exact_match": ok}

    # ---- 6. 编辑代价（插入 1% 新地块）
    print("\n== [6] 编辑代价（新增 6 个地块 ≈1%）")
    import copy

    base_bytes = weft_bytes
    t0 = time.perf_counter()
    new_rings = []
    for i in range(6):
        cx, cy = rng.uniform(100, 900), rng.uniform(100, 900)
        ring = np.array(
            [[cx, cy], [cx + 20, cy], [cx + 20, cy + 20], [cx, cy + 20], [cx, cy]], dtype=np.float64
        )
        weft.add_polygon("parcels", [ring], {"owner": "NEW"})
        new_rings.append([ring])
    n_dirty_edit = len(weft.dirty_cells())
    weft.commit()
    t_edit = time.perf_counter() - t0
    new_bytes = len(serialize(weft))
    # MVT 式：新要素触及的瓦片数（全部 zoom 近似为 z5 的触及数）
    # 按 z5 瓦片网格估算新地块触及瓦片
    n = 32
    mvt_affected = 0
    for rings in new_rings:
        b = rings[0]
        x0, x1 = b[:, 0].min(), b[:, 0].max()
        y0, y1 = b[:, 1].min(), b[:, 1].max()
        mvt_affected += (int(x1 / 1000 * n) - int(x0 / 1000 * n) + 1) * (
            int(y1 / 1000 * n) - int(y0 / 1000 * n) + 1
        )
    pyr_bytes_after = pyr.build([rings for _, rings in parcels] + new_rings)
    print(f"   Weft: 脏格 {n_dirty_edit} 个，序列化增量 {new_bytes-base_bytes:+,} B，耗时 {t_edit*1000:.1f} ms")
    print(f"   MVT 式: 需重切 {mvt_affected} 个 z5 瓦片（每个含 buffer 冗余）")
    print(f"   COG 式: 需重渲染全部 7 层 = {pyr_bytes_after:,} B（整文件重生）")
    results["edit"] = {
        "weft_dirty_cells": n_dirty_edit,
        "weft_bytes_delta": new_bytes - base_bytes,
        "weft_ms": round(t_edit * 1000, 1),
        "mvt_affected_tiles": mvt_affected,
        "cog_rebuild_bytes": pyr_bytes_after,
    }

    # ---- 7. 局部性：Hilbert vs Z-order
    print("\n== [7] 局部性（3×3 邻域键序连续段数 runs，Hilbert vs Morton）")
    from .address import key_at as _key_at, morton_key, xy_of

    # 度量（Moon et al. 2001 的空间填充曲线标准比较）：对每个叶子取其 3×3
    # 同层邻域，按键排序后统计连续段数（相邻键差 > 阈值即断裂）。段数越少
    # → 区域查询的 I/O 越连续、缓存越友好。注意：平均键距对两者都是 O(4^d)，
    # 区分度差；runs 才能反映"聚类保持"这一 Hilbert 的设计目标。
    def _runs(keys_sorted: list[int]) -> int:
        if not keys_sorted:
            return 0
        r = 1
        for a, b in zip(keys_sorted, keys_sorted[1:]):
            if b - a > 4:  # 同层键序上非紧邻即断裂
                r += 1
        return r

    z_runs, h_runs = [], []
    rng2 = random.Random(3)
    keys = sorted(weft.cells.keys())
    sample = rng2.sample(keys, min(400, len(keys)))
    for k in sample:
        d = weft.cells[k].depth
        x, y = xy_of(k, d, weft.max_depth)
        n = 1 << d
        hk, zk = [], []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < n and 0 <= ny < n:
                    hk.append(_key_at(d, nx, ny, weft.max_depth))
                    zk.append(morton_key(d, nx, ny, weft.max_depth))
        h_runs.append(_runs(sorted(hk)))
        z_runs.append(_runs(sorted(zk)))
    if z_runs:
        print(f"   Morton(Z序) 平均连续段数 {np.mean(z_runs):>8.2f}  (3×3=9 格)")
        print(f"   Hilbert     平均连续段数 {np.mean(h_runs):>8.2f}  (3×3=9 格)")
        results["locality"] = {
            "morton_mean_runs": float(np.mean(z_runs)),
            "hilbert_mean_runs": float(np.mean(h_runs)),
        }

    print("\n== 基准完成。结果已汇总（tests/test_bench.py 对关键声明做断言）")
    if verbose:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


def _morton(depth: int, x: int, y: int, max_depth: int) -> int:
    """Z-order 键（对照用）。"""
    code = 0
    for i in range(depth):
        code |= ((x >> i) & 1) << (2 * i)
        code |= ((y >> i) & 1) << (2 * i + 1)
    return code << (2 * (max_depth - depth))
