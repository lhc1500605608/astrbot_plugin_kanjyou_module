"""主动消息整合 tmemory 记忆召回测试 (TMEAAA-397).

覆盖：tmemory 缺失 / 超时 / 抛异常时生成流程不被阻塞；群聊默认不注入、
开启后按 group 模式（仅非私有记忆）；私聊召回写入 {recalled_memory}。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.units.unit_memory_recall import (  # noqa: E402
    MemoryRecallUnitsMixin,
)


class FakeRecallAdapter:
    def __init__(self, memories=None, exc=None, delay=0.0):
        self.memories = list(memories or [])
        self.exc = exc
        self.delay = delay
        self.calls = []

    async def recall(self, umo, query, session_type="private", limit=3):
        self.calls.append(
            {
                "umo": umo,
                "query": query,
                "session_type": session_type,
                "limit": limit,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return list(self.memories)


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


def test_missing_tmemory_degrades_to_none(plugin):
    # 默认 stub Context 没有 get_registered_star，tmemory 视为缺失。
    result = asyncio.run(
        plugin._recall_memory_for_prompt("umo", "private:1", 3600, {})
    )
    assert result == "无"


def test_recall_exception_does_not_block(plugin):
    adapter = FakeRecallAdapter(exc=RuntimeError("tmemory boom"))
    plugin._memory_recall_adapter_override = adapter
    text = asyncio.run(
        plugin._generate_proactive_text("umo", "private:1", 3600, {})
    )
    assert isinstance(text, str) and text.strip()
    assert adapter.calls, "adapter should have been invoked"
    assert adapter.calls[0]["session_type"] == "private"


def test_recall_timeout_does_not_block(plugin):
    plugin.config["memory_recall_timeout_sec"] = 0.05
    adapter = FakeRecallAdapter(memories=["慢记忆"], delay=5.0)
    plugin._memory_recall_adapter_override = adapter

    async def _run():
        started = asyncio.get_event_loop().time()
        fragment = await plugin._recall_memory_for_prompt(
            "umo", "private:1", 3600, {}
        )
        elapsed = asyncio.get_event_loop().time() - started
        text = await plugin._generate_proactive_text("umo", "private:1", 3600, {})
        return fragment, elapsed, text

    fragment, elapsed, text = asyncio.run(_run())
    assert fragment == "无"
    assert elapsed < 2.0
    assert isinstance(text, str) and text.strip()


def test_group_session_skipped_by_default(plugin):
    adapter = FakeRecallAdapter(memories=["群私有记忆"])
    plugin._memory_recall_adapter_override = adapter
    result = asyncio.run(
        plugin._recall_memory_for_prompt("umo", "group:9", 3600, {})
    )
    assert result == "无"
    assert adapter.calls == []


def test_group_session_uses_group_mode_when_enabled(plugin):
    plugin.config["memory_recall_private_only"] = False
    plugin.config["memory_recall_group_enabled"] = True
    adapter = FakeRecallAdapter(memories=["公开的群记忆"])
    plugin._memory_recall_adapter_override = adapter
    result = asyncio.run(
        plugin._recall_memory_for_prompt("umo", "group:9", 3600, {})
    )
    assert adapter.calls and adapter.calls[0]["session_type"] == "group"
    assert result == "- 公开的群记忆"


def test_merge_dedupes_whitespace_and_case():
    merged = MemoryRecallUnitsMixin._merge_memory_snippets(
        [" 她喜欢美式 ", "最近在改论文"],
        ["她喜欢美式", "She Likes Coffee", "she likes coffee"],
        5,
    )
    assert merged == ["她喜欢美式", "最近在改论文", "She Likes Coffee"]


def test_merge_respects_total_cap_own_first():
    merged = MemoryRecallUnitsMixin._merge_memory_snippets(
        ["自己一", "自己二"], ["桥一", "桥二"], 3
    )
    assert merged == ["自己一", "自己二", "桥一"]


def test_companion_memory_merged_and_deduped(plugin):
    adapter = FakeRecallAdapter(memories=["她喜欢美式咖啡"])
    plugin._memory_recall_adapter_override = adapter
    companion_memory = {
        "snippets": ["她喜欢美式咖啡", "最近在改论文"],
        "profile": {"summary": "安静、爱喝咖啡"},
    }
    result = asyncio.run(
        plugin._recall_memory_for_prompt(
            "umo", "private:1", 3600, {}, "", "", companion_memory
        )
    )
    assert adapter.calls, "own recall should still run"
    assert result == "- 她喜欢美式咖啡\n- 最近在改论文\n- 对方画像摘要：安静、爱喝咖啡"


def test_companion_memory_only_when_own_recall_available(plugin):
    # 自身召回为空但 companion 有数据时仍注入；禁用时不注入。
    plugin._memory_recall_adapter_override = FakeRecallAdapter(memories=[])
    companion_memory = {"snippets": ["桥接记忆"]}
    result = asyncio.run(
        plugin._recall_memory_for_prompt(
            "umo", "private:1", 3600, {}, "", "", companion_memory
        )
    )
    assert result == "- 桥接记忆"
    plugin.config["memory_recall_enabled"] = False
    disabled = asyncio.run(
        plugin._recall_memory_for_prompt(
            "umo", "private:1", 3600, {}, "", "", companion_memory
        )
    )
    assert disabled == "无"


def test_group_never_injects_companion_private_memory_by_default(plugin):
    adapter = FakeRecallAdapter(memories=[])
    plugin._memory_recall_adapter_override = adapter
    companion_memory = {"snippets": ["私聊里提过的事"]}
    result = asyncio.run(
        plugin._recall_memory_for_prompt(
            "umo", "group:9", 3600, {}, "", "", companion_memory
        )
    )
    assert result == "无"
    assert adapter.calls == []


def test_private_recall_injected_into_prompt(plugin):
    adapter = FakeRecallAdapter(memories=["主人喜欢喝美式咖啡"])
    plugin._memory_recall_adapter_override = adapter
    captured = {}

    async def _provider_id(_umo):
        return "provider-1"

    async def _llm_generate(chat_provider_id, prompt):
        captured["prompt"] = prompt
        captured["provider_id"] = chat_provider_id
        return _Completion("早安，今天也想来杯美式吗？")

    plugin.context.get_current_chat_provider_id = _provider_id
    plugin.context.llm_generate = _llm_generate
    text = asyncio.run(
        plugin._generate_proactive_text("umo", "private:1", 3600, {})
    )
    assert text == "早安，今天也想来杯美式吗？"
    assert "主人喜欢喝美式咖啡" in captured["prompt"]
    assert captured["provider_id"] == "provider-1"
