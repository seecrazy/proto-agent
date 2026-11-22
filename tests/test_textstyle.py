import json
import threading
from dataclasses import replace

import pytest

from proto import run
from proto.exec.contracts import ParameterError
from proto.llm.client import LLMClient
from proto.metacog.attribution import HeuristicAttributor
from proto.runtime.agent import ProtoAgent
from proto.system.execution_log import execution_logging
from proto.textstyle import MAX_TEXT, polish_style, scan_style
from proto.textstyle import rewrite, scanner
from proto.web.server import ProtoWebServer
from tests.test_web import call


def ids(report):
    return [hit["pattern_id"] for hit in report["hits"]]


@pytest.mark.parametrize("text", [
    "",
    "  \t\r\n\r\n好，我在想要不要先等你回复。　\r\n",
    "如果这个改动要和上周发布给客户的版本同时维护，我会先对照实际调用者留下的日志确认影响面，再决定这次是出补丁还是只给一个临时规避方法。",
    "当然可以！",
    "编译、测试、归档，三个步骤都过了。",
    "Looks good. Please leave this sentence length and spacing alone.  ",
])
def test_no_hits_is_exact_noop(text):
    report = polish_style(text)
    assert report["status"] == "passed" and report["attempts"] == 0
    assert report["final_text"] == report["rewritten_text"] == text
    assert report["changes"] == report["facts_added"] == []
    assert report["scan_before"] == report["scan_after"] == scan_style(text)
    assert report["scan_before"]["total_score"] == 100
    assert report["scan_before"]["verdict"] == "这段不需要去模板腔"
    assert report["scan_before"]["instruction"] == "不要把长句拆短，不要重排段落，不要改语序"


def test_report_locations_are_source_coordinates_and_rewrites_only_match():
    before = ("🤖第一次提交已留档。\r\n\r\n我只复制了「需要说明的是：尚未验收。」这句话。\r\n\r\n"
              "我核对过日志。  值得注意的是，王青 2026 年 11 月 22 日在杭州查了 2 个项目，1 个没通过。\r\n")
    scan = scan_style(before)
    assert scan == scan_style(before) and scan == json.loads(json.dumps(scan))
    hit, = scan["hits"]
    assert (hit["pattern_id"], hit["location"], hit["matched_text"], hit["severity"]) == (
        "P01", "第3段第2句", "值得注意的是，", "high")
    assert scan["verdict"] == "需要改写" and scan["total_score"] == 100 - hit["deduction"]
    result = polish_style(before)
    assert result["final_text"] == before[:hit["start"]] + before[hit["end"]:]
    change, = result["changes"]
    assert change["original"] == before[change["start"]:change["end"]] == hit["matched_text"]
    assert change["rewritten"] == "" and "P01" in change["rule_applied"]
    assert set(hit["prescription"]) == {"primary", "alternative", "forbidden"}
    assert result["scan_after"] == scan_style(result["final_text"])


@pytest.mark.parametrize("text", [
    "```text\n值得注意的是，这里原样输出。\n希望以上内容对你有所帮助。\n```",
    "~~~~text\n值得注意的是，代码里有反引号。\n```\n综上所述，一个字也不变。\n~~~~",
    "原稿是这样的。\n\n```\n值得注意的是，未关闭的代码块也保护。",
    "> 文档写道：\n值得注意的是，这是同一段引用。\n希望以上内容对你有所帮助。",
    "- 实际步骤：\n  首先保留行格式。\n值得注意的是，这行也是列表续句。",
    "项目 | 结果 | 备注\n-- | -- | --\n验收 | 未通过 | 值得注意的是，不发布。",
    "    值得注意的是，这行是缩进代码。",
    "“第一句。\n综上所述，这里仍在同一个引文里。\n希望以上内容对你有所帮助。”",
    '不改 `需要说明的是：原命令`，也不改 "值得注意的是，引用中的数字 42"。',
    '[值得注意的是，原链接](https://example.invalid/?x=1)。',
    ' {"reply":"需要说明的是，原来是这个样子。","n":3} ',
    ' ["保持数据原样。", "希望以上内容对你有所帮助。"] ',
    "这里是输出。\n\n<data>值得注意的是，不能动。\n希望以上内容对你有所帮助。</data>",
])
def test_protected_regions_not_rewritten_or_scored(text):
    result = polish_style(text)
    assert result["attempts"] == 0
    assert result["final_text"] == text and result["scan_before"]["hits"] == []


