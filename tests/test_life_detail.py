"""companion-core ``life_detail`` 消费测试 (TMEAAA-530)。

覆盖：sanitize 白名单/边界；契约能力缺失时剥离（fail-closed）；私聊注入
``{life_detail}`` 占位符 / 模板缺占位符时安全追加；缺失 / 群聊时行为 = v2.9.0；
与 2-E ``memory`` 注入按同一空白/casefold 规整去重。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.units.unit_companion import (  # noqa: E402
    CompanionContextAdapter,
    sanitize_companion_context,
)

LIFE_DETAIL = {
    "weather": {"code": 3, "temp": 21.5, "precip": 0.0, "text": "阴"},
    "meal": {"slot": "lunch", "label": "午餐", "at": "11:30", "in_window": True},
    "sleep": {"window": "23:00-07:30", "since": "23:00", "source": "inferred"},
    "quiet": False,
    "diary": {
        "day": "2026-09-20",
        "summary": "2026-09-20：23:00–07:30 在休息；吃了午餐。心情平静。",
        "mood": "平静",
    },
}

FULL_CONTEXT = {
    "api_version": 1,
    "life_state": {"summary": "她刚下课，在食堂", "energy": 0.7},
    "relationship": {"stage": "熟悉", "affinity": 0.42},
    "motivation": {"reason": "她刚看到你提过的乐队出新歌", "score": 0.71},
    "life_detail": LIFE_DETAIL,
}


class FakeCompanionAdapter:
    def __init__(self, ctx=None, exc=None):
        self.ctx = ctx
        self.exc = exc
        self.fetch_calls = []

    async def fetch_context(self, umo, persona_id=None):
        self.fetch_calls.append({"umo": umo, "persona_id": persona_id})
        if self.exc:
            raise self.exc
        return self.ctx


class _LifeStar:
    def __init__(self, raw, life_line=True):
        self._raw = raw
        self._life_line = life_line

    async def get_contract_info(self):
        return {
            "api_version": 1,
            "capabilities": {"life_state": True, "life_line": self._life_line},
        }

    async def get_proactive_context(self, umo, persona_id=None):
        return self._raw


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


def _mock_llm(plugin, captured, reply="早安，今天也想来杯美式吗？"):
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


def test_sanitize_keeps_life_detail_and_drops_unknown():
    ctx = sanitize_companion_context(
        {
            "api_version": 1,
            "life_detail": dict(
                LIFE_DETAIL,
                weather={"code": 3, "temp": 21.5, "text": " 阴 ", "unknown": "x"},
                future_key={"ignored": True},
            ),
        }
    )
    detail = ctx["life_detail"]
    assert detail["weather"]["text"] == "阴"
    assert detail["weather"]["temp"] == 21.5
    assert "unknown" not in detail["weather"]
    assert detail["meal"]["label"] == "午餐"
    assert detail["meal"]["in_window"] is True
    assert detail["sleep"]["window"] == "23:00-07:30"
    assert detail["quiet"] is False
    assert detail["diary"]["mood"] == "平静"
    assert "future_key" not in detail


def test_sanitize_malformed_life_detail_is_dropped():
    assert "life_detail" not in sanitize_companion_context(
        {"api_version": 1, "life_detail": "nope"}
    )
    assert "life_detail" not in sanitize_companion_context(
        {"api_version": 1, "life_detail": {"weather": {"temp": "x"}, "extra": 1}}
    )


def test_adapter_strips_life_detail_without_capability(plugin):
    _install_star(plugin, _LifeStar(FULL_CONTEXT, life_line=False))
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")
    )
    assert "life_detail" not in ctx


def test_adapter_keeps_life_detail_with_capability(plugin):
    _install_star(plugin, _LifeStar(FULL_CONTEXT, life_line=True))
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")
    )
    assert ctx["life_detail"]["weather"]["text"] == "阴"


# --------------------------------------------------------------------------- #
# generation injection
# --------------------------------------------------------------------------- #


def test_generation_injects_life_detail_private(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "今天天气：阴，21.5℃" in prompt
    assert "对方正在午餐时间" in prompt
    assert "对方作息约 23:00-07:30" in prompt
    assert "生活细节：" in prompt  # template 缺占位符 -> 安全追加


def test_generation_uses_life_detail_placeholder_when_present(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    plugin.config["proactive_prompt_template"] = "人格：{persona}\n细节：{life_detail}\n"
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "细节：今天天气：阴，21.5℃" in prompt
    assert "生活细节：" not in prompt


def test_generation_missing_life_detail_is_unchanged(plugin):
    ctx = {k: v for k, v in FULL_CONTEXT.items() if k != "life_detail"}
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=ctx)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "今天天气" not in prompt
    assert "生活细节" not in prompt


def test_generation_group_strips_life_detail(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "group:1", 3600, {}))
    prompt = captured["prompt"]
    assert "今天天气" not in prompt
    assert "生活细节" not in prompt


def test_generation_life_detail_dedupes_with_memory(plugin):
    overlap = "今天天气：阴，21.5℃"
    plugin._companion_adapter_override = FakeCompanionAdapter(
        ctx=dict(FULL_CONTEXT, memory={"snippets": [overlap]})
    )
    plugin._memory_recall_adapter_override = _FakeRecall(memories=[])
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    # 同一条生活细节只出现一次（由 memory 承载，life_detail 去重后不再重复）。
    assert prompt.count(overlap) == 1
    # 与 memory 不重复的其余细节照常注入。
    assert "对方正在午餐时间" in prompt


def test_life_detail_inject_flag_disables(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin, companion_inject_life_detail=False)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "今天天气" not in prompt
    # 其它陪伴字段不受影响。
    assert "她刚下课，在食堂" in prompt
