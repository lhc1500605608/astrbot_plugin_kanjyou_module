import asyncio
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

IMPORT_MODE = "package"
IMPORT_FALLBACK_REASON = ""


def _plugin_data_dir() -> Path:
    """AstrBot 插件 data 目录（不在插件安装目录内落盘）。"""
    plugin_name = Path(__file__).resolve().parent.name
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:
        base = Path.cwd() / "data" / "plugin_data"
    return base / plugin_name


def _resolve_state_path() -> Path:
    target_dir = _plugin_data_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return target_dir / "idle_state.json"


def _migrate_legacy_state(target: Path) -> None:
    """一次性把插件目录下的旧 idle_state.json 迁移到 AstrBot data 目录。"""
    legacy = Path(__file__).resolve().parent / "idle_state.json"
    if not legacy.exists() or target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    except Exception:
        pass


try:
    # Preferred package imports when AstrBot loads plugin as a package.
    from .config import (
        CONFIG_EXECUTION_ORDER,
        EXECUTION_ORDER,
        PluginConfigView,
    )
    from .units.unit_advanced import AdvancedPolicyUnitsMixin
    from .units.unit_commands import CommandUnitsMixin
    from .units.unit_companion import CompanionContextUnitsMixin
    from .units.unit_emotion_events import EmotionEventUnitsMixin
    from .units.unit_events import EventUnitsMixin
    from .units.unit_generation import PolicyGenerationUnitsMixin
    from .units.unit_memory_recall import MemoryRecallUnitsMixin
    from .units.unit_open_threads import OpenThreadUnitsMixin
    from .units.unit_reply_context import ReplyContextUnitsMixin
    from .units.unit_runtime import RuntimeUnitsMixin
    from .units.unit_segmentation import SegmentationUnitsMixin
    from .units.unit_session import SessionConfigUnitsMixin
    from .units.unit_webui import WebUIUnitsMixin
except ImportError:
    # Fallback for script-style loading in some runtimes.
    IMPORT_MODE = "fallback"
    IMPORT_FALLBACK_REASON = "package relative import failed"
    PLUGIN_DIR = Path(__file__).parent
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    from config import (
        CONFIG_EXECUTION_ORDER,
        EXECUTION_ORDER,
        PluginConfigView,
    )
    from units.unit_advanced import AdvancedPolicyUnitsMixin
    from units.unit_commands import CommandUnitsMixin
    from units.unit_companion import CompanionContextUnitsMixin
    from units.unit_emotion_events import EmotionEventUnitsMixin
    from units.unit_events import EventUnitsMixin
    from units.unit_generation import PolicyGenerationUnitsMixin
    from units.unit_memory_recall import MemoryRecallUnitsMixin
    from units.unit_open_threads import OpenThreadUnitsMixin
    from units.unit_reply_context import ReplyContextUnitsMixin
    from units.unit_runtime import RuntimeUnitsMixin
    from units.unit_segmentation import SegmentationUnitsMixin
    from units.unit_session import SessionConfigUnitsMixin
    from units.unit_webui import WebUIUnitsMixin


class KanjyouIdleProactivePlugin(
    CommandUnitsMixin,
    EventUnitsMixin,
    SessionConfigUnitsMixin,
    AdvancedPolicyUnitsMixin,
    PolicyGenerationUnitsMixin,
    SegmentationUnitsMixin,
    MemoryRecallUnitsMixin,
    CompanionContextUnitsMixin,
    EmotionEventUnitsMixin,
    OpenThreadUnitsMixin,
    ReplyContextUnitsMixin,
    RuntimeUnitsMixin,
    WebUIUnitsMixin,
    Star,
):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = PluginConfigView(config)
        self._state_path = _resolve_state_path()
        _migrate_legacy_state(self._state_path)
        self._normalize_webui_config()
        self._sessions: Dict[str, Dict] = self._load_state()
        self._global_send_history: List[float] = []
        self._global_fail_streak: int = 0
        self._global_pause_until: float = 0.0
        self._decision_last: Dict[str, Dict] = {}
        self._decision_trace: List[Dict] = []
        self._quality_trace: Dict[str, int] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._idle_loop())
        plugin_dir = Path(__file__).parent
        units_dir = plugin_dir / "units"
        import_summary = (
            f"[idle-proactive] import self-check mode={IMPORT_MODE} "
            f"plugin_dir={plugin_dir} units_dir={units_dir} py={sys.version.split()[0]}"
        )
        if IMPORT_MODE == "fallback" and IMPORT_FALLBACK_REASON:
            import_summary += f" reason={IMPORT_FALLBACK_REASON}"
        if self.config.get("lifecycle_log", True):
            logger.info("[idle-proactive] initialized")
            logger.info(import_summary)
        else:
            self._debug(import_summary)
        self._debug(f"execution order: {' -> '.join(EXECUTION_ORDER)}")
        self._debug(f"config order: {' -> '.join(CONFIG_EXECUTION_ORDER)}")
        self._debug(
            f"debug window active={int(self.config.get('debug_status_window_sec', 300))}s "
            f"check_interval={int(self.config.get('check_interval_sec', 30))}s"
        )
        self._run_startup_config_checks()
        self._register_webui_routes()
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
        await self._evt_on_all_message(event)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def segment_prepare(self, event: AstrMessageEvent):
        await self._evt_segment_prepare(event)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        await self._evt_on_decorating_result(event)

    @filter.on_llm_request()
    async def inject_proactive_reply_context(self, event: AstrMessageEvent, req):
        await self._reply_context_on_llm_request(event, req)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        await self._evt_after_message_sent(event)

    @filter.command("idle_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_status(self, event: AstrMessageEvent):
        async for result in self._cmd_idle_status(event):
            yield result

    @filter.command("idle_enable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_enable(self, event: AstrMessageEvent):
        async for result in self._cmd_idle_enable(event):
            yield result

    @filter.command("idle_disable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_disable(self, event: AstrMessageEvent):
        async for result in self._cmd_idle_disable(event):
            yield result

    @filter.command("idle_wl_add_private")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_add_private(self, event: AstrMessageEvent, user_id: str):
        async for result in self._cmd_idle_wl_add_private(event, user_id):
            yield result

    @filter.command("idle_wl_del_private")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_del_private(self, event: AstrMessageEvent, user_id: str):
        async for result in self._cmd_idle_wl_del_private(event, user_id):
            yield result

    @filter.command("idle_wl_add_group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_add_group(self, event: AstrMessageEvent, group_id: str):
        async for result in self._cmd_idle_wl_add_group(event, group_id):
            yield result

    @filter.command("idle_wl_del_group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_wl_del_group(self, event: AstrMessageEvent, group_id: str):
        async for result in self._cmd_idle_wl_del_group(event, group_id):
            yield result

    @filter.command("idle_sleep_set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_sleep_set(self, event: AstrMessageEvent, start_hm: str, end_hm: str):
        async for result in self._cmd_idle_sleep_set(event, start_hm, end_hm):
            yield result

    @filter.command("idle_test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def idle_test(self, event: AstrMessageEvent):
        async for result in self._cmd_idle_test(event):
            yield result
