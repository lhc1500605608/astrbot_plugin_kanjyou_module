import asyncio
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register

from config import CONFIG_EXECUTION_ORDER, EXECUTION_ORDER, PLUGIN_VERSION
from units.advanced import AdvancedPolicyUnitsMixin
from units.commands import CommandUnitsMixin
from units.events import EventUnitsMixin
from units.generation import PolicyGenerationUnitsMixin
from units.runtime import RuntimeUnitsMixin
from units.session import SessionConfigUnitsMixin


@register("kanjyou_idle_proactive", "Tango", "闲时主动聊天：分会话计时、白名单、夜间免打扰", PLUGIN_VERSION)
class KanjyouIdleProactivePlugin(
    CommandUnitsMixin,
    EventUnitsMixin,
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
