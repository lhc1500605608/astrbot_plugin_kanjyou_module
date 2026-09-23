"""Phase 3-C2 群聊参与闸门 + 群氛围表达测试 (TMEAAA-550)。

覆盖：group/participation 白名单清洗与边界；短标签提取（无原文）；adapter 按
``group_aware`` 能力门控（缺失/异常 fail-closed）；群消息路径上报
``record_group_activity``；群内主动前按 ``get_group_context`` 闸门决定是否参与
（allow=false 不发、reason 可观测）；群聊不注入私聊陪伴字段、只注入群氛围；
companion 缺失/异常/超时回落 v2.10.2 行为。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.units.unit_companion import (  # noqa: E402
    CompanionContextAdapter,
    _short_group_topic,
    sanitize_companion_context,
)

GROUP_UMO = "aiocqhttp:group:g1"
PRIVATE_UMO = "aiocqhttp:private:u1"

GROUP_CONTEXT = {
    "api_version": 1,
    "life_state": {"summary": "她刚下课，在食堂", "energy": 0.7},
    "relationship": {"stage": "熟悉", "affinity": 0.42},
    "motivation": {"reason": "她刚看到你提过的乐队出新歌", "score": 0.71},
    "group": {
        "member_count": 12,
        "activity_level": "high",
        "topic": "开黑",
        "topic_age_min": 3.0,
        "last_activity": "2026-09-23T12:00:00+00:00",
    },
    "participation": {
        "allow": True,
        "reason": "ok",
        "cooldown_remaining_sec": 0,
        "hourly_remaining": 5,
    },
}


class FakeGroupAdapter:
    """Records group calls so tests can assert exact intent."""

    def __init__(self, ctx=None, group_view=None, exc=None, delay=0.0):
        self.ctx = ctx
        self.group_view = group_view
        self.exc = exc
        self.delay = delay
        self.fetch_calls = []
        self.activities = []
        self.gates = []

    async def fetch_context(self, umo, persona_id=None):
        self.fetch_calls.append({"umo": umo, "persona_id": persona_id})
        return self.ctx

    async def record_group_activity(self, umo, *, member_id=None, topic=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        self.activities.append(
            {"umo": umo, "member_id": member_id, "topic": topic}
        )
        return {"applied": True, "isolated": False, "group": {}, "degraded": False}

    async def group_context(self, umo, *, member_id=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        self.gates.append({"umo": umo, "member_id": member_id})
        return self.group_view


class _GroupStar:
    """Minimal star surface for the real ``CompanionContextAdapter``."""

    def __init__(self, group_aware=True, group_view=None, activity_exc=None):
        self._group_aware = group_aware
        self._group_view = group_view or {}
        self._activity_exc = activity_exc
        self.activities = []
        self.gates = []

    async def get_contract_info(self):
        return {
            "api_version": 1,
            "capabilities": {"group_aware": self._group_aware},
        }

    async def get_proactive_context(self, umo, persona_id=None):
        return self._group_view

    async def record_group_activity(self, umo, **kwargs):
        if self._activity_exc:
            raise self._activity_exc
        self.activities.append({"umo": umo, **kwargs})
        return {"applied": True}

    async def get_group_context(self, umo, **kwargs):
        self.gates.append({"umo": umo, **kwargs})
        return self._group_view


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


class _FetchAdapter:
    def __init__(self, ctx):
        self.ctx = ctx

    async def fetch_context(self, umo, persona_id=None):
        return self.ctx


def _install_star(plugin, star, name="astrbot_plugin_tcompanion_core", activated=True):
    metadata = types.SimpleNamespace(activated=activated, star_cls=star)
    plugin.context.get_registered_star = lambda _name, _m=metadata: _m


def _enable(plugin, **overrides):
    plugin.config["companion_enabled"] = True
    plugin.config["private_whitelist"] = ["u1"]
    plugin.config["group_whitelist"] = ["g1"]
    for key, value in overrides.items():
        plugin.config[key] = value


def _mock_llm(plugin, captured, reply="今晚一起开黑吗？"):
    async def _provider_id(_umo):
        return "provider-1"

    async def _llm_generate(chat_provider_id, prompt):
        captured["prompt"] = prompt
        return _Completion(reply)

    plugin.context.get_current_chat_provider_id = _provider_id
    plugin.context.llm_generate = _llm_generate


def _event(umo=GROUP_UMO, user_id="u1", group_id="g1", text="今晚开黑"):
    message_obj = types.SimpleNamespace(
        message_id="m1",
        group_id=group_id,
        sender=types.SimpleNamespace(user_id=user_id),
    )
    return types.SimpleNamespace(
        message_obj=message_obj,
        unified_msg_origin=umo,
        message_str=text,
    )


def _group_session(now_ts=2_000_000_000.0):
    return {
        "session_key": "group:g1",
        "unified_msg_origin": GROUP_UMO,
        "last_interaction_at": 0.0,
        "next_check_at": 0.0,
        "cooldown_until": 0.0,
        "today_proactive_count": 0,
        "counter_date": "2026-01-01",
        "pending_human_reply": False,
        "no_reply_streak": 0,
        "period_counter_date": "2026-01-01",
        "period_proactive_count": {"morning": 0, "afternoon": 0, "evening": 0},
        "recent_proactive_texts": [],
        "mood": 100.0,
        "mood_updated_at": 0.0,
        "mood_low_streak": 0,
        "companion_cold_streak": 0,
        "companion_positive_seen": False,
        "companion_receipt_pending": None,
    }


# --------------------------------------------------------------------------- #
# sanitize / short label
# --------------------------------------------------------------------------- #


def test_sanitize_group_and_participation_blocks():
    ctx = sanitize_companion_context(
        {
            "api_version": 1,
            "group": {
                "member_count": 5,
                "activity_level": "HIGH",
                "topic": "  开黑  ",
                "topic_age_min": 3.5,
                "last_activity": "2026-09-23T12:00:00+00:00",
                "future_key": "ignored",
            },
            "participation": {
                "allow": False,
                "reason": "COOLDOWN",
                "cooldown_remaining_sec": 42,
                "hourly_remaining": 4,
                "future_key": 1,
            },
        }
    )
    assert ctx["group"]["member_count"] == 5
    assert ctx["group"]["activity_level"] == "high"
    assert ctx["group"]["topic"] == "开黑"
    assert ctx["group"]["topic_age_min"] == 3.5
    assert "future_key" not in ctx["group"]
    assert ctx["participation"] == {
        "allow": False,
        "reason": "cooldown",
        "cooldown_remaining_sec": 42,
        "hourly_remaining": 4,
    }


def test_sanitize_malformed_group_blocks_dropped():
    ctx = sanitize_companion_context(
        {
            "api_version": 1,
            "group": {"activity_level": "wild", "member_count": "x", "topic": ""},
            "participation": {"reason": "not-a-reason", "allow": "yes"},
        }
    )
    assert "group" not in ctx
    assert "participation" not in ctx


def test_short_group_topic_bounds_and_no_body():
    assert _short_group_topic("今晚开黑") == "今晚开黑"
    assert _short_group_topic("  ") == ""
    assert _short_group_topic("，。！？") == ""
    # 长消息不算短标签（只计数量，不上报正文）。
    assert _short_group_topic("很长的一句话" * 6) == ""
    clipped = _short_group_topic("啊" * 20)
    assert len(clipped) == 16
    assert clipped.endswith("…")


# --------------------------------------------------------------------------- #
# adapter capability gating (fail-closed)
# --------------------------------------------------------------------------- #


def test_adapter_group_context_requires_capability(plugin):
    star = _GroupStar(group_aware=False, group_view=GROUP_CONTEXT)
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.group_context(GROUP_UMO)) is None
    assert star.gates == []


def test_adapter_group_context_returns_sanitized_view(plugin):
    star = _GroupStar(group_aware=True, group_view=GROUP_CONTEXT)
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    view = asyncio.run(adapter.group_context(GROUP_UMO))
    assert view["group"]["activity_level"] == "high"
    assert view["participation"]["allow"] is True


def test_adapter_group_context_missing_method_degrades(plugin):
    star = types.SimpleNamespace(
        get_contract_info=lambda: None,
        get_proactive_context=lambda *a: None,
    )
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.group_context(GROUP_UMO)) is None


def test_adapter_record_group_activity_requires_capability(plugin):
    star = _GroupStar(group_aware=False)
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert (
        asyncio.run(
            adapter.record_group_activity(GROUP_UMO, member_id="u1", topic="开黑")
        )
        is None
    )
    assert star.activities == []


def test_adapter_record_group_activity_degrades_on_error(plugin):
    star = _GroupStar(group_aware=True, activity_exc=RuntimeError("boom"))
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.record_group_activity(GROUP_UMO)) is None


def test_fetch_context_strips_group_without_capability(plugin):
    star = _GroupStar(group_aware=False, group_view=GROUP_CONTEXT)
    _install_star(plugin, star)
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context(GROUP_UMO)
    )
    assert "group" not in ctx
    assert "participation" not in ctx


# --------------------------------------------------------------------------- #
# participation gate (fail-open)
# --------------------------------------------------------------------------- #


def test_group_participation_blocks_on_explicit_deny(plugin):
    _enable(plugin)
    adapter = FakeGroupAdapter(
        group_view={
            "group": {"activity_level": "high"},
            "participation": {"allow": False, "reason": "cooldown"},
        }
    )
    plugin._companion_adapter_override = adapter
    allow, view = asyncio.run(plugin._companion_group_participation(GROUP_UMO))
    assert allow is False
    assert view["participation"]["reason"] == "cooldown"
    assert adapter.gates == [{"umo": GROUP_UMO, "member_id": None}]


def test_group_participation_allows_when_gate_ok(plugin):
    _enable(plugin)
    adapter = FakeGroupAdapter(
        group_view={"participation": {"allow": True, "reason": "ok"}}
    )
    plugin._companion_adapter_override = adapter
    allow, _ = asyncio.run(plugin._companion_group_participation(GROUP_UMO))
    assert allow is True


def test_group_participation_fail_open_when_disabled(plugin):
    adapter = FakeGroupAdapter(group_view={"participation": {"allow": False}})
    plugin._companion_adapter_override = adapter
    # companion 未启用：不调用 adapter，直接放行（v2.10.2 行为）。
    assert asyncio.run(plugin._companion_group_participation(GROUP_UMO)) == (True, {})
    assert adapter.gates == []


def test_group_participation_fail_open_on_error_or_isolation(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = FakeGroupAdapter(exc=RuntimeError("boom"))
    assert asyncio.run(plugin._companion_group_participation(GROUP_UMO)) == (True, {})
    plugin._companion_adapter_override = FakeGroupAdapter(group_view={"isolated": True})
    assert asyncio.run(plugin._companion_group_participation(GROUP_UMO)) == (
        True,
        {"isolated": True},
    )
    # 旧 core 的 group_context 返回 None -> fail-open。
    plugin._companion_adapter_override = FakeGroupAdapter(group_view=None)
    assert asyncio.run(plugin._companion_group_participation(GROUP_UMO)) == (True, {})


# --------------------------------------------------------------------------- #
# inbound group activity reporting
# --------------------------------------------------------------------------- #


def test_inbound_group_message_records_activity(plugin):
    _enable(plugin)
    adapter = FakeGroupAdapter()
    plugin._companion_adapter_override = adapter
    asyncio.run(plugin._evt_on_all_message(_event(text="今晚开黑")))
    assert adapter.activities == [
        {"umo": GROUP_UMO, "member_id": "u1", "topic": "今晚开黑"}
    ]


def test_inbound_group_long_message_sends_counts_only(plugin):
    _enable(plugin)
    adapter = FakeGroupAdapter()
    plugin._companion_adapter_override = adapter
    long_text = "这是一句比较长的话" * 5
    asyncio.run(plugin._evt_on_all_message(_event(text=long_text)))
    assert len(adapter.activities) == 1
    assert adapter.activities[0]["topic"] is None
    assert long_text not in str(adapter.activities[0])


def test_inbound_group_activity_skipped_when_disabled(plugin):
    # companion 默认关闭：不上报、不报错。
    adapter = FakeGroupAdapter()
    plugin._companion_adapter_override = adapter
    asyncio.run(plugin._evt_on_all_message(_event(text="今晚开黑")))
    assert adapter.activities == []


def test_inbound_private_message_does_not_record_group_activity(plugin):
    _enable(plugin)
    adapter = FakeGroupAdapter()
    plugin._companion_adapter_override = adapter
    asyncio.run(
        plugin._evt_on_all_message(
            _event(umo=PRIVATE_UMO, user_id="u1", group_id="", text="在吗")
        )
    )
    assert adapter.activities == []


# --------------------------------------------------------------------------- #
# group prompt isolation + atmosphere
# --------------------------------------------------------------------------- #


def test_group_prompt_fields_are_suppressed(plugin):
    ctx = {
        "life_state": {"summary": "她刚下课，在食堂"},
        "relationship": {"stage": "熟悉", "affinity": 0.42},
        "motivation": {"reason": "乐队出新歌"},
    }
    fields = plugin._companion_prompt_fields(ctx, "group:g1")
    assert fields == {"life_state": "", "relationship": "", "motivation": ""}
    # 私聊行为不变。
    private = plugin._companion_prompt_fields(ctx, "private:u1")
    assert private["life_state"] == "她刚下课，在食堂"
    assert private["relationship"]


def test_group_atmosphere_text_from_group_block(plugin):
    text = plugin._companion_group_atmosphere_text(GROUP_CONTEXT)
    assert "活跃度热闹" in text
    assert "群成员约 12 人" in text
    assert "最近在聊「开黑」（约 3 分钟前）" in text
    assert plugin._companion_group_atmosphere_text({}) == ""


def test_generation_group_injects_atmosphere_not_private(plugin):
    plugin._companion_adapter_override = _FetchAdapter(GROUP_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(GROUP_UMO, "group:g1", 3600, {}))
    prompt = captured["prompt"]
    assert "【群氛围】" in prompt
    assert "活跃度热闹" in prompt
    # 不注入任何私聊陪伴字段。
    assert "【陪伴上下文】" not in prompt
    assert "她刚下课，在食堂" not in prompt
    assert "与对方关系" not in prompt
    assert "乐队出新歌" not in prompt


def test_generation_group_without_group_block_is_plain(plugin):
    ctx = {k: v for k, v in GROUP_CONTEXT.items() if k not in ("group", "participation")}
    plugin._companion_adapter_override = _FetchAdapter(ctx)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(GROUP_UMO, "group:g1", 3600, {}))
    prompt = captured["prompt"]
    assert "【群氛围】" not in prompt
    assert "【陪伴上下文】" not in prompt


def test_generation_private_keeps_companion_block(plugin):
    plugin._companion_adapter_override = _FetchAdapter(GROUP_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(PRIVATE_UMO, "private:u1", 3600, {}))
    prompt = captured["prompt"]
    assert "【陪伴上下文】" in prompt
    assert "她刚下课，在食堂" in prompt
    assert "【群氛围】" not in prompt


def test_generation_group_uses_atmosphere_placeholder(plugin):
    plugin._companion_adapter_override = _FetchAdapter(GROUP_CONTEXT)
    _enable(plugin)
    plugin.config["proactive_prompt_template"] = "人格：{persona}\n群：{group_atmosphere}\n"
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(GROUP_UMO, "group:g1", 3600, {}))
    prompt = captured["prompt"]
    assert "群：活跃度热闹" in prompt
    assert "【群氛围】" not in prompt


# --------------------------------------------------------------------------- #
# decision-engine integration
# --------------------------------------------------------------------------- #


def _drive_decision(plugin, allow, view, patch_gate=True):
    now_ts = 2_000_000_000.0
    now = _dt.datetime.fromtimestamp(now_ts)
    plugin._trigger_probability = lambda idle_sec, now: 1.0
    plugin._in_sleep_window = lambda now: False
    plugin._persona_state_enabled = lambda: False
    if patch_gate:
        plugin._companion_group_participation = _async_result((allow, view))
    session = _group_session(now_ts)
    return asyncio.run(
        plugin._decision_engine("group:g1", session, now, now_ts)
    ), session


def _async_result(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def test_decision_gate_blocks_group_proactive(plugin):
    _enable(plugin)
    decision, session = _drive_decision(
        plugin,
        allow=False,
        view={"participation": {"allow": False, "reason": "hourly_limit"}},
    )
    assert decision["allow"] is False
    assert "companion_group_hourly_limit" in decision["reason_codes"]
    # 命中闸门时推进 next_check（不刷屏）。
    assert session["next_check_at"] > 2_000_000_000.0


def test_decision_gate_allows_group_proactive(plugin):
    _enable(plugin)
    decision, _ = _drive_decision(
        plugin,
        allow=True,
        view={"participation": {"allow": True, "reason": "ok"}},
    )
    assert decision["allow"] is True


def test_decision_gate_absent_companion_is_fail_open(plugin):
    # companion 未启用：真实闸门直接放行，按 v2.10.2 行为发送。
    plugin.config["group_whitelist"] = ["g1"]
    decision, _ = _drive_decision(plugin, allow=True, view={}, patch_gate=False)
    assert decision["allow"] is True
