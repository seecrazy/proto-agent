# -*- coding: utf-8 -*-
"""M15c: 部署向导 UX——getpass 不回显/任意回退/确认摘要/数字选人设。"""
import pytest

from proto.setup import PERSONA_PRESETS, WIZARD_STEPS, mask_key, run_wizard


class RecordingAsk:
    """脚本化应答：普通输入与机密输入分开记录。"""
    def __init__(self, answers, secure_answers=None):
        self.answers = list(answers)
        self.secure = list(secure_answers or [])
        self.asked_secure = []
        self.printed = []

    def ask(self, prompt, default=""):
        self.printed.append(prompt)
        answer = self.answers.pop(0) if self.answers else ""
        return answer or default

    def secure_ask(self, prompt):
        self.printed.append(prompt)
        self.asked_secure.append(prompt)
        return self.secure.pop(0) if self.secure else ""


def test_mask_key():
    assert mask_key("sk-abcdefgh1234") == "sk-****1234"
    assert mask_key("short") == "****"


def test_forward_flow_with_persona_preset_number():
    rec = RecordingAsk(
        answers=["1", "", "", "", "1", "工具链", ""],   # 提供商/主模型/快模型/令牌/人设1/优先级/确认
        secure_answers=["sk-zhipu-key-9876"])
    values = run_wizard(ask=rec.ask, secure_ask=rec.secure_ask)
    assert values["provider"] == "zhipu-coding"
    assert values["api_key"] == "sk-zhipu-key-9876"
    assert values["model"] == "glm-5.3" and values["fast_model"] == "glm-5.3-flash"
    assert values["persona"] == PERSONA_PRESETS[0][1]       # 数字选了"软件专家"
    assert values["priority"] == "工具链"


def test_persona_other_free_input():
    rec = RecordingAsk(answers=["3", "", "6", "优雅的函数式编程布道者", "", ""],
                       secure_answers=[])
    values = run_wizard(ask=rec.ask, secure_ask=rec.secure_ask)  # 离线模式
    assert values["persona"] == "优雅的函数式编程布道者"


def test_api_key_never_echoed_only_masked(capsys):
    rec = RecordingAsk(answers=["1", "", "", "", "", "", ""],
                       secure_answers=["sk-secret-abc123"])   # 提供商/模型×2/令牌/人设/优先级/确认
    values = run_wizard(ask=rec.ask, secure_ask=rec.secure_ask)
    assert values["api_key"] == "sk-secret-abc123"
    captured = capsys.readouterr()
    assert "sk-secret-abc123" not in captured.out          # 明文永不出现
    assert "sk-****c123" in captured.out                   # 只有掩码（输入回执+确认页）


def test_back_navigation_reenters_key():
    rec = RecordingAsk(
        # 提供商/主模/快模 → 令牌步输 < 回到模型 → 再 < 回到 key → 重填后走完
        answers=["1", "", "", "<", "<", "", "", "proto-t", "2", "", ""],
        secure_answers=["sk-first-key-1111", "sk-second-key-2222"])
    values = run_wizard(ask=rec.ask, secure_ask=rec.secure_ask)
    assert values["api_key"] == "sk-second-key-2222"       # 第二次输入生效


def test_confirm_jump_to_step():
    rec = RecordingAsk(answers=["3", "", "", "", "5", "自定义人设X", "", ""],
                       secure_answers=[])
    values = run_wizard(ask=rec.ask, secure_ask=rec.secure_ask)  # 离线
    # confirm 输入 5 → 跳到人设步 → 自由填写
    # （离线模式步数偏移由 WIZARD_STEPS 决定，跳步输入按显示序号）
    assert values["persona"] == "自定义人设X"


def test_initial_values_keep_on_enter():
    rec = RecordingAsk(answers=["", "", "", "", "", "", ""],
                       secure_answers=["sk-new-9999"])
    values = run_wizard(
        ask=rec.ask, secure_ask=rec.secure_ask,
        initial={"provider": "zhipu-coding", "api_key": "",
                 "token": "keep-token", "persona": "", "priority": ""})
    assert values["token"] == "keep-token"
