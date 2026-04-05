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
    "max_per_session_per_day": 8,
    "trigger_base_prob": 0.02,
    "trigger_max_prob": 0.18,
    "require_human_reply_before_next_proactive": True,
    "persona_id": "",
    "proactive_provider_id": "",
    "proactive_prompt_template": (
        "你是一个在聊天中主动关怀用户的助手。"
        "请严格基于下方人格设定进行表达，不要脱离人格。\n"
        "人格设定：\n{persona}\n\n"
        "当前会话类型：{session_type}\n"
        "距离上次互动约 {idle_minutes} 分钟（{idle_seconds} 秒）。\n"
        "请输出 1 条中文主动问候（只输出消息正文，不加引号），要求：\n"
        "1) 语气自然、有温度，不要机械。\n"
        "2) 结尾带一个轻量开放问题，促进继续对话。\n"
        "3) 避免重复“在吗/你好”。\n"
        "4) 长度 20-60 字。"
    ),
    "fallback_proactive_text": "刚刚想到你，最近有没有一件小事让你有点开心？",
}

@register("kanjyou_idle_proactive", "Tango", "闲时主动聊天：分会话计时、白名单、夜间免打扰", "1.4.0")
class KanjyouIdleProactivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._state_path = Path(__file__).parent / "idle_state.json"
        self._normalize_webui_config()
        self._sessions: Dict[str, Dict] = self._load_state()
        self._debug_status_last: Dict[str, float] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._idle_loop())
        logger.info("[idle-proactive] initialized")
        self._debug("plugin initialize complete")

    async def terminate(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._save_state()
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
            s["last_human_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["pending_human_reply"] = False
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
            s["last_bot_at"] = now_ts
            s["last_interaction_at"] = now_ts
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by bot session={session_key} last_interaction={self._fmt_ts(now_ts)}"
            )

    @filter.command("idle_status")
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
    async def idle_enable(self, event: AstrMessageEvent):
        self.config["enabled"] = True
        self._save_webui_config()
        yield event.plain_result("已开启闲时主动聊天。")

    @filter.command("idle_disable")
    async def idle_disable(self, event: AstrMessageEvent):
        self.config["enabled"] = False
        self._save_webui_config()
        yield event.plain_result("已关闭闲时主动聊天。")

    @filter.command("idle_wl_add_private")
    async def idle_wl_add_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self.config["private_whitelist"]
        if user_id not in wl:
            wl.append(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已添加: {user_id}")

    @filter.command("idle_wl_del_private")
    async def idle_wl_del_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self.config["private_whitelist"]
        if user_id in wl:
            wl.remove(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已移除: {user_id}")

    @filter.command("idle_wl_add_group")
    async def idle_wl_add_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self.config["group_whitelist"]
        if group_id not in wl:
            wl.append(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已添加: {group_id}")

    @filter.command("idle_wl_del_group")
    async def idle_wl_del_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self.config["group_whitelist"]
        if group_id in wl:
            wl.remove(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已移除: {group_id}")

    @filter.command("idle_sleep_set")
    async def idle_sleep_set(self, event: AstrMessageEvent, start_hm: str, end_hm: str):
        if not self._is_hhmm(start_hm) or not self._is_hhmm(end_hm):
            yield event.plain_result("格式错误，请使用 HH:MM，例如 /idle_sleep_set 23:30 08:00")
            return
        self.config["sleep_start"] = start_hm
        self.config["sleep_end"] = end_hm
        self._save_webui_config()
        yield event.plain_result(f"免打扰时段已设置为 {start_hm}-{end_hm}")

    @filter.command("idle_test")
    async def idle_test(self, event: AstrMessageEvent):
        if not self._is_whitelisted(event):
            yield event.plain_result("当前会话不在白名单，先加入白名单再测试。")
            return
        await self._send_proactive(event.unified_msg_origin, event, self._session_key(event), 0)
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
        changed = False

        async with self._lock:
            for session_key, s in list(self._sessions.items()):
                self._rollover_daily_counter(s, now)

                if now_ts < s.get("next_check_at", 0):
                    self._maybe_log_status(session_key, s, now_ts, "waiting_next_check")
                    self._debug(
                        f"session skip(next_check) session={session_key} next_check={self._fmt_ts(s.get('next_check_at'))}"
                    )
                    continue

                if now_ts < s.get("cooldown_until", 0):
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._maybe_log_status(session_key, s, now_ts, "cooldown")
                    self._debug(
                        f"session skip(cooldown) session={session_key} cooldown_until={self._fmt_ts(s.get('cooldown_until'))}"
                    )
                    continue

                if s.get("today_proactive_count", 0) >= self.config["max_per_session_per_day"]:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._maybe_log_status(session_key, s, now_ts, "daily_limit")
                    self._debug(
                        f"session skip(limit) session={session_key} today_count={s.get('today_proactive_count', 0)}"
                    )
                    continue

                if self.config.get("require_human_reply_before_next_proactive", True) and s.get(
                    "pending_human_reply", False
                ):
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._maybe_log_status(session_key, s, now_ts, "await_human_reply")
                    self._debug(f"session skip(await_human_reply) session={session_key}")
                    continue

                idle_sec = now_ts - s.get("last_interaction_at", now_ts)
                if idle_sec < self._min_idle_sec():
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._maybe_log_status(session_key, s, now_ts, "idle_not_enough")
                    self._debug(
                        f"session skip(idle_short) session={session_key} idle_sec={int(idle_sec)} min_idle={self._min_idle_sec()}"
                    )
                    continue

                should_trigger = self._should_trigger(float(idle_sec))
                if not should_trigger:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._maybe_log_status(session_key, s, now_ts, "probability_miss")
                    self._debug(
                        f"session skip(probability) session={session_key} idle_sec={int(idle_sec)}"
                    )
                    continue

                umo = s.get("unified_msg_origin")
                if not umo:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._maybe_log_status(session_key, s, now_ts, "missing_origin")
                    self._debug(f"session skip(no_origin) session={session_key}")
                    continue

                success = await self._send_proactive(umo, None, session_key, idle_sec)
                s["next_check_at"] = now_ts + self._randomized_interval()
                if success:
                    s["last_bot_at"] = now_ts
                    s["last_interaction_at"] = now_ts
                    s["today_proactive_count"] = int(s.get("today_proactive_count", 0)) + 1
                    s["cooldown_until"] = now_ts + self._cooldown_sec()
                    s["pending_human_reply"] = True
                    self._debug(
                        f"session trigger(success) session={session_key} idle_sec={int(idle_sec)} today_count={s['today_proactive_count']}"
                    )
                    self._maybe_log_status(session_key, s, now_ts, "trigger_success", force=True)
                else:
                    self._debug(f"session trigger(failed) session={session_key}")
                    self._maybe_log_status(session_key, s, now_ts, "trigger_failed", force=True)
                changed = True

            if changed:
                self._save_state()
                self._debug("state persisted")

    async def _send_proactive(
        self,
        unified_msg_origin: str,
        event: Optional[AstrMessageEvent],
        session_key: str = "",
        idle_sec: float = 0.0,
    ) -> bool:
        topic = await self._generate_proactive_text(unified_msg_origin, session_key, idle_sec)
        try:
            chain = MessageChain().message(topic)
            await self.context.send_message(unified_msg_origin, chain)
            self._debug(f"send proactive ok session={session_key} topic={topic}")
            return True
        except Exception:
            try:
                # 兼容部分适配器对 MessageChain 构造差异
                await self.context.send_message(unified_msg_origin, [Plain(topic)])
                self._debug(f"send proactive ok(fallback) session={session_key} topic={topic}")
                return True
            except Exception as exc:
                logger.error(f"[idle-proactive] send failed: {exc}")
                self._debug(f"send proactive failed session={session_key} err={exc}")
                if event:
                    await event.send(event.plain_result("主动消息发送失败，请检查适配器是否支持主动消息。"))
                return False

    async def _generate_proactive_text(self, unified_msg_origin: str, session_key: str, idle_sec: float) -> str:
        fallback = str(self.config.get("fallback_proactive_text") or DEFAULT_CONFIG["fallback_proactive_text"]).strip()
        try:
            session_type = "私聊" if session_key.startswith("private:") else "群聊"
            persona_text = await self._resolve_persona_prompt()
            prompt_tpl = str(
                self.config.get("proactive_prompt_template") or DEFAULT_CONFIG["proactive_prompt_template"]
            )
            prompt = prompt_tpl.format(
                persona=persona_text,
                session_type=session_type,
                idle_seconds=int(idle_sec),
                idle_minutes=max(1, int(idle_sec // 60)),
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
            self._debug(f"generate ok provider={provider_id} session={session_key} text={cleaned}")
            return cleaned
        except Exception as exc:
            logger.error(f"[idle-proactive] generate proactive text failed: {exc}")
            self._debug(f"generate failed session={session_key} err={exc}")
            return fallback

    def _should_trigger(self, idle_sec: float) -> bool:
        min_idle = float(self._min_idle_sec())
        max_idle = float(self._max_idle_sec())

        if idle_sec >= max_idle:
            return True

        span = max(max_idle - min_idle, 1.0)
        progress = max(0.0, min(1.0, (idle_sec - min_idle) / span))
        base_prob = float(self.config["trigger_base_prob"])  # 刚到最小 idle 时也有少量概率
        max_prob = float(self.config["trigger_max_prob"])
        p = base_prob + (max_prob - base_prob) * progress
        return random.random() < p

    def _min_idle_sec(self) -> int:
        return max(60, int(float(self.config["min_idle_min"]) * 60))

    def _max_idle_sec(self) -> int:
        return max(self._min_idle_sec() + 60, int(float(self.config["max_idle_min"]) * 60))

    def _cooldown_sec(self) -> int:
        return max(60, int(float(self.config["cooldown_min"]) * 60))

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
        cleaned = text.strip().strip('"').strip("'")
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if lines:
            cleaned = lines[0]
        if len(cleaned) > 120:
            cleaned = cleaned[:120].rstrip("，,。.!?？") + "。"
        return cleaned

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
        }

    def _rollover_daily_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("counter_date") != today:
            session["counter_date"] = today
            session["today_proactive_count"] = 0

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

    def _normalize_webui_config(self):
        changed = False
        for key, value in DEFAULT_CONFIG.items():
            if self.config.get(key) is None:
                self.config[key] = copy.deepcopy(value)
                changed = True

        if not isinstance(self.config.get("private_whitelist"), list):
            self.config["private_whitelist"] = copy.deepcopy(DEFAULT_CONFIG["private_whitelist"])
            changed = True
        if not isinstance(self.config.get("group_whitelist"), list):
            self.config["group_whitelist"] = copy.deepcopy(DEFAULT_CONFIG["group_whitelist"])
            changed = True
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
        if not isinstance(self.config.get("debug_status_window_sec"), int):
            self.config["debug_status_window_sec"] = DEFAULT_CONFIG["debug_status_window_sec"]
            changed = True
        if int(self.config.get("debug_status_window_sec", 0)) < 60:
            self.config["debug_status_window_sec"] = 60
            changed = True
        if not isinstance(self.config.get("persona_id"), str):
            self.config["persona_id"] = DEFAULT_CONFIG["persona_id"]
            changed = True
        if not isinstance(self.config.get("proactive_provider_id"), str):
            self.config["proactive_provider_id"] = DEFAULT_CONFIG["proactive_provider_id"]
            changed = True
        if not isinstance(self.config.get("require_human_reply_before_next_proactive"), bool):
            self.config["require_human_reply_before_next_proactive"] = DEFAULT_CONFIG[
                "require_human_reply_before_next_proactive"
            ]
            changed = True
        if not isinstance(self.config.get("proactive_prompt_template"), str) or not self.config["proactive_prompt_template"].strip():
            self.config["proactive_prompt_template"] = DEFAULT_CONFIG["proactive_prompt_template"]
            changed = True
        if not isinstance(self.config.get("fallback_proactive_text"), str) or not self.config["fallback_proactive_text"].strip():
            self.config["fallback_proactive_text"] = DEFAULT_CONFIG["fallback_proactive_text"]
            changed = True

        if changed:
            self._save_webui_config()

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
            logger.info(f"[idle-proactive][debug] {msg}")

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
        min_idle_at = float(s.get("last_interaction_at", now_ts)) + float(self._min_idle_sec())
        earliest_trigger_at = max(next_check_at, float(s.get("cooldown_until", 0)), min_idle_at)
        earliest_trigger_in = max(0, int(earliest_trigger_at - now_ts))

        self._debug(
            "status "
            f"reason={reason} session={session_key} "
            f"idle={idle_sec}s cooldown_left={cooldown_left}s "
            f"next_check={self._fmt_ts(next_check_at)}(+{next_check_in}s) "
            f"next_trigger_earliest={self._fmt_ts(earliest_trigger_at)}(+{earliest_trigger_in}s)"
        )
