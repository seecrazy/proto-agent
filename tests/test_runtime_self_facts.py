import json
import time
from datetime import datetime, timezone

import pytest

from proto import run
from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.exec.context import create_context
from proto.runtime.agent import ProtoAgent
from proto.runtime.manager import AgentManager
from proto.runtime.self_facts import self_question
from tests.test_m66_fleet_runs import console
from tests.test_web import call


class ModelRecorder:
    def __init__(self):
        self.messages = []

    def describe(self):
        return "offline test model"

    def complete(self, system, user):
        self.messages.append((system, user))
        if system == LOOP_SYSTEM:
            return json.dumps({"done": True, "summary": "已读取当前时点"})
        return "按运行时事实回答。"


def test_lifetime_includes_restarts_and_pause_but_session_uses_monotonic_clock(tmp_path):
    born = datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp()
    wall, tick = [born], [200]
    directory = tmp_path / "standalone"
    agent = ProtoAgent(directory, now=lambda: wall[0], monotonic=lambda: tick[0])
    initial = agent.self_snapshot()["lifecycle"]
    assert initial["source"] == "initialization"
    assert datetime.fromisoformat(initial["created_at"]).timestamp() == born
    assert "没有分配" in agent.self_fact_reply("你的 Agent ID 是什么？")

    wall[0] += 86400 + 3661
    tick[0] += 4
    assert "1天1小时1分钟1秒" in agent.self_fact_reply("你知道你自己存在多久了吗？")
    assert "4秒" in agent.self_fact_reply("你本次运行了多久？")
    assert "没有跨重启" in agent.self_fact_reply("你累计工作了多久？")
    assert agent.tools.get("clock")() == agent.self_snapshot()["lifecycle"]["as_of"]

    wall[0] += 3 * 86400
    agent = ProtoAgent(directory, now=lambda: wall[0], monotonic=lambda: tick[0])
    assert agent.self_snapshot()["lifecycle"]["session_seconds"] == 0
    fleet = AgentManager(tmp_path / "fleet")
    member = fleet.adopt(agent, "旧成员")["id"]
    fleet.rename(member, "改过名")
    fleet.pause(member)
    agent.set_persona("设定年龄三百岁的设计师", recommend=False)
    assert "4天1小时1分钟1秒" in agent.self_fact_reply("你多大了？")
    assert "三百岁" in agent.self_fact_reply("你的当前人设是什么？")
    assert "改过名" in agent.self_fact_reply("你叫什么名字？")
    assert datetime.fromisoformat(agent.self_snapshot()["lifecycle"]["created_at"]).timestamp() == born

    wall[0] = born - 1
    tick[0] += 6
    rolled_back = agent.self_snapshot()["lifecycle"]
    assert rolled_back["created_at"] == initial["created_at"]
    assert rolled_back["age_seconds"] is None and rolled_back["session_seconds"] == 6
    assert "时钟早于该记录" in agent.self_fact_reply("你存在多久了？")


def test_older_adopted_agent_recovers_first_initialization_and_keeps_it_after_reanchor(tmp_path):
    directory = tmp_path / "standalone"
    legacy = ProtoAgent(directory)
    first = legacy.spine.first_verified_event()
    original = datetime.fromisoformat(first["ts"]).timestamp()
    legacy.spine.store.delete("config", "lifecycle")
    root = tmp_path / "fleet"
    fleet = AgentManager(root)
    record = fleet.adopt(legacy, "旧成员")
    assert record["created_at"] > original

    reopened = AgentManager(root).get(record["id"])
    restored = reopened.self_snapshot()["lifecycle"]
    assert restored["source"] == "initial_audit"
    assert reopened.spine.store.get("config", "lifecycle")["origin_at"] == original
    assert "首次初始化" in reopened.chat("你是什么时候创建的？")

    reopened.spine.store.delete("anchor", "owner")
    (directory / "owner.key").unlink()
    reanchored = AgentManager(root).get(record["id"])
    assert reanchored.fingerprint != reopened.fingerprint
    assert reanchored.self_snapshot()["lifecycle"]["created_at"] == restored["created_at"]


