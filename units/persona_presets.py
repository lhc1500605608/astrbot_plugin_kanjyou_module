"""Data-only persona state presets.

This module contains NO persona-specific logic: it is a plain data container
plus two small generic helpers. The plugin exposes the *mechanism*
``mood -> semantic state -> tone/length/proactivity modulation`` while the
*content* (state names, style hints, prompt notes, length ranges and mapping
rules) lives entirely in these presets and in plugin config.

Preset schema
-------------

.. code-block:: python

    {
        "default": "<state name>",   # fallback state, always present
        "states": {
            "<state name>": {
                "priority": 100,          # higher wins first
                "when": {...},            # optional match rule (see below)
                "style_hint": "...",      # tone hint for prompt
                "prompt_note": "...",     # extra prompt sentence
                "length_range": "20-60",  # target length range
                "suppress_proactive": False,  # optional: skip idle proactive
            },
            ...
        },
    }

``when`` accepts a dict (all keys must match / AND) or a list of dicts
(any dict matching / OR). Supported generic condition keys:

* ``holiday_qa`` (bool)
* ``important_topic`` (bool)
* ``mood_below`` / ``mood_gte``: ``low`` | ``mid`` | ``high`` or a number
* ``low_persist`` (bool): low-mood persistence rounds reached
* ``no_reply_streak_gte`` (int)
* ``private_only`` (bool)
* ``idle_gte_clingy`` (bool)
* ``default`` (bool): always matches
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Optional

# Neutral preset with no persona-specific vocabulary.
GENERIC_PRESET: Dict = {
    "default": "平静",
    "states": {
        "专注": {
            "priority": 100,
            "when": [{"holiday_qa": True}, {"important_topic": True}],
            "style_hint": "专注平和、条理清晰，少寒暄",
            "prompt_note": "话题较重要，表达有条理，减少玩笑。",
            "length_range": "40-80",
        },
        "疲惫": {
            "priority": 80,
            "when": {"mood_below": "low", "low_persist": True},
            "style_hint": "简短克制、降低主动度，不追问",
            "prompt_note": "精力较低，表达简短，降低主动度，不追问。",
            "length_range": "6-20",
            "suppress_proactive": True,
        },
        "平静": {
            "priority": 0,
            "style_hint": "平实中性、自然简洁",
            "prompt_note": "保持平实中性的表达，自然简洁。",
            "length_range": "20-60",
        },
    },
}

# Default bundled preset. Content mirrors the previously hard-coded behavior
# byte-for-byte; only the mapping rules were externalized here.
CHIKA_PRESET: Dict = {
    "default": "日常",
    "states": {
        "认真": {
            "priority": 100,
            "when": [{"holiday_qa": True}, {"important_topic": True}],
            "style_hint": "认真平和、条理清晰，收起玩笑",
            "prompt_note": "话题重要或涉及节假日事务，收起玩笑，平和而条理清晰。",
            "length_range": "40-80",
        },
        "低落": {
            "priority": 80,
            "when": {"mood_below": "low", "low_persist": True},
            "style_hint": "低落安静、话少而轻，不主动追问",
            "prompt_note": "心情有点低落，话少、语气轻，不主动追问，也不迁怒对方。",
            "length_range": "6-20",
            "suppress_proactive": True,
        },
        "小别扭": {
            "priority": 60,
            "when": [{"no_reply_streak_gte": 2}, {"mood_below": "mid"}],
            "style_hint": "温和克制、给台阶，避免连续追问",
            "prompt_note": "有点闷闷的，嘴上说不在意，语气克制，给对方留台阶。",
            "length_range": "10-35",
        },
        "撒娇": {
            "priority": 40,
            "when": {
                "mood_gte": "high",
                "private_only": True,
                "idle_gte_clingy": True,
            },
            "style_hint": "黏人俏皮、想被关注，短句带点试探",
            "prompt_note": "想被关注，语气更黏一点、俏皮一点，用短句试探。",
            "length_range": "8-28",
        },
        "日常": {
            "priority": 0,
            "style_hint": "自然亲切、像朋友一样",
            "prompt_note": "自然、有温度，带点小吐槽但始终站在对方这边。",
            "length_range": "20-60",
        },
    },
}

BUILTIN_PRESETS: Dict[str, Dict] = {
    "chika": CHIKA_PRESET,
    "generic": GENERIC_PRESET,
}

DEFAULT_PRESET_NAME = "chika"


def deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    if not isinstance(base, dict):
        return copy.deepcopy(override) if isinstance(override, dict) else copy.deepcopy(base)
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def is_valid_preset(preset: Optional[Dict]) -> bool:
    """A usable preset must declare a default state and at least one state."""
    if not isinstance(preset, dict):
        return False
    states = preset.get("states")
    if not isinstance(states, dict) or not states:
        return False
    return True


def _parse_when(value: Any) -> Any:
    """Return a usable ``when`` value, or ``None`` when it must be dropped.

    Accepts ready-made dict/list conditions as-is (legacy hand-written JSON);
    a string is parsed as JSON and only kept when it yields a dict or list.
    Anything else (empty string, malformed JSON, scalars) is discarded.
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, (dict, list)) else None
    return None


