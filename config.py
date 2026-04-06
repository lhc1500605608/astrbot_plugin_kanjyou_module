DEFAULT_CONFIG = {
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
    "lite_llm_enabled": True,
    "lite_provider_id": "",
    "lite_llm_timeout_sec": 6,
    "decision_mode": "balanced",
    "decision_min_confidence": 0.6,
    "decision_group_quiet_threshold": 3,
    "decision_trace_enabled": True,
    "quality_trace_enabled": True,
    "holiday_qa_main_llm_enabled": True,
    "proactive_lite_refine_enabled": True,
    "proactive_prompt_template": (
        "你是一个在聊天中主动关怀用户的助手。"
        "请严格基于下方人格设定进行表达，不要脱离人格。\n"
        "人格设定：\n{persona}\n\n"
        "当前会话类型：{session_type}\n"
        "环境感知信息：{env_perception}\n"
        "距离上次互动约 {idle_minutes} 分钟（{idle_seconds} 秒）。\n"
        "建议语气：{style_hint}\n"
        "最近已发过的主动问候（避免重复）：\n{recent_history}\n"
        "请输出 1 条中文主动问候（只输出消息正文，不加引号），要求：\n"
        "1) 语气自然、有温度，不要机械。\n"
        "2) 结尾带一个轻量开放问题，促进继续对话。\n"
        "3) 避免重复“在吗/你好”。\n"
        "4) 长度 20-60 字。\n"
        "5) 和最近问候不重复。"
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

PLUGIN_VERSION = "2.1.2"
