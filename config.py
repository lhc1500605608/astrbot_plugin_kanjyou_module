"""kanjyou 配置定义与兼容层。

v2.3.0 起配置由 91 个扁平 key 收敛为 9 个可折叠 object 组（见 `CONFIG_GROUPS`）。
为兼容 AstrBot 升级场景，本模块同时提供：

- `DEFAULT_CONFIG`：新的嵌套结构默认值（供 schema/外部消费者使用）。
- `DEFAULT_CONFIG_FLAT`：旧扁平默认值镜像（内部读取与旧代码兼容使用）。
- `PluginConfigView`：运行期统一读取入口。优先读新嵌套路径，缺失时回退旧扁平
  key；写入统一落到新嵌套路径。
- `migrate_flat_to_nested` / `run_config_file_migration`：导入期（AstrBot 构造
  插件配置对象之前）把旧扁平 key 幂等归一化进新嵌套结构，旧 key 不删除。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FLAT = {
    "enabled": True,
    "advanced_enabled": False,
    "lifecycle_log": True,
    "debug_log": False,
    "debug_status_window_sec": 300,
    "timezone": "Asia/Shanghai",
    "sleep_start": "23:30",
    "sleep_end": "08:00",
    "private_whitelist": [],
    "group_whitelist": [],
    "check_interval_sec": 30,
    "min_idle_min": 45,
    "max_idle_min": 180,
    "cooldown_min": 90,
    "persona_id": "",
    "proactive_provider_id": "",
    "lite_llm_enabled": False,
    "lite_provider_id": "",
    "lite_llm_timeout_sec": 6,
    "decision_mode": "balanced",
    "decision_min_confidence": 0.6,
    "decision_group_quiet_threshold": 3,
    "decision_trace_enabled": True,
    "quality_trace_enabled": True,
    "dialogue_wait_enabled": False,
    "dialogue_wait_timeout_sec": 4,
    "dialogue_wait_max_merge": 2,
    "output_segment_enabled": False,
    "output_segment_max_parts": 4,
    "output_segment_max_chars": 20,
    "proactive_segment_enabled": True,
    "proactive_segment_max_parts": 3,
    "proactive_segment_delay_min_ms": 300,
    "proactive_segment_delay_max_ms": 900,
    "holiday_qa_main_llm_enabled": True,
    "proactive_lite_refine_enabled": False,
    "proactive_prompt_template": (
        "你是一个在聊天中主动关怀用户的助手。"
        "重要：本次问候是因为对话空闲时间达到阈值而自动触发的，用户并没有主动发送消息。"
        "请以开启新话题、自然寒暄的方式表达，不要表现出“回复用户上一条消息”的意图。"
        "请严格基于下方人格设定进行表达，不要脱离人格。\n"
        "人格设定：\n{persona}\n\n"
        "{persona_state_block}"
        "当前会话类型：{session_type}\n"
        "环境感知信息：{env_perception}\n"
        "距离上次互动约 {idle_minutes} 分钟（{idle_seconds} 秒）。\n"
        "建议语气：{style_hint}\n"
        "相关长期记忆（可能为空，仅在自然相关时引用，严禁生硬复述或暴露隐私）：\n"
        "{recalled_memory}\n"
        "最近已发过的主动问候（避免重复）：\n{recent_history}\n"
        "请输出 1 条中文主动问候（只输出消息正文，不加引号），要求：\n"
        "1) 语气自然、有温度，不要机械。\n"
        "2) 结尾带一个轻量开放问题，促进继续对话。\n"
        "3) 避免重复“在吗/你好”。\n"
        "4) 长度 {length_range} 字。\n"
        "5) 和最近问候不重复。\n"
        "6) 记忆只在相关时自然带一句，不要罗列、不要生硬复述；避开记忆中主人明确不喜欢的话题。"
    ),
    "fallback_proactive_text": "刚刚想到你，最近有没有一件小事让你有点开心？",
    "enable_holiday_perception": True,
    "enable_platform_perception": True,
    "holiday_country": "CN",
    "holiday_qa_enabled": True,
    "holiday_api_enabled": True,
    "holiday_api_timeout_sec": 3,
    "holiday_api_cache_ttl_sec": 21600,
    "security_global_hourly_cap": 6,
    "security_max_fail_streak": 3,
    "security_fail_pause_min": 180,
    "security_allow_links": False,
    "security_blocked_words": [],
    "security_max_text_length": 90,
    "mood_enabled": True,
    "mood_initial": 70.0,
    "mood_min_trigger": 35.0,
    "mood_cost_on_proactive": 28.0,
    "mood_cost_on_dialogue": 8.0,
    "mood_recover_per_min": 1.2,
    "persona_state_enabled": True,
    "persona_state_preset": "chika",
    "persona_state_custom": {},
    "persona_state_overrides": {},
    "persona_state_low_threshold": 35.0,
    "persona_state_high_threshold": 75.0,
    "persona_state_clingy_idle_sec": 14400,
    "persona_state_low_persist_rounds": 2,
    "memory_recall_enabled": True,
    "memory_recall_limit": 3,
    "memory_recall_timeout_sec": 2,
    "memory_recall_private_only": True,
    "memory_recall_group_enabled": False,
    "memory_recall_plugin_name": "astrbot_plugin_tmemory",
    "debug_decision_log": True,
}

INTERNAL_POLICY = {
    "max_per_session_per_day": 8,
    "trigger_base_prob": 0.02,
    "trigger_max_prob": 0.18,
    "require_human_reply_before_next_proactive": True,
    "period_quota_enabled": True,
    "period_quota_morning_max": 1,
    "period_quota_afternoon_max": 1,
    "period_quota_evening_max": 1,
    "no_reply_decay_enabled": True,
    "no_reply_decay_factor": 1.6,
    "no_reply_decay_max_factor": 4.0,
    "weekend_mode_enabled": True,
    "weekend_min_idle_multiplier": 1.25,
    "weekend_cooldown_multiplier": 1.35,
    "weekend_quota_multiplier": 0.8,
    "quality_dedupe_enabled": True,
    "quality_history_size": 6,
}

# 9 个可折叠配置组（顺序即 WebUI 展示顺序）。
CONFIG_GROUPS: dict[str, list[str]] = {
    "basic": [
        "enabled",
        "advanced_enabled",
    ],
    "trigger": [
        "min_idle_min",
        "max_idle_min",
        "cooldown_min",
        "check_interval_sec",
        "trigger_base_prob",
        "trigger_max_prob",
        "decision_mode",
        "decision_min_confidence",
        "decision_group_quiet_threshold",
        "require_human_reply_before_next_proactive",
    ],
    "schedule": [
        "timezone",
        "sleep_start",
        "sleep_end",
    ],
    "quota": [
        "max_per_session_per_day",
        "period_quota_enabled",
        "period_quota_morning_max",
        "period_quota_afternoon_max",
        "period_quota_evening_max",
        "no_reply_decay_enabled",
        "no_reply_decay_factor",
        "no_reply_decay_max_factor",
        "weekend_mode_enabled",
        "weekend_min_idle_multiplier",
        "weekend_cooldown_multiplier",
        "weekend_quota_multiplier",
        "quality_dedupe_enabled",
        "quality_history_size",
    ],
    "generation": [
        "persona_id",
        "proactive_provider_id",
        "proactive_prompt_template",
        "fallback_proactive_text",
        "proactive_lite_refine_enabled",
        "proactive_segment_enabled",
        "proactive_segment_max_parts",
        "proactive_segment_delay_min_ms",
        "proactive_segment_delay_max_ms",
        "lite_llm_timeout_sec",
        "lite_provider_id",
        "enable_platform_perception",
    ],
    "emotion": [
        "mood_enabled",
        "mood_initial",
        "mood_min_trigger",
        "mood_cost_on_proactive",
        "mood_cost_on_dialogue",
        "mood_recover_per_min",
        "persona_state_enabled",
        "persona_state_preset",
        "persona_state_custom",
        "persona_state_overrides",
        "persona_state_low_threshold",
        "persona_state_high_threshold",
        "persona_state_clingy_idle_sec",
        "persona_state_low_persist_rounds",
    ],
    "memory": [
        "memory_recall_enabled",
        "memory_recall_limit",
        "memory_recall_timeout_sec",
        "memory_recall_private_only",
        "memory_recall_group_enabled",
        "memory_recall_plugin_name",
    ],
    "holiday": [
        "enable_holiday_perception",
        "holiday_country",
        "holiday_qa_enabled",
        "holiday_api_enabled",
        "holiday_api_timeout_sec",
        "holiday_api_cache_ttl_sec",
        "holiday_qa_main_llm_enabled",
    ],
    "security": [
        "security_global_hourly_cap",
        "security_max_fail_streak",
        "security_fail_pause_min",
        "security_allow_links",
        "security_blocked_words",
        "security_max_text_length",
        "private_whitelist",
        "group_whitelist",
    ],
    "debug": [
        "debug_log",
        "debug_decision_log",
        "debug_status_window_sec",
        "lifecycle_log",
        "decision_trace_enabled",
        "quality_trace_enabled",
    ],
}

# group -> sub key 的唯一映射，供读取/归一化层使用。
FLAT_TO_NESTED: dict[str, tuple[str, str]] = {
    flat: (group, flat) for group, members in CONFIG_GROUPS.items() for flat in members
}
NESTED_TO_FLAT: dict[tuple[str, str], str] = {
    nested: flat for flat, nested in FLAT_TO_NESTED.items()
}

# 已废弃 key：从 schema 移除，读取端保留旧扁平兜底（不再写回）。
DEPRECATED_CONFIG_KEYS = frozenset(
    {
        "config_mode",
        "advanced_group_expand",
        "lite_llm_enabled",
        "dialogue_wait_enabled",
        "dialogue_wait_timeout_sec",
        "dialogue_wait_max_merge",
        "output_segment_enabled",
        "output_segment_max_parts",
        "output_segment_max_chars",
    }
)

SCHEMA_VERSION = 2


def _build_nested_defaults() -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for group, members in CONFIG_GROUPS.items():
        section: dict[str, Any] = {}
        for key in members:
            if key in DEFAULT_CONFIG_FLAT:
                section[key] = DEFAULT_CONFIG_FLAT[key]
            elif key in INTERNAL_POLICY:
                section[key] = INTERNAL_POLICY[key]
            else:  # pragma: no cover - 防御性校验
                raise KeyError(f"config key missing default: {key}")
        nested[group] = section
    return nested


DEFAULT_CONFIG = _build_nested_defaults()

VALID_DECISION_MODES = frozenset({"balanced", "strict", "active"})

_MISSING = object()


class PluginConfigView:
    """统一配置读取入口：优先新嵌套路径，回退旧扁平 key。

    - `get` / `[]` 读取：先查 `config[group][key]`，缺失时回退 `config[flat_key]`。
    - `[] =` 写入：统一落到新嵌套路径，旧扁平 key 不再被写入。
    - 其它属性（如 `save_config`）透传底层 AstrBotConfig 实例。
    """

    def __init__(self, raw: Any):
        object.__setattr__(self, "_raw", raw)

    def _resolve(self, key: str) -> Any:
        spec = FLAT_TO_NESTED.get(key)
        if spec is not None:
            group, sub = spec
            section = self._raw.get(group)
            if isinstance(section, dict):
                value = section.get(sub)
                if value is not None:
                    return value
        if key in self._raw:
            return self._raw[key]
        return _MISSING

    def get(self, key: str, default: Any = None) -> Any:
        value = self._resolve(key)
        return default if value is _MISSING else value

    def __getitem__(self, key: str) -> Any:
        value = self._resolve(key)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        spec = FLAT_TO_NESTED.get(key)
        if spec is None:
            self._raw[key] = value
            return
        group, sub = spec
        section = self._raw.get(group)
        if not isinstance(section, dict):
            section = {}
            self._raw[group] = section
        section[sub] = value

    def __contains__(self, key: str) -> bool:
        return self._resolve(key) is not _MISSING

    def save_config(self, *args: Any, **kwargs: Any) -> Any:
        save = getattr(self._raw, "save_config", None)
        if callable(save):
            return save(*args, **kwargs)
        return None

    def __getattr__(self, item: str) -> Any:
        return getattr(self._raw, item)


def migrate_flat_to_nested(data: dict) -> bool:
    """把旧扁平配置键幂等归一化进新嵌套结构，返回是否发生变更。

    旧 key 不删除；仅在嵌套位置缺失时写入。特殊历史字段：
    - `config_mode` -> `advanced_enabled`
    - `lite_llm_enabled` -> `generation.proactive_lite_refine_enabled` /
      `generation.holiday_qa_main_llm_enabled`（保持旧主开关语义）
    """
    changed = False

    if "config_mode" in data and "advanced_enabled" not in data:
        mode = str(data.get("config_mode") or "basic").strip().lower()
        data["advanced_enabled"] = mode == "advanced"
        changed = True

    if "lite_llm_enabled" in data:
        lite = bool(data.get("lite_llm_enabled"))
        section = data.get("generation")
        if not isinstance(section, dict):
            section = {}
            data["generation"] = section
        if section.get("proactive_lite_refine_enabled") != lite:
            section["proactive_lite_refine_enabled"] = lite
            changed = True

    for flat, (group, sub) in FLAT_TO_NESTED.items():
        if flat not in data:
            continue
        section = data.get(group)
        if not isinstance(section, dict):
            section = {}
            data[group] = section
        if sub not in section:
            section[sub] = data[flat]
            changed = True

    return changed


def _resolve_plugin_config_file() -> Path | None:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path
    except Exception:
        return None
    plugin_dir = Path(__file__).resolve().parent
    return Path(get_astrbot_config_path()) / f"{plugin_dir.name}_config.json"


def run_config_file_migration(path: str | os.PathLike | None = None) -> bool:
    """在 AstrBot 构造插件配置对象之前，迁移磁盘上的旧扁平配置。

    AstrBot 4.28.x 的 `AstrBotConfig.check_config_integrity` 会剔除不在新 schema
    中的旧 key，因此在插件模块导入期完成迁移是保留旧用户数值的关键时机。
    """
    target = Path(path) if path is not None else _resolve_plugin_config_file()
    if target is None or not target.exists():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    try:
        if not migrate_flat_to_nested(data):
            return False
        tmp = target.with_name(f".{target.name}.{os.getpid()}.migrate.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return True
    except Exception:
        return False


# 导入期执行一次（幂等、失败静默），确保旧配置在 schema 归一化前完成迁移。
run_config_file_migration()

EXECUTION_ORDER = (
    "unit_global_guard",
    "unit_rollover_counters",
    "unit_gate_next_check",
    "unit_gate_cooldown",
    "unit_gate_daily_limit",
    "unit_gate_pending_reply",
    "unit_gate_period_limit",
    "unit_gate_idle",
    "unit_gate_mood",
    "unit_gate_persona_state",
    "unit_gate_probability",
    "unit_gate_origin",
    "unit_execute_send",
    "unit_finalize_result",
)

CONFIG_EXECUTION_ORDER = (
    "config_defaults",
    "config_basic_layer",
    "config_timing_layer",
    "config_generation_layer",
    "config_security_layer",
    "config_debug_layer",
)

PLUGIN_VERSION = "2.3.0"
