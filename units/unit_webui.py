"""kanjyou v2.3.0 后端 Web API（TMEAAA-415）。

按 CTO 冻结契约（TMEAAA-412 `plan` 第 3 节）实现 8 条路由，供插件 Pages 经
AstrBot bridge（`apiGet` / `apiPost` / `subscribeSSE`）调用：

- `status` / `events`（GET，events 为 `text/event-stream`）
- `toggle` / `test` / `whitelist` / `sleep` / `mood` / `reload`（POST）

安全约束：**响应绝不包含消息正文与密钥**。`status.recent` 与 SSE `decision`
只暴露时间戳、会话标识与结构化决策字段（不含 `umo` / 生成文本）。

路由前缀必须带插件名（`astrbot_plugin_kanjyou_module/...`）：AstrBot 4.28.x
`_match_registered_web_api` 用整段 `plugin_path` 匹配。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional

try:
    from ..config import migrate_flat_to_nested
except ImportError:  # pragma: no cover - script-style loading
    from config import migrate_flat_to_nested

try:
    from astrbot.api.web import (  # type: ignore
        error_response,
        json_response,
        request,
        stream_response,
    )

    WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover - 旧版本 AstrBot 无 astrbot.api.web
    WEB_API_AVAILABLE = False

    class _UnavailableRequest:
        def __getattr__(self, item):
            raise RuntimeError("astrbot.api.web 不可用，无法处理 Web API 请求")

    request = _UnavailableRequest()

    def json_response(data=None, **kwargs):  # type: ignore[misc]
        raise RuntimeError("astrbot.api.web 不可用")

    def error_response(message, **kwargs):  # type: ignore[misc]
        raise RuntimeError("astrbot.api.web 不可用")

    def stream_response(content, **kwargs):  # type: ignore[misc]
        raise RuntimeError("astrbot.api.web 不可用")


WEB_API_PREFIX = "astrbot_plugin_kanjyou_module"

# (endpoint, method, handler method name, description)
WEB_API_ROUTES = (
    ("status", "GET", "_web_api_status", "闲时主动插件状态"),
    ("events", "GET", "_web_api_events", "闲时主动插件事件流 (SSE)"),
    ("toggle", "POST", "_web_api_toggle", "启用/禁用闲时主动"),
    ("test", "POST", "_web_api_test", "手动触发一次主动消息"),
    ("whitelist", "POST", "_web_api_whitelist", "白名单增删"),
    ("sleep", "POST", "_web_api_sleep", "设置免打扰时段"),
    ("mood", "POST", "_web_api_mood", "调整会话 mood"),
    ("reload", "POST", "_web_api_reload", "重载配置与运行状态"),
)

VALID_WHITELIST_OPS = frozenset({"add", "del"})
VALID_WHITELIST_KINDS = frozenset({"private", "group"})

WEB_EVENT_INTERVAL_SEC = 2.0
WEB_EVENT_HEARTBEAT_SEC = 15.0
WEB_RECENT_LIMIT = 10


def _plugin_config_file() -> Optional[Path]:
    """解析插件在 AstrBot `data/config/` 下的配置文件路径。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path
    except Exception:
        return None
    plugin_name = Path(__file__).resolve().parents[1].name
    return Path(get_astrbot_config_path()) / f"{plugin_name}_config.json"


