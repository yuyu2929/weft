"""Weft 序列化：确定性字节流 + 内容哈希。

确定性三原则（docs/THEORY.md §2.5）：
1. 一切字典序显式排序（格按键、要素按 fid、账本按格对、日志按写入序）；
2. 浮点用 struct '<d' 原样落盘（IEEE 754 双精度，跨平台逐位一致）；
3. 无墙钟时间戳——逻辑版本号即时钟。

格式（全部小端）：
    MAGIC "WEFT1"
    u8 max_depth, u8 field_res, u8 sub
    f64 width, f64 height
    u64 logical_version, u64 next_fid
    u32 n_layers;  每层: str name, u8 kind, u32 n_cols, str[] cols
    u32 n_cells;   每格(按键升序): u64 key, u64 version, u32 n_fids,
                    fid 组内 delta-varint, u32 field_len, zlib(field bytes)
    u32 n_features; 每要素(按 fid 升序): u64 fid, u8 layer_idx, u8 kind,
                    u32 n_props, props(str key, u8 type, value),
                    geom: u32 n_rings, 每环 u32 n + f64 坐标(zlib)
    u32 n_ledger;  每对(按 (a,b) 升序): u64 a, u64 b, u32 n, fid delta-varint
    u32 n_journal; 每条: u64 logical_version, u8 op, u64 fid, u32 n_keys, keys delta-varint
    footer: b"HASH" + blake2b-256(此前全部字节)
"""

from __future__ import annotations

import struct
import zlib

from .feature import KIND_LINE, KIND_POINT, KIND_POLYGON, Feature, Layer
from .grid import Cell, Weft

MAGIC = b"WEFT1"
HASH_TAG = b"HASH"

_T_INT = 0
_T_FLOAT = 1
_T_STR = 2
_T_BOOL = 3


# ------------------------------------------------------------------ varint
def _uv(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63) if n < 0 else (n << 1)


