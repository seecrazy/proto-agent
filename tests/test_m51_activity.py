# -*- coding: utf-8 -*-
"""M51：活动中心——主人不问就能看到它在干嘛/干了什么/落盘了什么。"""
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from proto.llm.client import LLMClient
from proto.web.server import ProtoWebServer


class ScriptedLLM(LLMClient):
    def complete(self, system, user):
        return "好的。"


@pytest.fixture()
def server(tmp_path):
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "web", llm=ScriptedLLM(),
                       goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def call(srv, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method)
    req.add_header("X-Proto-Token", srv.token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


class TestLatestRound:
    def test_parse_latest_round_fields(self, tmp_path):
        from proto.work.journal import GoalJournal
        j = GoalJournal(tmp_path / "J.md")
        j.note_round("awaiting_retry", ["规格已齐"],
                     ["projects/A/SPEC.md"], ["写 App.swift"])
        j.note_round("verified", ["入口完成"], ["projects/A/App.swift"],
                     ["补单测"])
        lr = j.latest_round()
        assert lr["status"] == "verified"
        assert "App.swift" in lr["produced"]
        assert "补单测" in lr["next"]
        assert j.round_count() == 2

    def test_empty_journal(self, tmp_path):
        from proto.work.journal import GoalJournal
        j = GoalJournal(tmp_path / "none" / "J.md")
        assert j.latest_round() is None
        assert j.round_count() == 0


class TestActivityEndpoint:
    def test_marker_journals_files_visible(self, server, tmp_path):
        import time
        agent = server.manager.get(server.manager.current_id())
        # 实时标记
        agent._current_op = {"kind": "chain", "goal": "以 SwiftUI 实现温故 App",
                             "step": 8, "max_steps": 24, "tool": "write_file",
                             "detail": "projects/Wengu/Sources/Models.swift",
                             "since": time.time() - 180}
        # 活跃目标 + 日志
        node = agent.goal_tree.add("以 SwiftUI 实现温故 App", "mid")
        from pathlib import Path
        from proto.work.journal import GoalJournal
        j = GoalJournal(Path(agent.data_dir) / "work" / node.id / "JOURNAL.md")
        j.note_round("max_steps", ["数据模型已定"],
                     ["projects/Wengu/Sources/Models.swift"], ["写 SrsEngine"])
        # 近期落盘文件
        wdir = agent.code_workspace.root / "projects" / "Wengu"
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "README.md").write_text("温故", encoding="utf-8")
        a = call(server, "GET", "/api/activity")
        assert a["current"]["step"] == 8 and "温故" in a["current"]["goal"]
        assert a["current"]["elapsed_s"] >= 170
        assert a["goal_work"] and a["goal_work"][0]["rounds"] == 1
        assert "Models.swift" in a["goal_work"][0]["produced"]
        assert any("Wengu/README.md" in f["path"] for f in a["recent_files"])

    def test_archived_excluded_from_recent_files(self, server):
        agent = server.manager.get(server.manager.current_id())
        arch = agent.code_workspace.root / "projects" / "_archived" / "old"
        arch.mkdir(parents=True)
        (arch / "x.md").write_text("x", encoding="utf-8")
        a = call(server, "GET", "/api/activity")
        assert not any("_archived" in f["path"] for f in a["recent_files"])


