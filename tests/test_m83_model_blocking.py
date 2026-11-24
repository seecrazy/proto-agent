# -*- coding: utf-8 -*-
"""M83：为什么总出现「模型或额度受阻」。

生产实证（2026-11-24）：专家 2h45m 在干活、2h35m 在等模型（占 48%）。逐层拆开：
- 模型组里两个接入返回 429/1310「已达到每周/每月使用上限，11-28 重置」——不是瞬时限流，
  却被当成限流每半小时重试一次，还把同一个主机上的健康接入一起冷却；
- 限流按「主机」连带冷却：六个接入都在 open.bigmodel.cn，主接入一限流，备用接入全部陪绑，
  「自动切换」形同虚设；
- 模型输出截断被归为不可重试，任务停下等 10 分钟，界面就显示「模型或额度受阻」；
- 默认日预算（20 万 token、2000 次调用）不够一个 24 步工程循环跑两片。
"""
import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from proto.coordination.coordination import CoordinationDB
from proto.llm.breaker import GROUP_FAILOVER_REASONS, TRANSIENT_MODEL_REASONS, describe_model_failure
from proto.llm.catalog import ModelCatalog
from proto.llm.client import LLMError, ModelPressureWait, _http_failure
from proto.runtime.model_pressure import ModelPressure
from tests.test_m64_live_models import profile
from tests.test_m76_model_groups import complete, configured, provider  # noqa: F401  (fixtures)
from tests.test_m71_continuity import WRITE
from tests.test_model_decision_transport import (  # noqa: F401  (fixtures/helpers)
    LOOP_SYSTEM, anthropic_decision, openai_delta, openai_tool, sse, wire)

QUOTA = {"error": {"code": 1310, "message": "您已达到每周/每月使用上限，您的限额将在 2026-11-28 18:59:13 重置。"}}


