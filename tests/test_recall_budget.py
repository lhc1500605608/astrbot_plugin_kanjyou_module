import asyncio
import sys


class _Adapter:
    def __init__(self, ctx=None, delay=0.0):
        self.ctx = ctx or {}
        self.delay = delay
        self.calls = []

    async def fetch_context(self, umo, persona_id=None):
        self.calls.append((umo, persona_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.ctx


class _Recall:
    def __init__(self, result=None, delay=0.0):
        self.result = list(result or [])
        self.delay = delay
        self.calls = []

    async def recall(self, umo, query, session_type="private", limit=3):
        self.calls.append((umo, query, session_type, limit))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


class _Completion:
    completion_text = "今天想和你聊聊。"


def _setup_generation(plugin):
    plugin.config["companion_enabled"] = True
    plugin.config["memory_recall_enabled"] = True
    plugin.config["proactive_prompt_template"] = "记忆：{recalled_memory}"
    async def provider(_umo):
        return "provider"

    async def generate(chat_provider_id, prompt):
        plugin._test_prompt = prompt
        return _Completion()

    plugin.context.get_current_chat_provider_id = provider
    plugin.context.llm_generate = generate


def test_generation_reuses_companion_memory_without_second_recall(plugin):
    _setup_generation(plugin)
    adapter = _Adapter({"memory": {"snippets": ["偏好手冲咖啡"]}})
    recall = _Recall(["偏好手冲咖啡", "刚好相关的话题"])
    plugin._companion_adapter_override = adapter
    plugin._memory_recall_adapter_override = recall

    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))

    assert adapter.calls == [("umo", None)]
    assert recall.calls == []
    assert plugin._test_prompt.count("偏好手冲咖啡") == 1


def test_missing_companion_memory_uses_one_topic_recall(plugin):
    _setup_generation(plugin)
    plugin._companion_adapter_override = _Adapter({})
    recall = _Recall(["周末想去看展"])
    plugin._memory_recall_adapter_override = recall

    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))

    assert len(recall.calls) == 1
    assert recall.calls[0][2] == "private"
    assert "周末想去看展" in plugin._test_prompt


def test_disabled_memory_recall_does_not_inject_companion_memory(plugin):
    _setup_generation(plugin)
    plugin.config["memory_recall_enabled"] = False
    plugin._companion_adapter_override = _Adapter(
        {"memory": {"snippets": ["disabled memory"]}}
    )
    recall = _Recall(["should not be read"])
    plugin._memory_recall_adapter_override = recall

    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))

    assert recall.calls == []
    assert "disabled memory" not in plugin._test_prompt
    assert "should not be read" not in plugin._test_prompt


def test_explicit_persona_skips_unscoped_memory_sources(plugin):
    _setup_generation(plugin)
    plugin.config["persona_id"] = "persona-fixed"
    plugin._companion_adapter_override = _Adapter(
        {"memory": {"snippets": ["persona scoped detail"]}}
    )
    recall = _Recall(["unscoped detail"])
    plugin._memory_recall_adapter_override = recall

    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))

    assert recall.calls == []
    assert "persona scoped detail" not in plugin._test_prompt
    assert "unscoped detail" not in plugin._test_prompt


def test_direct_recall_respects_remaining_generation_budget(plugin, monkeypatch):
    _setup_generation(plugin)
    plugin._companion_adapter_override = _Adapter({})
    recall = _Recall(["迟到响应"], delay=0.05)
    plugin._memory_recall_adapter_override = recall
    generation = sys.modules[
        "astrbot_plugin_kanjyou_module.units.unit_generation"
    ]
    monkeypatch.setattr(generation, "_PROACTIVE_RECALL_BUDGET_SEC", 0.005)
    debug = []
    plugin._debug = debug.append

    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))

    assert len(recall.calls) == 1
    assert "迟到响应" not in plugin._test_prompt
    assert "memory recall budget exhausted" in " ".join(debug)


def test_group_recall_keeps_existing_private_memory_gate(plugin):
    _setup_generation(plugin)
    plugin.config["memory_recall_private_only"] = True
    plugin._companion_adapter_override = _Adapter({})
    recall = _Recall(["private detail"])
    plugin._memory_recall_adapter_override = recall

    asyncio.run(plugin._generate_proactive_text("umo", "group:1", 3600, {}))

    assert recall.calls == []
    assert "private detail" not in plugin._test_prompt
