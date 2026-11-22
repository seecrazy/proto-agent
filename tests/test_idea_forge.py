import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from proto.exec.webtools import WebToolkit
from proto.goals.idea_contract import DEFAULT_DISTANCE, candidate, distance, diverse_set, materials
from proto.goals.ideas import CRITIQUE, GENERATE, REGENERATE, REPAIR
from proto.runtime.agent import ProtoAgent
from proto.web.server import ProtoWebServer


def load_example():
    return json.loads(Path(__file__).with_name("fixtures").joinpath("idea_round.json").read_text(encoding="utf-8"))


def material_packet(case, evidence=None):
    rows = deepcopy(case["materials"])
    if evidence is None:
        for item in rows:
            item.pop("evidence_ref", None)
    return {"materials": rows, "candidates": deepcopy(case["candidates"])}


class Script:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.responses:
            expected, value = self.responses.pop(0)
            assert system == expected
            if callable(value):
                value = value()
            return json.dumps(value, ensure_ascii=False)
        if "目标分解器" in system:
            return "[]"
        raise AssertionError("unexpected model call")


@pytest.fixture
def case(monkeypatch):
    monkeypatch.delenv("PROTO_IDEA_POSTURE", raising=False)
    monkeypatch.delenv("PROTO_IDEA_DUPLICATE_DISTANCE", raising=False)
    return load_example()


def runner(path, case, *, now=lambda: 1000.0, fast=None, main=None):
    if fast is None:
        fast = Script([(GENERATE, material_packet(case, case["evidence"])),
                       (REGENERATE, case["regeneration"]), (REPAIR, case["revisions"])])
    if main is None:
        main = Script([(CRITIQUE, case["criticism"]), (CRITIQUE, case["recheck"])])
    agent = ProtoAgent(path, llm=main, fast_llm=fast, now=now, goal_interval_s=10**9,
                       webtools=WebToolkit(fetch=lambda *args: (200, "")))
    agent.persona = "iOS App 开发专家，熟悉本机测试与离线体验。"
    context = agent.goal_forge._context
    agent.goal_forge._context = lambda current: dict(context(current), evidence=case["evidence"])
    return agent


def test_demo_is_critically_reviewed_and_only_one_goal_runs(tmp_path, case):
    agent = runner(tmp_path, case)
    goal = agent.goal_forge.maybe_create(agent, "drive")
    report = agent.goal_forge.latest()
    assert report["posture"] == "EXPLORE" and report["calls"] == 5 and report["yield"] == 3
    assert [entry["id"] for entry in report["shortlist"]] == ["C1", "C2", "C4"]
    assert any(item["reason"] == "duplicate_sibling" and item["id"] == "C3" for item in report["discarded"])
    assert report["criticism"]["C2"][0]["principle"] == "P1"
    fixed = {item["id"]: item for item in report["qualified"]}
    assert fixed["C1"]["novelty"] == 0 and fixed["C1"]["duplicate_reason"]
    assert fixed["C2"]["failure_condition"] != case["candidates"][1]["failure_condition"]
    assert fixed["C4"]["required_inputs"] == []
    assert fixed["C4"]["signature"] == case["regeneration"][0]["signature"]
    assert agent.goal_tree.active_long_term() == [goal] and goal.source == "self"
    assert goal.acceptance == fixed[report["selected_id"]]["acceptance"]
    assert goal.idea_brief["first_concrete_action"] in agent.pop_inbox()[-1]
    assert goal.idea_brief["first_concrete_action"] in agent.llm_fast.calls[-1][1]
    assert len(agent.goal_tree.children_of_level(goal.id, "mid")) == 4
    assert [call[0] for call in agent.llm.calls] == [CRITIQUE, CRITIQUE]
    assert set(json.loads(agent.llm.calls[0][1])) == {"candidates", "materials", "labels"}
    assert isinstance(agent.goal_forge.latest()["materials"][4]["actor"], str)


@pytest.mark.parametrize("field,value,reason", [
    ("required_inputs", ["A", "B", "C"], "required_inputs"),
    ("failure_condition", "", "invalid_text"),
    ("first_concrete_action", "过一阵再看", "no_local_evidence"),
    ("core_insight", "它能省事。现在就做。", "insight_not_causal_sentence"),
    ("elements", {"H": "M5", "V": "M5"}, "no_cross_material_recombination"),
    ("elements", {"H": "M1", "W": "M2"}, "invalid_operator_elements"),
    ("failure_condition", "\ud800", "invalid_text_encoding"),
])
def test_required_structure(case, field, value, reason):
    sources = materials(case["materials"], case["evidence"])
    with pytest.raises(ValueError, match=reason):
        candidate(dict(case["candidates"][0], **{field: value}), sources, "C1")


