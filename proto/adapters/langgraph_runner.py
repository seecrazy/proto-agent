# -*- coding: utf-8 -*-
"""LangGraph 编排适配器。

接入条件：pip install langgraph（或 pip install -e .[graph]）；
未安装时本模块不可导入（依赖已声明为可选），运行时一切照旧——
内置调度器与该图执行完全等价的节点序列。

图结构（每轮对话）：
    attribute ──▶ modulate ──▶ respond ──▶ proactive
      反馈归因       有界调制      LLM/离线回复    主动提议检查

节点体全部是 ProtoAgent 已有方法的薄封装——LangGraph 只负责编排与
状态传递（State 向下游累积），不引入任何新的业务逻辑；接入
checkpointer（pause/resume/branch）后即获得设计 §8 要求的断点续传。
"""
from __future__ import annotations

from typing import TypedDict


class TurnState(TypedDict, total=False):
    """一轮对话在图节点间流动的状态。"""
    text: str            # 主人输入
    signals: list        # 归因信号
    reply: str           # agent 回复
    proposal: str        # 主动提议（可为 None）


def build_turn_graph(agent):
    """编译 ProtoAgent 的单轮对话图。返回 langgraph 编译后的 app。

    用法：app = build_turn_graph(agent); result = app.invoke({"text": "..."})
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise ImportError(
            "LangGraph 适配器需要 langgraph：pip install langgraph "
            "（或 pip install -e .[graph]）。未安装时请使用内置调度器。") from exc

    def attribute(state: TurnState) -> TurnState:
        from proto.kernel.anchor import Channel
        signals = agent.attributor.attribute(state["text"], Channel.OWNER)
        return {"signals": signals}

    def modulate(state: TurnState) -> TurnState:
        agent.pipeline.process(state.get("signals", []))
        return {}

    def respond(state: TurnState) -> TurnState:
        # 复用 agent.chat 的注入+回复链路，但跳过其内部归因（图里已做）
        from proto.exec.injector import StateInjector
        from proto.runtime.agent import SYSTEM_PROMPT
        context = StateInjector().render(
            state["text"], agent.state,
            agent.library.retrieve(state["text"], k=3, epsilon=0.15))
        if agent.llm is not None:
            try:
                return {"reply": agent.llm.complete(
                    SYSTEM_PROMPT, context + "\n\n主人说：" + state["text"])}
            except Exception:
                pass
        return {"reply": agent._offline_reply(state["text"])}

    def proactive(state: TurnState) -> TurnState:
        return {"proposal": agent.maybe_proactive()}

    graph = StateGraph(TurnState)
    graph.add_node("attribute", attribute)
    graph.add_node("modulate", modulate)
    graph.add_node("respond", respond)
    graph.add_node("proactive", proactive)
    graph.set_entry_point("attribute")
    graph.add_edge("attribute", "modulate")
    graph.add_edge("modulate", "respond")
    graph.add_edge("respond", "proactive")
    graph.add_edge("proactive", END)
    return graph.compile()
