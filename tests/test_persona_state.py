"""Default-preset persona state mapping tests (TMEAAA-398).

Covers five-state boundaries, the low-state proactive gate and the
prompt differences between the low and clingy states.
"""

from __future__ import annotations

import asyncio

LLM_STUB_TEXT = "今天有点想你，忙完了吗？"


def _map(plugin, mood, session=None, env=None):
    return plugin._map_mood_to_persona_state(mood, session or {}, env or {})


def test_returns_daily_by_default(plugin):
    assert _map(plugin, 70, env={"session_key": "private:1", "idle_sec": 60}) == "日常"


def test_low_requires_both_threshold_and_persistence(plugin):
    session = {"mood_low_streak": 2}
    assert _map(plugin, 20, session) == "低落"
    # 单轮低于阈值还不算低落，先落入小别扭。
    assert _map(plugin, 20, {"mood_low_streak": 1}) == "小别扭"


def test_low_threshold_boundary_is_exclusive(plugin):
    # mood == low_threshold 不算低落，落到小别扭区间。
    assert _map(plugin, 35, {"mood_low_streak": 9}) == "小别扭"


def test_mid_low_mood_is_awkward(plugin):
    # 中低情绪（低于 low/high 中点 55）→ 小别扭。
    assert _map(plugin, 40) == "小别扭"


def test_no_reply_streak_is_awkward_even_with_good_mood(plugin):
    assert _map(plugin, 70, {"no_reply_streak": 2}) == "小别扭"


def test_mid_boundary_is_daily(plugin):
    # mood == 中点 55 不算小别扭，且未达 high，故日常。
    assert _map(plugin, 55, env={"session_key": "private:1"}) == "日常"


def test_clingy_requires_high_mood_private_and_long_idle(plugin):
    env = {
        "session_key": "private:1",
        "idle_sec": plugin._persona_state_clingy_idle_sec() + 1,
    }
    assert _map(plugin, 75, env=env) == "撒娇"
    # 群聊撒娇被抑制。
    assert _map(plugin, 75, env={**env, "session_key": "group:9"}) == "日常"
    # 久未互动不足则不撒娇。
    assert _map(plugin, 75, env={"session_key": "private:1", "idle_sec": 60}) == "日常"


def test_serious_overrides_by_holiday_or_important_topic(plugin):
    low = {"mood_low_streak": 5}
    assert _map(plugin, 20, low, {"holiday_qa": True}) == "认真"
    assert _map(plugin, 90, None, {"important_topic": True}) == "认真"


def test_disabled_flag_forces_daily(plugin):
    plugin.config["persona_state_enabled"] = False
    assert _map(plugin, 10, {"mood_low_streak": 9}) == "日常"


def test_low_streak_updates_and_resets(plugin):
    s = {"mood": 20}
    plugin._update_mood_low_streak(s)
    plugin._update_mood_low_streak(s)
    assert s["mood_low_streak"] == 2
    s["mood"] = 70
    plugin._update_mood_low_streak(s)
    assert s["mood_low_streak"] == 0


def test_style_hint_differs_by_state(plugin):
    clingy = plugin._style_hint(
        "private:1",
        {"mood": 90, "last_interaction_at": 0},
        plugin._persona_state_clingy_idle_sec() + 1,
    )
    daily = plugin._style_hint("private:1", {"mood": 70}, 60)
    assert "黏" in clingy
    assert clingy != daily


def test_low_state_style_hint_is_quiet(plugin):
    hint = plugin._style_hint("private:1", {"mood": 20, "mood_low_streak": 3}, 60)
    assert "不主动" in hint


def test_gate_skips_proactive_when_low(plugin):
    now_ts = plugin._now().timestamp()
    s = {
        "mood": 20,
        "mood_low_streak": 3,
        "last_interaction_at": now_ts - 3600,
        "next_check_at": 0,
        "cooldown_until": 0,
    }
    assert plugin._unit_gate_persona_state("private:1", s, now_ts) is True
    # 日常会话不拦截。
    s_ok = {"mood": 70, "mood_low_streak": 0, "last_interaction_at": now_ts}
    assert plugin._unit_gate_persona_state("private:1", s_ok, now_ts) is False


def test_gate_disabled_when_persona_state_off(plugin):
    plugin.config["persona_state_enabled"] = False
    now_ts = plugin._now().timestamp()
    s = {"mood": 5, "mood_low_streak": 9, "last_interaction_at": now_ts}
    assert plugin._unit_gate_persona_state("private:1", s, now_ts) is False


def _capture_prompt(plugin, session, idle_sec):
    captured = {}

    class _Completion:
        completion_text = LLM_STUB_TEXT

    async def _fake_llm(*, chat_provider_id, prompt):
        captured["prompt"] = prompt
        return _Completion()

    plugin.config["proactive_provider_id"] = "p1"
    plugin.config["enable_holiday_perception"] = False
    plugin.context.llm_generate = _fake_llm
    text = asyncio.run(
        plugin._generate_proactive_text("aiocqhttp:FriendMessage:1", "private:1", idle_sec, session)
    )
    assert text == LLM_STUB_TEXT
    return captured["prompt"]


def test_prompt_length_and_state_differ_between_low_and_daily(plugin):
    low_prompt = _capture_prompt(
        plugin,
        {"mood": 20, "mood_low_streak": 3},
        60,
    )
    daily_prompt = _capture_prompt(plugin, {"mood": 70, "mood_low_streak": 0}, 60)
    assert "情绪状态：低落" in low_prompt
    assert "长度 6-20 字" in low_prompt
    assert "情绪状态：日常" in daily_prompt
    assert "长度 20-60 字" in daily_prompt


def test_prompt_clingy_is_shorter_and_stickier(plugin):
    idle_sec = plugin._persona_state_clingy_idle_sec() + 60
    clingy_prompt = _capture_prompt(
        plugin,
        {"mood": 90, "no_reply_streak": 0},
        idle_sec,
    )
    daily_prompt = _capture_prompt(plugin, {"mood": 70, "no_reply_streak": 0}, 60)
    assert "情绪状态：撒娇" in clingy_prompt
    assert "长度 8-28 字" in clingy_prompt
    assert "黏" in clingy_prompt
    assert "情绪状态：撒娇" not in daily_prompt