def test_material_is_verbatim_and_evidence_is_not_invented(case):
    rows = case["materials"]
    checked = materials(rows, case["evidence"])
    assert checked["M5"]["basis"] == "hypothesis"
    assert checked["M10"]["basis"] == "observation"
    with pytest.raises(ValueError, match="unknown_material_evidence"):
        materials(rows, {})
    rows[0]["how"] = "重新归因"
    with pytest.raises(ValueError, match="material_not_verbatim"):
        materials(rows, case["evidence"])
    with pytest.raises(ValueError, match="material_count"):
        materials(rows[:-1], case["evidence"])


def test_distance_boundary_and_maximum_set(case):
    source = materials(case["materials"], case["evidence"])
    card = candidate(case["candidates"][0], source, "C1")
    handoff = candidate(case["candidates"][1], source, "C2")
    assert distance(card, handoff) == 1
    variant = dict(handoff, signature=dict(handoff["signature"], problem=card["signature"]["problem"]))
    assert distance(card, variant) == DEFAULT_DISTANCE
    assert len(diverse_set([card, variant], DEFAULT_DISTANCE, "EXPLORE", lambda item: 1)[0]) == 2
    assert len(diverse_set([card, variant], 0.81, "EXPLORE", lambda item: 1)[0]) == 1
    rival = dict(card, id="C3", core_insight="另一个方案", signature=dict(handoff["signature"], method="receipt"))
    third = dict(card, id="C4", core_insight="不同结构", signature=dict(card["signature"], method="compare"))
    selected, _ = diverse_set([card, rival, third], DEFAULT_DISTANCE, "EXPLORE",
                              lambda item: 10 if item is card else 1)
    assert selected == [rival, third]


def test_archive_rejects_rephrases_after_restart(tmp_path, case):
    first = runner(tmp_path, case)
    goal = first.goal_forge.maybe_create(first)
    new_packet = material_packet(case, case["evidence"])
    new_packet["candidates"][0]["core_insight"] = "分别展示两张回执，可以减少误以为测试运行的交付检查。"
    replacement = Script([(GENERATE, new_packet), (REGENERATE, [])])
    resumed = runner(tmp_path, case, now=lambda: 1901.0, fast=replacement, main=Script())
    assert resumed.goal_tree.get(goal.id).idea_brief == goal.idea_brief
    assert resumed.goal_forge.maybe_create(resumed) is None
    report = resumed.goal_forge.latest()
    assert report["yield"] == 0 and len(report["discarded"]) == 3
    assert all(row["reason"] == "duplicate_history" for row in report["discarded"])
    assert resumed.goal_tree.active_long_term()[0].id == goal.id
    assert not resumed.llm.calls


def test_merge_does_not_let_repair_overwrite_uncriticized_fields_or_introduce_violation(tmp_path, case):
    case["revisions"][0]["changes"]["target_context"] = "换成另一类人"
    case["recheck"] = [{"id": "C4", "issues": [
        {"principle": "P1", "fields": ["failure_condition"], "criticism": "修正后出现新的停止条件冲突"}]}]
    agent = runner(tmp_path, case)
    assert agent.goal_forge.maybe_create(agent)
    report = agent.goal_forge.latest()
    assert report["yield"] == 1
    assert {row["reason"] for row in report["discarded"]} >= {
        "revision_changed_uncriticized_fields", "criticism_unresolved"}


def test_review_missing_candidate_fails_closed_and_throttles_both_triggers(tmp_path, case):
    case["criticism"].pop()
    agent = runner(tmp_path, case)
    assert agent.goal_forge.maybe_create(agent, trigger="schedule") is None
    assert agent.goal_forge.latest()["reason"] == "review_coverage"
    assert agent.goal_forge.maybe_create(agent, trigger="drive") is None
    assert agent.goal_forge.maybe_create(agent, trigger="schedule") is None
    assert len(agent.llm_fast.calls) == 2 and len(agent.llm.calls) == 1
    assert not agent.goal_tree.active_long_term()


def test_tradeoff_cannot_ignore_earlier_principle(tmp_path, case):
    revision = case["revisions"][1]
    revision["changes"] = {"why_existing_fails": revision["changes"]["why_existing_fails"]}
    revision["tradeoff"] = "为了修正 P3，先不减少用户必填项"
    case["recheck"] = [case["recheck"][0]]
    agent = runner(tmp_path, case)
    assert agent.goal_forge.maybe_create(agent)
    report = agent.goal_forge.latest()
    assert report["yield"] == 2
    assert any(item["reason"] == "revision_principle_priority" for item in report["discarded"])


