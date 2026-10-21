"""Weft 许可证：Ed25519 签名验证（纯 Python，零依赖，RFC 8032）。

令牌格式：WEFT1.<payload_b64url>.<sig_b64url>
payload = {"org": str, "seats": int, "exp": int(unix 天), "features": [str]}

运行时只做验证（内嵌公钥）；签发在 scripts/issue_license.py（私钥不出现在
仓库与发布包中）。纯 Python 实现的签名由 tests 用 cryptography 库交叉验证
（若环境可用），确保与标准实现互操作。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

# ---------------------------------------------------------------- Ed25519 (RFC 8032)
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * pow(5, _Q - 2, _Q) % _Q
_BX = _xrecover(_BY)
_B = [_BX % _Q, _BY % _Q]


def _edwards_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    denom = pow(_D * x1 * x2 * y1 * y2 + 1, _Q - 2, _Q)
    x3 = (x1 * y2 + x2 * y1) * denom % _Q
    denom = pow(1 - _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    y3 = (y1 * y2 + x1 * x2) * denom % _Q
    return [x3, y3]


def _scalarmult(P, e: int):
    Q = [0, 1]
    while e > 0:
        if e & 1:
            Q = _edwards_add(Q, P)
        P = _edwards_add(P, P)
        e >>= 1
    return Q


def _encode_point(P) -> bytes:
    x, y = P
    bits = y & ((1 << 255) - 1)
    bits |= (x & 1) << 255
    return bits.to_bytes(32, "little")


def _decode_point(data: bytes):
    if len(data) != 32:
        raise ValueError("点编码必须 32 字节")
    bits = int.from_bytes(data, "little")
    y = bits & ((1 << 255) - 1)
    sign = (bits >> 255) & 1
    x = _xrecover(y)
    if x & 1 != sign:
        x = _Q - x
    return [x, y]


def _secret_scalar(sk: bytes) -> tuple[int, bytes]:
    h = _sha512(sk)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(sk: bytes) -> bytes:
    a, _ = _secret_scalar(sk)
    return _encode_point(_scalarmult(_B, a))


def sign(sk: bytes, message: bytes) -> bytes:
    a, prefix = _secret_scalar(sk)
    A = _encode_point(_scalarmult(_B, a))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    R = _encode_point(_scalarmult(_B, r))
    h = int.from_bytes(_sha512(R + A + message), "little") % _L
    s = (r + h * a) % _L
    return R + s.to_bytes(32, "little")


def verify(pk: bytes, message: bytes, sig: bytes) -> bool:
    try:
        if len(sig) != 64 or len(pk) != 32:
            return False
        R = _decode_point(sig[:32])
        A = _decode_point(pk)
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(_sha512(sig[:32] + pk + message), "little") % _L
        # 校验 sB == R + hA
        left = _scalarmult(_B, s)
        right = _edwards_add(R, _scalarmult(A, h))
        return left == right
    except Exception:
        return False


# ---------------------------------------------------------------- 令牌
def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue(sk: bytes, org: str, seats: int, days: int, features: list[str]) -> str:
    exp = int(time.time()) + days * 86400
    payload = {"org": org, "seats": int(seats), "exp": exp, "features": sorted(features)}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = sign(sk, body)
    return f"WEFT1.{_b64url_encode(body)}.{_b64url_encode(sig)}"


def parse(token: str) -> dict:
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != "WEFT1":
        raise ValueError("许可证格式错误（期待 WEFT1.<payload>.<sig>）")
    body = _b64url_decode(parts[1])
    sig = _b64url_decode(parts[2])
    return {"payload": json.loads(body), "body": body, "sig": sig}


class LicenseError(Exception):
    pass


class License:
    """企业许可证校验器（运行时只验证）。"""

    def __init__(self, token: str | None, public_pem_hex: str | None) -> None:
        self.valid = False
        self.info: dict = {}
        if not token:
            return
        try:
            parsed = parse(token)
        except Exception as e:
            raise LicenseError(f"许可证不可解析: {e}") from e
        if not public_pem_hex:
            raise LicenseError("缺少内嵌公钥")
        pk = bytes.fromhex(public_pem_hex.strip())
        if not verify(pk, parsed["body"], parsed["sig"]):
            raise LicenseError("许可证签名无效")
        payload = parsed["payload"]
        if int(payload.get("exp", 0)) < int(time.time()):
            raise LicenseError("许可证已过期")
        self.valid = True
        self.info = payload

    def days_left(self) -> int:
        if not self.valid:
            return 0
        return max(0, (int(self.info["exp"]) - int(time.time())) // 86400)

    def has_feature(self, name: str) -> bool:
        return self.valid and name in self.info.get("features", [])
