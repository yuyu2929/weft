"""许可证测试：Ed25519（RFC 8032 向量 + cryptography 互操作）、签发/解析/门控。"""

import base64
import json
import time

import pytest

from weft.license import License, LicenseError, issue, parse, public_key, sign, verify

# RFC 8032 Section 7.1 Test 2 向量
SK = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
PK_HEX = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
SIG_HEX = (
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)


def test_public_key_matches_rfc8032():
    assert public_key(SK).hex() == PK_HEX


def test_sign_matches_rfc8032():
    assert sign(SK, b"\x72").hex() == SIG_HEX


def test_verify_rfc8032_vector():
    assert verify(bytes.fromhex(PK_HEX), b"\x72", bytes.fromhex(SIG_HEX))
    assert not verify(bytes.fromhex(PK_HEX), b"\x73", bytes.fromhex(SIG_HEX))  # 改消息必失败
    assert not verify(bytes.fromhex(PK_HEX), b"\x72", bytes(64))  # 空签名失败


def test_cryptography_interop():
    """与 cryptography 库双向互操作（工程兼容性）。"""
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

    pk_bytes = public_key(SK)
    msg = b"weft interop check"
    sig = sign(SK, msg)
    Ed25519PublicKey.from_public_bytes(pk_bytes).verify(sig, msg)  # 库验我们的签

    # 我们的验签验库签的
    lib_sk = Ed25519PrivateKey.generate()
    lib_sig = lib_sk.sign(msg)
    assert verify(lib_sk.public_key().public_bytes_raw(), msg, lib_sig)


def test_issue_and_parse():
    token = issue(SK, "ACME", 25, 365, ["shard", "encrypt"])
    assert token.startswith("WEFT1.")
    parsed = parse(token)
    payload = parsed["payload"]
    assert payload["org"] == "ACME"
    assert payload["seats"] == 25
    assert set(payload["features"]) == {"shard", "encrypt"}
    assert payload["exp"] > time.time()


def test_license_object_features():
    token = issue(SK, "ACME", 25, 365, ["shard"])
    lic = License(token, PK_HEX)
    assert lic.has_feature("shard")
    assert not lic.has_feature("encrypt")
    assert lic.days_left() > 360


def test_license_rejects_tampered_token():
    token = issue(SK, "ACME", 25, 365, ["shard"])
    magic, payload, sig = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(payload + "=="))
    raw[0] ^= 0x01  # 改 payload 一字节
    bad_payload = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    with pytest.raises(LicenseError):
        License(f"{magic}.{bad_payload}.{sig}", PK_HEX)


def test_license_rejects_wrong_key():
    """用另一把私钥签的 token 不被接受（防伪造）。"""
    other_sk = bytes.fromhex("1111111111111111111111111111111111111111111111111111111111111111")
    token = issue(other_sk, "EVIL", 1, 1, ["shard"])
    with pytest.raises(LicenseError):
        License(token, PK_HEX)


def test_none_license_is_community():
    """社区版：无许可证时企业功能关闭但不崩溃。"""
    lic = License(None, None)
    assert not lic.has_feature("shard")
    assert lic.days_left() == 0