def test_mode_change_during_critique_does_not_nominate(tmp_path, case):
    def cancel():
        agent.set_fleet_paused(True)
        return case["criticism"]

    agent = runner(tmp_path, case, main=Script([(CRITIQUE, cancel)]))
    assert agent.goal_forge.maybe_create(agent) is None
    assert agent.goal_forge.latest()["status"] == "cancelled"
    assert agent.goal_forge.latest()["calls"] == 3
    assert not agent.goal_tree.active_long_term()


def test_provider_error_has_safe_reason_and_cannot_replay(tmp_path, case):
    def timeout():
        raise TimeoutError("secret from provider must not be retained")

    agent = runner(tmp_path, case, main=Script([(CRITIQUE, timeout)]))
    assert agent.goal_forge.maybe_create(agent) is None
    report = agent.goal_forge.latest()
    assert report["status"] == "failed" and report["reason"] == "model_timeout"
    assert "secret from provider" not in json.dumps(report)
    assert not agent.goal_tree.active_long_term()
    agent.goal_forge._save_archive(dict(report, status="running"))
    resumed = runner(tmp_path, case)
    assert resumed.goal_forge.summary()["status"] == "interrupted"
    assert resumed.goal_forge.maybe_create(resumed) is None and not resumed.llm_fast.calls


def test_final_gamble_prefers_largest_predicted_range(tmp_path, case, monkeypatch):
    monkeypatch.setenv("PROTO_IDEA_POSTURE", "FINAL_GAMBLE")
    agent = runner(tmp_path, case)
    assert agent.goal_forge.maybe_create(agent)
    report = agent.goal_forge.latest()
    assert report["posture"] == "FINAL_GAMBLE" and report["selected_id"] == "C4" and report["yield"] == 3
    assert max(item["outcome_variance"] for item in report["qualified"]) == 4


def test_exploit_only_changes_first_step_of_best_unlaunched_idea(tmp_path, case, monkeypatch):
    first = runner(tmp_path, case)
    assert first.goal_forge.maybe_create(first)
    archive = [row for row in first.goal_forge._history if not row["nominated"]]
    packet = material_packet(case, case["evidence"])
    packet.pop("candidates")
    packet["experiment"] = {"first_concrete_action": "把首次本地试验缩为一个有返回值的状态对照测试。",
                            "rollback": "删掉临时测试文件，不改变用户数据。", "duplicate_reason": "同一假设只缩小初验。"}
    monkeypatch.setenv("PROTO_IDEA_POSTURE", "EXPLOIT")
    second = runner(tmp_path, case, now=lambda: 1901.0, fast=Script([(GENERATE, packet)]),
                    main=Script([(CRITIQUE, [{"id": "C1", "issues": []}])]))
    goal = second.goal_forge.maybe_create(second)
    report = second.goal_forge.latest()
    assert goal and report["yield"] == 1 and report["posture"] == "EXPLOIT"
    variant = report["qualified"][0]
    base = next(row["idea"] for row in archive if row["key"] == variant["variant_of"])
    assert base["quality"] == max(row["idea"]["quality"] for row in archive)
    assert variant["novelty"] == 0 and variant["duplicate_reason"]
    assert variant["core_insight"] == base["core_insight"]
    assert goal.idea_brief["first_concrete_action"] == packet["experiment"]["first_concrete_action"]


def test_exploit_without_base_waits_without_model_calls(tmp_path, case, monkeypatch):
    monkeypatch.setenv("PROTO_IDEA_POSTURE", "EXPLOIT")
    agent = runner(tmp_path, case)
    assert agent.goal_forge.maybe_create(agent) is None
    assert agent.goal_forge.latest()["reason"] == "no_exploit_base"
    assert not agent.llm.calls and not agent.llm_fast.calls


def test_summary_and_full_report_are_available_read_only(tmp_path, case):
    from tests.test_web import call

    agent = runner(tmp_path, case)
    assert agent.goal_forge.maybe_create(agent)
    server = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    original_calls = len(agent.llm.calls) + len(agent.llm_fast.calls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, summary = call(server, "GET", "/api/goaltree", token=server.token)
        assert status == 200 and summary["idea_round"]["yield"] == 3
        assert "materials" not in summary["idea_round"]
        assert any(node.get("idea_brief", {}).get("failure_condition") for node in summary["nodes"])
        status, full = call(server, "GET", "/api/goaltree?ideas=latest", token=server.token)
        assert status == 200 and full["idea_round"]["materials"][0]["id"] == "M1"
        assert len(agent.llm.calls) + len(agent.llm_fast.calls) == original_calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
