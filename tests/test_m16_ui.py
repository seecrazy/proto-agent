# -*- coding: utf-8 -*-
"""M16.1: 终端 UI 层与向导引导。"""
import pytest

from proto import ui


@pytest.fixture(autouse=True)
def plain():
    ui.force_color(False)
    yield
    ui.force_color(None)


def test_no_color_env_disables():
    ui.force_color(None)
    import os
    os.environ["NO_COLOR"] = "1"
    try:
        assert ui.color_enabled() is False
    finally:
        del os.environ["NO_COLOR"]


def test_plain_mode_has_no_ansi():
    assert "\033[" not in ui.banner("PROTO")
    assert "\033[" not in ui.ok("完成")


def test_color_mode_emits_ansi():
    ui.force_color(True)
    assert "\033[" in ui.ok("完成")
    assert ui.strip_ansi(ui.ok("完成")) == "✓ 完成"


def test_banner_box_shape():
    text = ui.banner("PROTO 部署向导", "v0.14")
    lines = text.splitlines()
    assert lines[0].startswith("╔") and lines[-1].startswith("╚")
    assert lines[1].strip().startswith("║")
    # 中文按显示宽度对齐：所有行等宽
    widths = {ui.display_width(l) for l in lines}
    assert len(widths) == 1, widths


def test_step_and_kv_table():
    assert "[3/7]" in ui.step(3, 7, "配置 LLM")
    table = ui.kv_table([("提供商", "智谱 Coding Plan"), ("API Key", "sk-****c123")])
    assert "sk-****c123" in table and "提供商" in table


class TestWizardGuidance:
    def test_priority_step_explains_and_shows_examples(self, capsys):
        from proto.setup import run_wizard

        run_wizard(ask=lambda p, d="": d, secure_ask=lambda p: "")
        out = capsys.readouterr().out
        # 引导三要素：说明作用 + 示例 + 可跳过
        assert "优先级" in out and "仲裁" in out and "学习" in out
        assert "示例" in out
        assert "回车" in out

    def test_priority_numeric_example_or_free_text(self):
        from proto.setup import run_wizard

        # provider 离线(3) → 令牌(回车) → 优先级选示例 1 → 确认
        values = run_wizard(
            ask=lambda p, d="": "1" if p.startswith("输入数字选示例") else "",
            secure_ask=lambda p: "")
        # 离线路径下优先级菜单选 1 → 第一条示例文本
        assert values["priority"] != ""
