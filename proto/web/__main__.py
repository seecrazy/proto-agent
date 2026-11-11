# -*- coding: utf-8 -*-
"""PROTO Web 控制台入口：python3 -m proto.web

一条命令拉起本地管理界面（默认 http://127.0.0.1:8420，仅绑定本机回环）。
终端会打印带一次性令牌的完整 URL——令牌在 URL 片段里，不会发往服务器，
本机其他网页无法跨站调用受保护 API。

参数：--port 8420 --data-dir ./proto_data
LLM：与 proto.run 相同的环境变量（PROTO_LLM_BASE_URL / API_KEY / MODEL）。
"""
from __future__ import annotations

import argparse
import logging
import os
import webbrowser
from pathlib import Path

from proto.config import load_default_env
from proto.llm.client import create_llm_stack
from proto.runtime.agent import ProtoAgent
from proto.web.server import ProtoWebServer


def main() -> int:
    parser = argparse.ArgumentParser(prog="proto.web", description="PROTO Web 控制台")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--data-dir", default="./proto_data")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--caffeinate", action="store_true",
                        default=bool(os.environ.get("PROTO_CAFFEINATE")),
                        help="主人显式开启防休眠（caffeinate 绑定本进程；合盖仍会睡）")
    parser.add_argument("--token", default=os.environ.get("PROTO_WEB_TOKEN", ""),
                        help="固定访问令牌（默认随机；设 PROTO_WEB_TOKEN 可固定，便于长驻）")
    args = parser.parse_args()
    load_default_env()  # 部署配置（proto.env）自动生效

    stack = create_llm_stack()
    llm = stack["main"] if stack else None
    agent = ProtoAgent(data_dir=Path(args.data_dir), llm=llm,
                       fast_llm=stack["fast"] if stack else None)
    from proto.system.keepawake import KeepAwake
    keeper = KeepAwake()
    server = ProtoWebServer(agent, host="127.0.0.1", port=args.port,
                            token=args.token.strip() or None, keep_awake=keeper)
    if args.caffeinate:
        if keeper.start():
            print("  防休眠  : 已开启（主人显式授权；进程退出自动解除；合盖仍会睡眠）")
        else:
            print("  防休眠  : 当前平台不可用（需 macOS caffeinate）")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = server.url()
    print(f"\n  PROTO Web 控制台已启动")
    print(f"  地址   : {url}")
    print(f"  模型   : {llm.describe() if llm else '离线模式（配置 PROTO_LLM_BASE_URL 启用真实对话）'}")
    print(f"  数据   : {args.data_dir}/（owner.key 请备份）")
    print(f"  Ctrl+C 停止\n")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
