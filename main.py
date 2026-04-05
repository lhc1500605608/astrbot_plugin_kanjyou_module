import asyncio
import copy
import inspect
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

DEFAULT_CONFIG = {
    "enabled": True,
    "config_mode": "basic",
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
    "proactive_prompt_template": (
        "你是一个在聊天中主动关怀用户的助手。"
        "请严格基于下方人格设定进行表达，不要脱离人格。\n"
        "人格设定：\n{persona}\n\n"
        "当前会话类型：{session_type}\n"
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
    "security_global_hourly_cap": 6,
    "security_max_fail_streak": 3,
    "security_fail_pause_min": 180,
    "security_allow_links": False,
    "security_blocked_words": [],
    "security_max_text_length": 90,
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

@register("kanjyou_idle_proactive", "Tango", "闲时主动聊天：分会话计时、白名单、夜间免打扰", "1.7.1")
class KanjyouIdleProactivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._state_path = Path(__file__).parent / "idle_state.json"
        self._normalize_webui_config()
        self._sessions: Dict[str, Dict] = self._load_state()
        self._debug_status_last: Dict[str, float] = {}
        self._global_send_history: List[float] = []
        self._global_fail_streak: int = 0
        self._global_pause_until: float = 0.0
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._idle_loop())
        if self.config.get("lifecycle_log", True):
            logger.info("[idle-proactive] initialized")
        self._debug(f"execution order: {' -> '.join(EXECUTION_ORDER)}")
        self._debug(f"config order: {' -> '.join(CONFIG_EXECUTION_ORDER)}")
        self._debug("plugin initialize complete")

    async def terminate(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._save_state()
        if self.config.get("lifecycle_log", True):
            logger.info("[idle-proactive] terminated")
        self._debug("plugin terminate complete")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        session_key = self._session_key(event)
        if not session_key:
            self._debug("skip message: session key unavailable")
            return

        if not self._is_whitelisted(event):
            self._debug(f"skip message: not in whitelist session={session_key}")
            return

        now_ts = self._now().timestamp()
        async with self._lock:
            s = self._get_or_create_session(event)
            self._ensure_session_shape(s)
            s["last_human_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["pending_human_reply"] = False
            s["no_reply_streak"] = 0
            s["next_check_at"] = now_ts + self._randomized_interval()
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by human session={session_key} last_interaction={self._fmt_ts(now_ts)} next_check={self._fmt_ts(s['next_check_at'])}"
            )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        session_key = self._session_key(event)
        if not session_key:
            return

        if not self._is_whitelisted(event):
            return

        now_ts = self._now().timestamp()
        async with self._lock:
            s = self._get_or_create_session(event)
            self._ensure_session_shape(s)
            s["last_bot_at"] = now_ts
            s["last_interaction_at"] = now_ts
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by bot session={session_key} last_interaction={self._fmt_ts(now_ts)}"
            )

    @filter.command("idle_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_status(self, event: AstrMessageEvent):
        now = self._now()
        session_key = self._session_key(event)
        if not session_key:
            yield event.plain_result("当前会话无法识别，无法查看 idle 状态。")
            return

        s = self._sessions.get(session_key)
        if not s:
            yield event.plain_result("当前会话还没有记录。先聊一句再查看状态。")
            return

        idle_sec = int(now.timestamp() - s.get("last_interaction_at", now.timestamp()))
        sleep_on = self._in_sleep_window(now)
        summary = (
            f"enabled={self.config['enabled']} | idle={idle_sec}s | "
            f"today_count={s.get('today_proactive_count', 0)} | "
            f"cooldown_until={self._fmt_ts(s.get('cooldown_until'))} | "
            f"sleep_mode={sleep_on}"
        )
        yield event.plain_result(summary)

    @filter.command("idle_enable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_enable(self, event: AstrMessageEvent):
        self.config["enabled"] = True
        self._save_webui_config()
        yield event.plain_result("已开启闲时主动聊天。")

    @filter.command("idle_disable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_disable(self, event: AstrMessageEvent):
        self.config["enabled"] = False
        self._save_webui_config()
        yield event.plain_result("已关闭闲时主动聊天。")

    @filter.command("idle_wl_add_private")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_add_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self.config["private_whitelist"]
        if user_id not in wl:
            wl.append(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已添加: {user_id}")

    @filter.command("idle_wl_del_private")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_del_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self.config["private_whitelist"]
        if user_id in wl:
            wl.remove(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已移除: {user_id}")

    @filter.command("idle_wl_add_group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_add_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self.config["group_whitelist"]
        if group_id not in wl:
            wl.append(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已添加: {group_id}")

    @filter.command("idle_wl_del_group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_del_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self.config["group_whitelist"]
        if group_id in wl:
            wl.remove(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已移除: {group_id}")

    @filter.command("idle_sleep_set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_sleep_set(self, event: AstrMessageEvent, start_hm: str, end_hm: str):
        if not self._is_hhmm(start_hm) or not self._is_hhmm(end_hm):
            yield event.plain_result("格式错误，请使用 HH:MM，例如 /idle_sleep_set 23:30 08:00")
            return
        self.config["sleep_start"] = start_hm
        self.config["sleep_end"] = end_hm
        self._save_webui_config()
        yield event.plain_result(f"免打扰时段已设置为 {start_hm}-{end_hm}")

    @filter.command("idle_test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_test(self, event: AstrMessageEvent):
        if not self._is_whitelisted(event):
            yield event.plain_result("当前会话不在白名单，先加入白名单再测试。")
            return
        await self._send_proactive(event.unified_msg_origin, event, self._session_key(event), 0, None)
        yield event.plain_result("已发送一条主动话题测试消息。")

    async def _idle_loop(self):
        while True:
            try:
                await asyncio.sleep(self.config["check_interval_sec"])
                await self._check_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[idle-proactive] idle loop error: {exc}")

    async def _check_sessions(self):
        if not self.config.get("enabled", True):
            self._debug("loop skip: plugin disabled")
            return

        now = self._now()
        if self._in_sleep_window(now):
            self._debug(f"loop skip: in sleep window now={now.strftime('%H:%M')}")
            return

        now_ts = now.timestamp()
        if not self._unit_global_guard(now_ts):
            return

        changed = False

        async with self._lock:
            for session_key, s in list(self._sessions.items()):
                session_changed = await self._process_session(session_key, s, now, now_ts)
                changed = changed or session_changed

            if changed:
                self._save_state()
                self._debug("state persisted")

    async def _process_session(self, session_key: str, s: Dict, now: datetime, now_ts: float) -> bool:
        self._unit_rollover_counters(s, now)

        if self._unit_gate_next_check(session_key, s, now_ts):
            return False
        if self._unit_gate_cooldown(session_key, s, now_ts):
            return True
        if self._unit_gate_daily_limit(session_key, s, now_ts):
            return True
        if self._unit_gate_pending_reply(session_key, s, now_ts):
            return True

        period = self._get_period(now)
        if self._unit_gate_period_limit(session_key, s, period, now, now_ts):
            return True

        idle_sec = now_ts - s.get("last_interaction_at", now_ts)
        decay = self._no_reply_decay_factor(s)
        if self._unit_gate_idle(session_key, s, idle_sec, decay, now, now_ts):
            return True
        if self._unit_gate_probability(session_key, s, idle_sec, now, now_ts):
            return True

        umo = s.get("unified_msg_origin")
        if self._unit_gate_origin(session_key, s, umo, now_ts):
            return True

        success, sent_text = await self._unit_execute_send(umo, session_key, idle_sec, s)
        self._unit_finalize_result(session_key, s, success, sent_text, period, idle_sec, decay, now, now_ts)
        return True

    def _unit_global_guard(self, now_ts: float) -> bool:
        if now_ts < self._global_pause_until:
            self._debug(
                f"loop skip: global pause active until={self._fmt_ts(self._global_pause_until)}"
            )
            return False

        self._trim_global_send_history(now_ts)
        if len(self._global_send_history) >= self._security_global_hourly_cap():
            self._debug(
                f"loop skip: global hourly cap reached count={len(self._global_send_history)} cap={self._security_global_hourly_cap()}"
            )
            return False
        return True

    def _unit_rollover_counters(self, s: Dict, now: datetime):
        self._ensure_session_shape(s)
        self._rollover_daily_counter(s, now)
        self._rollover_period_counter(s, now)

    def _unit_gate_next_check(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if now_ts >= s.get("next_check_at", 0):
            return False
        self._maybe_log_status(session_key, s, now_ts, "waiting_next_check")
        self._debug(
            f"session skip(next_check) session={session_key} next_check={self._fmt_ts(s.get('next_check_at'))}"
        )
        return True

    def _unit_defer_session(self, session_key: str, s: Dict, now_ts: float, reason: str, debug_msg: str):
        s["next_check_at"] = now_ts + self._randomized_interval()
        self._maybe_log_status(session_key, s, now_ts, reason)
        self._debug(debug_msg)

    def _unit_gate_cooldown(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if now_ts >= s.get("cooldown_until", 0):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "cooldown",
            f"session skip(cooldown) session={session_key} cooldown_until={self._fmt_ts(s.get('cooldown_until'))}",
        )
        return True

    def _unit_gate_daily_limit(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if s.get("today_proactive_count", 0) < self._max_per_session_per_day():
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "daily_limit",
            f"session skip(limit) session={session_key} today_count={s.get('today_proactive_count', 0)}",
        )
        return True

    def _unit_gate_pending_reply(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if not self._require_human_reply_before_next_proactive():
            return False
        if not s.get("pending_human_reply", False):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "await_human_reply",
            f"session skip(await_human_reply) session={session_key}",
        )
        return True

    def _unit_gate_period_limit(
        self, session_key: str, s: Dict, period: str, now: datetime, now_ts: float
    ) -> bool:
        if not self._period_quota_enabled() or not period:
            return False
        counters = s.get("period_proactive_count")
        if not isinstance(counters, dict):
            counters = {"morning": 0, "afternoon": 0, "evening": 0}
            s["period_proactive_count"] = counters
        current_period_count = int(counters.get(period, 0))
        period_limit = self._effective_period_quota_limit(period, now)
        if current_period_count < period_limit:
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            f"period_limit_{period}",
            (
                f"session skip(period_limit) session={session_key} "
                f"period={period} count={current_period_count} limit={period_limit}"
            ),
        )
        return True

    def _unit_gate_idle(
        self, session_key: str, s: Dict, idle_sec: float, decay: float, now: datetime, now_ts: float
    ) -> bool:
        needed_idle_sec = int(self._effective_min_idle_sec(now) * decay)
        if idle_sec >= needed_idle_sec:
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "idle_not_enough",
            (
                f"session skip(idle_short) session={session_key} "
                f"idle_sec={int(idle_sec)} min_idle={needed_idle_sec} decay={decay:.2f}"
            ),
        )
        return True

    def _unit_gate_probability(
        self, session_key: str, s: Dict, idle_sec: float, now: datetime, now_ts: float
    ) -> bool:
        if self._should_trigger(float(idle_sec), now):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "probability_miss",
            f"session skip(probability) session={session_key} idle_sec={int(idle_sec)}",
        )
        return True

    def _unit_gate_origin(self, session_key: str, s: Dict, umo: str, now_ts: float) -> bool:
        if umo:
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "missing_origin",
            f"session skip(no_origin) session={session_key}",
        )
        return True

    async def _unit_execute_send(self, umo: str, session_key: str, idle_sec: float, s: Dict) -> tuple[bool, str]:
        return await self._send_proactive(umo, None, session_key, idle_sec, s)

    def _unit_finalize_result(
        self,
        session_key: str,
        s: Dict,
        success: bool,
        sent_text: str,
        period: str,
        idle_sec: float,
        decay: float,
        now: datetime,
        now_ts: float,
    ):
        s["next_check_at"] = now_ts + self._randomized_interval()
        if success:
            self._global_send_history.append(now_ts)
            self._global_fail_streak = 0
            s["last_bot_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["today_proactive_count"] = int(s.get("today_proactive_count", 0)) + 1
            s["cooldown_until"] = now_ts + int(self._effective_cooldown_sec(now) * decay)
            s["pending_human_reply"] = True
            s["no_reply_streak"] = int(s.get("no_reply_streak", 0)) + 1
            self._inc_period_count(s, period)
            self._push_proactive_history(s, sent_text)
            self._debug(
                f"session trigger(success) session={session_key} idle_sec={int(idle_sec)} "
                f"today_count={s['today_proactive_count']} no_reply_streak={s.get('no_reply_streak', 0)} decay={decay:.2f}"
            )
            self._maybe_log_status(session_key, s, now_ts, "trigger_success", force=True)
            return

        self._global_fail_streak += 1
        if self._global_fail_streak >= self._security_max_fail_streak():
            self._global_pause_until = now_ts + self._security_fail_pause_sec()
            self._debug(
                f"trigger safety pause fail_streak={self._global_fail_streak} pause_until={self._fmt_ts(self._global_pause_until)}"
            )
        self._debug(f"session trigger(failed) session={session_key}")
        self._maybe_log_status(session_key, s, now_ts, "trigger_failed", force=True)

    async def _send_proactive(
        self,
        unified_msg_origin: str,
        event: Optional[AstrMessageEvent],
        session_key: str = "",
        idle_sec: float = 0.0,
        session: Optional[Dict] = None,
    ) -> tuple[bool, str]:
        topic = await self._generate_proactive_text(unified_msg_origin, session_key, idle_sec, session)
        try:
            chain = MessageChain().message(topic)
            await self.context.send_message(unified_msg_origin, chain)
            self._debug(f"send proactive ok session={session_key} topic={topic}")
            return True, topic
        except Exception:
            try:
                # 兼容部分适配器对 MessageChain 构造差异
                await self.context.send_message(unified_msg_origin, [Plain(topic)])
                self._debug(f"send proactive ok(fallback) session={session_key} topic={topic}")
                return True, topic
            except Exception as exc:
                logger.error(f"[idle-proactive] send failed: {exc}")
                self._debug(f"send proactive failed session={session_key} err={exc}")
                if event:
                    await event.send(event.plain_result("主动消息发送失败，请检查适配器是否支持主动消息。"))
                return False, ""

    async def _generate_proactive_text(
        self, unified_msg_origin: str, session_key: str, idle_sec: float, session: Optional[Dict]
    ) -> str:
        fallback = self._sanitize_outgoing_text(
            str(self.config.get("fallback_proactive_text") or DEFAULT_CONFIG["fallback_proactive_text"]).strip()
        )
        try:
            session_type = "私聊" if session_key.startswith("private:") else "群聊"
            persona_text = await self._resolve_persona_prompt()
            style_hint = self._style_hint(session_key, session, idle_sec)
            recent_history = self._recent_history_text(session)
            prompt_tpl = str(
                self.config.get("proactive_prompt_template") or DEFAULT_CONFIG["proactive_prompt_template"]
            )
            prompt = prompt_tpl.format(
                persona=persona_text,
                session_type=session_type,
                idle_seconds=int(idle_sec),
                idle_minutes=max(1, int(idle_sec // 60)),
                style_hint=style_hint,
                recent_history=recent_history,
            )

            provider_id = str(self.config.get("proactive_provider_id") or "").strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(unified_msg_origin)

            if not provider_id:
                self._debug("generate skip: provider_id unavailable, use fallback")
                return fallback

            completion = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            text = self._completion_to_text(completion)
            if not text:
                self._debug("generate empty completion, use fallback")
                return fallback
            cleaned = self._clean_generated_text(text)
            if not self._is_safe_proactive_text(cleaned):
                self._debug("generate unsafe text blocked, use fallback")
                return fallback
            if self._is_repetitive(cleaned, session):
                self._debug("generate repetitive text, use fallback")
                return fallback
            self._debug(f"generate ok provider={provider_id} session={session_key} text={cleaned}")
            return cleaned
        except Exception as exc:
            logger.error(f"[idle-proactive] generate proactive text failed: {exc}")
            self._debug(f"generate failed session={session_key} err={exc}")
            return fallback

    def _should_trigger(self, idle_sec: float, now: datetime) -> bool:
        min_idle = float(self._effective_min_idle_sec(now))
        max_idle = float(self._effective_max_idle_sec(now))

        if idle_sec >= max_idle:
            return True

        span = max(max_idle - min_idle, 1.0)
        progress = max(0.0, min(1.0, (idle_sec - min_idle) / span))
        base_prob = float(self._trigger_base_prob())  # 刚到最小 idle 时也有少量概率
        max_prob = float(self._trigger_max_prob())
        p = base_prob + (max_prob - base_prob) * progress
        return random.random() < p

    def _min_idle_sec(self) -> int:
        return max(60, int(float(self.config["min_idle_min"]) * 60))

    def _max_idle_sec(self) -> int:
        return max(self._min_idle_sec() + 60, int(float(self.config["max_idle_min"]) * 60))

    def _effective_max_idle_sec(self, now: datetime) -> int:
        base = float(self._max_idle_sec())
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return int(base)
        mul = max(1.0, float(self._weekend_min_idle_multiplier()))
        return int(base * mul)

    def _cooldown_sec(self) -> int:
        return max(60, int(float(self.config["cooldown_min"]) * 60))

    def _policy(self, key: str):
        default = INTERNAL_POLICY[key]
        if not self._advanced_mode():
            return default
        raw = self.config.get(key)
        if raw is None:
            return default

        try:
            if key in {
                "require_human_reply_before_next_proactive",
                "period_quota_enabled",
                "no_reply_decay_enabled",
                "weekend_mode_enabled",
                "quality_dedupe_enabled",
            }:
                return self._to_bool(raw, default)
            if key == "max_per_session_per_day":
                return max(1, int(raw))
            if key in {"trigger_base_prob", "trigger_max_prob"}:
                return min(1.0, max(0.0, float(raw)))
            if key in {"period_quota_morning_max", "period_quota_afternoon_max", "period_quota_evening_max"}:
                return max(0, int(raw))
            if key in {"no_reply_decay_factor", "no_reply_decay_max_factor"}:
                return max(1.0, float(raw))
            if key in {"weekend_min_idle_multiplier", "weekend_cooldown_multiplier"}:
                return max(1.0, float(raw))
            if key == "weekend_quota_multiplier":
                return max(0.0, float(raw))
            if key == "quality_history_size":
                return max(1, int(raw))
        except Exception:
            return default
        return default

    def _advanced_mode(self) -> bool:
        return str(self.config.get("config_mode", "basic")).lower() == "advanced"

    def _max_per_session_per_day(self) -> int:
        return int(self._policy("max_per_session_per_day"))

    def _trigger_base_prob(self) -> float:
        return float(self._policy("trigger_base_prob"))

    def _trigger_max_prob(self) -> float:
        return max(self._trigger_base_prob(), float(self._policy("trigger_max_prob")))

    def _require_human_reply_before_next_proactive(self) -> bool:
        return bool(self._policy("require_human_reply_before_next_proactive"))

    def _period_quota_enabled(self) -> bool:
        return bool(self._policy("period_quota_enabled"))

    def _period_quota_morning_max(self) -> int:
        return int(self._policy("period_quota_morning_max"))

    def _period_quota_afternoon_max(self) -> int:
        return int(self._policy("period_quota_afternoon_max"))

    def _period_quota_evening_max(self) -> int:
        return int(self._policy("period_quota_evening_max"))

    def _no_reply_decay_enabled(self) -> bool:
        return bool(self._policy("no_reply_decay_enabled"))

    def _no_reply_decay_factor_base(self) -> float:
        return float(self._policy("no_reply_decay_factor"))

    def _no_reply_decay_max_factor(self) -> float:
        return float(self._policy("no_reply_decay_max_factor"))

    def _weekend_mode_enabled(self) -> bool:
        return bool(self._policy("weekend_mode_enabled"))

    def _weekend_min_idle_multiplier(self) -> float:
        return float(self._policy("weekend_min_idle_multiplier"))

    def _weekend_cooldown_multiplier(self) -> float:
        return float(self._policy("weekend_cooldown_multiplier"))

    def _weekend_quota_multiplier(self) -> float:
        return float(self._policy("weekend_quota_multiplier"))

    def _quality_dedupe_enabled(self) -> bool:
        return bool(self._policy("quality_dedupe_enabled"))

    def _quality_history_size(self) -> int:
        return int(self._policy("quality_history_size"))

    def _is_weekend(self, now: datetime) -> bool:
        # Monday=0 ... Sunday=6
        return now.weekday() >= 5

    def _effective_min_idle_sec(self, now: datetime) -> int:
        base = float(self._min_idle_sec())
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return int(base)
        mul = max(1.0, float(self._weekend_min_idle_multiplier()))
        return int(base * mul)

    def _effective_cooldown_sec(self, now: datetime) -> int:
        base = float(self._cooldown_sec())
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return int(base)
        mul = max(1.0, float(self._weekend_cooldown_multiplier()))
        return int(base * mul)

    def _get_period(self, now: datetime) -> str:
        hm = now.strftime("%H:%M")
        if "06:00" <= hm <= "11:59":
            return "morning"
        if "12:00" <= hm <= "17:59":
            return "afternoon"
        if "18:00" <= hm <= "22:59":
            return "evening"
        return "offhours"

    def _get_period_quota_limit(self, period: str) -> int:
        mapping = {
            "morning": int(self._period_quota_morning_max()),
            "afternoon": int(self._period_quota_afternoon_max()),
            "evening": int(self._period_quota_evening_max()),
            "offhours": 0,
        }
        return max(0, mapping.get(period, 0))

    def _effective_period_quota_limit(self, period: str, now: datetime) -> int:
        limit = self._get_period_quota_limit(period)
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return limit
        mul = max(0.0, float(self._weekend_quota_multiplier()))
        return max(0, int(limit * mul))

    def _no_reply_decay_factor(self, session: Dict) -> float:
        if not self._no_reply_decay_enabled():
            return 1.0
        streak = max(0, int(session.get("no_reply_streak", 0)))
        if streak <= 0:
            return 1.0
        base = max(1.0, float(self._no_reply_decay_factor_base()))
        max_factor = max(1.0, float(self._no_reply_decay_max_factor()))
        factor = base ** streak
        return min(max_factor, factor)

    def _style_hint(self, session_key: str, session: Optional[Dict], idle_sec: float) -> str:
        if session_key.startswith("group:"):
            if idle_sec > 6 * 3600:
                return "群聊里简短自然、轻松抛题，不要过度热情"
            return "群聊里简短友好、克制不刷屏"
        streak = int((session or {}).get("no_reply_streak", 0))
        if streak >= 2:
            return "私聊里温和克制、低打扰、避免连续追问"
        if idle_sec > 4 * 3600:
            return "私聊里温暖真诚、轻柔开启话题"
        return "私聊里自然亲切、像朋友一样"

    def _recent_history_text(self, session: Optional[Dict]) -> str:
        history = (session or {}).get("recent_proactive_texts", [])
        if not isinstance(history, list) or not history:
            return "无"
        return "\n".join(f"- {str(x)}" for x in history[-5:])

    def _push_proactive_history(self, session: Dict, text: str):
        if not self._quality_dedupe_enabled():
            return
        if not text:
            return
        history = session.get("recent_proactive_texts")
        if not isinstance(history, list):
            history = []
        history.append(text.strip())
        size = max(1, int(self._quality_history_size()))
        session["recent_proactive_texts"] = history[-size:]

    def _is_repetitive(self, text: str, session: Optional[Dict]) -> bool:
        if not self._quality_dedupe_enabled():
            return False
        if not text:
            return True
        history = (session or {}).get("recent_proactive_texts", [])
        if not isinstance(history, list):
            return False
        normalized = "".join(text.lower().split())
        for h in history:
            hn = "".join(str(h).lower().split())
            if normalized == hn:
                return True
        return False

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return False

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(getattr(getattr(msg_obj, "sender", None), "user_id", "") or event.get_sender_id())

        if group_id:
            return group_id in self.config["group_whitelist"]
        return sender_id in self.config["private_whitelist"]

    def _session_key(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(getattr(getattr(msg_obj, "sender", None), "user_id", "") or event.get_sender_id())
        if group_id:
            return f"group:{group_id}"
        return f"private:{sender_id}"

    async def _resolve_persona_prompt(self) -> str:
        persona_id = str(self.config.get("persona_id") or "").strip()
        if not persona_id:
            return "未指定人格，请保持温暖、真诚、自然。"
        try:
            manager = getattr(self.context, "persona_manager", None)
            if not manager:
                return f"人格ID: {persona_id}"
            get_func = getattr(manager, "get_persona", None)
            if not callable(get_func):
                return f"人格ID: {persona_id}"
            persona = get_func(persona_id)
            if inspect.isawaitable(persona):
                persona = await persona
            if not persona:
                return f"人格ID: {persona_id}"
            for field in ("prompt", "system_prompt", "content", "description"):
                val = getattr(persona, field, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return str(persona)
        except Exception as exc:
            self._debug(f"resolve persona failed id={persona_id} err={exc}")
            return f"人格ID: {persona_id}"

    def _completion_to_text(self, completion: object) -> str:
        text = getattr(completion, "completion_text", None)
        if isinstance(text, str):
            return text.strip()
        if isinstance(completion, str):
            return completion.strip()
        return str(completion).strip()

    def _clean_generated_text(self, text: str) -> str:
        cleaned = self._sanitize_outgoing_text(text.strip().strip('"').strip("'"))
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if lines:
            cleaned = lines[0]
        max_len = max(20, int(self.config.get("security_max_text_length", DEFAULT_CONFIG["security_max_text_length"])))
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip("，,。.!?？") + "。"
        return cleaned

    def _sanitize_outgoing_text(self, text: str) -> str:
        cleaned = text or ""
        if not self._security_allow_links():
            lowered = cleaned.lower()
            if "http://" in lowered or "https://" in lowered or "www." in lowered:
                cleaned = "刚刚想到你，今天有哪件小事值得被夸一下？"
        return cleaned.strip()

    def _is_safe_proactive_text(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if not self._security_allow_links():
            if "http://" in lowered or "https://" in lowered or "www." in lowered:
                return False
        blocked_words = self._security_blocked_words()
        for w in blocked_words:
            if w and w in text:
                return False
        return True

    def _get_or_create_session(self, event: AstrMessageEvent) -> Dict:
        key = self._session_key(event)
        now_ts = self._now().timestamp()
        old = self._sessions.get(key)
        if old:
            old["unified_msg_origin"] = event.unified_msg_origin
            return old

        return {
            "session_key": key,
            "unified_msg_origin": event.unified_msg_origin,
            "last_human_at": now_ts,
            "last_bot_at": 0.0,
            "last_interaction_at": now_ts,
            "next_check_at": now_ts + self._randomized_interval(),
            "today_proactive_count": 0,
            "counter_date": self._now().strftime("%Y-%m-%d"),
            "cooldown_until": 0.0,
            "pending_human_reply": False,
            "no_reply_streak": 0,
            "period_counter_date": self._now().strftime("%Y-%m-%d"),
            "period_proactive_count": {"morning": 0, "afternoon": 0, "evening": 0},
            "recent_proactive_texts": [],
        }

    def _ensure_session_shape(self, session: Dict):
        if "pending_human_reply" not in session:
            session["pending_human_reply"] = False
        if "no_reply_streak" not in session:
            session["no_reply_streak"] = 0
        if "period_counter_date" not in session:
            session["period_counter_date"] = self._now().strftime("%Y-%m-%d")
        if not isinstance(session.get("period_proactive_count"), dict):
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}
        if not isinstance(session.get("recent_proactive_texts"), list):
            session["recent_proactive_texts"] = []

    def _rollover_daily_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("counter_date") != today:
            session["counter_date"] = today
            session["today_proactive_count"] = 0
            session["period_counter_date"] = today
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}

    def _rollover_period_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("period_counter_date") != today:
            session["period_counter_date"] = today
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}
            return
        if not isinstance(session.get("period_proactive_count"), dict):
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}

    def _inc_period_count(self, session: Dict, period: str):
        if period not in {"morning", "afternoon", "evening"}:
            return
        counters = session.get("period_proactive_count")
        if not isinstance(counters, dict):
            counters = {"morning": 0, "afternoon": 0, "evening": 0}
        counters[period] = int(counters.get(period, 0)) + 1
        session["period_proactive_count"] = counters

    def _in_sleep_window(self, now: datetime) -> bool:
        hm = now.strftime("%H:%M")
        start = self.config.get("sleep_start")
        end = self.config.get("sleep_end")
        if not (isinstance(start, str) and isinstance(end, str) and self._is_hhmm(start) and self._is_hhmm(end)):
            start = DEFAULT_CONFIG["sleep_start"]
            end = DEFAULT_CONFIG["sleep_end"]

        if start <= end:
            if start <= hm <= end:
                return True
        else:
            # 跨天窗口，例如 23:30-08:00
            if hm >= start or hm <= end:
                return True
        return False

    def _randomized_interval(self) -> int:
        base = int(self.config["check_interval_sec"])
        low = max(5, int(base * 0.85))
        high = max(low + 1, int(base * 1.25))
        return random.randint(low, high)

    def _is_hhmm(self, val: str) -> bool:
        try:
            datetime.strptime(val, "%H:%M")
            return True
        except ValueError:
            return False

    def _now(self) -> datetime:
        tz = ZoneInfo(self.config["timezone"])
        return datetime.now(tz)

    def _fmt_ts(self, ts: Optional[float]) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts, ZoneInfo(self.config["timezone"])).strftime("%Y-%m-%d %H:%M:%S")

    def _trim_global_send_history(self, now_ts: float):
        window_start = now_ts - 3600
        self._global_send_history = [ts for ts in self._global_send_history if ts >= window_start]

    def _security_global_hourly_cap(self) -> int:
        return max(1, int(self.config.get("security_global_hourly_cap", DEFAULT_CONFIG["security_global_hourly_cap"])))

    def _security_max_fail_streak(self) -> int:
        return max(1, int(self.config.get("security_max_fail_streak", DEFAULT_CONFIG["security_max_fail_streak"])))

    def _security_fail_pause_sec(self) -> int:
        return max(300, int(float(self.config.get("security_fail_pause_min", DEFAULT_CONFIG["security_fail_pause_min"])) * 60))

    def _security_allow_links(self) -> bool:
        return self._to_bool(
            self.config.get("security_allow_links", DEFAULT_CONFIG["security_allow_links"]),
            DEFAULT_CONFIG["security_allow_links"],
        )

    def _security_blocked_words(self) -> List[str]:
        raw = self.config.get("security_blocked_words", DEFAULT_CONFIG["security_blocked_words"])
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            token = item.strip()
            if token:
                out.append(token)
        return out

    def _to_bool(self, value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "on"}:
                return True
            if v in {"0", "false", "no", "off"}:
                return False
        return default

    def _normalize_webui_config(self):
        changed = False
        changed = self._normalize_defaults() or changed
        changed = self._normalize_basic_layer() or changed
        changed = self._normalize_timing_layer() or changed
        changed = self._normalize_generation_layer() or changed
        changed = self._normalize_security_layer() or changed
        changed = self._normalize_debug_layer() or changed

        if changed:
            self._save_webui_config()

    def _normalize_defaults(self) -> bool:
        changed = False
        for key, value in DEFAULT_CONFIG.items():
            if self.config.get(key) is None:
                self.config[key] = copy.deepcopy(value)
                changed = True
        return changed

    def _normalize_basic_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("private_whitelist"), list):
            self.config["private_whitelist"] = copy.deepcopy(DEFAULT_CONFIG["private_whitelist"])
            changed = True
        if not isinstance(self.config.get("group_whitelist"), list):
            self.config["group_whitelist"] = copy.deepcopy(DEFAULT_CONFIG["group_whitelist"])
            changed = True

        mode = str(self.config.get("config_mode", "basic")).lower()
        if mode not in {"basic", "advanced"}:
            self.config["config_mode"] = "basic"
            changed = True

        for key in ("enabled", "lifecycle_log", "debug_log"):
            if not isinstance(self.config.get(key), bool):
                self.config[key] = self._to_bool(self.config.get(key), DEFAULT_CONFIG[key])
                changed = True
        return changed

    def _normalize_timing_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("sleep_start"), str):
            self.config["sleep_start"] = DEFAULT_CONFIG["sleep_start"]
            changed = True
        if not isinstance(self.config.get("sleep_end"), str):
            self.config["sleep_end"] = DEFAULT_CONFIG["sleep_end"]
            changed = True

        # 兼容旧版秒级配置，自动迁移为分钟配置。
        if self.config.get("min_idle_min") is None and isinstance(self.config.get("min_idle_sec"), (int, float)):
            self.config["min_idle_min"] = max(1, int(float(self.config["min_idle_sec"]) // 60))
            changed = True
        if self.config.get("max_idle_min") is None and isinstance(self.config.get("max_idle_sec"), (int, float)):
            self.config["max_idle_min"] = max(1, int(float(self.config["max_idle_sec"]) // 60))
            changed = True
        if self.config.get("cooldown_min") is None and isinstance(self.config.get("cooldown_sec"), (int, float)):
            self.config["cooldown_min"] = max(1, int(float(self.config["cooldown_sec"]) // 60))
            changed = True

        if not isinstance(self.config.get("min_idle_min"), (int, float)):
            self.config["min_idle_min"] = DEFAULT_CONFIG["min_idle_min"]
            changed = True
        if not isinstance(self.config.get("max_idle_min"), (int, float)):
            self.config["max_idle_min"] = DEFAULT_CONFIG["max_idle_min"]
            changed = True
        if not isinstance(self.config.get("cooldown_min"), (int, float)):
            self.config["cooldown_min"] = DEFAULT_CONFIG["cooldown_min"]
            changed = True

        if float(self.config["max_idle_min"]) <= float(self.config["min_idle_min"]):
            self.config["max_idle_min"] = max(
                float(self.config["min_idle_min"]) + 30, float(DEFAULT_CONFIG["max_idle_min"])
            )
            changed = True
        return changed

    def _normalize_generation_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("persona_id"), str):
            self.config["persona_id"] = DEFAULT_CONFIG["persona_id"]
            changed = True
        if not isinstance(self.config.get("proactive_provider_id"), str):
            self.config["proactive_provider_id"] = DEFAULT_CONFIG["proactive_provider_id"]
            changed = True
        if not isinstance(self.config.get("proactive_prompt_template"), str) or not self.config[
            "proactive_prompt_template"
        ].strip():
            self.config["proactive_prompt_template"] = DEFAULT_CONFIG["proactive_prompt_template"]
            changed = True
        if not isinstance(self.config.get("fallback_proactive_text"), str) or not self.config[
            "fallback_proactive_text"
        ].strip():
            self.config["fallback_proactive_text"] = DEFAULT_CONFIG["fallback_proactive_text"]
            changed = True
        return changed

    def _normalize_security_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("security_allow_links"), bool):
            self.config["security_allow_links"] = self._to_bool(
                self.config.get("security_allow_links"), DEFAULT_CONFIG["security_allow_links"]
            )
            changed = True
        if not isinstance(self.config.get("security_blocked_words"), list):
            self.config["security_blocked_words"] = copy.deepcopy(DEFAULT_CONFIG["security_blocked_words"])
            changed = True

        if not isinstance(self.config.get("security_global_hourly_cap"), (int, float)):
            self.config["security_global_hourly_cap"] = DEFAULT_CONFIG["security_global_hourly_cap"]
            changed = True
        if int(self.config.get("security_global_hourly_cap", 0)) < 1:
            self.config["security_global_hourly_cap"] = 1
            changed = True
        if not isinstance(self.config.get("security_max_fail_streak"), (int, float)):
            self.config["security_max_fail_streak"] = DEFAULT_CONFIG["security_max_fail_streak"]
            changed = True
        if int(self.config.get("security_max_fail_streak", 0)) < 1:
            self.config["security_max_fail_streak"] = 1
            changed = True
        if not isinstance(self.config.get("security_fail_pause_min"), (int, float)):
            self.config["security_fail_pause_min"] = DEFAULT_CONFIG["security_fail_pause_min"]
            changed = True
        if float(self.config.get("security_fail_pause_min", 0)) < 5:
            self.config["security_fail_pause_min"] = 5
            changed = True
        if not isinstance(self.config.get("security_max_text_length"), (int, float)):
            self.config["security_max_text_length"] = DEFAULT_CONFIG["security_max_text_length"]
            changed = True
        if int(self.config.get("security_max_text_length", 0)) < 20:
            self.config["security_max_text_length"] = 20
            changed = True
        return changed

    def _normalize_debug_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("debug_status_window_sec"), int):
            self.config["debug_status_window_sec"] = DEFAULT_CONFIG["debug_status_window_sec"]
            changed = True
        if int(self.config.get("debug_status_window_sec", 0)) < 60:
            self.config["debug_status_window_sec"] = 60
            changed = True
        return changed

    def _save_webui_config(self):
        save_func = getattr(self.config, "save_config", None)
        if callable(save_func):
            try:
                save_func()
            except Exception as exc:
                logger.error(f"[idle-proactive] save webui config failed: {exc}")

    def _load_state(self) -> Dict[str, Dict]:
        if not self._state_path.exists():
            return {}
        try:
            data = self._read_json(self._state_path)
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:
            logger.error(f"[idle-proactive] load state failed: {exc}")
            return {}

    def _save_state(self):
        self._write_json(self._state_path, self._sessions)

    def _read_json(self, path: Path):
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data):
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _debug(self, msg: str):
        if self.config.get("debug_log", False):
            logger.debug(f"[idle-proactive] {msg}")

    def _maybe_log_status(self, session_key: str, s: Dict, now_ts: float, reason: str, force: bool = False):
        if not self.config.get("debug_log", False):
            return

        window = max(60, int(self.config.get("debug_status_window_sec", 300)))
        last = self._debug_status_last.get(session_key, 0.0)
        if not force and (now_ts - last) < window:
            return

        self._debug_status_last[session_key] = now_ts
        idle_sec = max(0, int(now_ts - s.get("last_interaction_at", now_ts)))
        cooldown_left = max(0, int(s.get("cooldown_until", 0) - now_ts))
        next_check_at = float(s.get("next_check_at", now_ts))
        next_check_in = max(0, int(next_check_at - now_ts))
        now_dt = self._now()
        min_idle_at = float(s.get("last_interaction_at", now_ts)) + float(self._effective_min_idle_sec(now_dt))
        earliest_trigger_at = max(next_check_at, float(s.get("cooldown_until", 0)), min_idle_at)
        earliest_trigger_in = max(0, int(earliest_trigger_at - now_ts))
        no_reply_streak = int(s.get("no_reply_streak", 0))
        decay = self._no_reply_decay_factor(s)

        self._debug(
            "status "
            f"reason={reason} session={session_key} "
            f"idle={idle_sec}s cooldown_left={cooldown_left}s "
            f"no_reply_streak={no_reply_streak} decay={decay:.2f} "
            f"next_check={self._fmt_ts(next_check_at)}(+{next_check_in}s) "
            f"next_trigger_earliest={self._fmt_ts(earliest_trigger_at)}(+{earliest_trigger_in}s)"
        )
