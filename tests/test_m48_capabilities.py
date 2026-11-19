# -*- coding: utf-8 -*-
"""M48：能力对齐专项——以编程 agent 清单逐项补齐。

用户三项控诉逐一对症：
- 预算失真 → LLM 真实用量计费（API usage 字段，不再 chars/4 猜）
- 大文件/多文件读不了 → read_file 行级分页 + read_files 批量读
- 循环卡死 → 工具参数错误降级为单步失败（链不再暴毙）+
  相同调用三连硬拦截 + run_python 输出头尾保留（测试输出在尾部）
"""
import json

import pytest

from proto.exec.agentic import AgenticLoop


class LoopLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, user))
        item = self.decisions.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture()
def agent(tmp_path):
    from proto.exec.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
    return ProtoAgent(data_dir=tmp_path, llm=LoopLLM([]), goal_interval_s=10**9,
                      webtools=dead)


class TestRealUsageBilling:
    def test_clients_expose_usage(self):
        from proto.llm.client import OpenAICompatClient, AnthropicCompatClient
        import proto.llm.client as lc

        class FakeResp:
            def __init__(self, body):
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        c = OpenAICompatClient("http://x", "k", "m")
        orig = lc.urllib.request.urlopen
        lc.urllib.request.urlopen = lambda req, timeout=None: FakeResp(
            {"choices": [{"message": {"content": "ok"}}],
             "usage": {"prompt_tokens": 120, "completion_tokens": 80}})
        try:
            assert c.complete("s", "u") == "ok"
            assert c.last_usage == {"input_tokens": 120, "output_tokens": 80}
        finally:
            lc.urllib.request.urlopen = orig

    def test_complete_charges_actual_tokens(self, agent):
        """_complete 优先按 API usage 实报实销（thinking token 不再漏计）。"""
        agent.kernel.pools.refill()

        class UsageLLM:
            last_usage = None

            def complete(self, system, user):
                self.last_usage = {"input_tokens": 900,
                                   "output_tokens": 1500}  # thinking 很贵
                return "回复" * 10

        before = agent.kernel.pools.remaining_all()["owner_tasks"]["tokens"]
        agent._complete(UsageLLM(), "s", "u" * 400, pool="owner_tasks")
        after = agent.kernel.pools.remaining_all()["owner_tasks"]["tokens"]
        spent = before - after
        assert 2300 <= spent <= 2600   # ≈900+1500 实报 + 少量 reservation 差额


class TestReadFilePaging:
    def test_read_file_line_paging(self, agent):
        root = agent.code_workspace.root
        (root / "big.swift").write_text(
            "\n".join(f"line {i}" for i in range(1, 501)), encoding="utf-8")
        out = str(agent.code_workspace.read_file("big.swift", offset=100, limit=20))
        assert "line 101" in out and "line 120" in out
        assert "line 121" not in out
        assert "offset=120" in out          # 告诉模型下一页怎么读
        assert "500 行" in out or "共 500" in out

    def test_read_files_batch(self, agent):
        root = agent.code_workspace.root
        for name in ("a.swift", "b.swift", "c.md"):
            (root / name).write_text(f"{name} 内容\n" + "x" * 50, encoding="utf-8")
        out = str(agent.tools.get("read_files")(
            paths=["a.swift", "b.swift", "c.md"]))
        assert "a.swift" in out and "b.swift" in out and "c.md" in out
        assert "内容" in out

    def test_catalog_has_batch_read(self, agent):
        assert "read_files" in agent.tools.names()


class TestRouterResilience:
    def test_missing_arg_is_step_failure_not_task_death(self, agent):
        """漏传参数：单步报错给足提示，链继续（原来 TypeError 直接任务暴毙）。"""
        from proto.exec.router import Router, Task
        task = Task(id="t1", description="d",
                    steps=[{"tool": "read_file", "args": {},   # 缺 path
                            "category": "read_draft"},
                           {"tool": "clock", "args": {},
                            "category": "read_draft"}])
        result = agent.router.run(task)
        assert result.status == "completed"          # 链没死
        assert any("read_file" in str(r) and "path" in str(r)
                   for r in result.step_results)
        assert any("202" in str(r) or ":" in str(r)   # clock 仍执行了
                   for r in result.step_results if "path" not in str(r))

    def test_generic_tool_error_continues_chain(self, agent):
        from proto.exec.router import Router, Task
        task = Task(id="t2", description="d",
                    steps=[{"tool": "read_file",
                            "args": {"path": "不存在的文件.md"},
                            "category": "read_draft"},
                           {"tool": "clock", "args": {},
                            "category": "read_draft"}])
        result = agent.router.run(task)
        assert result.status == "completed"
        assert len(result.step_results) == 2


class TestLoopGuards:
    def test_identical_call_triple_blocked(self, agent):
        """同一工具+同参数连续第 3 次调用被硬拦截（提示词拦不住就代码拦）。"""
        agent.llm.decisions = [
            "干",
            {"tool": "grep", "args": {"pattern": "zzz"}, "category": "read_draft"},
            {"tool": "grep", "args": {"pattern": "zzz"}, "category": "read_draft"},
            {"tool": "grep", "args": {"pattern": "zzz"}, "category": "read_draft"},
            {"done": True, "summary": "ok"},
            "发现：ok\n产出：无\n下一步：done",
        ]
        result = AgenticLoop(agent, max_steps=6).run("t")
        blocked = [o for o in result["observations"] if "重复" in o]
        assert blocked, "三连相同调用未被拦截"

    def test_run_python_output_keeps_tail(self, agent):
        """长输出保留头+尾（测试报错在尾部，全砍头会丢关键信息）。"""
        out = agent.code_workspace._clip_output(
            "\n".join(f"row {i}" for i in range(500)))
        assert "row 0" in out and "row 499" in out
        assert "省略" in out