def test_prefix_rewrite_preserves_exact_inline_evidence():
    body = '先别急。`git status -sb` 显示工作区干净。「下一步检查日志。」是原记录。'
    result = polish_style("需要说明的是，" + body + "这，才是关键。")
    assert result["status"] == "passed"
    assert result["final_text"] == body
    assert ids(result["scan_before"]) == ["P01", "P11"]


@pytest.mark.parametrize("pattern_id,text", [
    ("P02", "在当今这个快速变化的时代，我昨晚没来得及看日志。"),
    ("P03", "以下是为你整理的说明：我还差一次真机测试。"),
    ("P04", "先把页面关了。展望未来，一切皆有可能。"),
    ("P05", "这不仅是提交，也是临时修复。不是编译失败，而是测试失败。"),
    ("P06", "我刚看过日志——这段日志没有报错——你可以继续。"),
    ("P07", "这个月既要压缩流程，又要打磨体验，还要提升影响力。"),
    ("P08", "全链路赋能并形成价值闭环。"),
    ("P09", "对这个系统的当前版本的使用方式的定位的进一步梳理的目标，是确定新用户一开始应该先看哪个页面。"),
    ("P10", "今天查过日志了。\n\n现在只等你回复。\n\n晚上会整理记录。\n\n明早我再跑一次。"),
])
def test_configured_patterns_detect_evidence(pattern_id, text):
    scan = scan_style(text)
    assert pattern_id in ids(scan)
    for hit in scan["hits"]:
        assert hit["matched_text"] == text[hit["start"]:hit["end"]]


def test_profiles_make_different_decisions():
    text = "综上所述，明天公告仍按原计划发。"
    assert ids(scan_style(text, "dialogue")) == ids(scan_style(text, "technical")) == ["P01"]
    assert scan_style(text, "formal")["hits"] == scan_style(text, "creative")["hits"] == []
    contrived = "以下是为你整理的回答：先看日志。"
    assert ids(scan_style(contrived, "creative")) == ["P03"]
    with pytest.raises(ValueError, match="invalid_style_input"):
        scan_style(contrived, "invalid")


def test_safe_contrast_and_dashes_keep_only_source_words():
    text = "不是套话，而是我刚看过日志。你可以继续——我已确认结果——我来补回执。"
    result = polish_style(text)
    assert result["status"] == "passed" and result["facts_added"] == []
    assert result["final_text"] == "我刚看过日志。你可以继续，我已确认结果，我来补回执。"
    assert ids(result["scan_after"]) == []


@pytest.mark.parametrize("text", [
    "不是编译失败，而是还没运行编译。",
    "设备从下午 3:00——5:00 下线——下午 5 点后恢复。",
    "成都——上海——北京三站都停。",
    "随着社会的不断发展，大家转向新的通勤方式。",
])
def test_factful_contrast_ranges_and_causal_explanations_roll_back(text):
    result = polish_style(text)
    assert result["status"] == "manual_required" and result["message"] == "需人工处理"
    assert result["attempts"] == 2 and result["final_text"] == text
    assert [item["reason"] for item in result["verification"]] == ["hits_not_reduced"] * 2
    assert result["changes"] == [] and result["scan_before"] == result["scan_after"]


def test_low_confidence_residual_can_stay_when_safe_edit_improves_report():
    result = polish_style("值得注意的是，我确认过记录。不是编译失败，而是还没编译。")
    assert result["status"] == "passed"
    assert result["final_text"] == "我确认过记录。不是编译失败，而是还没编译。"
    assert ids(result["scan_after"]) == ["P05"]


