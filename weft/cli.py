"""Weft 命令行。

子命令：
  build   从 GeoJSON 构建 .weft 文件
  info    查看 .weft 文件统计
  query   点查询（精确 + 积分场近似）
  bench   跑基准套件
  demo    一键演示（合成工程区）
  verify  完整性校验（字节可复现 + 内容哈希）
  doctor  运维诊断（团队版功能）
  license 查看许可证状态
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .codec import deserialize, serialize
from .feature import Layer
from .grid import WeftError


def _load_public_hex() -> str | None:
    """加载内嵌 Ed25519 公钥（hex）。查找顺序：环境变量 → 包内 → CWD 相对。"""
    import os

    candidates = []
    env = os.environ.get("WEFT_PUBLIC_KEY")
    if env:
        return env.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "license_public.pem"))
    candidates.append(os.path.join("weft", "license_public.pem"))
    for path in candidates:
        try:
            lines = [
                ln.strip()
                for ln in open(path)
                if ln.strip() and not ln.startswith("-----")
            ]
            return "".join(lines)
        except OSError:
            continue
    return None


def _load_license() -> "object | None":
    import os

    from .license import License

    path = os.environ.get("WEFT_LICENSE")
    if not path or not os.path.exists(path):
        return None
    token = open(path).read().strip()
    return License(token, _load_public_hex())


def _geojson_to_weft(path: str, args) -> None:
    from .codec import serialize
    from .feature import KIND_POINT, Layer
    from .grid import Weft

    data = json.load(open(path))
    feats = data.get("features", [])
    # 推断层：按 geometry type 分三个默认层
    layers = [
        Layer(name="points", kind=KIND_POINT, columns=("name",)),
        Layer(name="lines", kind="line", columns=("name",)),
        Layer(name="polygons", kind="polygon", columns=("name",)),
    ]
    weft = Weft(args.width, args.height, layers)
    for f in feats:
        g = f.get("geometry") or {}
        t = g.get("type")
        props = f.get("properties") or {}
        name = str(props.get("name", ""))
        coords = g.get("coordinates") or []
        try:
            if t == "Point":
                weft.add_point("points", coords[0], coords[1], {"name": name})
            elif t == "LineString":
                weft.add_line("lines", coords, {"name": name})
            elif t == "Polygon":
                weft.add_polygon("polygons", coords, {"name": name})
        except WeftError:
            continue
    weft.commit()
    blob = serialize(weft)
    with open(args.out, "wb") as fh:
        fh.write(blob)
    print(f"构建完成: {args.out} ({len(blob)} 字节)")
    print(json.dumps(weft.stats(), ensure_ascii=False))


def cmd_build(args) -> None:
    if args.input.endswith(".geojson") or args.input.endswith(".json"):
        _geojson_to_weft(args.input, args)
    else:
        raise SystemExit("build 目前支持 GeoJSON 输入（.geojson/.json）")


def cmd_info(args) -> None:
    weft = deserialize(open(args.file, "rb").read())
    st = weft.stats()
    st["domain"] = [weft.width, weft.height]
    st["layers"] = {n: l.kind for n, l in weft.layers.items()}
    print(json.dumps(st, ensure_ascii=False, indent=2))


def cmd_query(args) -> None:
    weft = deserialize(open(args.file, "rb").read())
    hits = weft.exact_point_query(args.layer, args.x, args.y)
    approx, bound = weft.field_at(args.layer, args.x, args.y)
    out = {
        "point": [args.x, args.y],
        "layer": args.layer,
        "exact_hits": hits,
        "field_approx": approx,
        "field_bound": bound,
    }
    if hits:
        f = weft.reconstruct(hits[0])
        out["reconstructed_fid"] = f.fid
        out["props"] = f.props
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_bench(args) -> None:
    from .bench import run_all

    run_all(verbose=True)


def cmd_demo(args) -> None:
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import demo

    demo.main()


def cmd_verify(args) -> None:
    """完整性校验（社区版免费）：字节可复现 + 内容哈希 + 结构统计。"""
    from .codec import content_hash

    raw = open(args.file, "rb").read()
    # deserialize 内部会校验尾部 blake2b 摘要；文件被改过一个字节就会抛错。
    weft = deserialize(raw)
    rebuilt = serialize(weft)
    report = {
        "file": args.file,
        "bytes": len(raw),
        "content_hash": content_hash(raw).hex(),     # bytes → hex，否则 JSON 序列化失败
        "roundtrip_identical": rebuilt == raw,
        "stats": weft.stats(),
        "ok": rebuilt == raw,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(2)


def cmd_doctor(args) -> None:
    """运维诊断（团队版功能）：格分布、场误差抽样、体积构成。"""
    from .enterprise import diagnose

    weft = deserialize(open(args.file, "rb").read())
    report = diagnose(weft, lic=_load_license())
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_license(args) -> None:
    from .enterprise import license_status

    lic = _load_license()
    print(json.dumps(license_status(lic), ensure_ascii=False, indent=2))


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 cp936，打印中文/路径会 UnicodeEncodeError 或乱码。

    CLI 是给人看的，把自己切成 UTF-8 比要求用户改 chcp 更靠谱。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(prog="weft", description="统一矢量-栅格数据结构")
    ap.add_argument("--version", action="version", version=f"weft {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="从 GeoJSON 构建 .weft")
    p.add_argument("input")
    p.add_argument("-o", "--out", default="out.weft")
    p.add_argument("--width", type=float, default=1000.0)
    p.add_argument("--height", type=float, default=1000.0)
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("info", help="查看 .weft 统计")
    p.add_argument("file")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("query", help="点查询")
    p.add_argument("file")
    p.add_argument("--layer", required=True)
    p.add_argument("--x", type=float, required=True)
    p.add_argument("--y", type=float, required=True)
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("bench", help="基准套件")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("demo", help="一键演示")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("verify", help="完整性校验（字节可复现 + 内容哈希）")
    p.add_argument("file")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("doctor", help="运维诊断（团队版）")
    p.add_argument("file")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("license", help="许可证状态")
    p.set_defaults(fn=cmd_license)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except LicenseError_ as e:  # pragma: no cover
        print(f"许可证错误: {e}", file=sys.stderr)
        return 2
    except WeftError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


from .license import LicenseError as LicenseError_  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
