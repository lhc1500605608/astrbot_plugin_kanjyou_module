"""Phase 2 idempotent companion attempt receipts.

Focused adapter coverage: optional ``event_id`` / ``persona_id`` negotiation,
legacy signatures, explicit contract acknowledgement, pending replay, and the
new emotion attempt dedupe key. Async tests use ``asyncio.run`` (this suite has
no pytest-asyncio dependency).
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
    ATTEMPT_OUTCOME_RECEIPT_LIMIT,
    CompanionContextAdapter,
)


class _Completion:
    def __init__(self, text: str):
        self.completion_text = text


class _OutcomeStar:
    def __init__(self, results=None, exc=None):
        self.results = list(results or [])
        self.exc = exc
        self.calls = []

    async def get_contract_info(self):
        return {"api_version": 1, "capabilities": {}}

    async def on_proactive_outcome(
        self, umo, *, sent, reason_code, replied=False, persona_id=None, event_id=None
    ):
        self.calls.append(
            {
                "umo": umo,
                "sent": sent,
                "reason_code": reason_code,
                "replied": replied,
                "persona_id": persona_id,
                "event_id": event_id,
            }
        )
        if self.exc:
            raise self.exc
        if self.results:
            return self.results.pop(0)
        return {"applied": True, "duplicate": False}


class _LegacyStar:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    async def get_contract_info(self):
        return {"api_version": 1, "capabilities": {}}

    async def on_proactive_outcome(self, umo, *, sent, reason_code, replied=False):
        self.calls.append({"umo": umo, "sent": sent, "reason_code": reason_code})
        return self.result


class _AttemptAdapter:
    """Adapter override shaped like the coordinator-visible report API."""

    def __init__(self, results=None, exc=None):
        self.results = list(results or [])
        self.exc = exc
        self.calls = []

    async def report_outcome(
        self,
        umo,
        *,
        sent,
        reason_code,
        replied=False,
        persona_id=None,
        event_id=None,
    ):
        self.calls.append(
            {
                "umo": umo,
                "sent": sent,
                "reason_code": reason_code,
                "replied": replied,
                "persona_id": persona_id,
                "event_id": event_id,
            }
        )
        if self.exc:
            raise self.exc
        if self.results:
            result = self.results.pop(0)
            return {
                "ok": result.get("applied") is True or result.get("duplicate") is True,
                "result": result,
                "retryable": True,
            }
        return {"ok": True, "result": {"applied": True}, "retryable": False}


class _RecordingEmotionAdapter:
    def __init__(self):
        self.calls = []

    async def record_emotion_event(
        self, umo, *, event_type, reason="", dedupe_key=None
    ):
        self.calls.append(
            {
                "umo": umo,
                "event_type": event_type,
                "reason": reason,
                "dedupe_key": dedupe_key,
            }
        )
        return {"applied": True, "duplicate": False}


def _install_star(plugin, star):
    metadata = types.SimpleNamespace(activated=True, star_cls=star)
    plugin.context.get_registered_star = lambda _name, _m=metadata: _m


def _enable(plugin, **overrides):
    plugin.config["companion_enabled"] = True
    plugin.config["private_whitelist"] = [plugin._whitelist_entry("u1", "aiocqhttp")]
    for key, value in overrides.items():
        plugin.config[key] = value


def _session(**overrides):
    session = {
        "session_key": "private:aiocqhttp:private:u1",
        "unified_msg_origin": "aiocqhttp:private:u1",
        "identity": {
            "platform": "aiocqhttp",
            "native_id": "u1",
            "sender_id": "u1",
        },
    }
    session.update(overrides)
    return session


def test_same_event_replay_and_two_distinct_ids_are_idempotent(plugin):
    _enable(plugin)
    star = _OutcomeStar(
        results=[
            {"applied": True, "duplicate": False},
        ]
    )
    _install_star(plugin, star)
    session = _session()
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-1",
                persona_id="persona-a",
                sent=True,
                reason_code="ok",
            )
        )
        is True
    )
    # Replays may reach the core again with the original idempotency key.
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-1",
                persona_id="persona-a",
                sent=True,
                reason_code="ok",
            )
        )
        is True
    )
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-2",
                persona_id="persona-a",
                sent=True,
                reason_code="ok",
            )
        )
        is True
    )
    assert star.calls[0]["event_id"] == "evt-1"
    assert star.calls[1]["event_id"] == "evt-1"
    assert star.calls[2]["event_id"] == "evt-2"
    assert len(star.calls) == 3
    assert "companion_attempt_receipt_pending" not in session


def test_duplicate_and_personas_are_forwarded(plugin):
    _enable(plugin)
    star = _OutcomeStar(
        results=[
            {"applied": True, "duplicate": False},
            {"applied": False, "duplicate": True},
        ]
    )
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    first = asyncio.run(
        adapter.report_outcome(
            "aiocqhttp:private:u1",
            sent=True,
            reason_code="ok",
            persona_id="persona-a",
            event_id="evt-1",
        )
    )
    second = asyncio.run(
        adapter.report_outcome(
            "aiocqhttp:private:u1",
            sent=False,
            reason_code="blocked",
            persona_id="persona-b",
            event_id="evt-2",
        )
    )
    assert first["ok"] is True and second["ok"] is True
    assert star.calls[0]["persona_id"] == "persona-a"
    assert star.calls[1]["persona_id"] == "persona-b"


def test_legacy_signature_and_legacy_none_are_preserved(plugin):
    _enable(plugin)
    star = _LegacyStar(result=None)
    _install_star(plugin, star)
    adapter = CompanionContextAdapter(plugin, timeout_sec=0.5)
    result = asyncio.run(
        adapter.report_outcome(
            "aiocqhttp:private:u1",
            sent=True,
            reason_code="legacy",
            persona_id="persona-a",
            event_id="evt-legacy",
        )
    )
    assert result["ok"] is True and result["legacy"] is True
    # New kwargs were intentionally not sent to the old callable.
    assert star.calls == [
        {"umo": "aiocqhttp:private:u1", "sent": True, "reason_code": "legacy"}
    ]


def test_internal_typeerror_is_called_once_and_stays_pending(plugin):
    _enable(plugin)
    star = _OutcomeStar(exc=TypeError("internal mismatch"))
    _install_star(plugin, star)
    session = _session()
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-error",
                persona_id=None,
                sent=False,
                reason_code="generation_timeout",
            )
        )
        is False
    )
    # Retry never changes the call shape after an internal TypeError.
    assert asyncio.run(plugin._flush_attempt_outcomes(session)) is False
    assert len(star.calls) == 1
    assert (
        session["companion_attempt_receipt_pending"]["evt-error"]["event_id"]
        == "evt-error"
    )


def test_degraded_and_unapplied_reject_and_remain_pending(plugin):
    _enable(plugin)
    adapter = _AttemptAdapter(
        results=[
            {"applied": False, "duplicate": False, "degraded": True},
            {"applied": False, "duplicate": False, "reason": "unapplied"},
            {"applied": False, "duplicate": False, "degraded": True},
            {"applied": False, "duplicate": False, "reason": "unapplied"},
        ]
    )
    plugin._companion_adapter_override = adapter
    session = _session()
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-degraded",
                persona_id=None,
                sent=True,
                reason_code="ok",
            )
        )
        is False
    )
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-unapplied",
                persona_id=None,
                sent=True,
                reason_code="ok",
            )
        )
        is False
    )
    assert set(session["companion_attempt_receipt_pending"]) == {
        "evt-degraded",
        "evt-unapplied",
    }
    assert asyncio.run(plugin._flush_attempt_outcomes(session)) is False
    assert len(adapter.calls) == 4


def test_pending_replay_preserves_ids_and_never_sends(plugin):
    _enable(plugin)
    adapter = _AttemptAdapter(
        results=[
            {"applied": False, "duplicate": False, "degraded": True},
            {"applied": False, "duplicate": True},
        ],
        exc=None,
    )
    plugin._companion_adapter_override = adapter
    session = _session()
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-retry",
                persona_id="persona-a",
                sent=True,
                reason_code="ok",
            )
        )
        is False
    )
    assert asyncio.run(plugin._flush_attempt_outcomes(session)) is True
    assert adapter.calls[0]["event_id"] == adapter.calls[1]["event_id"] == "evt-retry"
    assert (
        adapter.calls[0]["persona_id"] == adapter.calls[1]["persona_id"] == "persona-a"
    )
    assert "companion_attempt_receipt_pending" not in session


def test_receipt_overflow_is_explicit_failure(plugin):
    _enable(plugin)
    adapter = _AttemptAdapter(
        results=[{"applied": False, "duplicate": False, "degraded": True}] * 100
    )
    plugin._companion_adapter_override = adapter
    session = _session()
    for index in range(ATTEMPT_OUTCOME_RECEIPT_LIMIT):
        assert (
            asyncio.run(
                plugin._record_attempt_outcome(
                    session,
                    "aiocqhttp:private:u1",
                    event_id=f"evt-{index}",
                    persona_id=None,
                    sent=False,
                    reason_code="generation_timeout",
                )
            )
            is False
        )
    overflow = asyncio.run(
        plugin._record_attempt_outcome(
            session,
            "aiocqhttp:private:u1",
            event_id="evt-overflow",
            persona_id=None,
            sent=False,
            reason_code="generation_timeout",
        )
    )
    assert overflow is False
    assert "evt-overflow" not in session["companion_attempt_receipt_pending"]
    assert (
        len(session["companion_attempt_receipt_pending"])
        == ATTEMPT_OUTCOME_RECEIPT_LIMIT
    )


def test_disabled_companion_short_circuits_receipts(plugin):
    _enable(plugin, companion_enabled=False)
    adapter = _AttemptAdapter()
    plugin._companion_adapter_override = adapter
    session = _session()
    assert (
        asyncio.run(
            plugin._record_attempt_outcome(
                session,
                "aiocqhttp:private:u1",
                event_id="evt-disabled",
                persona_id=None,
                sent=True,
                reason_code="ok",
            )
        )
        is False
    )
    assert asyncio.run(plugin._flush_attempt_outcomes(session)) is False
    assert adapter.calls == []
    assert "companion_attempt_receipt_pending" not in session


def test_reply_and_ignored_share_new_attempt_dedupe_key(plugin):
    _enable(plugin, emotion_reply_window_sec=3600, emotion_ignore_window_sec=3600)
    adapter = _RecordingEmotionAdapter()
    plugin._companion_adapter_override = adapter
    session = _session()
    asyncio.run(
        plugin._begin_proactive_receipt(
            session, "aiocqhttp:private:u1", 1000.0, attempt_id="evt-emotion"
        )
    )
    assert session["companion_receipt_pending"]["attempt_id"] == "evt-emotion"
    assert (
        plugin._proactive_receipt_key(1000.0, "evt-emotion") == "proactive:evt-emotion"
    )
    asyncio.run(plugin._settle_expired_receipt(session, 5000.0))
    assert adapter.calls[-1]["event_type"] == "ignored_proactive"
    assert adapter.calls[-1]["dedupe_key"] == "proactive:evt-emotion"

    # A reply to a second attempt uses the exact same key, ensuring true
    # mutual exclusion in companion-core.
    asyncio.run(
        plugin._begin_proactive_receipt(
            session, "aiocqhttp:private:u1", 6000.0, attempt_id="evt-emotion"
        )
    )
    decided = plugin._collect_inbound_emotion_event(
        "private:aiocqhttp:private:u1",
        session,
        "谢谢你",
        types.SimpleNamespace(
            message_obj=types.SimpleNamespace(
                message_id="m1",
                sender=types.SimpleNamespace(user_id="u1"),
            )
        ),
        "aiocqhttp:private:u1",
        6100.0,
    )
    assert decided is not None
    assert decided[0] == "valued_reply"
    assert decided[1] == "proactive:evt-emotion"


def test_legacy_pending_receipt_still_uses_send_ts_key(plugin):
    _enable(plugin, emotion_ignore_window_sec=3600)
    adapter = _RecordingEmotionAdapter()
    plugin._companion_adapter_override = adapter
    session = _session(
        companion_receipt_pending={"send_ts": 1234.9, "umo": "aiocqhttp:private:u1"}
    )
    asyncio.run(plugin._settle_expired_receipt(session, 9999.0))
    assert adapter.calls[-1]["dedupe_key"] == "proactive:1234"