def http_error(status, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    return urllib.error.HTTPError("https://provider.invalid/v1/x", status, "private", headers or {}, io.BytesIO(body))


def reset_epoch(text="2026-11-28 18:59:13"):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def classify(status, payload, headers=None):
    """调用点写的是 `raise _http_failure(exc) from exc`；这里照原样接上因果链再断言。"""
    error = http_error(status, payload, headers)
    failure = _http_failure(error)
    failure.__cause__ = error
    return failure


# ---- 供应方错误分类 ---------------------------------------------------------

def test_weekly_cap_429_is_quota_exhausted_until_the_reset_time():
    failure = classify(429, QUOTA)
    assert isinstance(failure, ModelPressureWait) and failure.reason == "model_quota_exhausted"
    assert abs(failure.retry_at - reset_epoch()) < 2
    assert describe_model_failure(failure).reason == "model_quota_exhausted"


def test_balance_429_without_reset_time_waits_hours_not_seconds():
    failure = classify(429, {"error": {"code": 1113, "message": "余额不足，请充值 PRIVATE-MARKER"}})
    assert isinstance(failure, ModelPressureWait) and failure.reason == "model_quota_exhausted"
    assert failure.retry_at - time.time() > 5 * 3600 and "PRIVATE-MARKER" not in str(failure)


def test_anthropic_style_quota_error_is_recognised_too():
    body = {"type": "error", "error": {"type": "rate_limit_error",
                                       "message": "您已达到 5 小时使用上限，将在 2026-12-01 09:30:00 重置"}}
    failure = classify(429, body)
    assert isinstance(failure, ModelPressureWait) and failure.reason == "model_quota_exhausted"
    assert abs(failure.retry_at - reset_epoch("2026-12-01 09:30:00")) < 2


def test_transient_rate_limit_keeps_its_code_but_not_the_private_message():
    limited = classify(429, {"error": {"code": 1302, "message": "您当前使用该API的并发数过高 PRIVATE-MARKER"}},
                       {"Retry-After": "90"})
    assert isinstance(limited, LLMError) and not isinstance(limited, ModelPressureWait)
    described = describe_model_failure(limited)
    assert described.reason == "model_rate_limited" and described.http_status == 429
    assert limited.provider_code == "1302" and described.provider_code == "1302"
    assert "PRIVATE-MARKER" not in str(limited) and "PRIVATE-MARKER" not in json.dumps(described.__dict__)
    assert describe_model_failure(classify(503, b"<html>private provider response</html>")).reason == "model_server_error"
    assert describe_model_failure(classify(401, {"error": {"code": 1000, "message": "private"}})).reason == "model_unauthorized"


def test_quota_is_failover_eligible_but_never_auto_retried_and_truncation_retries_quickly():
    assert "model_quota_exhausted" in GROUP_FAILOVER_REASONS and "model_quota_exhausted" not in TRANSIENT_MODEL_REASONS
    assert "model_output_truncated" in TRANSIENT_MODEL_REASONS       # 循环已切换小步决策，立刻重试即可


# ---- 冷却表：额度用尽只隔离该接入，且自称其名 -------------------------------

def test_quota_quarantine_is_per_route_and_names_itself(tmp_path):
    clock = [time.time()]
    pressure = ModelPressure(CoordinationDB(tmp_path), now=lambda: clock[0])
    until = clock[0] + 3 * 86400
    wait = pressure.quarantine("route-a", until, origin="origin-shared")
    assert wait.reason == "model_quota_exhausted" and wait.retry_at == until
    assert pressure.snapshot("route-a") == {"state": "cooldown", "waiting": True,
                                           "reason": "model_quota_exhausted", "retry_at": until}
    assert pressure.snapshot("sibling", origin="origin-shared") is None       # 不连带同源的其他接入
    with pytest.raises(ModelPressureWait) as raised:
        with pressure.admit("route-a", origin="origin-shared"):
            pass
    assert raised.value.reason == "model_quota_exhausted" and raised.value.retry_at == until
    pressure.limited("route-b", http_error(429, {"error": {"code": 1302, "message": "busy"}}), origin="origin-shared")
    assert pressure.snapshot("route-b")["reason"] == "model_rate_limited"     # 普通限流仍叫限流
    reopened = ModelPressure(CoordinationDB(tmp_path), now=lambda: clock[0])
    assert reopened.snapshot("route-a")["reason"] == "model_quota_exhausted"  # 重启后仍说得出自己是哪种冷却
    clock[0] = until + 1
    pressure.succeeded("route-a", origin="origin-shared")
    assert pressure.snapshot("route-a") is None


def test_rate_limit_still_cools_every_model_on_the_same_key(tmp_path):
    clock = [time.time()]
    pressure = ModelPressure(CoordinationDB(tmp_path), now=lambda: clock[0])
    pressure.limited("main-model", http_error(429, {"error": {"code": 1302, "message": "busy"}}, {"Retry-After": "75"}),
                     origin="same-account")
    assert 74 <= pressure.snapshot("fast-model", origin="same-account")["retry_at"] - clock[0] <= 75


def test_origin_is_host_and_credential_not_host_alone(tmp_path):
    same_key = ModelCatalog._bind(profile("m1", "https://h.invalid/v1", api_key="k1", model="big"))
    same_key_fast = ModelCatalog._bind(profile("m2", "https://h.invalid/v1", api_key="k1", model="small"))
    other_key = ModelCatalog._bind(profile("m3", "https://h.invalid/v1", api_key="k2", model="big"))
    assert same_key.pressure_origin == same_key_fast.pressure_origin != other_key.pressure_origin


# ---- 模型组：死接入不再拖累健康接入 ------------------------------------------

def test_dead_member_is_quarantined_and_the_live_member_takes_over(tmp_path, provider):
    def capped(request, **options):
        raise http_error(429, QUOTA)

    provider.responses.update(dead=capped, live="live-online")
    fleet, agent, _ = configured(tmp_path, profile("dead", "https://same.invalid/one", api_key="key-dead"),
                                 profile("live", "https://same.invalid/two", api_key="key-live"))
    assert complete(agent) == "live-online"                 # 第一次：只问了一次 dead，立刻切到 live
    assert provider.models == ["dead", "live"]
    assert complete(agent) == "live-online"                 # 之后 dead 直接跳过，不再发请求
    assert provider.models == ["dead", "live", "live"]
    notes = [text for text in agent._inbox if "额度" in text]
    assert len(notes) == 1 and "11-28" in notes[0] and "dead" in notes[0]
    assert "PRIVATE" not in notes[0] and "key-dead" not in notes[0]
    provider.responses["live"] = (429, "40")
    with pytest.raises(ModelPressureWait) as blocked:
        complete(agent)
    assert blocked.value.reason in ("model_rate_limited", "model_group_unavailable")
    assert complete  # (keep import used)


def test_all_members_capped_parks_for_the_owner_with_an_honest_reason(tmp_path, provider):
    def capped(request, **options):
        raise http_error(429, QUOTA)

    provider.responses.update(a=capped, b=capped)
    fleet, agent, _ = configured(tmp_path, profile("a", "https://one.invalid/v1", api_key="ka"),
                                 profile("b", "https://two.invalid/v1", api_key="kb"))
    with pytest.raises(ModelPressureWait) as blocked:
        complete(agent)
    assert blocked.value.reason == "model_quota_exhausted"          # 不是「稍后重试」，等下去不会好
    assert agent.model_status()["reason"] == "model_quota_exhausted"


def test_a_rate_limited_key_does_not_cool_down_another_key_on_the_same_host(tmp_path, provider):
    provider.responses.update(primary=(429, "120"), alias="alias-online")
    fleet, agent, _ = configured(tmp_path, profile("primary", "https://same.invalid/one", api_key="key-one"),
                                 profile("alias", "https://same.invalid:443/two", api_key="key-two"))
    assert complete(agent) == "alias-online"                 # 备用接入真的顶上，而不是陪绑
    assert provider.models == ["primary", "alias"]


# ---- 表达与默认值 -----------------------------------------------------------

def test_activity_text_explains_quota_exhaustion():
    from proto.fleet.activity import REASONS

    assert "额度" in REASONS["model_quota_exhausted"] and "限流" in REASONS["model_rate_limited"]
    assert REASONS["model_quota_exhausted"] != REASONS["model_rate_limited"]


def test_default_daily_budget_can_sustain_an_engineering_loop():
    from proto.runtime.agent import DEFAULT_POOLS

    assert DEFAULT_POOLS["owner_tasks"]["tokens"] >= 2_000_000      # 24 步 × 约 7k token 的一片 ≈ 17 万
    assert DEFAULT_POOLS["global"]["tokens"] >= DEFAULT_POOLS["owner_tasks"]["tokens"] * 2
    assert DEFAULT_POOLS["global"]["calls"] >= 20_000


# ---- 额度：旧默认一次性升级，主人改过的不动 ---------------------------------

def store_for(folder):
    from proto.storage.store import FileSystemStore

    return FileSystemStore(folder)


def saved_budget(folder, caps, remaining, today):
    store_for(folder).put("kernel_budget", "main", {"cycle_date": today, "caps": caps, "remaining": remaining})
    return store_for(folder)


def test_agent_still_on_the_old_default_budget_is_upgraded_once(tmp_path):
    from datetime import date

    from proto.kernel.budgets import DEFAULT_POOLS, LEGACY_DEFAULT_POOLS, BudgetPools

    today = date.today().isoformat()
    caps = {"global": {"tokens": 200000.0, "wallclock_s": 86400.0, "calls": 2000.0},
            "owner_tasks": {"tokens": 160000.0}, "self_initiated": {"tokens": 40000.0}}
    # 今天已用：全局 18 万 token / 1990 次调用，主人任务 15 万，自发 1 万
    left = {"global": {"tokens": 20000.0, "wallclock_s": 82800.0, "calls": 10.0},
            "owner_tasks": {"tokens": 10000.0}, "self_initiated": {"tokens": 30000.0}}
    pools = BudgetPools(LEGACY_DEFAULT_POOLS, store=saved_budget(tmp_path, caps, left, today))
    assert pools.legacy_upgraded is True
    remaining = pools.remaining_all()
    assert remaining["global"]["calls"] == DEFAULT_POOLS["global"]["calls"] - 1990      # 新出的额度补进来，花掉的不退
    assert remaining["owner_tasks"]["tokens"] == DEFAULT_POOLS["owner_tasks"]["tokens"] - 150000
    again = BudgetPools(DEFAULT_POOLS, store=store_for(tmp_path))
    assert again.legacy_upgraded is False and again.remaining_all() == remaining       # 只升一次
    assert store_for(tmp_path).get("kernel_budget", "main")["caps"]["global"]["calls"] == DEFAULT_POOLS["global"]["calls"]


def test_owner_customized_budget_is_never_touched(tmp_path):
    from datetime import date

    from proto.kernel.budgets import LEGACY_DEFAULT_POOLS, BudgetPools

    today = date.today().isoformat()
    caps = {"global": {"tokens": 50000000.0, "wallclock_s": 86400.0, "calls": 2000.0},
            "owner_tasks": {"tokens": 20000000.0}, "self_initiated": {"tokens": 30000000.0}}
    left = {"global": {"tokens": 44100000.0, "wallclock_s": 86300.0, "calls": 743.0},
            "owner_tasks": {"tokens": 14100000.0}, "self_initiated": {"tokens": 30000000.0}}
    pools = BudgetPools(LEGACY_DEFAULT_POOLS, store=saved_budget(tmp_path, caps, left, today))
    assert pools.legacy_upgraded is False and pools.remaining_all()["global"]["calls"] == 743   # 主人的 2000 次上限照旧


def test_budget_blocked_text_names_the_pool_and_how_to_clear_it(tmp_path):
    from proto.fleet.activity import budget_detail
    from proto.runtime.agent import ProtoAgent

    agent = ProtoAgent(tmp_path, goal_interval_s=0)
    assert budget_detail(agent, "budget_exhausted:calls") == ""            # 额度充足时不多嘴
    agent.kernel.pools.set_cap("global", "calls", 2000)
    with agent.kernel.pools._lock:
        agent.kernel.pools._remaining["global"]["calls"] = 0
        agent.kernel.pools._save()
    detail = budget_detail(agent, "budget_exhausted:calls")
    assert "全局的调用次数已用完（上限 2000）" in detail and "不是模型套餐" in detail
    assert budget_detail(agent, "fleet_budget_exhausted:tokens").startswith("舰队")
    assert budget_detail(agent, "model_rate_limited") == ""


def test_cache_read_tokens_are_reported_for_diagnosis(wire):
    from proto.llm.client import AnthropicCompatClient
    from tests.test_model_decision_transport import LOOP_SYSTEM, anthropic_decision

    frames = anthropic_decision(WRITE)
    frames[-2] = sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                      "usage": {"output_tokens": 11, "cache_read_input_tokens": 1536}})
    wire.replies.append(frames)
    client = AnthropicCompatClient(wire.url, "test-key", execution_mode="native")
    client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")
    assert client.last_usage == {"input_tokens": 20, "output_tokens": 11, "cache_read": 1536}