def test_managed_agent_uses_creation_registry_when_legacy_lifetime_is_missing(tmp_path):
    fleet = AgentManager(tmp_path)
    member = fleet.create("托管成员")
    agent = fleet.get(member["id"])
    assert agent.spine.store.get("config", "lifecycle")["origin_at"] == member["created_at"]
    agent.spine.store.delete("config", "lifecycle")
    registry = fleet._load_registry()
    earlier = member["created_at"] - 6 * 86400
    registry["agents"][0]["created_at"] = earlier
    fleet._save_registry(registry)
    loaded = AgentManager(tmp_path).get(member["id"])
    assert loaded.self_snapshot()["lifecycle"]["age_seconds"] >= 6 * 86400
    assert loaded.spine.store.get("config", "lifecycle")["origin_at"] == earlier


def test_adoption_registration_is_only_a_lower_bound_without_original_records(tmp_path):
    directory = tmp_path / "partial-old-backup"
    fleet = AgentManager(tmp_path / "fleet")
    agent = ProtoAgent(directory)
    member = fleet.adopt(agent, "旧成员")
    store = agent.spine.store
    store.delete("config", "lifecycle")
    store.delete("audit", "1")
    owner = store.get("anchor", "owner")
    owner.pop("created_at")
    store.put("anchor", "owner", owner)

    reopened = AgentManager(fleet.root).get(member["id"])
    evidence = reopened.self_snapshot()["lifecycle"]
    assert evidence["created_at"] is None and evidence["source"] == "fleet_adoption"
    assert reopened.spine.store.get("config", "lifecycle")["origin_at"] == member["created_at"]


@pytest.mark.parametrize("has_anchor_time", [True, False])
def test_missing_or_doctored_birth_evidence_yields_persisted_lower_bound(tmp_path, has_anchor_time):
    agent = ProtoAgent(tmp_path)
    store = agent.spine.store
    store.put("config", "lifecycle", {"schema_version": 1, "origin_at": "not-a-date", "source": "initialization"})
    first = store.get("audit", "1")
    first["ts"] = "2020-01-01T00:00:00+00:00"
    store.put("audit", "1", first)
    anchor = store.get("anchor", "owner")
    anchor["created_at"] = time.time() - 86400 if has_anchor_time else None
    store.put("anchor", "owner", anchor)

    observed = time.time()
    reopened = ProtoAgent(tmp_path, now=lambda: observed)
    evidence = reopened.self_snapshot()["lifecycle"]
    assert reopened.spine.first_verified_event() is None
    assert evidence["created_at"] is None and evidence["age_seconds"] is None
    assert evidence["source"] == ("owner_anchor" if has_anchor_time else "observed")
    assert "原始创建时间无法确认" in reopened.chat("你存在了多久？")
    later = ProtoAgent(tmp_path, now=lambda: observed + 25).self_snapshot()["lifecycle"]
    assert later["first_known_at"] == evidence["first_known_at"]
    assert later["minimum_age_seconds"] == evidence["minimum_age_seconds"] + 25


@pytest.mark.parametrize("text", [
    "iPhone 项目已经存在多久了？", "你做这个任务做了多久？", "你存在多久了，顺便列出任务。",
    "替客户写一段‘你是谁’", "你可以自己工作。", "把你的 ID 改成 123。",
])
def test_short_fact_intent_does_not_absorb_commands_or_other_entities(text):
    assert self_question(text) is None


