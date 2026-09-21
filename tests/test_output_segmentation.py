"""被动回复语义分段（方案 B）测试 (TMEAAA-482)。

覆盖：模式/开关守卫、非 LLM 与非纯文本放行、保护段（代码块/行内码/URL/
[[IMAGE]]）不拆、无损拼接、逐条发送与延迟、幂等、异常兜底回退。
"""

from __future__ import annotations

import asyncio
import importlib
import types

import pytest
from conftest import _StubImage, _StubPlain, _StubResult

MULTI = "先说明方案的整体思路。再优化一下实现细节。最后跑一遍测试验证。"


@pytest.fixture()
def gen(plugin_module):
    return importlib.import_module("astrbot_plugin_kanjyou_module.units.unit_segmentation")


def _event(plugin_module, group_id: str = ""):
    msg_obj = types.SimpleNamespace(
        group_id=group_id, sender=types.SimpleNamespace(user_id="7")
    )
    return plugin_module.AstrMessageEvent(
        message_str="hi", message_obj=msg_obj, umo="webchat:FriendMessage:7"
    )


def _llm_event(plugin_module, text: str, group_id: str = ""):
    event = _event(plugin_module, group_id=group_id)
    event.set_result(_StubResult([_StubPlain(text)], content_type="llm"))
    return event


def _run(plugin, coro):
    return asyncio.run(coro)


def test_mode_gating(plugin):
    assert plugin._output_segment_enabled() is True
    assert plugin._output_segment_mode() == "semantic"
    assert plugin._output_segment_active() is True

    plugin.config["output_segment_mode"] = "native_compat"
    assert plugin._output_segment_active() is False
    plugin.config["output_segment_mode"] = "off"
    assert plugin._output_segment_active() is False
    plugin.config["output_segment_mode"] = "semantic"
    plugin.config["output_segment_enabled"] = False
    assert plugin._output_segment_active() is False


def test_build_segments_semantic_and_lossless(plugin):
    parts = plugin._build_output_segments(MULTI)
    assert parts == ["先说明方案的整体思路。", "再优化一下实现细节。", "最后跑一遍测试验证。"]
    assert "".join(parts) == MULTI


def test_build_segments_respects_max_parts(plugin):
    plugin.config["output_segment_max_parts"] = 2
    parts = plugin._build_output_segments(MULTI)
    assert len(parts) == 2
    assert "".join(parts) == MULTI


def test_build_segments_short_reply_stays_single(plugin):
    assert plugin._build_output_segments("好的，没问题。") == ["好的，没问题。"]


def test_code_block_is_not_split(plugin):
    text = "看这段代码：\n```python\nprint(1)\n```\n这样就完成了实现。后续可以继续优化方案。"
    parts = plugin._build_output_segments(text)
    assert any("```python\nprint(1)\n```" in p for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_inline_code_is_not_split(plugin):
    text = "运行 `pip install requests` 安装依赖。然后优化实现方案。"
    parts = plugin._build_output_segments(text)
    assert any("`pip install requests`" in p for p in parts)
    assert "".join(parts) == text


def test_url_is_not_split(plugin):
    text = "参考这个链接 https://example.com/a.b 的说明。另外注意实现与优化方案。"
    parts = plugin._build_output_segments(text)
    holders = [p for p in parts if "https://example.com/a.b" in p]
    assert len(holders) == 1
    assert sum("https://" in p for p in parts) == 1


def test_image_token_is_not_split(plugin):
    text = "先看结果。[[IMAGE]] 一张说明实现思路的图。最后再优化方案。"
    parts = plugin._build_output_segments(text)
    assert any("[[IMAGE]]" in p for p in parts)
    assert "".join(parts) == text


def test_non_llm_result_passes_through(plugin, plugin_module):
    event = _event(plugin_module)
    original = _StubResult([_StubPlain(MULTI)], content_type="general")
    event.set_result(original)
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert event.get_result() is original
    assert event.sent == []


def test_non_plain_chain_passes_through(plugin, plugin_module):
    event = _event(plugin_module)
    original = _StubResult([_StubPlain(MULTI), _StubImage("x.png")], content_type="llm")
    event.set_result(original)
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert event.get_result() is original
    assert event.sent == []


def test_disabled_passes_through(plugin, plugin_module):
    plugin.config["output_segment_enabled"] = False
    event = _llm_event(plugin_module, MULTI)
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert event.sent == []
    assert event.get_result() is not None


def test_private_only_skips_group(plugin, plugin_module):
    plugin.config["output_segment_private_only"] = True
    event = _llm_event(plugin_module, MULTI, group_id="999")
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert event.sent == []
    assert event.get_result() is not None


def test_hook_sends_segments_clears_result(plugin, plugin_module):
    plugin.config["output_segment_delay_min_ms"] = 0
    plugin.config["output_segment_delay_max_ms"] = 0
    event = _llm_event(plugin_module, MULTI)
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert [c.chain[0].text for c in event.sent] == [
        "先说明方案的整体思路。",
        "再优化一下实现细节。",
        "最后跑一遍测试验证。",
    ]
    assert event.get_result() is None
    assert event.get_extra("_kanjyou_output_seg_done") is True


def test_hook_is_idempotent(plugin, plugin_module):
    plugin.config["output_segment_delay_min_ms"] = 0
    plugin.config["output_segment_delay_max_ms"] = 0
    event = _llm_event(plugin_module, MULTI)
    _run(plugin, plugin._evt_on_decorating_result(event))
    first = list(event.sent)
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert len(event.sent) == len(first)


def test_last_segment_has_no_delay(plugin, plugin_module, gen, monkeypatch):
    plugin.config["output_segment_delay_min_ms"] = 300
    plugin.config["output_segment_delay_max_ms"] = 900

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(gen.asyncio, "sleep", _fake_sleep)
    event = _llm_event(plugin_module, MULTI)
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert len(event.sent) == 3
    assert len(sleeps) == 2
    assert all(0.3 <= s <= 0.9 for s in sleeps)


def test_delay_is_smooth_and_within_range(plugin, gen, monkeypatch):
    plugin.config["output_segment_delay_min_ms"] = 300
    plugin.config["output_segment_delay_max_ms"] = 900
    monkeypatch.setattr(gen.random, "uniform", lambda a, b: a)
    for n in range(1, 60):
        assert 300 <= plugin._output_segment_delay_ms("字" * n) <= 900


def test_send_failure_restores_original(plugin, plugin_module):
    plugin.config["output_segment_delay_min_ms"] = 0
    plugin.config["output_segment_delay_max_ms"] = 0
    event = _llm_event(plugin_module, MULTI)
    original = event.get_result()

    async def _boom(_chain):
        raise RuntimeError("platform down")

    event.send = _boom
    _run(plugin, plugin._evt_on_decorating_result(event))
    assert event.get_result() is original


def test_segment_prepare_disables_streaming(plugin, plugin_module):
    event = _event(plugin_module)
    _run(plugin, plugin._evt_segment_prepare(event))
    assert event.get_extra("enable_streaming") is False


def test_segment_prepare_noop_when_disabled(plugin, plugin_module):
    plugin.config["output_segment_enabled"] = False
    event = _event(plugin_module)
    _run(plugin, plugin._evt_segment_prepare(event))
    assert event.get_extra("enable_streaming") is None