@pytest.mark.parametrize("text,first_rejection", [
    ("值得注意的是，总的来说，我已经保存了日志。", "hits_not_reduced"),
    ("值得注意的是，以下是为你整理的回答：我已经保存了日志。希望以上内容对你有帮助。",
     "rewrite_still_needed"),
])
def test_two_attempts_rescan_on_same_engine_and_preserve_safe_tail_removal(monkeypatch, text, first_rejection):
    scans = []
    original_scan = scanner.Scanner.scan

    def trace(instance, candidate, profile):
        scans.append((instance, candidate, profile))
        return original_scan(instance, candidate, profile)

    monkeypatch.setattr(scanner.Scanner, "scan", trace)
    report = polish_style(text)
    assert report["status"] == "passed" and report["attempts"] == 2
    assert report["verification"][0]["reason"] == first_rejection
    assert report["final_text"] == "我已经保存了日志。"
    assert len(scans) == 3 and all(row[0] is scans[0][0] and row[-1] == "dialogue" for row in scans)
    assert scans[0][1] == text and scans[-1][1] == report["final_text"]
    assert report["scan_before"]["rules_version"] == report["scan_after"]["rules_version"]


@pytest.mark.parametrize("bad_replacement,reason", [
    ({"replacement": "赵清于 2030 年 6 月在北京验证通过。"}, "facts_added"),
    ({"end": 1}, "invalid_edit"),
])
def test_reject_unapproved_candidate_even_if_scanner_would_like_it(monkeypatch, bad_replacement, reason):
    text = "先保留这一句。值得一提的是，我还没完成测试。"
    proposal = rewrite._propose_edits

    def corrupt(text, hits, attempt, leads):
        edit, = proposal(text, hits, attempt, leads)
        if bad_replacement.get("end") == 1:
            return [replace(edit, start=0, end=1)]
        return [replace(edit, **bad_replacement)]

    monkeypatch.setattr(rewrite, "_propose_edits", corrupt)
    result = polish_style(text)
    assert result["status"] == "manual_required" and result["attempts"] == 2
    assert [step["reason"] for step in result["verification"]] == [reason] * 2
    assert result["final_text"] == text and result["facts_added"] == []


@pytest.mark.parametrize("text,reason", [
    ("原样。" * MAX_TEXT, "text_limit"),
    ("值得注意的是，我看过日志。" * (scanner.MAX_HITS + 1), "hit_limit"),
])
def test_bounded_scan_never_pretends_to_be_clean(text, reason):
    report = polish_style(text)
    assert report["status"] == "manual_required" and report["attempts"] == 0
    assert report["final_text"] == text
    scan = report["scan_before"]
    assert (scan["complete"], scan["reason"], scan["total_score"]) == (False, reason, None)


def test_missing_rule_resource_leaves_text_verbatim(monkeypatch):
    def unavailable():
        raise FileNotFoundError("private file location")

    monkeypatch.setattr(scanner, "_bundled_scanner", unavailable)
    report = polish_style("值得一提的是，我刚查过这里。")
    assert report["status"] == "manual_required"
    assert report["scan_after"]["reason"] == "rules_unavailable"
    assert "private" not in json.dumps(report)


