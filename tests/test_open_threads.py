"""Phase 2-B 未完话题提取 + 自然续接测试 (TMEAAA-499)。

覆盖：提取优先级与单条上限、短标签化（不存原文）、完成闭闸、assistant 侧 topic、
消费闸门（冷却/沉淀/计数/配额/情绪）、群聊隔离、core 缺失 fail-closed、回执幂等、
generation 注入（占位符/安全追加）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.units.unit_companion import (  # noqa: E402
    CompanionContextAdapter,
)
from astrbot_plugin_kanjyou_module.units.unit_open_threads import (  # noqa: E402
    OPEN_THREAD_FOLLOWUP_MAX,
    OPEN_THREAD_LABEL_MAX,
    sanitize_open_thread_label,
)

UMO = "aiocqhttp:private:u1"
# Fixed "now" well past the 2020 sample last_seen so min-age gates pass.
NOW = 2_000_000_000.0


class _OpenThreadAdapter:
    """Records every open-thread call so tests can assert exact intent."""

    def __init__(self, threads=None, exc=None, delay=0.0):
        self.recorded = []
        self.closed = []
        self.followed = []
        self.threads = list(threads or [])
        self.exc = exc
        self.delay = delay

    async def record_open_thread(
        self, umo, *, label, kind, reason="", dedupe_key=None, confidence=1.0, source=""
    ):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        self.recorded.append(
            {
                "umo": umo,
                "label": label,
                "kind": kind,
                "reason": reason,
                "confidence": confidence,
                "source": source,
            }
        )
        return {"applied": True, "thread_id": "thread:x"}

    async def open_threads(self, umo, limit=3):
        if self.exc:
            raise self.exc
        return [dict(row) for row in self.threads][:limit]

    async def close_open_thread(self, umo, thread_id, reason=""):
        if self.exc:
            raise self.exc
        self.closed.append({"umo": umo, "thread_id": thread_id, "reason": reason})
        return {"closed": True}

    async def mark_thread_followup(self, umo, thread_id):
        if self.exc:
            raise self.exc
        self.followed.append({"umo": umo, "thread_id": thread_id})
        return {"updated": True}


class _FetchAdapter:
    def __init__(self, ctx):
        self.ctx = ctx

    async def fetch_context(self, umo, persona_id=None):
        return self.ctx


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


class _ContractStar:
    """Minimal star surface for the real CompanionContextAdapter."""

    def __init__(self, capabilities, version=1, threads=None, raw=None):
        self._capabilities = capabilities
        self._version = version
        self._threads = threads or []
        self._raw = raw or {}
        self.recorded = []
        self.closed = []
        self.followed = []

    async def get_contract_info(self):
        return {"api_version": self._version, "capabilities": self._capabilities}

    async def get_proactive_context(self, umo, persona_id=None):
        return self._raw

    async def record_open_thread(self, umo, **kwargs):
        self.recorded.append({"umo": umo, **kwargs})
        return {"applied": True}

    async def get_open_threads(self, umo, limit=3):
        return list(self._threads)[:limit]

    async def close_open_thread(self, umo, thread_id, reason=""):
        self.closed.append({"umo": umo, "thread_id": thread_id, "reason": reason})
        return {"closed": True}

    async def mark_thread_followup(self, umo, thread_id):
        self.followed.append({"umo": umo, "thread_id": thread_id})
        return {"updated": True}


def _install_star(plugin, star, name="astrbot_plugin_tcompanion_core", activated=True):
    metadata = types.SimpleNamespace(activated=activated, star_cls=star)
    plugin.context.get_registered_star = lambda _name, _m=metadata: _m


def _enable(plugin, **overrides):
    plugin.config["companion_enabled"] = True
    plugin.config["private_whitelist"] = ["u1"]
    plugin.config["group_whitelist"] = ["g1"]
    for key, value in overrides.items():
        plugin.config[key] = value


def _session(**overrides):
    session = {
        "session_key": "private:u1",
        "unified_msg_origin": UMO,
        "companion_open_thread_cooldown": {},
    }
    session.update(overrides)
    return session


def _detail(**overrides):
    row = {
        "thread_id": "thread:a",
        "label": "把方案发你",
        "kind": "commitment",
        "status": "open",
        "confidence": 0.8,
        "followup_count": 0,
        "last_seen": "2020-01-01T00:00:00",
    }
    row.update(overrides)
    return row


def _ctx(**overrides):
    ctx = {
        "open_thread_details": [_detail()],
        "quota": {"allow": True},
        "unanswered_streak": 0,
        "expression": {"mode": "放松", "style_hints": {"proactive_bias": 0.0}},
    }
    ctx.update(overrides)
    return ctx


def _event(umo=UMO, user_id="u1", group_id="", text=""):
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


def _mock_llm(plugin, captured, reply="嗯，我在。"):
    async def _provider_id(_umo):
        return "provider-1"

    async def _llm_generate(chat_provider_id, prompt):
        captured["prompt"] = prompt
        return _Completion(reply)

    plugin.context.get_current_chat_provider_id = _provider_id
    plugin.context.llm_generate = _llm_generate


# --------------------------------------------------------------------------- #
# extraction / priority / single-item cap
# --------------------------------------------------------------------------- #


def test_extract_priority_commitment_over_plan_and_question(plugin):
    item = plugin._extract_inbound_open_thread(
        "我明天把方案发你？顺便打算再整理一版"
    )
    assert item["kind"] == "commitment"
    assert item["confidence"] == 0.8
    assert "明天" in item["label"]


def test_extract_plan_when_no_commitment(plugin):
    item = plugin._extract_inbound_open_thread("我打算周末去爬山")
    assert item["kind"] == "plan"
    assert item["confidence"] == 0.8


def test_extract_pending_question(plugin):
    item = plugin._extract_inbound_open_thread("这个报错到底怎么解决呢？")
    assert item["kind"] == "pending_question"
    assert item["confidence"] == 0.6


def test_extract_at_most_one_and_no_raw_text(plugin):
    long_text = "我下次一定" + "很长的内容" * 30
    item = plugin._extract_inbound_open_thread(long_text)
    assert item is not None
    assert len(item["label"]) <= OPEN_THREAD_LABEL_MAX
    assert long_text not in item["label"]


def test_sanitize_label_collapses_and_truncates():
    assert sanitize_open_thread_label("  回头   再说。 ") == "回头 再说"
    assert len(sanitize_open_thread_label("啊" * 100)) <= OPEN_THREAD_LABEL_MAX
    assert sanitize_open_thread_label("，。！？ ") == ""


# --------------------------------------------------------------------------- #
# completion close gate
# --------------------------------------------------------------------------- #


def test_completion_collects_close_action(plugin):
    _enable(plugin)
    assert plugin._collect_inbound_open_thread("private:u1", "发你了", UMO) == {
        "action": "close"
    }


def test_apply_completion_closes_newest_open_thread(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter(
        threads=[
            {"thread_id": "thread:new", "status": "open"},
            {"thread_id": "thread:old", "status": "open"},
        ]
    )
    plugin._companion_adapter_override = adapter
    ok = asyncio.run(plugin._apply_inbound_open_thread(UMO, {"action": "close"}))
    assert ok is True
    assert adapter.closed == [
        {"umo": UMO, "thread_id": "thread:new", "reason": "answered"}
    ]


def test_completion_without_open_thread_is_noop(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter(threads=[])
    plugin._companion_adapter_override = adapter
    assert asyncio.run(plugin._apply_inbound_open_thread(UMO, {"action": "close"})) is False
    assert adapter.closed == []


# --------------------------------------------------------------------------- #
# inbound hook integration
# --------------------------------------------------------------------------- #


def test_inbound_hook_records_commitment(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter()
    plugin._companion_adapter_override = adapter
    asyncio.run(plugin._evt_on_all_message(_event(text="我明天把方案发你")))
    assert len(adapter.recorded) == 1
    assert adapter.recorded[0]["kind"] == "commitment"
    assert adapter.recorded[0]["source"] == "kanjyou:rule"


def test_inbound_hook_group_isolated(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter()
    plugin._companion_adapter_override = adapter
    asyncio.run(
        plugin._evt_on_all_message(
            _event(umo="aiocqhttp:group:g1", user_id="u1", group_id="g1", text="我明天发你")
        )
    )
    assert adapter.recorded == []


def test_group_scope_is_isolated(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter(threads=[{"thread_id": "thread:g", "status": "open"}])
    plugin._companion_adapter_override = adapter
    assert plugin._collect_inbound_open_thread("group:g1", "发你了", "aiocqhttp:group:g1") is None
    assert plugin._select_open_thread_followup(_ctx(), "group:g1", _session(), 0.0) is None
    assert adapter.recorded == [] and adapter.closed == []


def test_non_whitelisted_private_is_ignored(plugin):
    _enable(plugin)
    assert (
        plugin._collect_inbound_open_thread("private:someone", "我明天发你", UMO) is None
    )


# --------------------------------------------------------------------------- #
# fail-closed
# --------------------------------------------------------------------------- #


def test_missing_companion_disables_everything(plugin):
    adapter = _OpenThreadAdapter()
    plugin._companion_adapter_override = adapter
    # companion_enabled default False.
    assert plugin._collect_inbound_open_thread("private:u1", "我明天发你", UMO) is None
    assert plugin._select_open_thread_followup(_ctx(), "private:u1", _session(), 0.0) is None
    assert (
        asyncio.run(plugin._record_companion_open_thread(
            UMO, kind="commitment", label="x", confidence=0.8
        ))
        is False
    )
    assert adapter.recorded == []


def test_record_exception_and_timeout_degrade(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _OpenThreadAdapter(exc=RuntimeError("boom"))
    assert (
        asyncio.run(plugin._record_companion_open_thread(
            UMO, kind="commitment", label="x", confidence=0.8
        ))
        is False
    )
    plugin._companion_adapter_override = _OpenThreadAdapter(delay=2.0)
    plugin.config["companion_timeout_sec"] = 0.2
    assert (
        asyncio.run(plugin._record_companion_open_thread(
            UMO, kind="commitment", label="x", confidence=0.8
        ))
        is False
    )


def test_adapter_requires_capability(plugin):
    star = _ContractStar({"life_state": True})
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert (
        asyncio.run(adapter.record_open_thread(UMO, label="x", kind="commitment"))
        is None
    )
    assert asyncio.run(adapter.open_threads(UMO)) == []
    assert star.recorded == []


def test_adapter_calls_when_capable(plugin):
    star = _ContractStar(
        {"life_state": True, "open_threads_followup": True},
        threads=[{"thread_id": "thread:a", "status": "open"}],
    )
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    assert asyncio.run(adapter.record_open_thread(UMO, label="x", kind="commitment"))
    assert asyncio.run(adapter.open_threads(UMO)) == [
        {"thread_id": "thread:a", "status": "open"}
    ]
    assert asyncio.run(adapter.close_open_thread(UMO, "thread:a", reason="answered"))
    assert asyncio.run(adapter.mark_thread_followup(UMO, "thread:a"))
    assert star.closed[0]["thread_id"] == "thread:a"
    assert star.followed[0]["thread_id"] == "thread:a"


def test_fetch_context_strips_details_without_capability(plugin):
    star = _ContractStar({"life_state": True}, raw=_ctx())
    _install_star(plugin, star)
    ctx = asyncio.run(CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context(UMO))
    assert "open_thread_details" not in ctx


def test_fetch_context_keeps_details_with_capability(plugin):
    star = _ContractStar(
        {"life_state": True, "open_threads_followup": True}, raw=_ctx()
    )
    _install_star(plugin, star)
    ctx = asyncio.run(CompanionContextAdapter(plugin, timeout_sec=0.5).fetch_context(UMO))
    assert ctx["open_thread_details"][0]["label"] == "把方案发你"
    assert ctx["open_thread_details"][0]["confidence"] == 0.8


# --------------------------------------------------------------------------- #
# consumption gates
# --------------------------------------------------------------------------- #


def test_select_returns_candidate_when_all_gates_pass(plugin):
    _enable(plugin)
    selected = plugin._select_open_thread_followup(_ctx(), "private:u1", _session(), NOW)
    assert selected == {
        "thread_id": "thread:a",
        "label": "把方案发你",
        "kind": "commitment",
    }


def test_select_skips_low_confidence_and_stale(plugin):
    _enable(plugin)
    ctx = _ctx(
        open_thread_details=[
            _detail(status="stale"),
            _detail(thread_id="thread:b", confidence=0.5),
        ]
    )
    assert plugin._select_open_thread_followup(ctx, "private:u1", _session(), NOW) is None


def test_select_respects_min_age(plugin):
    _enable(plugin)
    # last_seen very recent relative to now_ts -> age < 6h.
    recent = "2026-09-21T10:00:00"
    now = _dt.datetime.fromisoformat(recent).timestamp() + 60
    ctx = _ctx(open_thread_details=[_detail(last_seen=recent)])
    assert plugin._select_open_thread_followup(ctx, "private:u1", _session(), now) is None


def test_select_respects_cooldown_and_resets(plugin):
    _enable(plugin)
    session = _session(companion_open_thread_cooldown={"thread:a": NOW - 3600})
    assert plugin._select_open_thread_followup(_ctx(), "private:u1", session, NOW) is None
    session["companion_open_thread_cooldown"] = {"thread:a": NOW - 25 * 3600}
    assert plugin._select_open_thread_followup(_ctx(), "private:u1", session, NOW) is not None


def test_select_respects_followup_count_cap(plugin):
    _enable(plugin)
    ctx = _ctx(
        open_thread_details=[_detail(followup_count=OPEN_THREAD_FOLLOWUP_MAX)]
    )
    assert plugin._select_open_thread_followup(ctx, "private:u1", _session(), NOW) is None


def test_select_respects_quota_streak_and_bias(plugin):
    _enable(plugin)
    assert (
        plugin._select_open_thread_followup(
            _ctx(quota={"allow": False}), "private:u1", _session(), NOW
        )
        is None
    )
    assert (
        plugin._select_open_thread_followup(
            _ctx(unanswered_streak=3), "private:u1", _session(), NOW
        )
        is None
    )
    assert (
        plugin._select_open_thread_followup(
            _ctx(expression={"mode": "回避", "style_hints": {"proactive_bias": -0.6}}),
            "private:u1",
            _session(),
            NOW,
        )
        is None
    )


# --------------------------------------------------------------------------- #
# receipt idempotency
# --------------------------------------------------------------------------- #


def test_commit_followup_is_one_shot_and_stamps_cooldown(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter()
    plugin._companion_adapter_override = adapter
    session = _session(companion_open_thread_followup={"thread_id": "thread:a", "umo": UMO})
    assert asyncio.run(plugin._commit_open_thread_followup(session, UMO, 12345.0)) is True
    assert adapter.followed == [{"umo": UMO, "thread_id": "thread:a"}]
    assert session["companion_open_thread_cooldown"]["thread:a"] == 12345.0
    # Second call has no pending candidate -> no-op (idempotent).
    assert asyncio.run(plugin._commit_open_thread_followup(session, UMO, 99999.0)) is False
    assert len(adapter.followed) == 1


def test_commit_without_pending_is_noop(plugin):
    _enable(plugin)
    adapter = _OpenThreadAdapter()
    plugin._companion_adapter_override = adapter
    assert asyncio.run(plugin._commit_open_thread_followup(_session(), UMO, 1.0)) is False
    assert adapter.followed == []


# --------------------------------------------------------------------------- #
# assistant-side topic
# --------------------------------------------------------------------------- #


def test_assistant_topic_extraction(plugin):
    item = plugin._extract_assistant_open_topic("那我们先这样，下次再聊呀")
    assert item["kind"] == "topic"
    assert item["confidence"] == 0.7
    assert plugin._extract_assistant_open_topic("好的没问题") is None


# --------------------------------------------------------------------------- #
# generation integration
# --------------------------------------------------------------------------- #


def test_generation_injects_open_thread_block(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx())
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    text = asyncio.run(
        plugin._generate_proactive_text(UMO, "private:u1", 3600, session)
    )
    assert text
    assert "【未完话题】" in captured["prompt"]
    assert "把方案发你" in captured["prompt"]
    assert session["companion_open_thread_followup"]["thread_id"] == "thread:a"


def test_generation_uses_placeholder_when_present(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx())
    plugin.config["proactive_prompt_template"] = "话题：{open_thread_followup}\n"
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, _session()))
    prompt = captured["prompt"]
    assert "话题：对方之前提过" in prompt
    assert "【未完话题】" not in prompt


def test_generation_no_thread_leaves_v273_behavior(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter({"life_state": {"summary": "x"}})
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    assert "【未完话题】" not in captured["prompt"]
    assert "companion_open_thread_followup" not in session


def test_generation_group_never_injects_open_thread(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx())
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(
        plugin._generate_proactive_text("aiocqhttp:group:g1", "group:g1", 3600, _session())
    )
    assert "【未完话题】" not in captured["prompt"]


# --------------------------------------------------------------------------- #
# motivation field must not bypass the follow-up gate (TMEAAA-503)
# --------------------------------------------------------------------------- #

LEGACY_LABEL = "给你带的那本书"
LEGACY_CTX = {"open_threads": [LEGACY_LABEL, "另一件没做完的事"]}


def _ctx_with_legacy_labels(**overrides):
    ctx = _ctx(**overrides)
    ctx.update(LEGACY_CTX)
    return ctx


def test_motivation_text_drops_open_thread_labels(plugin):
    text = plugin._companion_motivation_text(
        {"motivation": {"reason": "想起你提过的事"}, **LEGACY_CTX}
    )
    assert "想起你提过的事" in text
    assert LEGACY_LABEL not in text
    assert "未完成话题" not in text


def test_generation_cooldown_keeps_label_out_of_prompt(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx_with_legacy_labels())
    captured = {}
    _mock_llm(plugin, captured)
    session = _session(companion_open_thread_cooldown={"thread:a": time.time()})
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    assert "【未完话题】" not in captured["prompt"]
    assert LEGACY_LABEL not in captured["prompt"]
    assert "companion_open_thread_followup" not in session


def test_generation_followup_cap_keeps_label_out_of_prompt(plugin):
    _enable(plugin)
    ctx = _ctx_with_legacy_labels(
        open_thread_details=[_detail(followup_count=OPEN_THREAD_FOLLOWUP_MAX)]
    )
    plugin._companion_adapter_override = _FetchAdapter(ctx)
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    assert "【未完话题】" not in captured["prompt"]
    assert LEGACY_LABEL not in captured["prompt"]


def test_generation_toggle_off_keeps_label_out_of_prompt(plugin):
    _enable(plugin, open_thread_followup_enabled=False)
    plugin._companion_adapter_override = _FetchAdapter(_ctx_with_legacy_labels())
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    assert "【未完话题】" not in captured["prompt"]
    assert LEGACY_LABEL not in captured["prompt"]


def test_generation_single_injection_path_when_gate_passes(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx_with_legacy_labels())
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    prompt = captured["prompt"]
    assert prompt.count("把方案发你") == 1
    assert LEGACY_LABEL not in prompt


# --------------------------------------------------------------------------- #
# core motivation.reason must not carry the label either (TMEAAA-504)
# --------------------------------------------------------------------------- #

MOTIVATION_LABEL = "把方案发你"
LEAKY_REASON = f"未完成话题「{MOTIVATION_LABEL}」，想找机会收个尾。"
GENERIC_REASON = "有件对方提过、还没收尾的事，想找机会提一句。"


def _ctx_with_motivation(reason, **overrides):
    ctx = _ctx(**overrides)
    ctx["motivation"] = {"reason": reason, "score": 0.85}
    return ctx


def test_motivation_text_drops_core_leaked_label(plugin):
    text = plugin._companion_motivation_text(_ctx_with_motivation(LEAKY_REASON))
    assert text == ""


def test_motivation_text_keeps_generic_reason(plugin):
    text = plugin._companion_motivation_text(_ctx_with_motivation(GENERIC_REASON))
    assert GENERIC_REASON in text
    assert MOTIVATION_LABEL not in text


def test_generation_cooldown_hides_motivation_label(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx_with_motivation(LEAKY_REASON))
    captured = {}
    _mock_llm(plugin, captured)
    session = _session(companion_open_thread_cooldown={"thread:a": time.time()})
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    assert MOTIVATION_LABEL not in captured["prompt"]
    assert "companion_open_thread_followup" not in session


def test_generation_followup_cap_hides_motivation_label(plugin):
    _enable(plugin)
    ctx = _ctx_with_motivation(
        LEAKY_REASON, open_thread_details=[_detail(followup_count=OPEN_THREAD_FOLLOWUP_MAX)]
    )
    plugin._companion_adapter_override = _FetchAdapter(ctx)
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, _session()))
    assert MOTIVATION_LABEL not in captured["prompt"]


def test_generation_toggle_off_hides_motivation_label(plugin):
    _enable(plugin, open_thread_followup_enabled=False)
    plugin._companion_adapter_override = _FetchAdapter(_ctx_with_motivation(LEAKY_REASON))
    captured = {}
    _mock_llm(plugin, captured)
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, _session()))
    assert MOTIVATION_LABEL not in captured["prompt"]


def test_generation_gate_pass_keeps_label_only_in_followup_block(plugin):
    _enable(plugin)
    plugin._companion_adapter_override = _FetchAdapter(_ctx_with_motivation(GENERIC_REASON))
    captured = {}
    _mock_llm(plugin, captured)
    session = _session()
    asyncio.run(plugin._generate_proactive_text(UMO, "private:u1", 3600, session))
    prompt = captured["prompt"]
    assert GENERIC_REASON in prompt
    assert prompt.count(MOTIVATION_LABEL) == 1
    assert "【未完话题】" in prompt

