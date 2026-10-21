"""校验打出来的 wheel：公钥、CLI 入口、必要模块都在包里。"""

from __future__ import annotations

import glob
import sys
import zipfile


def main() -> int:
    wheels = glob.glob("dist/*.whl")
    if not wheels:
        print("dist/ 下没有 wheel", file=sys.stderr)
        return 1
    whl = wheels[0]
    names = zipfile.ZipFile(whl).namelist()
    required = [
        "weft/license_public.pem",
        "weft/enterprise.py",
        "weft/audit.py",
        "weft/cli.py",
    ]
    missing = [r for r in required if not any(n.endswith(r) for n in names)]
    if missing:
        print(f"wheel 缺少: {missing}", file=sys.stderr)
        return 1
    print(f"wheel ok: {whl} ({len(names)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
