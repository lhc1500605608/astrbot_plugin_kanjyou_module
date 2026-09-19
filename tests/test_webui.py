"""v2.3.0 后端 Web API（8 路由 + SSE）契约与安全测试 (TMEAAA-415)。"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

ROOT_MODULE = "astrbot_plugin_kanjyou_module.units.unit_webui"
PREFIX = "astrbot_plugin_kanjyou_module"


class _Req:
    """Fake astrbot.api.web.request proxy for handler-level tests."""

    def __init__(self, payload=None):
        self._payload = payload or {}

    async def json(self, default=None):
        return self._payload if self._payload else default


@pytest.fixture()
def webui(plugin_module):
    return sys.modules[ROOT_MODULE]


@pytest.fixture()
def plugin(plugin):
    """Isolate each test from any persisted on-disk idle state."""
    plugin._sessions = {}
    plugin._decision_trace = []
    plugin._decision_last = {}
    plugin._global_pause_until = 0.0
    plugin._global_fail_streak = 0
    return plugin


def _session(plugin, session_id="private:10001", **overrides):
    now = plugin._now().timestamp()
    base = {
        "session_key": session_id,
        "unified_msg_origin": f"umo-{session_id}",
        "last_human_at": now - 120,
        "last_bot_at": now - 60,
        "last_interaction_at": now - 120,
        "next_check_at": now + 30,
        "today_proactive_count": 2,
        "counter_date": plugin._now().strftime("%Y-%m-%d"),
        "cooldown_until": 0.0,
        "pending_human_reply": False,
        "no_reply_streak": 0,
        "recent_proactive_texts": ["机密正文ABC"],
        "mood": 66.0,
        "mood_updated_at": now,
        "mood_low_streak": 0,
    }
    base.update(overrides)
    return base


def test_routes_cover_frozen_contract(plugin):
    plugin._register_webui_routes()
    registered = {
        route: methods for route, _handler, methods, _desc in plugin.context.registered_web_apis
    }
    assert len(registered) == 8
    expected = {
        "status": ["GET"],
        "events": ["GET"],
        "toggle": ["POST"],
        "test": ["POST"],
        "whitelist": ["POST"],
        "sleep": ["POST"],
        "mood": ["POST"],
        "reload": ["POST"],
    }
    for endpoint, methods in expected.items():
        route = f"{PREFIX}/{endpoint}"
        assert registered.get(route) == methods, route


def test_status_payload_shape_and_no_body_or_secret_leak(plugin):
    plugin._sessions["private:10001"] = _session(plugin)
    plugin._decision_trace = [
        {
            "session": "private:10001",
            "at": "2026-01-01 00:00:00",
            "outcome": "triggered",
            "reason_codes": ["cooldown"],
            "umo": "umo-private:10001",
            "suggested_tone": "秘密语气",
            "confidence": 0.9,
            "mode": "balanced",
            "idle_sec": 10,
            "mood": 60.0,
        }
    ]

    data = plugin._webui_status_data()
    dumped = json.dumps(data, ensure_ascii=False)
    assert "机密正文ABC" not in dumped
    assert "umo-private:10001" not in dumped
    assert "秘密语气" not in dumped

    row = data["sessions"][0]
    for field in (
        "session_id",
        "session_type",
        "idle_sec",
        "mood",
        "persona_state",
        "next_trigger_sec",
        "today_count",
        "last_proactive_at",
    ):
        assert field in row, field
    assert row["session_id"] == "private:10001"
    assert row["session_type"] == "private"
    assert row["today_count"] == 2
    assert data["global"] == {"enabled": True, "paused_until": None, "fail_streak": 0}
    assert data["recent"][0]["session_id"] == "private:10001"
    assert "umo" not in data["recent"][0]
    assert "suggested_tone" not in data["recent"][0]


def test_toggle_valid_and_invalid(plugin, webui, monkeypatch):
    monkeypatch.setattr(webui, "request", _Req({"enabled": False}))
    resp = asyncio.run(plugin._web_api_toggle())
    assert resp["status"] == "ok"
    assert resp["data"] == {"enabled": False}
    assert plugin.config.get("enabled") is False

    monkeypatch.setattr(webui, "request", _Req({"enabled": "yes"}))
    resp = asyncio.run(plugin._web_api_toggle())
    assert resp["status"] == "error"


def test_whitelist_add_del_and_invalid(plugin, webui, monkeypatch):
    monkeypatch.setattr(
        webui, "request", _Req({"op": "add", "kind": "private", "id": "10001"})
    )
    resp = asyncio.run(plugin._web_api_whitelist())
    assert resp["status"] == "ok"
    assert resp["data"]["private"] == ["10001"]

    monkeypatch.setattr(
        webui, "request", _Req({"op": "add", "kind": "group", "id": "555"})
    )
    asyncio.run(plugin._web_api_whitelist())
    monkeypatch.setattr(
        webui, "request", _Req({"op": "del", "kind": "private", "id": "10001"})
    )
    resp = asyncio.run(plugin._web_api_whitelist())
    assert resp["data"]["private"] == []
    assert resp["data"]["group"] == ["555"]

    monkeypatch.setattr(
        webui, "request", _Req({"op": "drop", "kind": "private", "id": "1"})
    )
    assert asyncio.run(plugin._web_api_whitelist())["status"] == "error"
    monkeypatch.setattr(
        webui, "request", _Req({"op": "add", "kind": "unknown", "id": "1"})
    )
    assert asyncio.run(plugin._web_api_whitelist())["status"] == "error"
    monkeypatch.setattr(
        webui, "request", _Req({"op": "add", "kind": "private", "id": "  "})
    )
    assert asyncio.run(plugin._web_api_whitelist())["status"] == "error"


def test_sleep_valid_and_invalid(plugin, webui, monkeypatch):
    monkeypatch.setattr(webui, "request", _Req({"start": "23:30", "end": "08:00"}))
    resp = asyncio.run(plugin._web_api_sleep())
    assert resp["status"] == "ok"
    assert plugin.config.get("sleep_start") == "23:30"
    assert plugin.config.get("sleep_end") == "08:00"

    monkeypatch.setattr(webui, "request", _Req({"start": "25:00", "end": "08:00"}))
    assert asyncio.run(plugin._web_api_sleep())["status"] == "error"
    monkeypatch.setattr(webui, "request", _Req({"start": "23:30"}))
    assert asyncio.run(plugin._web_api_sleep())["status"] == "error"


def test_mood_valid_range_and_missing_session(plugin, webui, monkeypatch):
    plugin._sessions["private:10001"] = _session(plugin, mood=10.0)
    monkeypatch.setattr(
        webui, "request", _Req({"session_id": "private:10001", "value": 42.5})
    )
    resp = asyncio.run(plugin._web_api_mood())
    assert resp["status"] == "ok"
    assert resp["data"]["mood"] == 42.5
    assert plugin._sessions["private:10001"]["mood"] == 42.5

    monkeypatch.setattr(
        webui, "request", _Req({"session_id": "private:10001", "value": 101})
    )
    assert asyncio.run(plugin._web_api_mood())["status"] == "error"
    monkeypatch.setattr(
        webui, "request", _Req({"session_id": "private:99999", "value": 50})
    )
    assert asyncio.run(plugin._web_api_mood())["status"] == "error"


def test_test_route_rejects_unknown_and_hides_text(plugin, webui, monkeypatch):
    monkeypatch.setattr(webui, "request", _Req({"session_id": "private:404"}))
    assert asyncio.run(plugin._web_api_test())["status"] == "error"

    plugin.config["private_whitelist"] = ["10001"]
    plugin._sessions["private:10001"] = _session(plugin)

    async def _fake_send(*_args, **_kwargs):
        return True, "SECRET_OUTPUT"

    monkeypatch.setattr(plugin, "_send_proactive", _fake_send)
    monkeypatch.setattr(webui, "request", _Req({}))
    resp = asyncio.run(plugin._web_api_test())
    assert resp["status"] == "ok"
    assert resp["data"] == {"triggered": True, "session_id": "private:10001"}
    assert "SECRET_OUTPUT" not in json.dumps(resp, ensure_ascii=False)


def test_reload_returns_true(plugin, webui, monkeypatch):
    monkeypatch.setattr(webui, "request", _Req({}))
    resp = asyncio.run(plugin._web_api_reload())
    assert resp["status"] == "ok"
    assert resp["data"] == {"reloaded": True}


def test_events_returns_sse_stream(plugin, webui):
    resp = asyncio.run(plugin._web_api_events())
    assert resp["content_type"] == "text/event-stream"
    assert resp["__stream__"] is not None


def test_event_stream_emits_status_then_decisions(plugin, webui, monkeypatch):
    monkeypatch.setattr(webui, "WEB_EVENT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(webui, "WEB_EVENT_HEARTBEAT_SEC", 1000.0)
    plugin._sessions["private:10001"] = _session(plugin)
    plugin._decision_trace = [
        {
            "session": "private:10001",
            "at": "2026-01-01 00:00:00",
            "outcome": "triggered",
            "reason_codes": ["cooldown"],
            "umo": "umo-private:10001",
            "confidence": 0.9,
            "mode": "balanced",
            "idle_sec": 10,
            "mood": 60.0,
        }
    ]

    async def _collect(gen, count):
        items = []
        async for item in gen:
            items.append(item)
            if len(items) >= count:
                break
        return items

    events = asyncio.run(_collect(plugin._webui_event_stream(), 3))
    assert events[0].startswith("data: ")
    first = json.loads(events[0].removeprefix("data: ").strip())
    assert first["type"] == "status"
    assert first["data"]["sessions"][0]["session_id"] == "private:10001"
    decision_events = [
        json.loads(item.removeprefix("data: ").strip())
        for item in events
        if '"type": "decision"' in item
    ]
    assert decision_events
    assert decision_events[0]["data"]["session_id"] == "private:10001"
    assert "umo" not in decision_events[0]["data"]
    assert "秘密语气" not in json.dumps(decision_events, ensure_ascii=False)
