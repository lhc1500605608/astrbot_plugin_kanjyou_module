"""TMEAAA-580: 被动回复注入「刚发出的主动消息」上下文。

覆盖：主动发送记录待回应文案；用户下一条消息在时间窗内 → 被动 on_llm_request
注入一次性临时上下文；窗口外 / 无待回应 / 注入后第二次请求 → 不注入；群聊只用
本群文案（私聊不串群）；命令消息清空待回应；旧核心无 extra_user_content_parts
时静默跳过。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

PROACTIVE_TEXT = "晚上好呀，今天过得怎么样？"


class FakeReq:
    def __init__(self):
        self.extra_user_content_parts = []


class LegacyReq:
    """Simulates an older core without extra_user_content_parts."""


def _event(umo="webchat!u1!c1", user_id="u1", group_id="", text="在的"):
    message_obj = types.SimpleNamespace(
        message_id="m1",
        group_id=group_id,
        sender=types.SimpleNamespace(user_id=user_id),
    )
    return types.SimpleNamespace(
        message_obj=message_obj,
        unified_msg_origin=umo,
        message_str=text,
        get_sender_id=lambda: str(user_id),
    )


def _session_for(plugin, event):
    key = plugin._session_key(event)
    s = plugin._get_or_create_session(event)
    plugin._ensure_session_shape(s)
    plugin._sessions[key] = s
    return key, s


# --------------------------------------------------------------------------- #
# send side
# --------------------------------------------------------------------------- #


def test_send_proactive_records_pending_text(plugin):
    event = _event()
    key, s = _session_for(plugin, event)

    async def _fake_generate(*_args, **_kwargs):
        return PROACTIVE_TEXT

    async def _fake_send(_umo, _chain):
        return True

    plugin._generate_proactive_text = _fake_generate
    plugin.context.send_message = _fake_send

    ok, text = asyncio.run(plugin._send_proactive(event.unified_msg_origin, None, key, 0, s))

    assert ok is True
    assert text == PROACTIVE_TEXT
    assert s["pending_proactive_text"] == PROACTIVE_TEXT
    assert s["pending_proactive_at"] > 0


# --------------------------------------------------------------------------- #
# inbound -> llm request injection
# --------------------------------------------------------------------------- #


def test_reply_injects_context_once(plugin):
    event = _event()
    key, s = _session_for(plugin, event)
    plugin._remember_proactive_text(key, PROACTIVE_TEXT, s)

    asyncio.run(plugin._evt_on_all_message(event))
    assert s.get("reply_context_llm_pending") is True
    # 待回应文案被消费，不重复。
    assert "pending_proactive_text" not in s

    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(event, req))
    assert len(req.extra_user_content_parts) == 1
    part = req.extra_user_content_parts[0]
    assert PROACTIVE_TEXT in part.text
    assert part.temp is True
    assert s.get("reply_context_llm_pending") is False

    # 第二次请求不再注入。
    req2 = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(event, req2))
    assert req2.extra_user_content_parts == []


def test_second_inbound_message_does_not_inject(plugin):
    first = _event(text="刚才那条是你说的？")
    key, s = _session_for(plugin, first)
    plugin._remember_proactive_text(key, PROACTIVE_TEXT, s)

    asyncio.run(plugin._evt_on_all_message(first))
    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(first, req))
    assert len(req.extra_user_content_parts) == 1

    # 后续消息不在待回应窗口内。
    second = _event(text="再说个别的")
    asyncio.run(plugin._evt_on_all_message(second))
    req2 = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(second, req2))
    assert req2.extra_user_content_parts == []


def test_outside_window_no_injection(plugin):
    event = _event()
    key, s = _session_for(plugin, event)
    plugin._remember_proactive_text(key, PROACTIVE_TEXT, s)
    # 人为把发送时间推到窗口之外。
    s["pending_proactive_at"] = plugin._now().timestamp() - 3 * 3600

    asyncio.run(plugin._evt_on_all_message(event))
    assert s.get("reply_context_llm_pending") in (None, False)

    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(event, req))
    assert req.extra_user_content_parts == []


def test_no_pending_proactive_no_injection(plugin):
    event = _event()
    _session_for(plugin, event)

    asyncio.run(plugin._evt_on_all_message(event))
    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(event, req))
    assert req.extra_user_content_parts == []


# --------------------------------------------------------------------------- #
# group isolation
# --------------------------------------------------------------------------- #


def test_group_uses_only_group_proactive_text(plugin):
    group_event = _event(umo="aiocqhttp:group:g1", group_id="g1")
    gkey, gs = _session_for(plugin, group_event)
    plugin._remember_proactive_text(gkey, "群里的各位，周末有什么安排？", gs)

    private_event = _event(umo="webchat!u1!c1", user_id="u1")
    pkey, ps = _session_for(plugin, private_event)
    plugin._remember_proactive_text(pkey, "私聊里的小秘密", ps)

    asyncio.run(plugin._evt_on_all_message(group_event))
    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(group_event, req))
    assert len(req.extra_user_content_parts) == 1
    block = req.extra_user_content_parts[0].text
    assert "周末有什么安排" in block
    assert "私聊里的小秘密" not in block
    assert "群里" in block


def test_group_session_does_not_leak_private_context(plugin):
    # 私聊有未回应主动，但群消息不能注入私聊内容。
    private_event = _event(umo="webchat!u1!c1", user_id="u1")
    pkey, ps = _session_for(plugin, private_event)
    plugin._remember_proactive_text(pkey, "私聊里的小秘密", ps)

    group_event = _event(umo="aiocqhttp:group:g1", group_id="g1")
    _session_for(plugin, group_event)
    asyncio.run(plugin._evt_on_all_message(group_event))
    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(group_event, req))
    assert req.extra_user_content_parts == []


# --------------------------------------------------------------------------- #
# command / supersede / legacy core
# --------------------------------------------------------------------------- #


def test_command_clears_pending_context(plugin):
    event = _event(text="/idle_status")
    key, s = _session_for(plugin, event)
    plugin._remember_proactive_text(key, PROACTIVE_TEXT, s)

    asyncio.run(plugin._touch_session_for_command(event))
    assert "pending_proactive_text" not in s

    req = FakeReq()
    asyncio.run(plugin._reply_context_on_llm_request(event, req))
    assert req.extra_user_content_parts == []


def test_new_proactive_supersedes_old(plugin):
    event = _event()
    key, s = _session_for(plugin, event)
    plugin._remember_proactive_text(key, "旧话题", s)
    plugin._remember_proactive_text(key, "新话题", s)
    assert s["pending_proactive_text"] == "新话题"


def test_legacy_core_without_parts_is_safe(plugin):
    event = _event()
    key, s = _session_for(plugin, event)
    plugin._remember_proactive_text(key, PROACTIVE_TEXT, s)

    asyncio.run(plugin._evt_on_all_message(event))
    # 不应抛异常。
    asyncio.run(plugin._reply_context_on_llm_request(event, LegacyReq()))
    assert s.get("reply_context_llm_pending") is False
