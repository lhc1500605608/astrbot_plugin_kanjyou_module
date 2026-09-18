"""主动消息分段真人化节奏测试 (TMEAAA-399).

覆盖：分段数量、最后一次无延迟、延迟区间与长度平滑、拼接无损、
首个分片不做人为拆分，以及主动路径统一走 _dispatch_reply_segments。
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture()
def gen(plugin_module):
    return importlib.import_module("astrbot_plugin_kanjyou_module.units.unit_generation")


def _install(gen, monkeypatch):
    events = []

    async def _send(segment):
        events.append(segment)

    async def _sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr(gen.asyncio, "sleep", _sleep)
    return events, _send


def _segments(events):
    return [e for e in events if isinstance(e, str)]


def _sleeps(events):
    return [e[1] for e in events if isinstance(e, tuple)]


def test_proactive_split_uses_semantic_boundaries(plugin):
    parts = plugin._split_proactive_segments("今天天气不错。要不要出去走走？")
    assert parts == ["今天天气不错。", "要不要出去走走？"]


def test_proactive_never_hard_splits_first_segment(plugin):
    text = "这是一条没有任何标点的超长主动问候内容用于验证不会被按字数人为切断" * 3
    assert plugin._split_proactive_segments(text) == [text]


def test_proactive_delay_is_smooth_and_within_range(plugin, gen, monkeypatch):
    plugin.config["proactive_segment_delay_min_ms"] = 300
    plugin.config["proactive_segment_delay_max_ms"] = 900

    monkeypatch.setattr(gen.random, "uniform", lambda a, b: 1.0)
    short = plugin._proactive_segment_delay_ms("短")
    long = plugin._proactive_segment_delay_ms("很长的分片内容" * 20)
    assert short < long

    monkeypatch.setattr(gen.random, "uniform", lambda a, b: a)
    for n in range(1, 60):
        assert 300 <= plugin._proactive_segment_delay_ms("字" * n) <= 900


def test_dispatch_proactive_segments_and_last_has_no_delay(plugin, gen, monkeypatch):
    plugin.config["proactive_segment_enabled"] = True
    plugin.config["proactive_segment_max_parts"] = 4
    plugin.config["proactive_segment_delay_min_ms"] = 300
    plugin.config["proactive_segment_delay_max_ms"] = 900

    events, send = _install(gen, monkeypatch)
    text = "第一句话。第二句话？第三句话！"
    asyncio.run(plugin._dispatch_reply_segments(send, text, proactive=True))

    segs = _segments(events)
    sleeps = _sleeps(events)
    assert segs == ["第一句话。", "第二句话？", "第三句话！"]
    assert len(sleeps) == len(segs) - 1
    assert all(0.3 <= s <= 0.9 for s in sleeps)
    assert "".join(segs) == text


def test_dispatch_proactive_respects_max_parts(plugin, gen, monkeypatch):
    plugin.config["proactive_segment_max_parts"] = 2

    events, send = _install(gen, monkeypatch)
    text = "第一句。第二句。第三句。"
    asyncio.run(plugin._dispatch_reply_segments(send, text, proactive=True))

    segs = _segments(events)
    assert segs == ["第一句。", "第二句。第三句。"]
    assert "".join(segs) == text


def test_dispatch_proactive_disabled_sends_whole(plugin, gen, monkeypatch):
    plugin.config["proactive_segment_enabled"] = False

    events, send = _install(gen, monkeypatch)
    text = "第一句。第二句。"
    asyncio.run(plugin._dispatch_reply_segments(send, text, proactive=True))

    assert _segments(events) == [text]


def test_send_proactive_routes_through_segmented_dispatch(plugin, gen, monkeypatch):
    plugin.config["proactive_segment_enabled"] = True
    plugin.config["proactive_segment_max_parts"] = 4
    topic = "第一句话。第二句话？第三句话！"

    async def _fake_generate(*_args, **_kwargs):
        return topic

    plugin._generate_proactive_text = _fake_generate

    sent = []

    async def _fake_send_message(_umo, chain):
        sent.append(chain.chain[0].text)
        return True

    plugin.context.send_message = _fake_send_message
    _install(gen, monkeypatch)

    ok, text = asyncio.run(
        plugin._send_proactive("aiocqhttp:FriendMessage:1", None, "private:1", 10, {})
    )
    assert ok is True
    assert text == topic
    assert sent == ["第一句话。", "第二句话？", "第三句话！"]


def test_proactive_history_dedupe_uses_whole_text(plugin):
    session = {}
    plugin._push_proactive_history(session, "第一句。第二句。")
    assert session["recent_proactive_texts"] == ["第一句。第二句。"]