class WebUIUnitsMixin:
    # ---------------------------------------------------------------- 注册
    def _register_webui_routes(self) -> None:
        if not WEB_API_AVAILABLE:
            self._log_error(
                "web_api_unavailable", "astrbot.api.web 不可用，跳过 Web API 注册"
            )
            return
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            self._log_error(
                "web_api_register_missing", "context.register_web_api 不可用"
            )
            return
        for endpoint, method, handler_name, desc in WEB_API_ROUTES:
            handler = getattr(self, handler_name, None)
            if not callable(handler):
                continue
            route = f"{WEB_API_PREFIX}/{endpoint}"
            try:
                register(route, handler, [method], desc)
            except Exception as exc:
                self._log_error(
                    "web_api_register_failed", f"register {route} failed: {exc}"
                )

    # ---------------------------------------------------------------- GET
    async def _web_api_status(self):
        return json_response({"status": "ok", "data": self._webui_status_data()})

    async def _web_api_events(self):
        return stream_response(
            self._webui_event_stream(), content_type="text/event-stream"
        )

    # ---------------------------------------------------------------- POST
    async def _web_api_toggle(self):
        body = await self._webui_json_body()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return error_response("enabled 必须为布尔值")
        self.config["enabled"] = enabled
        self._save_webui_config()
        return json_response({"status": "ok", "data": {"enabled": enabled}})

    async def _web_api_test(self):
        body = await self._webui_json_body()
        raw_session_id = body.get("session_id")
        if raw_session_id is not None and not isinstance(raw_session_id, str):
            return error_response("session_id 必须为字符串")
        session_id = (raw_session_id or "").strip()
        if not session_id:
            session_id = self._webui_default_session_id()
            if not session_id:
                return error_response("没有可测试的会话")
        session = self._sessions.get(session_id)
        if not isinstance(session, dict):
            return error_response("会话不存在")
        if not self._is_session_whitelisted(session_id):
            return error_response("会话不在白名单，无法测试")
        umo = str(session.get("unified_msg_origin") or "").strip()
        if not umo:
            return error_response("会话缺少 unified_msg_origin")
        now_ts = self._now().timestamp()
        idle_sec = max(0.0, now_ts - float(session.get("last_interaction_at", now_ts)))
        try:
            success, _text = await self._send_proactive(
                umo, None, session_id, idle_sec, session
            )
        except Exception as exc:
            self._log_error("web_api_test_failed", f"manual test failed: {exc}")
            return error_response("主动消息发送失败")
        return json_response(
            {
                "status": "ok",
                "data": {"triggered": bool(success), "session_id": session_id},
            }
        )

    async def _web_api_whitelist(self):
        body = await self._webui_json_body()
        op = body.get("op")
        kind = body.get("kind")
        target = body.get("id")
        if op not in VALID_WHITELIST_OPS:
            return error_response("op 必须为 add 或 del")
        if kind not in VALID_WHITELIST_KINDS:
            return error_response("kind 必须为 private 或 group")
        if not isinstance(target, str) or not target.strip():
            return error_response("id 必须为非空字符串")
        target = target.strip()
        if len(target) > 128:
            return error_response("id 长度超限")
        key = "private_whitelist" if kind == "private" else "group_whitelist"
        current = self.config.get(key)
        if not isinstance(current, list):
            current = []
        else:
            current = list(current)
        if op == "add":
            if target not in current:
                current.append(target)
        else:
            current = [item for item in current if item != target]
        self.config[key] = current
        self._save_webui_config()
        return json_response(
            {
                "status": "ok",
                "data": {
                    "private": self._webui_list("private_whitelist"),
                    "group": self._webui_list("group_whitelist"),
                },
            }
        )

    async def _web_api_sleep(self):
        body = await self._webui_json_body()
        start = body.get("start")
        end = body.get("end")
        if not isinstance(start, str) or not self._is_hhmm(start):
            return error_response("start 必须为 HH:MM 格式")
        if not isinstance(end, str) or not self._is_hhmm(end):
            return error_response("end 必须为 HH:MM 格式")
        self.config["sleep_start"] = start
        self.config["sleep_end"] = end
        self._save_webui_config()
        return json_response({"status": "ok", "data": {"start": start, "end": end}})

    async def _web_api_mood(self):
        body = await self._webui_json_body()
        raw_session_id = body.get("session_id")
        value = body.get("value")
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            return error_response("session_id 必须为非空字符串")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return error_response("value 必须为 0-100 的数字")
        if value < 0 or value > 100:
            return error_response("value 超出范围 0-100")
        session_id = raw_session_id.strip()
        session = self._sessions.get(session_id)
        if not isinstance(session, dict):
            return error_response("会话不存在")
        now_ts = self._now().timestamp()
        session["mood"] = float(self._mood_clamp(float(value)))
        session["mood_updated_at"] = now_ts
        self._save_state()
        return json_response(
            {
                "status": "ok",
                "data": {
                    "session_id": session_id,
                    "mood": round(float(session["mood"]), 2),
                },
            }
        )

    async def _web_api_reload(self):
        self._webui_reload()
        return json_response({"status": "ok", "data": {"reloaded": True}})

    # ------------------------------------------------------------ 内部实现
    async def _webui_json_body(self) -> Dict:
        try:
            data = await request.json(default={})
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _webui_list(self, key: str) -> List[str]:
        value = self.config.get(key)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    def _webui_default_session_id(self) -> str:
        candidates = [
            session_id
            for session_id in self._sessions
            if self._is_session_whitelisted(session_id)
        ]
        if not candidates:
            return ""
        now_ts = self._now().timestamp()

        def _idle_desc(session_id: str) -> float:
            session = self._sessions.get(session_id)
            if not isinstance(session, dict):
                return 0.0
            return now_ts - float(session.get("last_interaction_at", now_ts))

        candidates.sort(key=_idle_desc, reverse=True)
        return candidates[0]

    def _webui_status_data(self) -> Dict:
        now = self._now()
        now_ts = now.timestamp()
        sessions: List[Dict] = []
        for session_id, session in list(self._sessions.items()):
            if not isinstance(session, dict):
                continue
            sessions.append(
                self._webui_session_snapshot(session_id, session, now, now_ts)
            )
        sessions.sort(key=lambda row: row["idle_sec"], reverse=True)

        pause_until = float(self._global_pause_until or 0.0)
        return {
            "sessions": sessions,
            "global": {
                "enabled": bool(self.config.get("enabled", True)),
                "paused_until": int(pause_until) if pause_until > now_ts else None,
                "fail_streak": int(self._global_fail_streak or 0),
            },
            "whitelist": {
                "private": self._webui_list("private_whitelist"),
                "group": self._webui_list("group_whitelist"),
            },
            "recent": self._webui_recent_decisions(),
        }

    def _webui_session_snapshot(
        self, session_id: str, session: Dict, now, now_ts: float
    ) -> Dict:
        try:
            idle_sec = max(
                0, int(now_ts - float(session.get("last_interaction_at", now_ts)))
            )
        except (TypeError, ValueError):
            idle_sec = 0
        try:
            mood = float(session.get("mood", self._mood_initial()))
        except (TypeError, ValueError):
            mood = 0.0
        try:
            persona_state = self._current_persona_state(session_id, session, idle_sec)
        except Exception:
            persona_state = ""
        last_proactive = session.get("last_bot_at")
        has_proactive = isinstance(last_proactive, (int, float)) and last_proactive > 0
        return {
            "session_id": session_id,
            "session_type": "group" if session_id.startswith("group:") else "private",
            "idle_sec": idle_sec,
            "mood": round(mood, 2),
            "persona_state": persona_state,
            "next_trigger_sec": self._webui_next_trigger_sec(session, now, now_ts),
            "today_count": int(session.get("today_proactive_count", 0) or 0),
            "last_proactive_at": int(last_proactive) if has_proactive else None,
        }

    def _webui_next_trigger_sec(self, session: Dict, now, now_ts: float) -> int:
        try:
            last_interaction = float(session.get("last_interaction_at", now_ts))
            decay = float(self._no_reply_decay_factor(session))
            min_idle_at = last_interaction + (
                float(self._effective_min_idle_sec(now)) * decay
            )
            earliest = max(
                float(session.get("next_check_at", now_ts) or now_ts),
                float(session.get("cooldown_until", 0) or 0),
                min_idle_at,
            )
            return max(0, int(earliest - now_ts))
        except Exception:
            return 0

    def _webui_recent_decisions(self) -> List[Dict]:
        trace = self._decision_trace if isinstance(self._decision_trace, list) else []
        recent = [
            self._webui_safe_decision(item)
            for item in trace[-WEB_RECENT_LIMIT:]
            if isinstance(item, dict)
        ]
        return recent

    @staticmethod
    def _webui_safe_decision(decision: Dict) -> Dict:
        outcome = decision.get("outcome")
        if not outcome:
            outcome = "allow" if decision.get("allow") else "skip"
        reasons = decision.get("reason_codes")
        if not isinstance(reasons, list):
            reasons = []
        return {
            "session_id": decision.get("session"),
            "at": decision.get("at"),
            "outcome": outcome,
            "reason_codes": [str(item) for item in reasons],
            "confidence": decision.get("confidence"),
            "mode": decision.get("mode"),
            "idle_sec": decision.get("idle_sec"),
            "mood": decision.get("mood"),
        }

    @staticmethod
    def _webui_sse(payload: Dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _webui_new_decisions(self, trace: List, last_seen) -> List[Dict]:
        if not trace:
            return []
        if last_seen is None:
            return [item for item in trace[-WEB_RECENT_LIMIT:] if isinstance(item, dict)]
        for idx in range(len(trace) - 1, -1, -1):
            if trace[idx] is last_seen:
                return [
                    item for item in trace[idx + 1 :] if isinstance(item, dict)
                ]
        return [item for item in trace[-WEB_RECENT_LIMIT:] if isinstance(item, dict)]

    async def _webui_event_stream(self):
        last_seen = None
        heartbeat = 0.0
        yield self._webui_sse(
            {"type": "status", "data": self._webui_status_data()}
        )
        while True:
            try:
                await asyncio.sleep(WEB_EVENT_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            yield self._webui_sse(
                {"type": "status", "data": self._webui_status_data()}
            )
            trace = self._decision_trace if isinstance(self._decision_trace, list) else []
            for decision in self._webui_new_decisions(trace, last_seen):
                yield self._webui_sse(
                    {"type": "decision", "data": self._webui_safe_decision(decision)}
                )
            if trace:
                last_seen = trace[-1]
            heartbeat += WEB_EVENT_INTERVAL_SEC
            if heartbeat >= WEB_EVENT_HEARTBEAT_SEC:
                heartbeat = 0.0
                yield ": keep-alive\n\n"

    def _webui_reload(self) -> None:
        try:
            config_data = self._webui_read_config_file()
        except Exception as exc:
            self._log_error("web_api_reload_failed", f"reload config failed: {exc}")
            config_data = None
        if config_data is not None:
            raw = getattr(self.config, "_raw", None)
            if isinstance(raw, dict):
                raw.update(config_data)
            self._save_webui_config()
        try:
            self._sessions = self._load_state()
        except Exception as exc:
            self._log_error("web_api_reload_state_failed", f"reload state failed: {exc}")
        self._normalize_webui_config()
        self._run_startup_config_checks()

    def _webui_read_config_file(self) -> Optional[Dict]:
        path = _plugin_config_file()
        if path is None or not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return None
        migrate_flat_to_nested(data)
        return data
