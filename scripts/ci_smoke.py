"""跨平台 CLI 冒烟：build → info → verify → query，以及许可证门控。

为什么要有这个文件：GitHub Actions 的 Windows runner 用 PowerShell，
`python - <<'PY'` 这种 heredoc 语法会直接语法错误；把冒烟逻辑写进仓库，
两边跑同一条命令，才是真的"两平台一致"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 脚本自身也要能 import weft（CI 里虽然 pip install -e . 过，但本地直接跑脚本时未必）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-m", "weft"] + args, cwd=cwd, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败: weft {' '.join(args)}")
    return r


def main() -> int:
    gj = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "p1"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}},
            {"type": "Feature", "properties": {"name": "s1"},
             "geometry": {"type": "Point", "coordinates": [50, 50]}},
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.geojson")
        out = os.path.join(tmp, "out.weft")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump(gj, fh, ensure_ascii=False)

        run(["build", src, "-o", out], tmp)
        run(["info", out], tmp)
        vr = run(["verify", out], tmp)
        report = json.loads(vr.stdout)
        assert report["ok"] is True, report
        assert report["roundtrip_identical"] is True
        assert len(report["content_hash"]) == 64

        qr = run(["query", out, "--layer", "polygons", "--x", "50", "--y", "50"], tmp)
        q = json.loads(qr.stdout)
        assert q["exact_hits"], q

        print("CLI 冒烟通过:", json.dumps(report["stats"], ensure_ascii=False))

    # 门控：无证不可用企业功能
    from weft import Layer, Weft
    from weft.enterprise import plan_shards
    from weft.license import LicenseError

    w = Weft(10, 10, [Layer(name="p", kind="point")])
    w.add_point("p", 1, 1, {})
    w.commit()
    try:
        plan_shards(w, 2, lic=None)
    except LicenseError:
        print("门控正常：无证不可用企业功能")
    else:
        raise SystemExit("门控失效：无证竟然能分片")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
