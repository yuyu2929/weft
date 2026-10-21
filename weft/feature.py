"""Weft 要素与层模型。

要素一次存储、绝不按格裁剪（docs/THEORY.md §2.3）。层带类型与可选列模式，
属性以定长标量存储（原型：int/float/str/bool）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geom import Polygon, bbox_of

KIND_POINT = "point"
KIND_LINE = "line"
KIND_POLYGON = "polygon"

_COLUMN_TYPES = (int, float, str, bool)


@dataclass(frozen=True)
class Layer:
    name: str
    kind: str
    columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in (KIND_POINT, KIND_LINE, KIND_POLYGON):
            raise ValueError(f"未知层类型 {self.kind!r}")


@dataclass
class Feature:
    fid: int
    layer: str
    kind: str
    geom: np.ndarray | Polygon  # point:(1,2) line:(N,2) polygon:[rings]
    props: dict = field(default_factory=dict)
    # 由 grid 维护：该要素覆盖的叶子键（Hilbert 序）
    cell_keys: tuple[int, ...] = ()

    def bbox(self) -> tuple[float, float, float, float]:
        if self.kind == KIND_POLYGON:
            x0, y0, x1, y1 = bbox_of(self.geom[0])
            for ring in self.geom[1:]:
                b = bbox_of(ring)
                x0, y0 = min(x0, b[0]), min(y0, b[1])
                x1, y1 = max(x1, b[2]), max(y1, b[3])
            return x0, y0, x1, y1
        return bbox_of(self.geom)

    def validate_props(self, layer: Layer) -> None:
        for k, v in self.props.items():
            if k not in layer.columns:
                raise ValueError(f"层 {layer.name} 无属性列 {k!r}")
            if not isinstance(v, _COLUMN_TYPES):
                raise ValueError(f"属性 {k!r} 类型不支持: {type(v)}")