def _normalize_state_entry(entry: Dict) -> Dict:
    """Strip template bookkeeping and coerce the ``when`` field."""
    state = {
        key: value
        for key, value in entry.items()
        if key not in ("__template_key", "name")
    }
    if "when" in state:
        parsed = _parse_when(state["when"])
        if parsed is None:
            state.pop("when", None)
        else:
            state["when"] = parsed
    return state


def normalize_state_payload(payload: Any) -> Dict:
    """Normalize a WebUI/legacy persona-state payload into the runtime shape.

    Returns ``{"default": str, "states": {name: {...}}}``. Handles both the
    AstrBot ``template_list`` form (``states`` is a list of entries carrying a
    ``name`` key) and the legacy hand-written JSON (``states`` is a dict).
    Never raises: unknown/missing shapes degrade to empty states so callers
    fall back to the bundled preset.
    """
    if not isinstance(payload, dict):
        return {}
    result: Dict[str, Any] = {"default": payload.get("default", "")}
    states = payload.get("states")
    normalized: Dict[str, Any] = {}
    if isinstance(states, list):
        for entry in states:
            if not isinstance(entry, dict):
                continue
            raw_name = entry.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name or name in normalized:
                continue
            normalized[name] = _normalize_state_entry(entry)
    elif isinstance(states, dict):
        for raw_name, entry in states.items():
            name = str(raw_name).strip()
            if not name:
                continue
            if isinstance(entry, dict):
                normalized[name] = _normalize_state_entry(entry)
            else:
                normalized[name] = copy.deepcopy(entry)
    result["states"] = normalized
    return result


def resolve_preset(
    name: Optional[str],
    custom: Any = None,
    overrides: Any = None,
) -> Dict:
    """Resolve the effective preset.

    ``custom`` replaces the bundled preset entirely when, after normalization,
    it declares at least one state; ``overrides`` deep-merges on top under the
    same rule. Empty/invalid payloads (including the ``{default:"",states:[]}``
    shape AstrBot injects for the structured schema) are ignored so they never
    clobber the selected preset. Invalid results fall back to the neutral
    generic preset so callers never crash on bad config.
    """
    custom_norm = normalize_state_payload(custom)
    if custom_norm.get("states"):
        preset = copy.deepcopy(custom_norm)
    else:
        key = str(name or "").strip() or DEFAULT_PRESET_NAME
        preset = copy.deepcopy(BUILTIN_PRESETS.get(key) or GENERIC_PRESET)

    overrides_norm = normalize_state_payload(overrides)
    if overrides_norm.get("states"):
        merge_payload: Dict[str, Any] = {"states": overrides_norm["states"]}
        if str(overrides_norm.get("default") or "").strip():
            merge_payload["default"] = overrides_norm["default"]
        preset = deep_merge(preset, merge_payload)

    if not is_valid_preset(preset) or not isinstance(preset.get("states"), dict):
        preset = copy.deepcopy(GENERIC_PRESET)
    if not str(preset.get("default") or "").strip():
        # Fall back to the first declared state when default is missing.
        preset["default"] = next(iter(preset["states"]))
    return preset