def _unzigzag(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def take(self, n: int) -> bytes:
        b = self.d[self.p : self.p + n]
        if len(b) != n:
            raise ValueError("序列化数据意外结束")
        self.p += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def uv(self) -> int:
        shift = 0
        result = 0
        while True:
            b = self.take(1)[0]
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7

    def svar(self) -> int:
        return _unzigzag(self.uv())

    def string(self) -> str:
        n = self.u32()
        return self.take(n).decode("utf-8")

    def blob(self) -> bytes:
        n = self.u32()
        return self.take(n)


class _Writer:
    def __init__(self) -> None:
        self.buf = bytearray()

    def raw(self, b: bytes) -> None:
        self.buf += b

    def u8(self, v: int) -> None:
        self.buf += struct.pack("<B", v)

    def u32(self, v: int) -> None:
        self.buf += struct.pack("<I", v)

    def u64(self, v: int) -> None:
        self.buf += struct.pack("<Q", v)

    def f64(self, v: float) -> None:
        self.buf += struct.pack("<d", v)

    def uv(self, v: int) -> None:
        self.buf += _uv(v)

    def svar(self, v: int) -> None:
        self.buf += _uv(_zigzag(v))

    def string(self, s: str) -> None:
        b = s.encode("utf-8")
        self.u32(len(b))
        self.buf += b

    def blob(self, b: bytes) -> None:
        self.u32(len(b))
        self.buf += b


def _encode_props(w: _Writer, props: dict) -> None:
    w.u32(len(props))
    for k in sorted(props):
        v = props[k]
        w.string(k)
        if isinstance(v, bool):
            w.u8(_T_BOOL)
            w.u8(1 if v else 0)
        elif isinstance(v, int):
            w.u8(_T_INT)
            w.svar(v)
        elif isinstance(v, float):
            w.u8(_T_FLOAT)
            w.f64(v)
        elif isinstance(v, str):
            w.u8(_T_STR)
            w.string(v)
        else:
            raise ValueError(f"不支持的属性类型: {type(v)}")


def _decode_props(r: _Reader) -> dict:
    n = r.u32()
    out = {}
    for _ in range(n):
        k = r.string()
        t = r.u8()
        if t == _T_BOOL:
            out[k] = bool(r.u8())
        elif t == _T_INT:
            out[k] = r.svar()
        elif t == _T_FLOAT:
            out[k] = r.f64()
        elif t == _T_STR:
            out[k] = r.string()
        else:
            raise ValueError(f"未知属性类型 tag={t}")
    return out


def _encode_coords(w: _Writer, arr) -> None:
    import numpy as np

    a = np.ascontiguousarray(arr, dtype="<f8")
    w.blob(zlib.compress(a.tobytes(), 6))


def _decode_coords(r: _Reader):
    import numpy as np

    raw = zlib.decompress(r.blob())
    return np.frombuffer(raw, dtype="<f8").reshape(-1, 2).copy()


# ------------------------------------------------------------------ 主入口
def serialize(weft: Weft) -> bytes:
    w = _Writer()
    w.raw(MAGIC)
    w.u8(weft.max_depth)
    w.u8(weft.field_res)
    w.u8(weft.sub)
    w.f64(weft.width)
    w.f64(weft.height)
    w.u64(weft.logical_version)
    w.u64(weft._next_fid)
    w.u32(len(weft.layer_order))
    for name in weft.layer_order:
        lay = weft.layers[name]
        w.string(lay.name)
        w.u8({KIND_POINT: 0, KIND_LINE: 1, KIND_POLYGON: 2}[lay.kind])
        w.u32(len(lay.columns))
        for c in lay.columns:
            w.string(c)
    # 格（按键升序；键 delta-varint）
    keys = sorted(weft.cells.keys())
    w.u32(len(keys))
    prev = 0
    for k in keys:
        cell = weft.cells[k]
        w.uv(k - prev)
        prev = k
        w.u8(cell.depth)
        w.u64(cell.version)
        fids = sorted(cell.feature_ids)
        w.u32(len(fids))
        prevf = 0
        for fid in fids:
            w.uv(fid - prevf)
            prevf = fid
        field_bytes = b""
        if cell.field is not None:
            import numpy as np

            field_bytes = zlib.compress(np.ascontiguousarray(cell.field, dtype="<f4").tobytes(), 6)
        w.blob(field_bytes)
    # 要素（按 fid 升序）
    fids = sorted(weft.features.keys())
    w.u32(len(fids))
    for fid in fids:
        f = weft.features[fid]
        w.u64(fid)
        w.u8(weft.layer_order.index(f.layer))
        w.u8({KIND_POINT: 0, KIND_LINE: 1, KIND_POLYGON: 2}[f.kind])
        _encode_props(w, f.props)
        if f.kind == KIND_POLYGON:
            w.u32(len(f.geom))
            for ring in f.geom:
                _encode_coords(w, ring)
        else:
            w.u32(1)
            _encode_coords(w, f.geom)
    # 账本
    pairs = sorted(weft.ledger.keys())
    w.u32(len(pairs))
    for a, b in pairs:
        w.u64(a)
        w.u64(b)
        fids = sorted(weft.ledger[(a, b)])
        w.u32(len(fids))
        prevf = 0
        for fid in fids:
            w.uv(fid - prevf)
            prevf = fid
    # 日志
    w.u32(len(weft.journal))
    for entry in weft.journal:
        w.u64(entry["version"])
        w.u8({"insert": 0, "delete": 1, "update": 2}[entry["op"]])
        w.u64(entry["fid"])
        ks = sorted(entry["keys"])
        w.u32(len(ks))
        prev = 0
        for k in ks:
            w.uv(k - prev)
            prev = k
    body = bytes(w.buf)
    import hashlib

    digest = hashlib.blake2b(body, digest_size=32).digest()
    return body + HASH_TAG + digest


def deserialize(data: bytes) -> Weft:
    if data[:5] != MAGIC:
        raise ValueError("不是 Weft 文件（magic 不匹配）")
    r = _Reader(data)
    r.take(5)
    max_depth = r.u8()
    field_res = r.u8()
    sub = r.u8()
    width = r.f64()
    height = r.f64()
    logical_version = r.u64()
    next_fid = r.u64()
    n_layers = r.u32()
    layers = []
    for _ in range(n_layers):
        name = r.string()
        kind = {0: KIND_POINT, 1: KIND_LINE, 2: KIND_POLYGON}[r.u8()]
        n_cols = r.u32()
        cols = tuple(r.string() for _ in range(n_cols))
        layers.append(Layer(name=name, kind=kind, columns=cols))
    weft = Weft(width, height, layers, max_depth=max_depth, field_res=field_res, sub=sub)
    weft.logical_version = logical_version
    weft._next_fid = next_fid
    # 格
    n_cells = r.u32()
    prev = 0
    for _ in range(n_cells):
        k = prev + r.uv()
        prev = k
        depth = r.u8()
        version = r.u64()
        n_fids = r.u32()
        fids = []
        prevf = 0
        for _ in range(n_fids):
            prevf += r.uv()
            fids.append(prevf)
        fb = r.blob()
        cell = Cell(key=k, depth=depth, version=version, feature_ids=fids)
        if fb:
            import numpy as np

            cell.field = (
                np.frombuffer(zlib.decompress(fb), dtype="<f4")
                .reshape(field_res, field_res, len(weft.layer_order))
                .copy()
            )
        weft.cells[k] = cell
    # 要素
    n_features = r.u32()
    for _ in range(n_features):
        fid = r.u64()
        li = r.u8()
        kind = {0: KIND_POINT, 1: KIND_LINE, 2: KIND_POLYGON}[r.u8()]
        props = _decode_props(r)
        n_rings = r.u32()
        rings = [_decode_coords(r) for _ in range(n_rings)]
        layer_name = weft.layer_order[li]
        geom = rings if kind == KIND_POLYGON else rings[0]
        weft.features[fid] = Feature(fid=fid, layer=layer_name, kind=kind, geom=geom, props=props)
    # cell_keys 从序列化的格-要素列表反推（权威来源，不必重算覆盖规则）
    for k, cell in weft.cells.items():
        for fid in cell.feature_ids:
            f = weft.features.get(fid)
            if f is not None:
                f.cell_keys = tuple(sorted(set(f.cell_keys) | {k}))
    # _internal 从叶子沿 parent 链反推（分区纪律：内部节点 = 叶子的真祖先）
    from .address import parent as _parent

    for k, cell in weft.cells.items():
        key, depth = k, cell.depth
        while depth > 0:
            key, depth = _parent(key, depth, max_depth), depth - 1
            if key in weft.cells:  # 不应发生（分区纪律），防御性中断
                break
            weft._internal.add(key)
    # 账本
    n_ledger = r.u32()
    for _ in range(n_ledger):
        a = r.u64()
        b = r.u64()
        n = r.u32()
        fids = set()
        prevf = 0
        for _ in range(n):
            prevf += r.uv()
            fids.add(prevf)
        weft.ledger[(a, b)] = fids
    # 日志
    n_journal = r.u32()
    for _ in range(n_journal):
        version = r.u64()
        op = {0: "insert", 1: "delete", 2: "update"}[r.u8()]
        fid = r.u64()
        n_keys = r.u32()
        ks = []
        prev = 0
        for _ in range(n_keys):
            prev += r.uv()
            ks.append(prev)
        weft.journal.append({"version": version, "op": op, "fid": fid, "keys": ks})
    # 校验内容哈希
    body = data[: len(data) - len(HASH_TAG) - 32]
    if data[len(data) - len(HASH_TAG) - 32 : len(data) - 32] != HASH_TAG:
        raise ValueError("Weft 文件缺少 HASH footer")
    import hashlib

    if hashlib.blake2b(body, digest_size=32).digest() != data[-32:]:
        raise ValueError("内容哈希校验失败：文件被篡改或损坏")
    return weft


def content_hash(data: bytes) -> bytes:
    return data[-32:]
