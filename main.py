import asyncio
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from kanjyou_advanced_policy_units import AdvancedPolicyUnitsMixin
from kanjyou_command_units import CommandUnitsMixin
from kanjyou_constants import CONFIG_EXECUTION_ORDER, EXECUTION_ORDER, PLUGIN_VERSION
from kanjyou_policy_generation_units import PolicyGenerationUnitsMixin
from kanjyou_runtime_units import RuntimeUnitsMixin
from kanjyou_session_config_units import SessionConfigUnitsMixin


@register("kanjyou_idle_proactive", "Tango", "闲时主动聊天：分会话计时、白名单、夜间免打扰", PLUGIN_VERSION)
class KanjyouIdleProactivePlugin(
    CommandUnitsMixin,
    SessionConfigUnitsMixin,
    AdvancedPolicyUnitsMixin,
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
