"""companion-core ``life_content`` 消费测试 (TMEAAA-557, v2.12.0)。

覆盖：sanitize 白名单/边界；契约能力缺失时剥离（fail-closed，行为同 v2.11.0）；
私聊注入 ``{life_content}`` 占位符 / 模板缺占位符时安全追加；群聊不注入；
与 ``recalled_memory`` / ``life_detail`` 去重；调度循环里的刷新触发与失败静默。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import astrbot_plugin_kanjyou_module.units.unit_companion as unit_companion  # noqa: E402
from astrbot_plugin_kanjyou_module.units.unit_companion import (  # noqa: E402
    LIFE_CONTENT_MAX_ITEMS,
    CompanionContextAdapter,
    _sanitize_life_content,
    sanitize_companion_context,
)

LIFE_ITEMS = [
    {
        "ts": "2026-09-24T08:00:00",
        "kind": "rss",
        "source_ref": "abc123456789",
        "summary": "她读到一篇关于城市屋顶花园的文章",
        "tags": ["园艺", "城市"],
        "expires_at": "2026-10-08T08:00:00",
    },
    {
        "ts": "2026-09-24T07:00:00",
        "kind": "topic",
        "source_ref": "",
        "summary": "她最近对冷萃咖啡很感兴趣",
        "tags": ["咖啡"],
        "expires_at": "2026-10-08T07:00:00",
    },
]

LIFE_DETAIL = {
    "weather": {"code": 3, "temp": 21.5, "text": "阴"},
    "meal": {"slot": "lunch", "label": "午餐", "at": "11:30", "in_window": True},
}

FULL_CONTEXT = {
    "api_version": 1,
    "life_state": {"summary": "她刚下课，在食堂", "energy": 0.7},
    "relationship": {"stage": "熟悉", "affinity": 0.42},
    "motivation": {"reason": "她刚看到你提过的乐队出新歌", "score": 0.71},
    "life_detail": LIFE_DETAIL,
    "life_content": LIFE_ITEMS,
}

#: ``get_proactive_context`` never carries ``life_content`` (it is read
#: separately), so the adapter tests feed a raw context without it.
RAW_CONTEXT = {k: v for k, v in FULL_CONTEXT.items() if k != "life_content"}


class FakeCompanionAdapter:
    def __init__(self, ctx=None, exc=None, refresh_result=None, refresh_exc=None):
        self.ctx = ctx
        self.exc = exc
        self.refresh_result = refresh_result
        self.refresh_exc = refresh_exc
        self.refresh_calls = []

    async def fetch_context(self, umo, persona_id=None):
        if self.exc:
            raise self.exc
        return self.ctx

    async def refresh_life_content(self, persona_id=None):
        self.refresh_calls.append(persona_id)
        if self.refresh_exc:
            raise self.refresh_exc
        return self.refresh_result


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


class _FakeRecall:
    def __init__(self, memories=None):
        self.memories = list(memories or [])

    async def recall(self, umo, query, session_type="private", limit=3):
        return list(self.memories)


def _install_star(plugin, star, name="astrbot_plugin_tcompanion_core", activated=True):
    metadata = types.SimpleNamespace(activated=activated, star_cls=star)
    plugin.context.get_registered_star = lambda _name, _m=metadata: _m


def _enable(plugin, **overrides):
    plugin.config["companion_enabled"] = True
    for key, value in overrides.items():
        plugin.config[key] = value


def _mock_llm(plugin, captured, reply="早安，看到一篇屋顶花园的文章想到你。"):
    async def _provider_id(_umo):
        return "provider-1"

    async def _llm_generate(chat_provider_id, prompt):
        captured["prompt"] = prompt
        return _Completion(reply)

    plugin.context.get_current_chat_provider_id = _provider_id
    plugin.context.llm_generate = _llm_generate


# --------------------------------------------------------------------------- #
# sanitize / adapter
# --------------------------------------------------------------------------- #


def test_sanitize_life_content_whitelists_and_bounds():
    items = _sanitize_life_content(
        {
            "api_version": 1,
            "items": [
                {
                    "summary": " 第一条 ",
                    "tags": ["a", "b"],
                    "ts": "t1",
                    "source_ref": "r1",
                    "unknown": "drop",
                },
                {"summary": "第二条"},
                {"summary": ""},  # dropped
                "nope",  # dropped
                {"summary": "第三条"},
                {"summary": "第四条"},  # beyond cap
            ],
        }
    )
    assert len(items) == LIFE_CONTENT_MAX_ITEMS
    assert items[0]["summary"] == "第一条"
    assert items[0]["tags"] == ["a", "b"]
    assert items[0]["ts"] == "t1"
    assert items[0]["source_ref"] == "r1"
    assert "unknown" not in items[0]


def test_sanitize_malformed_life_content_is_dropped():
    assert _sanitize_life_content("nope") == []
    assert _sanitize_life_content({"items": "nope"}) == []
    assert _sanitize_life_content({"items": [{"tags": ["x"]}]}) == []


def test_sanitize_companion_context_keeps_life_content():
    ctx = sanitize_companion_context(
        {"api_version": 1, "life_content": {"items": LIFE_ITEMS}}
    )
    assert [item["summary"] for item in ctx["life_content"]] == [
        "她读到一篇关于城市屋顶花园的文章",
        "她最近对冷萃咖啡很感兴趣",
    ]


class _LifeStar:
    def __init__(self, raw, items, life_content=True, raise_read=False):
        self._raw = raw
        self._items = items
        self._life_content = life_content
        self._raise_read = raise_read
        self.read_calls = []

    async def get_contract_info(self):
        return {
            "api_version": 1,
            "capabilities": {
                "life_state": True,
                "life_line": True,
                "life_content": self._life_content,
            },
        }

    async def get_proactive_context(self, umo, persona_id=None):
        return self._raw

    async def get_life_content(self, persona_id=None, window=None, kind=None):
        self.read_calls.append(persona_id)
        if self._raise_read:
            raise RuntimeError("boom")
        return {"api_version": 1, "persona_id": persona_id, "items": self._items}


def test_adapter_strips_life_content_without_capability(plugin):
    star = _LifeStar(RAW_CONTEXT, LIFE_ITEMS, life_content=False)
    _install_star(plugin, star)
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")
    )
    assert "life_content" not in ctx
    assert star.read_calls == []  # legacy core never called


def test_adapter_keeps_life_content_with_capability(plugin):
    star = _LifeStar(RAW_CONTEXT, LIFE_ITEMS, life_content=True)
    _install_star(plugin, star)
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo", persona_id="chika")
    )
    assert ctx["life_content"][0]["summary"] == "她读到一篇关于城市屋顶花园的文章"
    assert star.read_calls == ["chika"]


def test_adapter_life_content_read_failure_is_silent(plugin):
    star = _LifeStar(RAW_CONTEXT, [], life_content=True, raise_read=True)
    _install_star(plugin, star)
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")
    )
    assert "life_content" not in ctx


# --------------------------------------------------------------------------- #
# generation injection
# --------------------------------------------------------------------------- #


def test_generation_injects_life_content_private(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "她读到一篇关于城市屋顶花园的文章" in prompt
    assert "她最近对冷萃咖啡很感兴趣" in prompt
    assert "见闻：" in prompt  # template 缺占位符 -> 安全追加


def test_generation_uses_life_content_placeholder_when_present(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    plugin.config["proactive_prompt_template"] = "人格：{persona}\n见闻：{life_content}\n"
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "她读到一篇关于城市屋顶花园的文章" in prompt
    assert "见闻：她读到一篇关于城市屋顶花园的文章" in prompt
    assert "\n见闻：\n" not in prompt  # 不重复追加独立块


def test_generation_missing_life_content_is_unchanged(plugin):
    ctx = {k: v for k, v in FULL_CONTEXT.items() if k != "life_content"}
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=ctx)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "她读到一篇关于城市屋顶花园的文章" not in prompt
    assert "见闻" not in prompt


def test_generation_group_strips_life_content(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "group:1", 3600, {}))
    prompt = captured["prompt"]
    assert "她读到一篇关于城市屋顶花园的文章" not in prompt
    assert "见闻" not in prompt


def test_life_content_inject_flag_disables(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin, companion_inject_life_content=False)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "她读到一篇关于城市屋顶花园的文章" not in prompt
    # 其它陪伴字段不受影响。
    assert "她刚下课，在食堂" in prompt


def test_generation_life_content_dedupes_with_memory(plugin):
    overlap = "她最近对冷萃咖啡很感兴趣"
    plugin._companion_adapter_override = FakeCompanionAdapter(
        ctx=dict(FULL_CONTEXT, memory={"snippets": [overlap]})
    )
    plugin._memory_recall_adapter_override = _FakeRecall(memories=[])
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert prompt.count(overlap) == 1
    assert "她读到一篇关于城市屋顶花园的文章" in prompt


def test_generation_life_content_dedupes_with_life_detail(plugin):
    overlap = "今天天气：阴，21.5℃"
    items = [{"summary": overlap, "tags": []}, {"summary": "她最近在读一本关于园艺的书"}]
    plugin._companion_adapter_override = FakeCompanionAdapter(
        ctx=dict(FULL_CONTEXT, life_content=items)
    )
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert prompt.count(overlap) == 1
    assert "她最近在读一本关于园艺的书" in prompt


# --------------------------------------------------------------------------- #
# scheduler loop refresh trigger
# --------------------------------------------------------------------------- #


def test_refresh_skips_when_disabled_or_switch_off(plugin):
    adapter = FakeCompanionAdapter(refresh_result={"applied": True, "generated": 1})
    plugin._companion_adapter_override = adapter

    assert asyncio.run(plugin._companion_maybe_refresh_life_content()) is False
    assert adapter.refresh_calls == []

    _enable(plugin, companion_inject_life_content=False)
    assert asyncio.run(plugin._companion_maybe_refresh_life_content()) is False
    assert adapter.refresh_calls == []


def test_refresh_calls_core_when_enabled(plugin):
    adapter = FakeCompanionAdapter(refresh_result={"applied": True, "generated": 2})
    plugin._companion_adapter_override = adapter
    plugin.config["persona_id"] = "chika"
    _enable(plugin)

    assert asyncio.run(plugin._companion_maybe_refresh_life_content()) is True
    assert adapter.refresh_calls == ["chika"]


def test_refresh_failure_is_silent(plugin):
    adapter = FakeCompanionAdapter(refresh_exc=RuntimeError("boom"))
    plugin._companion_adapter_override = adapter
    _enable(plugin)

    assert asyncio.run(plugin._companion_maybe_refresh_life_content()) is False


def test_adapter_refresh_requires_capability(plugin):
    star = _LifeStar(RAW_CONTEXT, LIFE_ITEMS, life_content=False)
    _install_star(plugin, star)
    result = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).refresh_life_content("chika")
    )
    assert result is None


def test_adapter_refresh_timeout_is_silent(plugin, monkeypatch):
    class _SlowStar(_LifeStar):
        async def refresh_life_content(self, persona_id=None):
            await asyncio.sleep(0.5)
            return {"applied": True, "generated": 1}

    monkeypatch.setattr(unit_companion, "LIFE_CONTENT_REFRESH_TIMEOUT_SEC", 0.05)
    star = _SlowStar(FULL_CONTEXT, LIFE_ITEMS, life_content=True)
    _install_star(plugin, star)
    result = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).refresh_life_content("chika")
    )
    assert result is None


def test_check_sessions_triggers_refresh(plugin, monkeypatch):
    called = []

    async def _fake_refresh():
        called.append(True)
        return True

    monkeypatch.setattr(plugin, "_companion_maybe_refresh_life_content", _fake_refresh)
    asyncio.run(plugin._check_sessions())
    assert called == [True]


def test_refresh_failure_does_not_break_proactive_send(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(
        ctx=FULL_CONTEXT, refresh_exc=RuntimeError("boom")
    )
    _enable(plugin)
    assert asyncio.run(plugin._companion_maybe_refresh_life_content()) is False

    captured = {}
    _mock_llm(plugin, captured, reply="记得看看那篇文章～")
    text = asyncio.run(
        plugin._generate_proactive_text("umo", "private:1", 3600, {})
    )
    assert text == "记得看看那篇文章～"
