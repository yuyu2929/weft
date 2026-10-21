#!/usr/bin/env python3.11
"""生成 Weft 许可证密钥对（开发/内部使用）。

产出：
  keys/ed25519_private.pem   私钥（hex，绝不进仓库/发布包）
  weft/license_public.pem    公钥（hex，运行时验证用，随包发布）

用法： python3.11 scripts/gen_keys.py
"""

from __future__ import annotations

import os
import secrets

from weft.license import public_key

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PRIV_PATH = os.path.join(ROOT, "keys", "ed25519_private.pem")
PUB_PATH = os.path.join(ROOT, "weft", "license_public.pem")


def main() -> None:
    os.makedirs(os.path.join(ROOT, "keys"), exist_ok=True)
    if os.path.exists(PRIV_PATH):
        print(f"私钥已存在，不覆盖: {PRIV_PATH}")
        print("如需重新签发，先备份并删除该文件。")
        return
    sk = secrets.token_bytes(32)
    pk = public_key(sk)
    with open(PRIV_PATH, "w") as f:
        f.write("-----BEGIN WEFT LICENSE PRIVATE KEY-----\n")
        f.write(sk.hex() + "\n")
        f.write("-----END WEFT LICENSE PRIVATE KEY-----\n")
    os.chmod(PRIV_PATH, 0o600)
    with open(PUB_PATH, "w") as f:
        f.write("-----BEGIN WEFT LICENSE PUBLIC KEY-----\n")
        f.write(pk.hex() + "\n")
        f.write("-----END WEFT LICENSE PUBLIC KEY-----\n")
    print(f"私钥: {PRIV_PATH} (权限 600，禁止提交)")
    print(f"公钥: {PUB_PATH} (随包发布)")


if __name__ == "__main__":
    main()
