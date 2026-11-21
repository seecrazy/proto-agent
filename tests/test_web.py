# -*- coding: utf-8 -*-
"""M6: Web 控制台——JSON API（token 鉴权）+ 页面服务。"""
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from proto.llm.client import LLMClient
from proto.runtime.manager import AgentManager
from proto.web import server as web_server
from proto.web.server import ProtoWebServer
from tests.test_m66_fleet_runs import console


class ScriptedLLM(LLMClient):
    def __init__(self):
        self.calls = []
        self.loop_steps = 0

    def complete(self, system, user):
        self.calls.append(user)
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "clock 返回当前时间"})
        if "任务执行循环" in system:
            self.loop_steps += 1
            return json.dumps({"tool": "clock", "args": {}} if self.loop_steps == 1
                              else {"done": True, "summary": "已读取时间"})
        if "归因" in system:
            return json.dumps([{"param": "instincts.social_initiative",
                                "direction": 1, "strength": 0.6}])
        return "好的主人。"


@pytest.fixture()
def server(tmp_path):
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "web", llm=ScriptedLLM(),
                       goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, token=None, agent_id=None, request_key=None):
    url = f"http://127.0.0.1:{srv.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Proto-Token", token)
    if agent_id:
        req.add_header("X-Proto-Agent", agent_id)
    if request_key:
        req.add_header("Idempotency-Key", request_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class TestAuth:
    def test_api_without_token_401(self, server):
        status, _ = call(server, "GET", "/api/state")
        assert status == 401

    def test_api_with_wrong_token_401(self, server):
        status, _ = call(server, "GET", "/api/state", token="bad")
        assert status == 401

    def test_index_served_without_token(self, server):
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=5) as r:
            html = r.read().decode()
        assert "PROTO" in html and "内在状态" in html


def test_console_serves_startup_html_until_restart(tmp_path, monkeypatch):
    asset = tmp_path / "index.html"
    initial, updated = b"initial console", b"updated console"
    asset.write_bytes(initial)
    monkeypatch.setattr(web_server, "INDEX_PATH", asset)
    fleet_root = tmp_path / "fleet"
    with console(AgentManager(fleet_root)) as running:
        asset.write_bytes(updated)
        for route in ("/", "/index.html"):
            with urllib.request.urlopen(f"http://127.0.0.1:{running.port}{route}", timeout=5) as response:
                assert response.read() == initial
    with console(AgentManager(fleet_root)) as restarted:
        with urllib.request.urlopen(f"http://127.0.0.1:{restarted.port}/", timeout=5) as response:
            assert response.read() == updated


class TestStateAndChat:
    def test_state_has_params_with_bounds(self, server):
        status, data = call(server, "GET", "/api/state", token=server.token)
        assert status == 200
        instincts = data["params"]["instincts"]
        exploration = next(p for p in instincts if p["name"] == "exploration")
        assert exploration["bounds"] == [0.1, 0.9]
        assert "emotion" in data and "model" in data

    def test_chat_modulates_and_replies(self, server):
        status, data = call(server, "POST", "/api/chat",
                            {"text": "以后更主动一点"}, token=server.token)
        assert status == 200 and data["reply"] == "好的主人。"
        assert server.agent.state.get("instincts.social_initiative") == pytest.approx(0.52)

    def test_audit_with_chain_status(self, server):
        call(server, "POST", "/api/chat", {"text": "hi"}, token=server.token)
        status, data = call(server, "GET", "/api/audit?n=5", token=server.token)
        assert status == 200 and data["chain_ok"] and data["events"]


class TestActivityChat:
    def test_status_ignores_running_agent_lock_without_mutating(self, server, monkeypatch):
        agent = server.agent
        runs = server.manager.runs
        entered, release = threading.Event(), threading.Event()

        def hold_tick():
            entered.set()
            assert release.wait(15)

        def unexpected_dialogue(*args):
            pytest.fail("状态问询不应调用聊天或记忆固化")

        monkeypatch.setattr(agent, "maybe_proactive", hold_tick)
        monkeypatch.setattr(agent, "chat", unexpected_dialogue)
        monkeypatch.setattr(agent, "remember_turn", unexpected_dialogue)
        other = server.manager.create("另一个 Agent")["id"]
        budget = runs.budget.view()
        history = agent._load_history()
        active = runs.enqueue(agent.fleet_id, "tick")
        try:
            assert entered.wait(4)
            status, data = call(server, "POST", "/api/chat", {"text": "在干嘛？"}, token=server.token)
            assert status == 200 and data == {"reply": "我正在进行后台自主检查。", "read_only": True}
            monkeypatch.setattr(agent, "_current_op", {"goal": "核对项目进展"}, raising=False)
            status, data = call(server, "POST", "/api/chat", {"text": "你现在在做什么呢?"}, token=server.token)
            assert status == 200 and "核对项目进展" in data["reply"]
            status, data = call(server, "POST", "/api/chat", {"text": "在干嘛"},
                                token=server.token, agent_id=other)
            assert status == 200 and "没有正在运行" in data["reply"]
            assert agent._load_history() == history
            assert runs.budget.view() == budget
            assert [row["id"] for row in runs.list(agent.fleet_id)] == [active["id"]]
        finally:
            release.set()

    def test_status_works_offline_and_requires_authentication(self, server, monkeypatch):
        monkeypatch.setattr(server.agent, "llm", None)
        status, _ = call(server, "POST", "/api/chat", {"text": "在干嘛"})
        assert status == 401
        status, data = call(server, "POST", "/api/chat", {"text": " 在干嘛？ "}, token=server.token)
        assert status == 200 and data["read_only"] and "没有正在运行" in data["reply"]
        assert server.manager.runs.list() == []
        assert not server.agent._load_history()
        status, data = call(server, "POST", "/api/chat", {"text": "在干嘛"},
                            token=server.token, agent_id="missing")
        assert status == 404 and data == {"error": "no_such_agent"}

    @pytest.mark.parametrize("body", [{}, {"text": " "}, {"text": 1}, {"text": ["在干嘛"]}])
    def test_rejects_invalid_chat_text(self, server, body):
        status, data = call(server, "POST", "/api/chat", body, token=server.token)
        assert status == 400 and data == {"error": "invalid_chat_text"}