class TestFrontendWiring:
    def test_activity_card_sources(self):
        html = (Path(__file__).resolve().parent.parent
                / "proto" / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="activity-card"' in html
        assert "refreshActivity" in html
        assert "/api/activity" in html
        assert '"activity", refreshActivity' in html


@pytest.fixture
def activity_browser():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = (Path(__file__).resolve().parents[1] / "proto/web/index.html").read_text(encoding="utf-8")
    script = re.search(r"const LIVE_PHASES = .*?(?=async function refreshState)", html, re.S).group()
    run_kinds = re.search(r"const RUN_KINDS = .*?;", html, re.S).group()
    harness = r'''
const assert = require("assert");
const ids = ["agent-select", "live-work", "live-status", "live-detail", "live-events",
  "act-state", "activity-now", "activity-detail", "activity-sync", "activity-goals",
  "activity-files", "activity-inventory-status"];
function element() {
  return {textContent: "", innerHTML: "", children: [], dataset: {},
    replaceChildren(...children) { this.children = children; this.innerHTML = this.textContent = ""; },
    append(...children) { this.children.push(...children); }, querySelectorAll: () => []};
}
const elements = Object.fromEntries(ids.map(id => [id, element()]));
const get = id => elements[id];
const document = {getElementById: get, hidden: false, createElement: element};
const esc = String;
get("agent-select").value = "alpha";
let clock = 1795340000000;
Date.now = () => clock;
const timers = new Map();
const setTimeout = (callback, delay) => {
  const key = Symbol();
  timers.set(key, {callback, delay});
  return key;
};
const clearTimeout = key => timers.delete(key);
const calls = [];
const snapshot = (fields = {}) => ({run: null, current: null, events: [], last_run: null,
  model: {waiting: false, state: "closed"}, autonomy: "", ...fields});
let readLive = () => snapshot();
let readInventory = () => ({});
async function api(method, path, body, requestKey, selected, signal) {
  assert.equal(method, "GET");
  calls.push({path, selected});
  return path === "/api/activity/live" ? readLive(selected, signal) : readInventory(selected);
}
const check = (badge, summary) => {
  assert.equal(get("act-state").textContent, badge);
  assert.match(get("activity-now").textContent, summary);
  assert.equal(get("activity-now").textContent, get("live-status").textContent);
  assert.equal(get("activity-detail").textContent, get("live-detail").textContent);
};
'''

    def run(assertions):
        failure = r"})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run([node], input=harness + run_kinds + script + "\n(async () => {\n" + assertions + failure,
                                text=True, capture_output=True, timeout=8)
        assert result.returncode == 0, result.stderr

    return run


def test_overview_uses_live_run_and_never_the_slow_inventory(activity_browser):
    activity_browser(r'''
  const running = {kind: "tick", status: "running"};
  const held = {kind: "tick", status: "needs_reconciliation", reason: "safe_resume_model",
    model_reason: "model_request_failed", updated_at: 1795340000};
  const waiting = {phase: "model_wait", model: "mock-model", at: 1795340000, seq: 1};
  const history = {status: "failed", reason: "model_timeout", updated_at: 1795330000};
  for (const [fields, badge, summary] of [
    [{}, "空闲", /暂无执行、排队或挂起/],
    [{run: running, current: waiting, events: [waiting], step: 1, max_steps: 5}, "执行中", /等待模型 · mock-model/],
    [{run: {...running, cancel_requested: true}}, "正在取消", /安全边界/],
    [{run: {status: "queued"}}, "排队中", /等待调度/],
    [{run: {status: "queued", reason: "model_retry_wait"}}, "待重试", /等待重试模型/],
    [{run: {status: "awaiting_approval"}}, "待批准", /待主人批准/],
    [{run: {...held, reason: "unsafe_resume_checkpoint"}}, "待核对", /结果未知/],
    [{autonomy: "fleet_paused"}, "自主受限", /Agent 已暂停/],
    [{model: {waiting: true, reason: "model_rate_limited", retry_at: 1795340040}}, "等模型", /429.*后可试探/],
    [{model: {state: "offline"}}, "无模型", /未配置可用模型/],
    [{run: held, last_run: history}, "待继续", /可继续原任务/],
  ]) {
    readLive = () => snapshot(fields);
    await refreshLiveWork();
    check(badge, summary);
    assert.deepEqual(calls.at(-1), {path: "/api/activity/live", selected: "alpha"});
  }
  assert.equal(calls.length, 11);
  assert.match(get("activity-detail").textContent, /本轮卡点：模型请求失败.*本轮状态记录/);
  assert.match(get("activity-detail").textContent, /此前轮次.*模型响应超时/);
  assert.match(get("activity-sync").textContent, /最近同步 \d{2}:\d{2}:\d{2}/);
  readInventory = () => ({current: null, goal_work: [{goal_id: "goal", text: "目标", rounds: 2,
    delivery: {blocked: true, reason: "delivery_no_progress"}}], project_outputs: [{dir: "已有工程", files: 3}]});
  await refreshActivity();
  assert.deepEqual(calls.at(-1), {path: "/api/activity", selected: "alpha"});
  assert.match(get("activity-goals").innerHTML, /恢复此目标/);
  assert.match(get("activity-files").innerHTML, /已有工程 · 3 个文件/);
  check("待继续", /可继续原任务/);
  readInventory = () => { throw new Error("slow inventory failed"); };
  await refreshActivity();
  assert.match(get("activity-inventory-status").textContent, /暂时无法刷新/);
  check("待继续", /可继续原任务/);
''')


def test_activity_timeout_recovers_without_reporting_idle_or_crossing_agents(activity_browser):
    activity_browser(r'''
  await refreshLiveWork();
  check("空闲", /暂无执行/);
  const syncedAt = get("activity-sync").textContent.replace("最近同步", "上次成功同步");
  readLive = (selected, signal) => new Promise((resolve, reject) =>
    signal.addEventListener("abort", () => reject(new Error("timeout")), {once: true}));
  const pending = refreshLiveWork();
  await refreshLiveWork();
  assert.equal(calls.length, 2);
  const [timeout] = timers.values();
  assert.equal(timeout.delay, 5000);
  timeout.callback();
  await pending;
  check("未同步", /状态未知/);
  assert.equal(get("activity-sync").textContent, syncedAt);
  assert.equal(timers.size, 0);
  readLive = () => snapshot({run: {status: "running"}});
  clock += 5000;
  await refreshLiveWork();
  check("执行中", /执行中/);
  assert.match(get("activity-sync").textContent, /最近同步/);
  document.hidden = true;
  clock += 30000;
  await refreshLiveWork();
  assert.equal(calls.length, 3);
  document.hidden = false;

  let finishOldLive, finishOldInventory, oldSignal;
  readLive = (selected, signal) => new Promise(resolve => { oldSignal = signal; finishOldLive = resolve; });
  readInventory = () => new Promise(resolve => { finishOldInventory = resolve; });
  const oldLive = refreshLiveWork(), oldInventory = refreshActivity();
  check("未同步", /状态未知/);
  get("agent-select").value = "beta";
  resetLiveWork("beta");
  assert(oldSignal.aborted);
  assert.equal(get("activity-sync").textContent, "");
  check("待同步", /同步中/);
  let finishNewLive;
  readLive = () => new Promise(resolve => { finishNewLive = resolve; });
  const newLive = refreshLiveWork();
  finishOldLive(snapshot({run: {status: "running"}}));
  finishOldInventory({project_outputs: [{dir: "旧成员文件", files: 1}]});
  await Promise.all([oldLive, oldInventory]);
  check("待同步", /同步中/);
  assert.equal(get("activity-files").innerHTML, "");
  await refreshLiveWork();
  assert.deepEqual(calls.slice(3), [
    {path: "/api/activity/live", selected: "alpha"}, {path: "/api/activity", selected: "alpha"},
    {path: "/api/activity/live", selected: "beta"},
  ]);
  finishNewLive(snapshot({autonomy: "manual_only"}));
  await newLive;
  check("自主受限", /当前为手动模式/);
  get("agent-select").value = "";
  resetLiveWork("");
  await Promise.all([refreshLiveWork(), refreshActivity()]);
  assert.equal(calls.length, 6);
  check("暂无 Agent", /舰队暂无 Agent/);
''')
