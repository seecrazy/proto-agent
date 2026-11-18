# -*- coding: utf-8 -*-
"""M22: 生成 PROTO.app——python3 -m proto.app"""
from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None) -> int:
    from proto import ui
    from proto.config import load_default_env
    load_default_env()
    parser = argparse.ArgumentParser(prog="proto.app", description="生成 PROTO 原生 Mac App")
    parser.add_argument("--data-dir", default="./proto_data")
    parser.add_argument("--token", default=None,
                        help="固定令牌（默认运行时随机；也可用 PROTO_WEB_TOKEN）")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--output", default="./PROTO.app")
    args = parser.parse_args(argv)

    import os
    from proto.appshell.generator import build_app
    result = build_app(app_dir=Path(args.output), data_dir=str(Path(args.data_dir).resolve()),
                       token=args.token or os.environ.get("PROTO_WEB_TOKEN", ""),
                       port=args.port)
    print(ui.ok(f"App 生成：{result['path']}"))
    if result["compiled"]:
        print(ui.ok("swiftc 编译通过"))
        print(ui.dim(f"使用：open {result['path']}（退出 App 即停服务）"))
    else:
        print(ui.warn("未找到 swiftc——已产出源码，装 Xcode Command Line Tools 后重跑"))
        print(ui.dim(f"源码：{result['swift']}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