def test_read_only_skill_and_tools_need_no_model_network_or_subprocess(tmp_path, monkeypatch):
    agent = ProtoAgent(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("style tools are local and deterministic")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr(agent, "_complete", forbidden)
    tools = agent.tools
    args = {"text": "值得一提的是，我刚查过这里。", "profile": "technical"}
    for name in ("scan_style", "polish_style"):
        assert tools.definition(name)["category"] == "read_draft"
        assert tools.validate(name, args) == args
        with pytest.raises(ParameterError):
            tools.validate(name, {**args, "profile": "not-a-profile"})
    assert ids(tools.get("scan_style")(**args)) == ["P01"]
    assert tools.get("polish_style")(**args)["final_text"] == "我刚查过这里。"
    assert "polish_style" in tools.get("read_skill")("natural-chinese-dialogue")["instructions"]


class DraftModel(LLMClient):
    def __init__(self):
        self.calls = []
        self.draft = "值得注意的是，老李的 2 个测试都没跑完。希望以上内容对你有所帮助。"

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.draft

    def describe(self):
        return "local draft fixture"


def fake_agent(tmp_path):
    model = DraftModel()
    agent = ProtoAgent(tmp_path, llm=model, persona="值班工程师，说话简明但不敷衍", goal_interval_s=0)
    agent.attributor = HeuristicAttributor()
    return agent, model


@pytest.mark.parametrize("question", ["原样输出上一句", "引用以下这句话", "写几句台词", "返回严格的 JSON", "起草一份公文"])
def test_owner_intent_overrides_default_polishing(tmp_path, question):
    agent, model = fake_agent(tmp_path)
    model.draft = "综上所述，下周的通知仍有效。"
    assert agent.chat(question) == model.draft and len(model.calls) == 1


def test_repl_prints_and_remembers_final_reply(tmp_path, monkeypatch, capsys):
    agent, model = fake_agent(tmp_path)
    turns = iter(["帮我说一下还剩什么", "/quit"])
    memory = []
    monkeypatch.setattr(run, "create_llm_stack", lambda: None)
    monkeypatch.setattr(run, "ProtoAgent", lambda **kwargs: agent)
    monkeypatch.setattr(agent, "remember_turn", lambda text, reply: memory.append((text, reply)))
    monkeypatch.setattr(agent, "maybe_proactive", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: next(turns))
    assert run.main(["--data-dir", str(tmp_path), "--base-url", ""]) == 0
    expected = "老李的 2 个测试都没跑完。"
    assert f"PROTO> {expected}" in capsys.readouterr().out
    assert memory == [("帮我说一下还剩什么", expected)]
    assert agent._load_history()[-1] == {"role": "agent", "text": expected}
    assert len(model.calls) == 1 and "值班工程师" in model.calls[0][0]


def test_web_live_reply_has_no_extra_request_and_persists_verified_text(tmp_path, monkeypatch):
    agent, model = fake_agent(tmp_path / "agent")
    remembered = []
    memory_ready = threading.Event()

    def remember(text, reply, *args):
        remembered.append(reply)
        memory_ready.set()

    monkeypatch.setattr(agent, "remember_dialogue", remember)
    server = ProtoWebServer(agent, port=0, heartbeat_s=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    message = {"text": "现在还有什么没验证？顺便说下你的判断。"}
    expected = {"reply": "老李的 2 个测试都没跑完。"}
    try:
        with execution_logging(tmp_path, "web"), server.manager.ops_lock(agent.fleet_id):
            assert call(server, "POST", "/api/chat", message, token=server.token, request_key="textstyle") == (200, expected)
            assert call(server, "POST", "/api/chat", message, token=server.token, request_key="textstyle") == (200, expected)
            status, identity = call(server, "POST", "/api/chat", {"text": "你存在多久了？"}, token=server.token)
            assert status == 200 and identity["read_only"]
            history = server.manager.runs.dialogue.history(agent.fleet_id, 8)
            assert history == [{"role": "owner", "text": message["text"]}, {"role": "agent", "text": expected["reply"]}]
            assert len(model.calls) == 1 and "[后台工作事实" in model.calls[0][1]
        assert memory_ready.wait(4)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=4)
    assert remembered == [expected["reply"]]
    content = (tmp_path / ".runtime-logs" / "execution.jsonl").read_text()
    style, = (row for line in content.splitlines() if (row := json.loads(line))["event"] == "dialogue_style_checked")
    assert (style["status"], style["attempt"], style["hits_before"], style["hits_after"]) == ("passed", 1, 2, 0)
    assert style["rules_version"] == "zh-style-1"
    assert all(private not in content for private in ("老李", "值得注意", message["text"]))


def test_unavailable_style_check_does_not_misreport_model_as_offline(tmp_path, monkeypatch):
    agent, model = fake_agent(tmp_path)

    def unavailable(*args, **kwargs):
        raise RuntimeError("secret diagnostic")

    monkeypatch.setattr("proto.runtime.dialogue.polish_style", unavailable)
    assert agent.chat("进度怎样了？给我个判断。") == model.draft
    assert len(model.calls) == 1
