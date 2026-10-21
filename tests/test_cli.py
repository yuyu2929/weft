"""CLI 冒烟测试：build/info/query/license 子命令端到端。"""

import json
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(args, cwd=ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    # 显式 UTF-8：Windows 默认的 cp936 会把子进程的 UTF-8 输出解成乱码，
    # 断言中文就会莫名失败（这是测试环境的坑，不是 CLI 的 bug）。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "weft"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        timeout=120,
    )


def _make_geojson(tmp_path):
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "p1"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]},
            },
            {
                "type": "Feature",
                "properties": {"name": "s1"},
                "geometry": {"type": "Point", "coordinates": [50, 50]},
            },
            {
                "type": "Feature",
                "properties": {"name": "r1"},
                "geometry": {"type": "LineString", "coordinates": [[0, 50], [100, 50]]},
            },
        ],
    }
    p = tmp_path / "in.geojson"
    p.write_text(json.dumps(gj))
    return p


def test_cli_build_info_query(tmp_path):
    gj = _make_geojson(tmp_path)
    out = tmp_path / "out.weft"

    r = _run(["build", str(gj), "-o", str(out), "--width", "100", "--height", "100"])
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert "构建完成" in r.stdout

    r = _run(["info", str(out)])
    assert r.returncode == 0
    st = json.loads(r.stdout)
    assert st["n_features"] == 3
    assert st["n_cells"] >= 1

    r = _run(["query", str(out), "--layer", "polygons", "--x", "50", "--y", "50"])
    assert r.returncode == 0
    res = json.loads(r.stdout)
    assert res["exact_hits"] == [1]
    assert res["props"]["name"] == "p1"


def test_cli_license_community():
    r = _run(["license"])
    assert r.returncode == 0
    st = json.loads(r.stdout)
    assert st["tier"] == "community"


def test_cli_version():
    r = _run(["--version"])
    assert r.returncode == 0
    assert r.stdout.startswith("weft ")
