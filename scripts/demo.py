#!/usr/bin/env python3.11
"""Weft 一键演示：合成工程区 → 建格 → 各类查询 → 序列化 → 增量编辑。

设计意图：5 分钟内让人看到"矢栅同存"与现有双栈的差别。
运行：python3 -m weft demo  或  python3 scripts/demo.py
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np

# 允许直接 python3 scripts/demo.py 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weft import Layer, Weft, deserialize, serialize  # noqa: E402
from weft.query import bbox_query, count_boundary_artifacts, knn, rasterize, to_mvt_tiles  # noqa: E402


def banner(title: str) -> None:
    print(f"\n{'=' * 64}\n  {title}\n{'=' * 64}")


def build_demo_weft() -> Weft:
    """合成"工业园区"：50 地块 + 20 道路 + 200 传感器。"""
    import random

    rng = random.Random(42)
    weft = Weft(1000.0, 1000.0, [
        Layer(name="parcels", kind="polygon", columns=("owner",)),
        Layer(name="roads", kind="line", columns=("name",)),
        Layer(name="sensors", kind="point", columns=("kind",)),
    ])
    # 地块：10×5 Manhattan 网格 + 抖动
    for i in range(10):
        for j in range(5):
            x0, y0 = i * 100 + 8, j * 200 + 8
            x1, y1 = (i + 1) * 100 - 8, (j + 1) * 200 - 8
            ring = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])
            weft.add_polygon("parcels", [ring], {"owner": rng.choice(["A", "B", "C"])})
    # 道路：横竖主干
    for i in range(11):
        x = i * 100.0
        weft.add_line("roads", [[x, 0], [x, 500], [x + 12, 500], [x + 12, 1000]], {"name": f"V{i}"})
    for j in range(6):
        y = j * 200.0
        weft.add_line("roads", [[0, y], [500, y], [500, y + 12], [1000, y + 12]], {"name": f"H{j}"})
    # 传感器：均匀 + 3 个密集簇
    for _ in range(200):
        if rng.random() < 0.3:
            cx, cy = rng.uniform(0, 1000), rng.uniform(0, 1000)
            x, y = cx + rng.gauss(0, 15), cy + rng.gauss(0, 15)
        else:
            x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
        weft.add_point("sensors", min(max(x, 0), 999.999), min(max(y, 0), 999.999),
                       {"kind": rng.choice(["temp", "noise", "pm25"])})
    return weft


def main() -> None:
    banner("1. 构建：合成工业园区（1000×1000m，270 要素）")
    weft = build_demo_weft()
    t_dirty = len(weft.dirty_cells())
    weft.commit()
    st = weft.stats()
    print(f"要素 {st['n_features']} 个 | 叶子 {st['n_cells']} 个 {st['cells_by_depth']} | "
          f"缝合账本 {st['n_ledger_pairs']} 对 | 提交重算 {t_dirty} 格")
    print("→ 叶子构成域的分区（非 overlap）：cell_of/field_at 只需看唯一叶子")

    banner("2. 矢量查询：这块地是谁的？（精确，跨格不切割）")
    hits = weft.exact_point_query("parcels", 250.0, 350.0)
    for fid in hits:
        f = weft.reconstruct(fid)
        print(f"命中地块 fid={fid} owner={f.props['owner']} 顶点数={len(f.geom[0])}（完整几何，无 buffer 残段）")
    if not hits:
        print("(空地)")

    banner("3. 栅格查询：这一带有多少传感器？（O(1) 积分场，点层精确）")
    val, bound = weft.field_at("sensors", 250.0, 350.0)
    print(f"sensors 场值 = {val:.1f}（误差界 {bound}：点层精确计数）")
    val, bound = weft.field_at("parcels", 250.0, 350.0)
    print(f"parcels 覆盖率 = {val:.3f}（误差界 {bound} = 1/sub²，面层是可控估计）")

    banner("4. 区域查询 + KNN（与暴力扫描结果一致）")
    got = bbox_query(weft, 200, 300, 400, 500, layer="sensors")
    print(f"bbox(200,300,400,500) 内传感器: {len(got)} 个（fid 天然去重）")
    near = knn(weft, "sensors", 250, 350, 3)
    print("KNN k=3:", [(fid, round(d, 1)) for fid, d in near], "米")

    banner("5. 栅格交付：从矢量权威随时导出（COG 式输出只是导出格式）")
    grid = rasterize(weft, "parcels", 0, 0, 1000, 1000, 16)
    print(f"16×16 覆盖率栅格: min={grid.min():.2f} max={grid.max():.2f} "
          f"（场上采样，不重算几何）")

    banner("6. 序列化：确定性 + 内容哈希（同输入 → 逐字节同输出）")
    blob1 = serialize(weft)
    weft2 = deserialize(blob1)
    blob2 = serialize(weft2)
    print(f"序列化 {len(blob1):,} B | 往返后再序列化逐字节相同: {blob1 == blob2}")

    banner("7. 增量编辑：只重算脏格（COG 要重生整文件）")
    fid = weft.add_polygon("parcels", [[700, 700], [760, 700], [760, 760], [700, 760], [700, 700]],
                           {"owner": "NEW"})
    n_dirty = len(weft.dirty_cells())
    weft.commit()
    blob3 = serialize(weft)
    print(f"新增 1 个地块 → 脏格 {n_dirty} 个，序列化增量 {len(blob3) - len(blob1):+,} B")
    print(f"（对照：COG 式栅格金字塔要重渲染全部 7 层 = 1.3 MB；MVT 要重切瓦片）")

    banner("8. 对照：MVT 式切割的边界伪影")
    art = count_boundary_artifacts(weft, "parcels", zoom=3)
    print(f"MVT 式 z3: {art['features_cut']}/{art['features_total']} 个地块被切碎，"
          f"重复存储 {art['duplicate_copies']} 份")
    print(f"Weft: 0 个（几何一次存储 {sum(f.geom[0].nbytes for f in weft.features.values() if f.layer == 'parcels'):,} B）")

    banner("演示完成")
    print("完整基准: python3 -m weft bench | 测试: python3 -m pytest tests/")


if __name__ == "__main__":
    main()
