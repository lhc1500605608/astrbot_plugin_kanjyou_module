import asyncio
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from kanjyou_constants import CONFIG_EXECUTION_ORDER, EXECUTION_ORDER, PLUGIN_VERSION
from kanjyou_policy_generation_units import PolicyGenerationUnitsMixin
from kanjyou_runtime_units import RuntimeUnitsMixin
from kanjyou_session_config_units import SessionConfigUnitsMixin


@register("kanjyou_idle_proactive", "Tango", "闲时主动聊天：分会话计时、白名单、夜间免打扰", PLUGIN_VERSION)
class KanjyouIdleProactivePlugin(
    SessionConfigUnitsMixin,
    PolicyGenerationUnitsMixin,
    RuntimeUnitsMixin,
    Star,
):
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
