"""Config-driven persona state preset tests (TMEAAA-409).

Proves the mechanism/content split: custom state sets, threshold/rules
overrides and the generic preset all work without touching source, the default
preset keeps byte-for-byte behavior, and disabling the feature falls back to
generic mood handling.
"""

from __future__ import annotations

import asyncio
import json

CUSTOM_PRESET = {
    "default": "平静",
    "states": {
        "专注": {
            "priority": 100,
            "when": [{"holiday_qa": True}, {"important_topic": True}],
            "style_hint": "专注、条理清晰",
            "prompt_note": "话题重要，表达有条理。",
            "length_range": "40-80",
        },
        "疲惫": {
            "priority": 80,
            "when": {"mood_below": "low", "low_persist": True},
            "style_hint": "简短、少追问",
            "prompt_note": "精力偏低，表达简短。",
            "length_range": "6-20",
            "suppress_proactive": True,
        },
        "平静": {
            "priority": 0,
            "style_hint": "平实中性",
            "prompt_note": "保持平实中性。",
            "length_range": "20-60",
        },
    },
}


def _map(plugin, mood, session=None, env=None):
    return plugin._map_mood_to_persona_state(mood, session or {}, env or {})


def _legacy_map(mood, session, env, enabled=True):
    """Reference implementation of the pre-refactor hard-coded mapping."""
    if not enabled:
        return "日常"
    session = session or {}
    env = env or {}
    try:
        current = float(mood)
    except (TypeError, ValueError):
        current = 70.0
    low, high = 35.0, 75.0
    mid = (low + high) / 2.0
    if bool(env.get("holiday_qa")) or bool(env.get("important_topic")):
        return "认真"
    try:
        streak = max(0, int(session.get("no_reply_streak", 0) or 0))
    except (TypeError, ValueError):
        streak = 0
    raw = env.get("mood_low_rounds", session.get("mood_low_streak", 0))
    try:
        low_rounds = max(0, int(raw or 0))
    except (TypeError, ValueError):
        low_rounds = 0
    if current < low and low_rounds >= 2:
        return "低落"
    if streak >= 2 or current < mid:
        return "小别扭"
    key = str(env.get("session_key") or session.get("session_key") or "")
    is_priv = env.get("is_private")
    if is_priv is None:
        is_priv = key.startswith("private:") if key else True
    try:
        idle = float(env.get("idle_sec", 0.0) or 0.0)
    except (TypeError, ValueError):
        idle = 0.0
    if current >= high and bool(is_priv) and idle >= 4 * 3600:
        return "撒娇"
    return "日常"


def test_default_preset_matches_legacy_mapping(plugin):
    moods = (0, 10, 34.9, 35, 40, 54.9, 55, 74.9, 75, 100)
    streaks = (0, 1, 2, 5)
    idles = (0.0, 60.0, 4 * 3600 - 1, 4 * 3600, 5 * 3600)
    keys = ("private:1", "group:9")
    for mood in moods:
        for streak in streaks:
            for idle in idles:
                for key in keys:
                    for extra in ({}, {"holiday_qa": True}, {"important_topic": True}):
                        env = {"session_key": key, "idle_sec": idle, **extra}
                        session = {"no_reply_streak": streak}
                        assert _map(plugin, mood, session, env) == _legacy_map(
                            mood, session, env
                        ), (mood, streak, idle, key, extra)


def test_custom_preset_switches_states_without_code_change(plugin):
    plugin.config["persona_state_custom"] = CUSTOM_PRESET
    plugin.config["persona_state_low_threshold"] = 20.0
    plugin.config["persona_state_high_threshold"] = 60.0
    plugin.config["persona_state_low_persist_rounds"] = 1

    assert _map(plugin, 50, env={"holiday_qa": True}) == "专注"
    assert _map(plugin, 10, {"mood_low_streak": 1}) == "疲惫"
    assert _map(plugin, 50) == "平静"
    assert plugin._persona_state_length_range("专注") == "40-80"
    assert plugin._persona_state_prompt_note("平静") == "保持平实中性。"


