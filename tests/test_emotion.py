# -*- coding: utf-8 -*-
"""Task 7a: PAD 情绪引擎（事件增量 / 衰减 / clamp，设计 §7.4）。"""
import pytest

from proto.state.emotion import PAD


def test_defaults_at_setpoint():
    pad = PAD()
    assert pad.pleasure == 0.0 and pad.arousal == 0.4 and pad.dominance == 0.5


def test_event_deltas_applied():
    pad = PAD()
    pad.apply_event("owner_praise")
    assert pad.pleasure > 0.0
    pad2 = PAD()
    pad2.apply_event("rejection")
    assert pad2.pleasure < 0.0


def test_all_fields_clamped():
    pad = PAD()
    for _ in range(50):
        pad.apply_event("owner_praise")
    assert pad.pleasure <= 1.0
    for _ in range(50):
        pad.apply_event("rejection")
    assert pad.pleasure >= -1.0


def test_tick_decays_toward_setpoint():
    pad = PAD()
    pad.apply_event("task_success")
    aroused = pad.arousal
    pad.tick(10)
    assert abs(pad.arousal - 0.4) < abs(aroused - 0.4)


def test_unknown_event_raises():
    pad = PAD()
    with pytest.raises(KeyError):
        pad.apply_event("does_not_exist")