def test_model_calling_a_real_tool_name_instead_of_the_envelope_is_recovered(wire):
    # 部署后仅剩的 invalid_response 形态：模型直接调了 write_file 而不是 proto_decision。
    # 它的参数就是决策本身——接受；解析不出决策才保持协议错误。
    from proto.llm.client import OpenAICompatClient
    from proto.llm.decision import parse_decision

    arguments = json.dumps({"tool": "write_file", "args": WRITE["args"], "category": "workspace_write"},
                           ensure_ascii=False)
    frames = [openai_tool(arguments, name="write_file", identifier="first"),
              openai_delta({}, "tool_calls"), sse("[DONE]")]
    wire.replies.append(frames)
    client = OpenAICompatClient(wire.url, "test-key", execution_mode="native")
    decision = parse_decision(client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None,
                                                       cancelled=lambda: ""))
    assert decision["tool"] == "write_file" and decision["args"] == WRITE["args"]

    anthropic = [sse({"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 4}}}),
                 sse({"type": "content_block_start", "index": 0, "content_block": {
                     "type": "tool_use", "id": "x", "name": "edit_file", "input": WRITE}}),
                 sse({"type": "content_block_stop", "index": 0}),
                 sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                      "usage": {"output_tokens": 4}}),
                 sse({"type": "message_stop"})]
    from proto.llm.client import AnthropicCompatClient
    wire.replies.append(anthropic)
    second = AnthropicCompatClient(wire.url, "test-key", execution_mode="native")
    assert parse_decision(second.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None,
                                                   cancelled=lambda: ""))["tool"] == "write_file"


