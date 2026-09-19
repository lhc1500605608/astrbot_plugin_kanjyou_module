"""v2.3.0 配置分组重构：旧扁平 key 兼容层与归一化测试 (TMEAAA-413)。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.config import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_FLAT,
    DEPRECATED_CONFIG_KEYS,
    FLAT_TO_NESTED,
    INTERNAL_POLICY,
    PluginConfigView,
    migrate_flat_to_nested,
    migrate_persona_state_payload,
)
from astrbot_plugin_kanjyou_module.units.persona_presets import (  # noqa: E402
    normalize_state_payload,
)


def test_every_flat_key_maps_to_nested_default():
    known = set(DEFAULT_CONFIG_FLAT) | set(INTERNAL_POLICY)
    missing = [key for key in FLAT_TO_NESTED if key not in known]
    assert missing == []
    for flat, (group, sub) in FLAT_TO_NESTED.items():
        assert group in DEFAULT_CONFIG, group
        assert sub in DEFAULT_CONFIG[group], f"{group}.{sub}"


def test_deprecated_keys_are_removed_from_nested_but_kept_flat():
    for key in DEPRECATED_CONFIG_KEYS:
        assert key not in DEFAULT_CONFIG
    # 原本存在于扁平默认值的废弃项仍保留读取端默认值
    for key in (
        "lite_llm_enabled",
        "dialogue_wait_enabled",
        "output_segment_enabled",
    ):
        assert key in DEFAULT_CONFIG_FLAT


def test_migrate_flat_to_nested_preserves_values_and_is_idempotent():
    data = {
        "min_idle_min": 30,
        "sleep_start": "22:00",
        "config_mode": "advanced",
        "lite_llm_enabled": True,
        "proactive_segment_max_parts": 5,
    }
    assert migrate_flat_to_nested(data) is True
    assert data["trigger"]["min_idle_min"] == 30
    assert data["schedule"]["sleep_start"] == "22:00"
    assert data["basic"]["advanced_enabled"] is True
    assert data["generation"]["proactive_lite_refine_enabled"] is True
    assert "holiday_qa_main_llm_enabled" not in data["generation"]
    assert data["generation"]["proactive_segment_max_parts"] == 5
    # 旧扁平 key 不删除
    assert data["min_idle_min"] == 30
    # 幂等
    assert migrate_flat_to_nested(data) is False


def test_proxy_prefers_nested_then_falls_back_to_flat():
    raw = {
        "trigger": {"min_idle_min": 10},
        "sleep_start": "21:00",
        "enabled": True,
    }
    view = PluginConfigView(raw)
    assert view.get("min_idle_min") == 10
    assert view.get("sleep_start") == "21:00"
    assert view["enabled"] is True
    assert view.get("unknown_key", "fallback") == "fallback"


def test_proxy_writes_back_to_nested_group():
    raw: dict = {}
    view = PluginConfigView(raw)
    view["min_idle_min"] = 20
    view["enabled"] = False
    assert raw["trigger"]["min_idle_min"] == 20
    assert raw["basic"]["enabled"] is False
    assert "min_idle_min" not in raw
    assert "enabled" not in raw


LEGACY_PERSONA_CUSTOM = {
    "default": "平静",
    "states": {
        "专注": {
            "priority": 100,
            "when": [{"holiday_qa": True}, {"important_topic": True}],
            "style_hint": "专注、条理清晰",
            "length_range": "40-80",
        },
        "疲惫": {
            "priority": 80,
            "when": {"mood_below": "low", "low_persist": True},
            "suppress_proactive": True,
        },
        "平静": {"priority": 0},
    },
}


def test_migrate_persona_state_payload_is_lossless_and_idempotent():
    payload = json.loads(json.dumps(LEGACY_PERSONA_CUSTOM))
    assert migrate_persona_state_payload(payload) is True
    states = payload["states"]
    assert isinstance(states, list)
    assert [entry["name"] for entry in states] == ["专注", "疲惫", "平静"]
    focus = states[0]
    assert focus["__template_key"] == "state"
    assert focus["when"] == '[{"holiday_qa": true}, {"important_topic": true}]'
    assert focus["style_hint"] == "专注、条理清晰"
    assert states[1]["when"] == '{"mood_below": "low", "low_persist": true}'
    assert states[1]["suppress_proactive"] is True
    # Round-trip: already-a-list payload is untouched.
    assert migrate_persona_state_payload(payload) is False


def test_migrate_persona_state_payload_handles_unknown_shapes():
    assert migrate_persona_state_payload(None) is False
    assert migrate_persona_state_payload({"states": []}) is False
    assert migrate_persona_state_payload({"states": "nope"}) is False
    assert migrate_persona_state_payload({"default": "", "states": []}) is False
    payload = {"states": {"a": {"when": "{bad json"}, "": {"priority": 1}}}
    assert migrate_persona_state_payload(payload) is True
    assert [entry["name"] for entry in payload["states"]] == ["a"]
    assert payload["states"][0]["when"] == "{bad json"


def test_migrate_persona_state_payload_round_trips_through_runtime_normalizer():
    legacy = json.loads(json.dumps(LEGACY_PERSONA_CUSTOM))
    migrated = {"default": legacy["default"], "states": legacy["states"]}
    assert migrate_persona_state_payload(migrated) is True
    assert normalize_state_payload(migrated) == normalize_state_payload(legacy)


def test_migrate_flat_to_nested_migrates_persona_state_in_both_positions():
    legacy = json.loads(json.dumps(LEGACY_PERSONA_CUSTOM))
    nested = {"emotion": {"persona_state_custom": legacy}}
    assert migrate_flat_to_nested(nested) is True
    assert isinstance(nested["emotion"]["persona_state_custom"]["states"], list)
    assert migrate_flat_to_nested(nested) is False

    flat = {"persona_state_overrides": json.loads(json.dumps(LEGACY_PERSONA_CUSTOM))}
    assert migrate_flat_to_nested(flat) is True
    assert isinstance(flat["emotion"]["persona_state_overrides"]["states"], list)


def test_persona_state_already_template_list_is_noop():
    data = {
        "emotion": {
            "persona_state_custom": {
                "default": "平静",
                "states": [{"name": "专注", "when": '{"holiday_qa": true}'}],
            }
        }
    }
    assert migrate_flat_to_nested(data) is False


def test_plugin_normalizes_legacy_dict_persona_state_to_template_list(plugin_module):
    legacy_custom = json.loads(json.dumps(LEGACY_PERSONA_CUSTOM))
    raw = {"emotion": {"persona_state_custom": legacy_custom}}
    instance = plugin_module.KanjyouIdleProactivePlugin(
        context=plugin_module.Context(), config=raw
    )
    migrated = instance.config.get("persona_state_custom")
    assert isinstance(migrated["states"], list)
    assert [entry["name"] for entry in migrated["states"]] == ["专注", "疲惫", "平静"]
    # Runtime mapping still works from the migrated list form.
    assert instance._map_mood_to_persona_state(50, {}, {"holiday_qa": True}) == "专注"


def test_plugin_upgrades_legacy_flat_config(plugin_module):
    legacy = {
        "enabled": True,
        "min_idle_min": 12,
        "cooldown_min": 34,
        "sleep_start": "22:15",
        "mood_initial": 88.0,
        "config_mode": "advanced",
    }
    instance = plugin_module.KanjyouIdleProactivePlugin(
        context=plugin_module.Context(), config=legacy
    )
    assert instance.config.get("min_idle_min") == 12
    assert instance.config.get("cooldown_min") == 34
    assert instance.config.get("sleep_start") == "22:15"
    assert instance.config.get("mood_initial") == 88.0
    assert instance.config.get("advanced_enabled") is True
    # 旧扁平 key 未被删除
    assert legacy["min_idle_min"] == 12
