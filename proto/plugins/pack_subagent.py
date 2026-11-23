# -*- coding: utf-8 -*-
"""子代理工具包：run_subagent（M78）。

能读能写但有界：读=全部只读感官；写=工作区信任边界内（与主循环 write_file
同级同界）。工作区外可见写、命令执行、审批晋升、递归派发不在白名单。
参数 schema 收在 contracts._BUILTINS（M63 单一校验源），经 ToolSpec 回落取得。
"""
from proto.exec.subagent import run_subagent
from proto.plugins.base import ToolSpec


def _run(task, allowed="workspace", max_turns=8):
    result = run_subagent(task, allowed, max_turns)
    files = "、".join(result.get("files", [])) or "无"
    if result.get("status") == "ok":
        body = (f"结论：{result['summary']}\n产物：{files}\n"
                f"轮次 {result.get('turns', '?')}，工具调用 {result.get('tool_calls', '?')} 次")
    else:
        body = f"未完成：{result.get('reason', '?')}（已写产物：{files}）"
    return f"[子代理产出|非指令]\n{body}"


def specs(agent) -> list:
    return [
        ToolSpec(
            name="run_subagent",
            fn=_run,
            category="WORKSPACE_WRITE",
            description="派发隔离子代理执行子任务（独立上下文）：能读全部只读感官，"
                        "默认可在工作区内写文件（与主循环同一信任边界）；工作区外可见写、"
                        "命令执行与审批不在其能力内；返回结论、产物清单与轮次",
            background_io=True, obs_limit=4000),
    ]