def test_web_answers_with_all_agent_locks_held_and_no_model_or_memory(tmp_path):
    model = ModelRecorder()
    fleet = AgentManager(tmp_path, llm=model)
    first = fleet.get(fleet.create("同名成员", "第一成员的人设")["id"])
    other = fleet.get(fleet.create("同名成员", "第二成员的人设")["id"])
    directory = fleet.runs.companies
    company = directory.create("样例公司", "custom")
    position = directory.view(company["id"])["positions"][0]
    directory.save_position(company["id"], position["id"], {
        "revision": 1, "title": "设计主管", "agent_id": first.fleet_id, "manager_id": None,
        "responsibility": "设计样例"})
    fleet.pause(first.fleet_id)
    first.kernel.set_mute(True)
    with console(fleet) as server, fleet.ops_lock(first.fleet_id), first.dialogue_lock, first.state_guard:
        def ask(text, agent=first):
            status, response = call(server, "POST", "/api/chat", {"text": text},
                                    token=server.token, agent_id=agent.fleet_id)
            assert status == 200 and response["read_only"] is True
            return response["reply"]

        assert call(server, "POST", "/api/chat", {"text": "你存在多久了"})[0] == 401
        assert "含停机和暂停" in ask("你已经存在多久了？")
        assert "本次加载" in ask("这次运行多久了？")
        assert first.fleet_id in ask("你的 ID 是什么？")
        assert other.fleet_id in ask("你的 ID 是什么？", other)
        assert "第二成员的人设" in ask("你是谁？", other)
        assert "公司任职记录" in ask("你在哪家公司？", other)
        assert "样例公司」担任「设计主管" in ask("你担任什么职位？")
        assert "我被舰队暂停" in ask("你现在能自主工作吗？")
        assert "我当前被静音" in ask("你是不是静音了？")
        assert "服务端时钟" in ask("现在几点了？")
        assert call(server, "POST", f"/api/agents/{first.fleet_id}/name", {"name": "新的显示名"},
                    token=server.token)[0] == 200
        assert "新的显示名" in ask("你叫什么？")
        status, payload = call(server, "GET", "/api/identity", token=server.token, agent_id=first.fleet_id)
        assert status == 200 and "ocean" in payload
        assert payload["self"]["id"] == first.fleet_id and payload["self"]["name"] == "新的显示名"
        assert "本次加载" in payload["self"]["lifecycle_summary"]
    assert model.messages == [] and fleet.runs.dialogue.pending_agents() == []
    assert first._load_history() == [] and other._load_history() == []


def test_model_dialogues_and_execution_see_fresh_facts_without_freezing_them(tmp_path):
    model = ModelRecorder()
    wall = [time.time()]
    agent = ProtoAgent(tmp_path / "agent", llm=model, now=lambda: wall[0])
    fleet = AgentManager(tmp_path / "fleet", llm=model)
    member = fleet.adopt(agent, "原名")
    task = "按固定上下文继续工作"
    context = create_context(agent, task, "", "owner_tasks")
    assert "实时自我事实" not in context["profile"]
    fleet.rename(member["id"], "改名后")
    wall[0] += 20
    now = agent.self_snapshot()["lifecycle"]["as_of"]
    question = "你存在多久了，顺便评价一下这次任务。"
    assert agent.chat(question) == "按运行时事实回答。"
    with console(fleet) as server:
        status, reply = call(server, "POST", "/api/chat", {"text": question}, token=server.token)
        assert status == 200 and reply == {"reply": "按运行时事实回答。"}
    dialogue = [(system, user) for system, user in model.messages if "主人说：" + question in user]
    assert len(dialogue) == 2
    assert all("实时自我事实" not in system and now in user and "改名后" in user and member["id"] in user
               for system, user in dialogue)
    assert any("后台工作事实" in user for _, user in dialogue)
    AgenticLoop(agent).run(task, frozen_context=context)
    [execution] = [user for system, user in model.messages if system == LOOP_SYSTEM]
    assert now in execution and "改名后" in execution and task in execution
    assert "实时自我事实" not in context["profile"]


def test_repl_self_question_skips_memory_and_proactivity(tmp_path, monkeypatch, capsys):
    model = ModelRecorder()
    monkeypatch.setattr(run, "create_llm_stack", lambda: {"main": model, "fast": model})
    inputs = iter(("你存在多久了？", "/quit"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    def forbidden(*args):
        pytest.fail("只读自我问询不应触发对话记忆或主动任务")

    monkeypatch.setattr(ProtoAgent, "remember_turn", forbidden)
    monkeypatch.setattr(ProtoAgent, "maybe_proactive", forbidden)
    assert run.main(["--data-dir", str(tmp_path), "--base-url", ""]) == 0
    assert "我创建于" in capsys.readouterr().out and model.messages == []
