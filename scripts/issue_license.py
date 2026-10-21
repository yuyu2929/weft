#!/usr/bin/env python3.11
"""签发 Weft 企业许可证（内部工具，需要 keys/ed25519_private.pem）。

用法：
  python3.11 scripts/issue_license.py --org "示例工程公司" --seats 50 --days 365 \
      --features shard,encrypt,merge --out license.key
"""

from __future__ import annotations

import argparse
import os

from weft.license import issue

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PRIV_PATH = os.path.join(ROOT, "keys", "ed25519_private.pem")

ALL_FEATURES = ["shard", "encrypt", "merge", "sla"]


def load_private() -> bytes:
    if not os.path.exists(PRIV_PATH):
        raise SystemExit(f"缺少私钥 {PRIV_PATH}，先运行 scripts/gen_keys.py")
    hex_lines = [
        ln.strip()
        for ln in open(PRIV_PATH)
        if ln.strip() and not ln.startswith("-----")
    ]
    return bytes.fromhex("".join(hex_lines))


def main() -> None:
    ap = argparse.ArgumentParser(description="签发 Weft 企业许可证")
    ap.add_argument("--org", required=True)
    ap.add_argument("--seats", type=int, default=10)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--features", default="shard,encrypt,merge,sla")
    ap.add_argument("--out", default="weft-enterprise.key")
    args = ap.parse_args()
    feats = [f.strip() for f in args.features.split(",") if f.strip()]
    unknown = [f for f in feats if f not in ALL_FEATURES]
    if unknown:
        raise SystemExit(f"未知功能 {unknown}，可用: {ALL_FEATURES}")
    sk = load_private()
    token = issue(sk, args.org, args.seats, args.days, feats)
    with open(args.out, "w") as f:
        f.write(token + "\n")
    print(f"已签发 → {args.out}")
    print(f"组织={args.org} 席位={args.seats} 有效期={args.days}天 功能={feats}")


if __name__ == "__main__":
    main()
