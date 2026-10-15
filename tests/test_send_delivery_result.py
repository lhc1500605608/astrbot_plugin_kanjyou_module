"""AstrBot send_message bool-result regression tests.

AstrBot 4.23.2 can return ``False`` when no platform matches. Both the primary
MessageChain path and the genuine-exception fallback must preserve that result
as a failure instead of treating it as legacy ``None`` success.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

TOPIC = "第一句话。第二句话。"
UMO = "aiocqhttp:FriendMessage:1"
KEY = "private:aiocqhttp:FriendMessage:1"
WEBUI_MODULE = "astrbot_plugin_kanjyou_module.units.unit_webui"


class _Req:
    def __init__(self, payload=None):
        self._payload = payload or {}

    async def json(self, default=None):
        return self._payload if self._payload else default


def _prepare(plugin):
    async def _generate(*_args, **_kwargs):
        return TOPIC

    plugin._generate_proactive_text = _generate
    plugin.config["proactive_segment_enabled"] = False
    plugin._sessions[KEY] = {
        "session_key": KEY,
        "unified_msg_origin": UMO,
        "identity": {"platform": "aiocqhttp", "native_id": "1"},
        "last_human_at": 0.0,
        "last_interaction_at": 0.0,
        "cooldown_until": 0.0,
        "today_proactive_count": 0,
        "user_message_revision": 0,
    }
    plugin.config["private_whitelist"] = [
        plugin._whitelist_entry("1", "aiocqhttp", "private")
    ]
    return plugin._sessions[KEY]


def test_primary_false_is_failure_without_retry(plugin):
    session = _prepare(plugin)
    calls = []

    async def _send(_umo, chain):
        calls.append(chain)
        return False

    plugin.context.send_message = _send

    ok, text = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))

    assert (ok, text) == (False, "")
    assert len(calls) == 1  # no exception-triggered retry/double-send


def test_fallback_false_is_failure_without_retry(plugin):
    session = _prepare(plugin)
    calls = []

    async def _send(_umo, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise TypeError("MessageChain adapter mismatch")
        return False

    plugin.context.send_message = _send

    ok, text = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))

    assert (ok, text) == (False, "")
    assert len(calls) == 2  # one genuine-exception fallback; False is not retried


@pytest.mark.parametrize("legacy_result", [None, True])
def test_legacy_successes_are_preserved(plugin, legacy_result):
    session = _prepare(plugin)

    async def _send(_umo, chain):
        return legacy_result

    plugin.context.send_message = _send

    ok, text = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))

    assert ok is True
    assert text == TOPIC


@pytest.mark.parametrize("stage", ["primary", "fallback"])
def test_failure_does_not_remember_text(plugin, stage):
    session = _prepare(plugin)
    remembered = []

    def _remember(*_args, **_kwargs):
        remembered.append(True)

    plugin._remember_proactive_text = _remember
    calls = []

    async def _send(_umo, _payload):
        calls.append(_payload)
        if stage == "primary":
            return False
        if len(calls) == 1:
            raise TypeError("MessageChain adapter mismatch")
        return False

    plugin.context.send_message = _send

    ok, text = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))

    assert ok is False
    assert text == ""
    assert remembered == []
    assert "pending_proactive_text" not in session
    assert "pending_proactive_at" not in session


def test_web_api_test_returns_false_trigger(plugin, monkeypatch):
    session_id = "private:webchat:FriendMessage:10001"
    plugin.config["private_whitelist"] = [
        plugin._whitelist_entry("1", "aiocqhttp", kind="private"),
        plugin._whitelist_entry("10001", "webchat", kind="private"),
    ]
    plugin._sessions[session_id] = {
        "session_key": session_id,
        "unified_msg_origin": "webchat:FriendMessage:10001",
        "identity": {"platform": "webchat", "native_id": "10001"},
    }

    async def _send(_umo, _chain):
        return False

    session = _prepare(plugin)
    plugin._sessions.pop(KEY, None)
    plugin._sessions.pop("__quarantine__:" + KEY, None)
    session["session_key"] = session_id
    session["unified_msg_origin"] = "webchat:FriendMessage:10001"
    session["identity"] = {"platform": "webchat", "native_id": "10001"}
    plugin._sessions[session_id] = session
    plugin.config["private_whitelist"] = [
        plugin._whitelist_entry("10001", "webchat", "private")
    ]
    plugin.context.send_message = _send
    monkeypatch.setattr(
        sys.modules[WEBUI_MODULE], "request", _Req({"session_id": session_id})
    )

    response = asyncio.run(plugin._web_api_test())

    assert response["status"] == "ok"
    assert response["data"]["triggered"] is False
