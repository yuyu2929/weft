"""Weft：统一的矢量-栅格数据结构（原型）。

公开 API：
    from weft import Weft, Layer, serialize, deserialize
"""

from .address import DEFAULT_MAX_DEPTH
from .codec import content_hash, deserialize, serialize
from .feature import KIND_LINE, KIND_POINT, KIND_POLYGON, Feature, Layer
from .grid import MAX_FEATS_PER_CELL, Cell, Weft, WeftError
from .license import License, LicenseError

__version__ = "0.1.0"

__all__ = [
    "Weft",
    "Layer",
    "Feature",
    "Cell",
    "WeftError",
    "serialize",
    "deserialize",
    "content_hash",
    "License",
    "LicenseError",
    "KIND_POINT",
    "KIND_LINE",
    "KIND_POLYGON",
    "DEFAULT_MAX_DEPTH",
    "MAX_FEATS_PER_CELL",
    "__version__",
]