def test_chat_script_shows_errors_and_does_not_repeat_send():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = (Path(__file__).resolve().parents[1] / "proto/web/index.html").read_text(encoding="utf-8")
    script = re.search(r"async function sendChat\(\) \{.*?(?=async function refreshInboxOnce)", html, re.S).group()
    harness = r'''
const assert = require("assert");
const input = {value: "你好"};
const button = {disabled: false, textContent: "发送"};
const document = {getElementById: () => input, querySelector: () => button};
const messages = [];
const calls = [];
let inboxReads = 0;
let respond;
function addMsg(text, role) { messages.push({text, role}); }
function refreshInboxOnce() { inboxReads++; }
function newRequestKey() { return "chat-" + calls.length; }
async function api(method, path, body, key) {
  calls.push({method, path, body, key});
  return respond();
}
'''
    assertions = r'''
(async () => {
  let finish;
  respond = () => new Promise(resolve => { finish = resolve; });
  const pending = sendChat();
  assert.equal(button.disabled, true);
  input.value = "别重发";
  await sendChat();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "chat-0");
  assert.equal(input.value, "别重发");
  finish({reply: "普通回复"});
  await pending;
  assert.equal(inboxReads, 1);

  respond = () => { throw new Error("chat_in_progress"); };
  await sendChat();
  assert.equal(messages.at(-1).role, "system");
  assert.match(messages.at(-1).text, /没有执行/);
  assert.equal(button.disabled, false);

  input.value = "在干嘛";
  respond = () => ({reply: "工作中", read_only: true});
  await sendChat();
  assert.deepEqual(messages.at(-1), {text: "工作中", role: "agent"});
  assert.equal(inboxReads, 1);

  input.value = "网络错误";
  respond = () => { throw new Error("private provider error"); };
  await sendChat();
  assert.match(messages.at(-1).text, /未收到聊天回复/);
  assert(!JSON.stringify(messages).includes("private provider error"));
  assert.equal(button.textContent, "发送");
  assert.equal(calls.length, 4);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run([node], input=harness + script + assertions, text=True, check=True, timeout=10)


def test_company_panel_disables_creation_until_catalog_is_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = web_server.INDEX_PATH.read_text(encoding="utf-8")
    transport = re.search(r"async function api\(.*?(?=let modelCatalog)", html, re.S).group()
    panel = re.search(r"function selectOptions\(.*?(?=function editCompanyPosition)", html, re.S).group()
    harness = r'''
const assert = require("node:assert/strict");
const TOKEN = "test";
const controls = new Map();
function control(id) {
  if (!controls.has(id)) controls.set(id, {disabled: false, replaceChildren(...children) {
    this.value = children.find(child => child.selected)?.value ?? children[0]?.value ?? "";
  }});
  return controls.get(id);
}
const document = {getElementById: control, createElement: () => ({})};
function Option(label, value, defaultSelected, selected) { return {label, value, selected}; }
function addMsg() {}
let respond;
function fetch(path, options) {
  assert.equal(path, "/api/companies");
  assert.equal(options.method, "GET");
  return respond();
}
function reply(status, body) { return {status, ok: status === 200, json: async () => body}; }
'''
    assertions = r'''
