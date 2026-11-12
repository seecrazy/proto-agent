# -*- coding: utf-8 -*-
"""PROTO 控制台——clone 后的入口：python3 -m proto.run

首次运行自动生成主人密钥并注册锚定（数据目录 ./proto_data，已 gitignore）。
直接输入文字 = OWNER 通道消息（本地同机，自动签名认证）；
配置 PROTO_LLM_BASE_URL 等环境变量后用真实模型对话，未配置则离线模式。

命令：
  /state          查看内在状态（参数/欲望/情绪/最近调制）
  /audit [n]      审计链尾部 n 条（默认 10）
  /goals          查看待审批的主动提议
  /accept <id>    批准一个主动提议（执行草稿产出）
  /deny <id>      拒绝一个主动提议（触发阻尼：阈值上调）
  /task <描述>    指派任务：LLM 规划步骤 → 安全管线执行
  /mute /unmute   静音/解除（静音期间不出自主提议）
  /quit           退出
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from proto.config import load_default_env
from proto.llm.client import create_llm_stack
from proto.runtime.agent import ProtoAgent

BANNER = """
╔══════════════════════════════════════════════════════════╗
║  PROTO — 可控成长型 Agent v0.12.0                        ║
║  能力完整 · 倾向可塑 · 主动但受约束 · 一切可审计         ║
╚══════════════════════════════════════════════════════════╝
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="proto", description="PROTO 成长型 Agent")
    parser.add_argument("--data-dir", default="./proto_data", help="数据目录（默认 ./proto_data）")
    parser.add_argument("--base-url", default=os.environ.get("PROTO_LLM_BASE_URL", ""),
                        help="OpenAI 兼容 API 地址（默认取 PROTO_LLM_BASE_URL）")
    parser.add_argument("--api-key", default=os.environ.get("PROTO_LLM_API_KEY", ""),
                        help="API 密钥（默认取 PROTO_LLM_API_KEY）")
    parser.add_argument("--model", default=os.environ.get("PROTO_LLM_MODEL", "gpt-4o-mini"),
                        help="模型名（默认取 PROTO_LLM_MODEL）")
    args = parser.parse_args(argv)
    load_default_env()  # 部署配置（proto.env）自动生效

    llm = None
    if args.base_url.strip():
        from proto.llm.client import OpenAICompatClient
        llm = OpenAICompatClient(base_url=args.base_url.strip(),
                                 api_key=args.api_key.strip(),
                                 model=args.model.strip())
        agent = ProtoAgent(data_dir=Path(args.data_dir), llm=llm)
    else:
        stack = create_llm_stack()
        agent = ProtoAgent(data_dir=Path(args.data_dir),
                           llm=stack["main"] if stack else None,
                           fast_llm=stack["fast"] if stack else None)
        llm = agent.llm

    print(BANNER)
    print(f"  主人锚定指纹 : {agent.fingerprint[:23]}...（密钥在 {args.data_dir}/owner.key，请备份）")
    print(f"  对话模型     : {llm.describe() if llm else '离线模式（配置 PROTO_LLM_BASE_URL 启用真实对话）'}")
    print("  直接说话即可；/state 看内在状态；/task 指派任务；/quit 退出。\n")

    while True:
        try:
            line = input("主人> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not line:
            continue
        if line == "/quit":
            print("再见。")
            return 0
        if line == "/state":
            print(agent.status(), "\n")
            continue
        if line.startswith("/audit"):
            n = int(line.split()[1]) if len(line.split()) > 1 else 10
            for r in agent.audit_tail(n):
                print(f"  #{r['seq']} {r['event_type']} {r['payload']}")
            print()
            continue
        if line == "/goals":
            proposals = agent.pending_proposals()
            if not proposals:
                print("  （暂无待审批提议）\n")
            for p in proposals:
                print(f"  [{p['proposal_id']}] {p['description']}\n")
            continue
        if line.startswith("/accept "):
            result = agent.accept_proposal(line.split()[1])
            print(f"  执行结果: {result['status']}"
                  f"（草稿见 {args.data_dir}/ws/scratch/）\n")
            continue
        if line.startswith("/deny "):
            agent.deny_proposal(line.split()[1])
            print("  已拒绝（对应欲望触发阈值已上调——阻尼生效）\n")
            continue
        if line.startswith("/task "):
            result = agent.run_task(line[6:].strip())
            if result["status"] == "no_plan":
                print(f"  {result['reason']}\n")
            elif result["status"] == "paused":
                print(f"  任务暂停等待审批：/accept {result['proposal_id']}\n")
            else:
                print(f"  任务{result['status']}：{result.get('step_results')}\n")
            continue
        if line == "/mute":
            agent.kernel.set_mute(True)
            print("  已静音：不再产生自主提议。\n")
            continue
        if line == "/unmute":
            agent.kernel.set_mute(False)
            print("  解除静音。\n")
            continue

        reply = agent.chat(line)
        print(f"PROTO> {reply}\n")

        # 每轮检查是否到了主动提议的时机
        proposal = agent.maybe_proactive()
        if proposal:
            print(f"PROTO> {proposal}\n")


if __name__ == "__main__":
    raise SystemExit(main())
