"""companion-core 上下文消费测试 (TMEAAA-440)。

覆盖：插件缺失 / 契约版本不符 / 超时 / 异常时降级为 v2.4.0（不注入、不阻塞）；
契约 v1 正常时按 {life_state}/{relationship}/{motivation} 注入 prompt，模板缺
占位符时安全追加独立块；发送后回执 on_proactive_outcome；quota.allow 软闸只降权。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.units.unit_companion import (  # noqa: E402
    CompanionContextAdapter,
    sanitize_companion_context,
)

FULL_CONTEXT = {
    "api_version": 1,
    "life_state": {
        "activity": "听课",
        "energy": 0.7,
        "scene": "work",
        "summary": "她刚下课，在食堂",
        "as_of": "2026-09-20T12:00:00+08:00",
        "mood_hint": "有点饿",
    },
    "relationship": {"stage": "熟悉", "affinity": 0.42, "bond": False, "mode": "放松"},
    "motivation": {"reason": "她刚看到你提过的乐队出新歌", "score": 0.71},
    "open_threads": ["你答应周末推荐的电影还没给"],
    "quota": {"hourly_remaining": 1, "daily_remaining": 2, "allow": True},
    "unanswered_streak": 0,
}


class FakeCompanionAdapter:
    def __init__(self, ctx=None, exc=None, delay=0.0, outcome_exc=None):
        self.ctx = ctx
        self.exc = exc
        self.delay = delay
        self.outcome_exc = outcome_exc
        self.fetch_calls = []
        self.outcome_calls = []

    async def fetch_context(self, umo, persona_id=None):
        self.fetch_calls.append({"umo": umo, "persona_id": persona_id})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.ctx

    async def report_outcome(self, umo, *, sent, reason_code, replied=False):
        self.outcome_calls.append(
            {
                "umo": umo,
                "sent": sent,
                "reason_code": reason_code,
                "replied": replied,
            }
        )
        if self.outcome_exc:
            raise self.outcome_exc
        return True


class _ContractStar:
    def __init__(self, raw, version=1, exc=None, delay=0.0, outcome_exc=None):
        self._raw = raw
        self._version = version
        self._exc = exc
        self._delay = delay
        self._outcome_exc = outcome_exc
        self.outcomes = []

    async def get_contract_info(self):
        return {
            "api_version": self._version,
            "capabilities": {
                "life_state": True,
                "emotion": True,
                "expression": True,
            },
        }

    async def get_proactive_context(self, umo, persona_id=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        return self._raw

    async def on_proactive_outcome(self, umo, *, sent, reason_code, replied=False):
        if self._outcome_exc:
            raise self._outcome_exc
        self.outcomes.append(
            {"umo": umo, "sent": sent, "reason_code": reason_code, "replied": replied}
        )


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


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
        captured["provider_id"] = chat_provider_id
        return _Completion(reply)

    plugin.context.get_current_chat_provider_id = _provider_id
    plugin.context.llm_generate = _llm_generate


# --------------------------------------------------------------------------- #
# sanitize / adapter
# --------------------------------------------------------------------------- #


def test_sanitize_clamps_and_drops_unknown_fields():
    ctx = sanitize_companion_context(
        {
            "api_version": 1,
            "life_state": {"summary": " 在食堂 ", "energy": 7, "unknown": "x"},
            "relationship": {"stage": "熟悉", "affinity": -1, "bond": True},
            "motivation": {"reason": "乐队", "score": 1.5},
            "open_threads": ["电影", "", None],
            "quota": {"allow": False, "hourly_remaining": 0},
            "unanswered_streak": 3,
            "future_key": {"ignored": True},
        }
    )
    assert ctx["life_state"]["summary"] == "在食堂"
    assert ctx["life_state"]["energy"] == 1.0
    assert "unknown" not in ctx["life_state"]
    assert ctx["relationship"]["affinity"] == 0.0
    assert ctx["relationship"]["bond"] is True
    assert ctx["motivation"]["score"] == 1.0
    assert ctx["open_threads"] == ["电影"]
    assert ctx["quota"] == {"hourly_remaining": 0, "allow": False}
    assert ctx["unanswered_streak"] == 3
    assert "future_key" not in ctx


def test_sanitize_non_dict_returns_empty():
    assert sanitize_companion_context(None) == {}
    assert sanitize_companion_context("nope") == {}
    assert sanitize_companion_context([]) == {}


def test_missing_companion_core_degrades(plugin):
    # 默认 stub Context 没有 get_registered_star -> 不可用。
    assert asyncio.run(plugin._fetch_companion_context("umo")) == {}


def test_adapter_rejects_version_mismatch(plugin):
    _install_star(plugin, _ContractStar(FULL_CONTEXT, version=2))
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.fetch_context("umo")) is None


def test_adapter_rejects_missing_contract_info(plugin):
    _install_star(plugin, types.SimpleNamespace(get_proactive_context=lambda *a: None))
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.fetch_context("umo")) is None


def test_adapter_fetches_v1_context(plugin):
    star = _ContractStar(FULL_CONTEXT, version=1)
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    ctx = asyncio.run(adapter.fetch_context("umo", persona_id="chika"))
    assert ctx["life_state"]["summary"] == "她刚下课，在食堂"
    assert ctx["api_version"] == 1


def test_adapter_exception_and_timeout_degrade(plugin):
    _install_star(plugin, _ContractStar(FULL_CONTEXT, exc=RuntimeError("boom")))
    assert asyncio.run(CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")) is None

    _install_star(plugin, _ContractStar(FULL_CONTEXT, delay=5.0))
    assert asyncio.run(CompanionContextAdapter(plugin, timeout_sec=0.1).fetch_context("umo")) is None


def test_adapter_inactive_star_degrades(plugin):
    _install_star(plugin, _ContractStar(FULL_CONTEXT), activated=False)
    assert asyncio.run(CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")) is None


# --------------------------------------------------------------------------- #
# generation injection
# --------------------------------------------------------------------------- #


def test_generation_injects_context_fields(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    text = asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    assert text == "早安，今天也想来杯美式吗？"
    prompt = captured["prompt"]
    assert "她刚下课，在食堂" in prompt
    assert "与对方关系：熟悉" in prompt
    assert "好感 0.42" in prompt
    assert "她刚看到你提过的乐队出新歌" in prompt
    assert "【陪伴上下文】" in prompt


def test_generation_uses_placeholders_when_template_has_them(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    plugin.config["proactive_prompt_template"] = (
        "人格：{persona}\n生活：{life_state}\n关系：{relationship}\n动机：{motivation}\n"
    )
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "生活：她刚下课，在食堂" in prompt
    assert "关系：与对方关系：熟悉" in prompt
    assert "【陪伴上下文】" not in prompt


def test_inject_flags_disable_fields(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(
        plugin,
        companion_inject_life_state=False,
        companion_inject_relationship=False,
    )
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    prompt = captured["prompt"]
    assert "她刚下课，在食堂" not in prompt
    assert "与对方关系：熟悉" not in prompt
    assert "她刚看到你提过的乐队出新歌" in prompt


def test_context_unavailable_no_injection(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(exc=RuntimeError("boom"))
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    text = asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    assert text.strip()
    assert "【陪伴上下文】" not in captured["prompt"]


def test_disabled_companion_is_zero_regression(plugin):
    # companion 未启用（默认）：不调用 adapter，prompt 无陪伴块。
    adapter = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    plugin._companion_adapter_override = adapter
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, {}))
    assert adapter.fetch_calls == []
    assert "【陪伴上下文】" not in captured["prompt"]


def test_generation_stashes_quota_for_soft_gate(plugin):
    plugin._companion_adapter_override = FakeCompanionAdapter(ctx=FULL_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    session = {}
    asyncio.run(plugin._generate_proactive_text("umo", "private:1", 3600, session))
    assert session["companion_quota_allow"] is True


# --------------------------------------------------------------------------- #
# outcome callback + soft gate
# --------------------------------------------------------------------------- #


def test_outcome_reported_after_send(plugin):
    adapter = FakeCompanionAdapter()
    plugin._companion_adapter_override = adapter
    _enable(plugin)
    ok = asyncio.run(
        plugin._report_companion_outcome("umo", sent=True, reason_code="probability_pass")
    )
    assert ok is True
    assert adapter.outcome_calls == [
        {"umo": "umo", "sent": True, "reason_code": "probability_pass", "replied": False}
    ]


def test_outcome_degradation_does_not_raise(plugin):
    adapter = FakeCompanionAdapter(outcome_exc=RuntimeError("boom"))
    plugin._companion_adapter_override = adapter
    _enable(plugin)
    assert (
        asyncio.run(plugin._report_companion_outcome("umo", sent=False, reason_code="x"))
        is False
    )
    # 未启用时直接短路，不调用 adapter。
    plugin.config["companion_enabled"] = False
    adapter.outcome_calls.clear()
    assert (
        asyncio.run(plugin._report_companion_outcome("umo", sent=True, reason_code="x"))
        is False
    )
    assert adapter.outcome_calls == []


def test_quota_soft_gate_is_one_shot_and_only_denies(plugin):
    session = {"companion_quota_allow": False}
    assert plugin._companion_quota_soft_gate(session) is True
    # 一次性：读取后清除，不会跨轮持续压制。
    assert plugin._companion_quota_soft_gate(session) is False
    assert plugin._companion_quota_soft_gate({"companion_quota_allow": True}) is False
    assert plugin._companion_quota_soft_gate({}) is False


@pytest.mark.parametrize("raw,expected", [(None, None), ({}, None), ({"allow": False}, False), ({"allow": True}, True)])
def test_companion_quota_allow_parsing(plugin, raw, expected):
    ctx = {"quota": raw} if raw is not None else {}
    assert plugin._companion_quota_allow(ctx) is expected
