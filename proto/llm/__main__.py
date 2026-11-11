# -*- coding: utf-8 -*-
"""LLM 自检：python3 -m proto.llm —— 一条命令验证你的 LLM 配置。

示例（智谱 Coding Plan）：
  export PROTO_LLM_PROVIDER=zhipu-coding
  export PROTO_LLM_API_KEY=你的key
  export PROTO_LLM_MODEL=glm-5.3
  export PROTO_LLM_FAST_MODEL=glm-5.3-flash   # 可选
  python3 -m proto.llm
"""
from __future__ import annotations

import time


def main() -> int:
    from proto.llm.client import create_llm_stack

    stack = create_llm_stack()
    if stack is None:
        print("未配置 LLM（离线模式）。")
        print("智谱 Coding Plan 示例：")
        print("  export PROTO_LLM_PROVIDER=zhipu-coding")
        print("  export PROTO_LLM_API_KEY=<你的CodingPlanKey>")
        print("  export PROTO_LLM_MODEL=glm-5.3")
        print("  export PROTO_LLM_FAST_MODEL=glm-5.3-flash")
        return 1
    for name in ("main", "fast"):
        client = stack[name]
        if name == "fast" and stack["fast"] is stack["main"]:
            print(f"[fast] 与主模型相同，跳过")
            continue
        started = time.time()
        try:
            reply = client.complete("你是连通性探针。", "请只回复两个字：在线")
            print(f"[{name}] {client.describe()} → OK ({time.time()-started:.1f}s) 回复: {reply[:30]}")
        except Exception as exc:
            print(f"[{name}] {client.describe()} → 失败: {exc}")
            return 1
    print("LLM 配置验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
