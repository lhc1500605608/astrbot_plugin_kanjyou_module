"""Phase 2-A 情绪事件探测 + Phase 2-C expression 消费测试 (TMEAAA-449)。

覆盖：零 LLM 四类关键词探测与优先级、主动回执状态机两类的真互斥/单条只结算一次、
capabilities(dict) 协商与静默降级、群聊隔离与硬抑制、expression×persona_state
合并（plan §4.1）与占位符/安全追加。
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
)
from astrbot_plugin_kanjyou_module.units.unit_emotion_events import (  # noqa: E402
    GROUP_WARMTH_CAP,
)

EMOTION_CONTEXT = {
    "api_version": 1,
    "life_state": {"summary": "她刚下课，在食堂"},
    "emotion_state": {
        "state": "回避",
        "valence": -0.4,
        "last_event": "ignored_proactive",
    },
    "expression": {
        "mode": "回避",
        "style_hints": {
            "tone": "简短克制",
            "warmth": 0.25,
            "length_bias": -0.5,
            "proactive_bias": -0.6,
        },
        "reason": "连续 2 次主动未回应，档位→回避",
    },
}


class _RecordingAdapter:
    def __init__(self, exc=None, delay=0.0):
        self.calls = []
        self.exc = exc
        self.delay = delay

    async def record_emotion_event(
        self, umo, *, event_type, reason="", dedupe_key=None
    ):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        self.calls.append(
            {
                "umo": umo,
                "event_type": event_type,
                "reason": reason,
                "dedupe_key": dedupe_key,
            }
        )
        return {"applied": True, "duplicate": False}


class _FetchAdapter:
    def __init__(self, ctx):
        self.ctx = ctx
        self.fetch_calls = []

    async def fetch_context(self, umo, persona_id=None):
        self.fetch_calls.append({"umo": umo, "persona_id": persona_id})
        return self.ctx


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


class _ContractStar:
    def __init__(self, capabilities, version=1, raw=None):
        self._capabilities = capabilities
        self._version = version
        self._raw = raw or {}
        self.recorded = []

    async def get_contract_info(self):
        return {"api_version": self._version, "capabilities": self._capabilities}

    async def get_proactive_context(self, umo, persona_id=None):
        return self._raw

    async def record_emotion_event(
        self, umo, *, event_type, reason="", dedupe_key=None
    ):
        self.recorded.append(
            {
                "umo": umo,
                "event_type": event_type,
                "reason": reason,
                "dedupe_key": dedupe_key,
            }
        )
        return {"applied": True, "duplicate": False}


def _install_star(plugin, star, name="astrbot_plugin_tcompanion_core", activated=True):
    metadata = types.SimpleNamespace(activated=activated, star_cls=star)
    plugin.context.get_registered_star = lambda _name, _m=metadata: _m


def _enable(plugin, **overrides):
    plugin.config["companion_enabled"] = True
    plugin.config["private_whitelist"] = ["u1"]
    plugin.config["group_whitelist"] = ["g1"]
    for key, value in overrides.items():
        plugin.config[key] = value


def _event(message_id="", umo="aiocqhttp:private:u1", user_id="u1", group_id=""):
    message_obj = types.SimpleNamespace(
        message_id=message_id,
        group_id=group_id,
        sender=types.SimpleNamespace(user_id=user_id),
    )
    return types.SimpleNamespace(
        message_obj=message_obj,
        unified_msg_origin=umo,
        message_str="",
    )


def _session(**overrides):
    session = {
        "session_key": "private:u1",
        "unified_msg_origin": "aiocqhttp:private:u1",
        "mood": 70.0,
        "companion_cold_streak": 0,
        "companion_positive_seen": False,
        "companion_receipt_pending": None,
    }
    session.update(overrides)
    return session


def _mock_llm(plugin, captured, reply="嗯，我在。"):
    async def _provider_id(_umo):
        return "provider-1"

    async def _llm_generate(chat_provider_id, prompt):
        captured["prompt"] = prompt
        return _Completion(reply)

    plugin.context.get_current_chat_provider_id = _provider_id
    plugin.context.llm_generate = _llm_generate


# --------------------------------------------------------------------------- #
# capability negotiation / degradation
# --------------------------------------------------------------------------- #


def test_adapter_has_capability_requires_dict(plugin):
    _install_star(plugin, _ContractStar({"emotion": True, "expression": True}))
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.has_capability("emotion")) is True
    assert asyncio.run(adapter.has_capability("expression")) is True
    assert asyncio.run(adapter.has_capability("nope")) is False


def test_adapter_array_capabilities_degrade_failclosed(plugin):
    # 历史遗留的错误形态：数组无法协商 -> 不调用新增方法。
    star = _ContractStar(["life_state"])
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.capabilities()) == {}
    assert (
        asyncio.run(
            adapter.record_emotion_event("umo", event_type="gratitude")
        )
        is None
    )
    assert star.recorded == []


def test_adapter_missing_emotion_capability_degrades(plugin):
    star = _ContractStar({"life_state": True, "expression": True})
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert (
        asyncio.run(
            adapter.record_emotion_event("umo", event_type="valued_reply")
        )
        is None
    )
    assert star.recorded == []


def test_adapter_records_when_capable(plugin):
    star = _ContractStar({"life_state": True, "emotion": True, "expression": True})
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    result = asyncio.run(
        adapter.record_emotion_event(
            "umo", event_type="gratitude", reason="k", dedupe_key="msg:1"
        )
    )
    assert result == {"applied": True, "duplicate": False}
    assert star.recorded[0]["dedupe_key"] == "msg:1"


def test_fetch_context_strips_new_fields_without_capability(plugin):
    _install_star(plugin, _ContractStar({"life_state": True}, raw=EMOTION_CONTEXT))
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")
    )
    assert ctx["life_state"]["summary"] == "她刚下课，在食堂"
    assert "emotion_state" not in ctx
    assert "expression" not in ctx


def test_fetch_context_keeps_new_fields_with_capability(plugin):
    _install_star(
        plugin,
        _ContractStar(
            {"life_state": True, "emotion": True, "expression": True},
            raw=EMOTION_CONTEXT,
        ),
    )
    ctx = asyncio.run(
        CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context("umo")
    )
    assert ctx["emotion_state"]["state"] == "回避"
    assert ctx["emotion_state"]["valence"] == -0.4
    assert ctx["expression"]["mode"] == "回避"
    assert ctx["expression"]["style_hints"]["warmth"] == 0.25


def test_record_companion_emotion_event_degrades_without_companion(plugin):
    adapter = _RecordingAdapter()
    plugin._companion_adapter_override = adapter
    # companion_enabled 默认 False -> 完全不调用。
    assert (
        asyncio.run(
            plugin._record_companion_emotion_event("umo", "gratitude")
        )
        is False
    )
    assert adapter.calls == []


def test_record_companion_emotion_event_exception_and_timeout(plugin):
    _enable(plugin)
    exc_adapter = _RecordingAdapter(exc=RuntimeError("boom"))
    plugin._companion_adapter_override = exc_adapter
    assert (
        asyncio.run(plugin._record_companion_emotion_event("umo", "gratitude"))
        is False
    )

    plugin.config["companion_timeout_sec"] = 0.2
    slow = _RecordingAdapter(delay=1.0)
    plugin._companion_adapter_override = slow
    assert (
        asyncio.run(plugin._record_companion_emotion_event("umo", "gratitude"))
        is False
    )


def test_record_companion_emotion_event_ok(plugin):
    _enable(plugin)
    adapter = _RecordingAdapter()
    plugin._companion_adapter_override = adapter
    assert (
        asyncio.run(
            plugin._record_companion_emotion_event(
                "umo", "valued_reply", dedupe_key="proactive:1"
            )
        )
        is True
    )
    assert adapter.calls[0]["event_type"] == "valued_reply"
    assert adapter.calls[0]["dedupe_key"] == "proactive:1"


# --------------------------------------------------------------------------- #
# keyword detection
# --------------------------------------------------------------------------- #


def test_keyword_detection_basic_types(plugin):
    session = _session()
    assert plugin._detect_keyword_event("谢谢你今天陪我", session) == "gratitude"
    assert plugin._detect_keyword_event("你误会我了", session) == "misunderstood"
    assert plugin._detect_keyword_event("早点休息别熬夜", session) == "sudden_warmth"
    assert plugin._detect_keyword_event("今天天气不错", session) is None


def test_keyword_priority_misunderstood_over_gratitude(plugin):
    session = _session()
    # 同时命中感谢与不满 -> 取 misunderstood（优先级更高）。
    assert plugin._detect_keyword_event("谢谢但是别烦我了", session) == "misunderstood"


def test_keyword_ascii_case_insensitive(plugin):
    session = _session()
    assert plugin._detect_keyword_event("THANKS a lot", session) == "gratitude"


def test_cold_shoulder_requires_streak_and_prior_positive(plugin):
    session = _session(companion_positive_seen=True)
    assert plugin._detect_keyword_event("嗯", session) is None
    assert plugin._detect_keyword_event("哦", session) is None
    assert plugin._detect_keyword_event("好", session) == "cold_shoulder"
    # 触发后计数重置，不会对后续每条消息重复触发。
    assert session["companion_cold_streak"] == 0


def test_cold_shoulder_blocked_without_prior_positive(plugin):
    session = _session(companion_positive_seen=False)
    for terse in ("嗯", "哦", "好"):
        assert plugin._detect_keyword_event(terse, session) is None


def test_keyword_hit_resets_cold_streak(plugin):
    session = _session(companion_positive_seen=True)
    plugin._detect_keyword_event("嗯", session)
    plugin._detect_keyword_event("哦", session)
    assert session["companion_cold_streak"] == 2
    plugin._detect_keyword_event("谢谢你", session)
    assert session["companion_cold_streak"] == 0


def test_terse_detection_length_bound(plugin):
    assert plugin._is_terse_message("嗯") is True
    assert plugin._is_terse_message("？") is True
    assert plugin._is_terse_message("嗯嗯嗯") is False
    assert plugin._is_terse_message("好") is True


# --------------------------------------------------------------------------- #
# inbound settlement: single event / dedupe key / group isolation
# --------------------------------------------------------------------------- #


def test_inbound_inside_reply_window_only_books_valued_reply(plugin):
    _enable(plugin)
    send_ts = 1000.0
    session = _session(
        companion_receipt_pending={"send_ts": send_ts, "umo": "umo"}
    )
    # 「谢谢你」同时命中 gratitude，但落在回复窗口内 -> 只结算 valued_reply。
    decided = plugin._collect_inbound_emotion_event(
        "private:u1", session, "谢谢你", _event("m1"), "umo", send_ts + 60
    )
    assert decided == (
        "valued_reply",
        f"proactive:{int(send_ts)}",
        "kanjyou:proactive_reply",
    )
    assert "companion_receipt_pending" not in session


def test_inbound_outside_window_uses_keyword(plugin):
    _enable(plugin)
    send_ts = 1000.0
    session = _session(
        companion_receipt_pending={"send_ts": send_ts, "umo": "umo"}
    )
    decided = plugin._collect_inbound_emotion_event(
        "private:u1", session, "谢谢你", _event("m2"), "umo", send_ts + 99999
    )
    assert decided == ("gratitude", "msg:m2", "kanjyou:keyword")


def test_inbound_dedupe_key_fallback_without_message_id(plugin):
    _enable(plugin)
    session = _session()
    decided = plugin._collect_inbound_emotion_event(
        "private:u1", session, "谢谢你", _event(""), "aiocqhttp:private:u1", 1234.9
    )
    assert decided[1] == "msg:aiocqhttp:private:u1:1234"


def test_inbound_group_is_isolated(plugin):
    _enable(plugin)
    session = _session(session_key="group:g1")
    decided = plugin._collect_inbound_emotion_event(
        "group:g1", session, "谢谢你", _event("m3", group_id="g1"), "umo", 1.0
    )
    assert decided is None


def test_inbound_requires_whitelist(plugin):
    _enable(plugin)
    plugin.config["private_whitelist"] = []
    session = _session()
    decided = plugin._collect_inbound_emotion_event(
        "private:u9", session, "谢谢你", _event("m4"), "umo", 1.0
    )
    assert decided is None


def test_disabled_emotion_events_short_circuit(plugin):
    _enable(plugin, emotion_event_enabled=False)
    session = _session()
    assert (
        plugin._collect_inbound_emotion_event(
            "private:u1", session, "谢谢你", _event("m5"), "umo", 1.0
        )
        is None
    )


# --------------------------------------------------------------------------- #
# proactive receipt state machine
# --------------------------------------------------------------------------- #


def test_proactive_receipts_share_one_dedupe_key(plugin):
    assert plugin._proactive_receipt_key(1700.9) == "proactive:1700"


def test_begin_and_settle_ignored_receipt(plugin):
    _enable(plugin, emotion_ignore_window_sec=3600)
    adapter = _RecordingAdapter()
    plugin._companion_adapter_override = adapter
    session = _session()
    now_ts = 5000.0
    asyncio.run(plugin._begin_proactive_receipt(session, "umo", now_ts))
    assert session["companion_receipt_pending"]["send_ts"] == now_ts
    # 未过期：不结算。
    assert asyncio.run(plugin._settle_expired_receipt(session, now_ts + 100)) is False
    # 过期：结算 ignored_proactive，键与 valued_reply 共用。
    assert asyncio.run(plugin._settle_expired_receipt(session, now_ts + 4000)) is True
    assert "companion_receipt_pending" not in session
    assert adapter.calls[-1]["event_type"] == "ignored_proactive"
    assert adapter.calls[-1]["dedupe_key"] == f"proactive:{int(now_ts)}"


def test_group_receipt_is_never_recorded(plugin):
    _enable(plugin)
    adapter = _RecordingAdapter()
    plugin._companion_adapter_override = adapter
    session = _session(session_key="group:g1")
    asyncio.run(plugin._begin_proactive_receipt(session, "aiocqhttp:group:g1", 1000.0))
    assert not session.get("companion_receipt_pending")
    assert (
        asyncio.run(plugin._settle_expired_receipt(session, 99999.0)) is False
    )
    assert adapter.calls == []


def test_superseding_proactive_settles_old_as_ignored(plugin):
    _enable(plugin, emotion_ignore_window_sec=3600)
    adapter = _RecordingAdapter()
    plugin._companion_adapter_override = adapter
    session = _session()
    asyncio.run(plugin._begin_proactive_receipt(session, "umo", 1000.0))
    asyncio.run(plugin._begin_proactive_receipt(session, "umo", 2000.0))
    assert adapter.calls[0]["event_type"] == "ignored_proactive"
    assert adapter.calls[0]["dedupe_key"] == "proactive:1000"
    assert session["companion_receipt_pending"]["send_ts"] == 2000.0


# --------------------------------------------------------------------------- #
# expression consumption + persona merge (plan §4.1)
# --------------------------------------------------------------------------- #


def test_persona_style_adjust_maps_length_and_mood(plugin):
    _enable(plugin)
    warmth_ps, length_bias_ps, suppress = plugin._persona_style_adjust(
        "日常", _session(mood=100.0)
    )
    assert warmth_ps == 0.10
    assert length_bias_ps == 0.0
    assert suppress is False

    warmth_ps, length_bias_ps, suppress = plugin._persona_style_adjust(
        "低落", _session(mood=0.0)
    )
    assert warmth_ps == -0.10
    assert length_bias_ps == -0.10
    assert suppress is True


def test_expression_merge_keeps_mode_and_bounds_total(plugin):
    _enable(plugin)
    expression = plugin._companion_expression(
        {"expression": EMOTION_CONTEXT["expression"]}, "private:u1"
    )
    merged = plugin._expression_style_merge(
        expression, "低落", _session(mood=100.0), "private:u1"
    )
    assert merged["mode"] == "回避"
    assert merged["warmth"] == 0.35
    assert merged["length_bias"] == -0.6
    # suppress_proactive 只降不升：min(-0.6, -0.5) = -0.6
    assert merged["proactive_bias"] == -0.6
    # persona 通道各自 ≤ ±0.10。
    warmth_ps, length_bias_ps, _ = plugin._persona_style_adjust(
        "低落", _session(mood=100.0)
    )
    assert abs(warmth_ps) <= 0.10
    assert abs(length_bias_ps) <= 0.10


def test_expression_group_suppression_and_warmth_cap(plugin):
    _enable(plugin)
    expression = plugin._companion_expression(
        {
            "expression": {
                "mode": "爱意",
                "style_hints": {
                    "tone": "深情温柔",
                    "warmth": 0.95,
                    "length_bias": 0.05,
                    "proactive_bias": 0.20,
                },
            }
        },
        "group:g1",
    )
    assert expression["mode"] == "放松"
    assert expression["group_suppressed"] is True
    merged = plugin._expression_style_merge(
        expression, "日常", _session(mood=100.0), "group:g1"
    )
    assert merged["mode"] == "放松"
    assert merged["warmth"] <= GROUP_WARMTH_CAP


def test_expression_unknown_mode_degrades(plugin):
    _enable(plugin)
    assert (
        plugin._companion_expression(
            {"expression": {"mode": "未知档"}}, "private:u1"
        )
        is None
    )


def test_emotion_state_text_suppressed_in_group(plugin):
    ctx = {"emotion_state": EMOTION_CONTEXT["emotion_state"]}
    assert plugin._companion_emotion_state_text(ctx, "private:u1")
    assert plugin._companion_emotion_state_text(ctx, "group:g1") == ""


def test_expression_length_range_shrinks_on_negative_bias(plugin):
    _enable(plugin)
    assert plugin._expression_length_range({"length_bias": 0.0}, "20-60") == "20-60"
    shortened = plugin._expression_length_range({"length_bias": -0.5}, "20-60")
    low, high = (int(x) for x in shortened.split("-"))
    assert high < 60


def test_expression_proactive_soft_gate_is_one_shot(plugin):
    session = {"companion_expression_proactive_bias": -0.6}
    assert plugin._companion_expression_proactive_soft_gate(session) == -0.6
    assert plugin._companion_expression_proactive_soft_gate(session) is None
    assert plugin._companion_expression_proactive_soft_gate({}) is None


# --------------------------------------------------------------------------- #
# generation integration
# --------------------------------------------------------------------------- #


def test_generation_injects_emotion_and_expression(plugin):
    plugin._companion_adapter_override = _FetchAdapter(EMOTION_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    text = asyncio.run(
        plugin._generate_proactive_text("aiocqhttp:private:u1", "private:u1", 3600, session)
    )
    assert text
    prompt = captured["prompt"]
    assert "【情绪与表达】" in prompt
    assert "当前情绪：回避" in prompt
    assert "表达档位：回避" in prompt
    # proactive_bias 存入 session，供下一轮决策软闸消费。
    assert session["companion_expression_proactive_bias"] == -0.6


def test_generation_uses_placeholders_when_template_has_them(plugin):
    plugin._companion_adapter_override = _FetchAdapter(EMOTION_CONTEXT)
    _enable(plugin)
    plugin.config["proactive_prompt_template"] = (
        "情绪：{emotion_state}\n档位：{expression_mode}\n"
    )
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(
        plugin._generate_proactive_text(
            "aiocqhttp:private:u1", "private:u1", 3600, _session()
        )
    )
    prompt = captured["prompt"]
    assert "情绪：回避" in prompt
    assert "档位：回避" in prompt
    assert "【情绪与表达】" not in prompt


def test_generation_group_hard_suppresses_intimate_mode(plugin):
    plugin._companion_adapter_override = _FetchAdapter(EMOTION_CONTEXT)
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(
        plugin._generate_proactive_text("aiocqhttp:group:g1", "group:g1", 3600, _session())
    )
    prompt = captured["prompt"]
    assert "表达档位：放松" in prompt
    assert "表达档位：回避" not in prompt
    # 群聊不注入私聊情绪（persona_state 的「当前情绪状态」不属于注入项）。
    assert "当前情绪：" not in prompt


def test_generation_without_expression_is_v240_equivalent(plugin):
    plugin._companion_adapter_override = _FetchAdapter({"life_state": {"summary": "x"}})
    _enable(plugin)
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    asyncio.run(
        plugin._generate_proactive_text("aiocqhttp:private:u1", "private:u1", 3600, session)
    )
    prompt = captured["prompt"]
    assert "【情绪与表达】" not in prompt
    assert "companion_expression_proactive_bias" not in session
