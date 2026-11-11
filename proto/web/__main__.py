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
import os
import webbrowser
from pathlib import Path

from proto.llm.client import OpenAICompatClient
from proto.runtime.agent import ProtoAgent
from proto.web.server import ProtoWebServer


def main() -> int:
    parser = argparse.ArgumentParser(prog="proto.web", description="PROTO Web 控制台")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--data-dir", default="./proto_data")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--token", default=os.environ.get("PROTO_WEB_TOKEN", ""),
                        help="固定访问令牌（默认随机；设 PROTO_WEB_TOKEN 可固定，便于长驻）")
    args = parser.parse_args()

    llm = OpenAICompatClient.from_env()
    agent = ProtoAgent(data_dir=Path(args.data_dir), llm=llm)
    server = ProtoWebServer(agent, host="127.0.0.1", port=args.port,
                             token=args.token.strip() or None)

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