def test_custom_suppress_flag_drives_proactive_gate(plugin):
    plugin.config["persona_state_custom"] = CUSTOM_PRESET
    plugin.config["persona_state_low_threshold"] = 20.0
    plugin.config["persona_state_low_persist_rounds"] = 1
    now_ts = plugin._now().timestamp()
    s = {"mood": 10, "mood_low_streak": 1, "last_interaction_at": now_ts}
    assert plugin._unit_gate_persona_state("private:1", s, now_ts) is True
    # Not in a suppressing state -> no defer.
    s_ok = {"mood": 50, "mood_low_streak": 0, "last_interaction_at": now_ts}
    assert plugin._unit_gate_persona_state("private:1", s_ok, now_ts) is False


def test_overrides_deep_merge_keep_other_fields(plugin):
    plugin.config["persona_state_overrides"] = {
        "states": {"低落": {"length_range": "1-5", "style_hint": "极简"}}
    }
    assert plugin._persona_state_length_range("低落") == "1-5"
    assert plugin._persona_state_style_hint("低落") == "极简"
    # Field not overridden is preserved from the bundled preset.
    assert plugin._persona_state_prompt_note("低落") == (
        "心情有点低落，话少、语气轻，不主动追问，也不迁怒对方。"
    )


def test_generic_preset_is_persona_free(plugin):
    plugin.config["persona_state_preset"] = "generic"
    blob = json.dumps(plugin._resolved_persona_preset(), ensure_ascii=False)
    # Bundled persona-specific vocabulary must not leak into the neutral preset.
    forbidden = (
        "\u5343\u8349",
        "\u50b2\u5a07",
        "日常",
        "撒娇",
        "小别扭",
        "低落",
        "认真",
    )
    for word in forbidden:
        assert word not in blob
    assert _map(plugin, 50) == "平静"


def test_invalid_custom_preset_falls_back_to_generic(plugin):
    plugin.config["persona_state_custom"] = {"states": {}}
    assert _map(plugin, 50) == "平静"


def test_unknown_condition_falls_back_to_default(plugin):
    plugin.config["persona_state_custom"] = {
        "default": "平静",
        "states": {
            "神秘": {
                "priority": 99,
                "when": {"not_a_real_key": True},
                "style_hint": "x",
                "prompt_note": "y",
                "length_range": "1-2",
            },
            "平静": {
                "priority": 0,
                "style_hint": "a",
                "prompt_note": "b",
                "length_range": "20-60",
            },
        },
    }
    assert _map(plugin, 50) == "平静"
    # Unknown state names safely resolve to the default state's fields.
    assert plugin._persona_state_length_range("不存在") == "20-60"


def test_disabled_flag_forces_default_and_skips_block(plugin):
    plugin.config["persona_state_enabled"] = False
    assert _map(plugin, 10, {"mood_low_streak": 9}) == "日常"
    assert plugin._persona_state_prompt_block("日常") == ""
    hint = plugin._style_hint("private:1", {"mood": 70}, 60)
    assert "黏" not in hint


def test_custom_preset_reaches_generated_prompt(plugin):
    plugin.config["persona_state_custom"] = CUSTOM_PRESET
    plugin.config["persona_state_low_threshold"] = 20.0
    plugin.config["persona_state_low_persist_rounds"] = 1
    captured = {}

    class _Completion:
        completion_text = "在的，今天还好吗？"

    async def _fake_llm(*, chat_provider_id, prompt):
        captured["prompt"] = prompt
        return _Completion()

    plugin.config["proactive_provider_id"] = "p1"
    plugin.config["enable_holiday_perception"] = False
    plugin.context.llm_generate = _fake_llm
    text = asyncio.run(
        plugin._generate_proactive_text(
            "aiocqhttp:FriendMessage:1",
            "private:1",
            60,
            {"mood": 10, "mood_low_streak": 1},
        )
    )
    assert text == "在的，今天还好吗？"
    assert "情绪状态：疲惫" in captured["prompt"]
    assert "长度 6-20 字" in captured["prompt"]
