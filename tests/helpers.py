"""测试公共 helper：合成数据与层定义。"""

import random

import numpy as np

from weft.feature import KIND_POINT, Layer


def layers():
    return [
        Layer(name="parcels", kind="polygon", columns=("owner",)),
        Layer(name="roads", kind="line", columns=("name",)),
        Layer(name="sensors", kind=KIND_POINT, columns=("kind",)),
    ]


def district(seed: int = 7):
    """小规模合成工程区（与 bench.make_district 同构，规模压小保证测试秒级）。"""
    from weft.grid import Weft

    rng = random.Random(seed)
    weft = Weft(1000.0, 1000.0, layers())
    gx = gy = 12
    cw, ch = 1000.0 / gx, 1000.0 / gy
    for i in range(gx):
        for j in range(gy):
            if rng.random() < 0.1:
                continue
            x0, y0 = i * cw + 4, j * ch + 4
            x1, y1 = (i + 1) * cw - 4, (j + 1) * ch - 4
            ring = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])
            weft.add_polygon("parcels", [ring], {"owner": rng.choice("ABCD")})
    for _ in range(40):
        x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
        weft.add_point("sensors", x, y, {"kind": "temp"})
    for i in range(gx + 1):
        x = i * cw
        weft.add_line("roads", [[x, 0], [x, 500], [x + 12, 500], [x + 12, 1000]], {"name": f"V{i}"})
    return weft


def seeded_pts(n, seed=11):
    rng = random.Random(seed)
    return [(rng.uniform(0, 1000), rng.uniform(0, 1000)) for _ in range(n)]