def test_tool_named_call_with_non_decision_arguments_stays_a_protocol_error(wire):
    from proto.llm.client import OpenAICompatClient
    from proto.llm.errors import ModelResponseError

    wire.replies.append([openai_tool(json.dumps({"path": "a.txt", "content": "x"}), name="write_file"),
                         openai_delta({}, "tool_calls"), sse("[DONE]")])
    client = OpenAICompatClient(wire.url, "test-key", execution_mode="native")
    with pytest.raises(ModelResponseError, match="model_invalid_response"):
        client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")


def test_stream_promise_of_a_tool_call_without_content_falls_back_to_text(wire):
    # 剩余 invalid_response 的两个疑凶之一：finish=tool_use 但没有任何工具块。
    # 模型有时把决策当正文输出——正文能解析成决策就照用。
    from proto.llm.client import AnthropicCompatClient
    from proto.llm.decision import parse_decision

    arguments = json.dumps(WRITE, ensure_ascii=False)
    frames = [sse({"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 4}}}),
              sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
              sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": arguments}}),
              sse({"type": "content_block_stop", "index": 0}),
              sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
              sse({"type": "message_stop"})]
    wire.replies.append(frames)
    client = AnthropicCompatClient(wire.url, "test-key", execution_mode="native")
    decision = parse_decision(client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None,
                                                       cancelled=lambda: ""))
    assert decision["tool"] == "write_file" and decision["args"] == WRITE["args"]


def test_execution_log_keeps_cjk_failure_detail(tmp_path):
    import json as jsonlib

    from proto.system.execution_log import execution_logging, record_execution

    with execution_logging(tmp_path, "test"):
        record_execution("model_failed", agent_id="a" * 12, detail="ValueError-决策必须是 JSON 对象；缺 tool 字段")
    rows = [jsonlib.loads(line) for line in (tmp_path / ".runtime-logs" / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
    failed = [row for row in rows if row["event"] == "model_failed"]
    assert failed and failed[-1]["detail"].startswith("ValueError-决策必须是")