(async () => {
  const fields = control("company-create-fields"), status = control("company-status");
  let finish;
  respond = () => new Promise(resolve => { finish = resolve; });
  const pending = reloadCompanyPanel();
  assert.equal(fields.disabled, true);
  assert.match(control("company-template-preview").textContent, /正在加载/);
  finish(reply(404, {error: "not_found"}));
  await pending;
  assert.match(status.textContent, /退出旧进程.*重启/);
  assert.equal(fields.disabled, true);
  for (const [code, payload, message] of [
    [200, {companies: [], templates: []}, /未返回可用/],
    [401, {error: "unauthorized"}, /令牌失效/],
    [503, {error: "internal"}, /刷新.*重试/],
  ]) {
    respond = () => reply(code, payload);
    await reloadCompanyPanel();
    assert.match(status.textContent, message);
    assert.equal(fields.disabled, true);
  }
  respond = () => reply(200, {companies: [], templates: [{
    id: "custom", label: "自定义", description: "从根职位开始", positions: [],
  }]});
  await reloadCompanyPanel();
  assert.equal(status.textContent, "");
  assert.equal(control("company-template").value, "custom");
  assert.equal(fields.disabled, false);
  assert.equal(control("company-create-box").open, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run([node], input=harness + transport + panel + assertions, text=True, check=True, timeout=10)


class TestProposalsAndControls:
    def _make_proposal(self, server):
        server.agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        call(server, "POST", "/api/tick", {}, token=server.token)
        status, data = call(server, "GET", "/api/proposals", token=server.token)
        assert data["proposals"]
        return data["proposals"][0]["proposal_id"]

    def test_deny_proposal_dampens(self, server):
        pid = self._make_proposal(server)
        status, data = call(server, "POST", f"/api/proposals/{pid}/deny",
                            {}, token=server.token)
        assert status == 200
        assert data["threshold"] == pytest.approx(0.65)

    def test_accept_proposal_completes(self, server):
        pid = self._make_proposal(server)
        status, data = call(server, "POST", f"/api/proposals/{pid}/accept",
                            {}, token=server.token)
        assert status == 200 and data["status"] == "completed"

    def test_mute_toggle(self, server):
        status, data = call(server, "POST", "/api/mute", {"on": True}, token=server.token)
        assert status == 200 and data["muted"] is True

    def test_lock_and_override(self, server):
        status, data = call(server, "POST", "/api/lock",
                            {"param": "instincts.exploration", "on": True},
                            token=server.token)
        assert status == 200 and data["locked"] is True
        status, data = call(server, "POST", "/api/override",
                            {"param": "personality.rigor", "value": 0.9},
                            token=server.token)
        assert status == 200 and data["value"] == 0.9

    def test_set_owner_priority(self, server):
        status, data = call(server, "POST", "/api/priority",
                            {"text": "补齐单元测试"}, token=server.token)
        assert status == 200
        assert server.agent.state.owner_priority == "补齐单元测试"

    def test_task_endpoint(self, server):
        status, data = call(server, "POST", "/api/task",
                            {"text": "看一下时间"}, token=server.token)
        assert status == 200
        assert data["status"] == "verified"
        assert data["trace"][0]["tool"] == "clock"

    def test_evolution_snapshot(self, server):
        status, data = call(server, "GET", "/api/evolution", token=server.token)
        assert status == 200
        assert "variants" in data and "testset_count" in data


class TestM8Endpoints:
    def test_rules_add_and_view(self, server):
        status, data = call(server, "POST", "/api/rules",
                            {"text": "涉及支付先写测试"}, token=server.token)
        assert status == 200 and data["rules"][0]["text"] == "涉及支付先写测试"

    def test_task_resume_endpoint(self, server):
        from proto.runtime.agent import ProtoAgent  # noqa: F401
        result = {"status": "failed", "reason": "no_checkpoint"}
        status, data = call(server, "POST", "/api/task/none/resume",
                            {"approved": True}, token=server.token)
        assert status == 200 and data["reason"] == "no_checkpoint"

    def test_evolution_testset_and_mutate(self, server):
        status, data = call(server, "POST", "/api/evolution/testset",
                            {"input": [" a "], "expected": "a"}, token=server.token)
        assert status == 200 and data["testset_count"] == 1
        status, data = call(server, "POST", "/api/evolution/mutate",
                            {"code": "def solve(items):\n    return '|'.join(str(x).strip() for x in items)\n"},
                            token=server.token)
        assert status == 200 and data["merged"] is True
        vid = data["variant_id"]
        status, data = call(server, "POST", "/api/evolution/promote",
                            {"variant_id": vid, "target_class": "l1_rules"},
                            token=server.token)
        assert status == 200 and data["promoted"] is False  # 高风险永远拒绝


class TestM10Endpoints:
    def test_persona_set_and_state(self, server):
        status, data = call(server, "POST", "/api/persona",
                            {"text": "资深软件专家"}, token=server.token)
        assert status == 200 and data["persona"] == "资深软件专家"
        status, data = call(server, "GET", "/api/state", token=server.token)
        assert data["persona"] == "资深软件专家"

    def test_scratch_notes_listing(self, server):
        server.agent.workspace.write("notes/learning-1.md", "# 学习笔记\n来源：github.com/x/y")
        status, data = call(server, "GET", "/api/scratch", token=server.token)
        assert status == 200
        assert data["notes"] and "github.com/x/y" in data["notes"][0]["preview"]
        assert data["notes"][0]["mtime"] > 0
