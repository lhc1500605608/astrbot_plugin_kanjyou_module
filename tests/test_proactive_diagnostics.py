"""Per-call diagnostic metadata must remain private and delivery-specific."""

import asyncio
import json

import pytest
from test_send_delivery_result import KEY, UMO, _prepare


@pytest.mark.parametrize(
    "generation,reason",
    [
        ("generated", "ok"),
        ("fallback", "provider_missing"),
        ("fallback", "provider_timeout"),
    ],
)
def test_generation_metadata_survives_delivery_without_body(plugin, generation, reason):
    from astrbot_plugin_kanjyou_module.units.proactive_diagnostics import GeneratedText

    session = _prepare(plugin)

    async def generate(*args, **kwargs):
        return GeneratedText("PRIVATE_GENERATED_BODY", generation, reason)

    async def send(*args):
        return True

    plugin._generate_proactive_text = generate
    plugin.context.send_message = send
    result = asyncio.run(
        plugin._send_proactive(UMO, None, KEY, 10, session, source="manual")
    )
    assert result[0] is True
    assert result.attempt["generation_status"] == generation
    assert result.attempt["generation_reason"] == reason
    assert result.attempt["delivery_status"] == "sent"
    assert result.attempt["source"] == "manual"
    assert "PRIVATE_GENERATED_BODY" not in json.dumps(result.attempt)
    assert session["last_proactive_attempt"] == result.attempt


@pytest.mark.parametrize(
    "mode,delivery,reason",
    [
        ("false", "failed", "send_message_false"),
        ("exception", "unknown", "send_unknown"),
    ],
)
def test_delivery_failures_have_distinct_certainty(plugin, mode, delivery, reason):
    session = _prepare(plugin)

    async def send(*args):
        if mode == "false":
            return False
        raise RuntimeError("PRIVATE_EXCEPTION")

    plugin.context.send_message = send
    result = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))
    assert result[0] is False
    assert result.attempt["delivery_status"] == delivery
    assert result.attempt["reason_code"] == reason
    assert "PRIVATE_EXCEPTION" not in json.dumps(result.attempt)


@pytest.mark.parametrize(
    "error,status,reason",
    [
        (TimeoutError, "timeout", "generation_timeout"),
        (asyncio.CancelledError, "cancelled", "generation_cancelled"),
    ],
)
def test_generation_abort_does_not_report_delivery(plugin, error, status, reason):
    session = _prepare(plugin)

    async def generate(*args, **kwargs):
        raise error()

    plugin._generate_proactive_text = generate
    result = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))
    assert result[0] is False
    assert result.attempt["generation_status"] == status
    assert result.attempt["delivery_status"] == "not_attempted"
    assert result.attempt["reason_code"] == reason


def test_busy_result_cannot_inherit_prior_attempt(plugin):
    session = _prepare(plugin)
    session["last_proactive_attempt"] = {"generation_status": "generated"}
    plugin._proactive_session_inflight.add(KEY)
    result = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))
    assert result.attempt["status"] == "busy"
    assert result.attempt["generation_status"] == "not_started"


def test_untrusted_saved_diagnostics_are_allowlisted(plugin):
    from astrbot_plugin_kanjyou_module.units.proactive_diagnostics import safe_attempt

    result = safe_attempt(
        {
            "body": "SECRET",
            "reason_code": "SECRET",
            "generation_status": ["SECRET"],
            "attempt_id": "SECRET",
            "elapsed_ms": float("inf"),
        }
    )
    assert "SECRET" not in json.dumps(result)
    assert result["elapsed_ms"] == 0


def test_overlapping_requests_keep_independent_diagnostics(plugin):
    session = _prepare(plugin)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            from astrbot_plugin_kanjyou_module.units.proactive_diagnostics import (
                GeneratedText,
            )

            return GeneratedText("PRIVATE_BODY", "fallback", "provider_missing")

        async def send(*args):
            return True

        plugin._generate_proactive_text = generate
        plugin.context.send_message = send
        first = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, session))
        await entered.wait()
        busy = await plugin._send_proactive(UMO, None, KEY, 10, session)
        release.set()
        sent = await first
        assert busy.attempt["status"] == "busy"
        assert busy.attempt["generation_status"] == "not_started"
        assert sent.attempt["generation_status"] == "fallback"
        assert busy.attempt["attempt_id"] != sent.attempt["attempt_id"]

    asyncio.run(scenario())


def test_queued_receipt_success_refreshes_matching_attempt(plugin):
    from astrbot_plugin_kanjyou_module.units.proactive_diagnostics import safe_attempt

    session = _prepare(plugin)
    plugin.config["companion_enabled"] = True
    old = safe_attempt(
        {
            "source": "scheduled",
            "status": "failed",
            "generation_status": "generated",
            "generation_reason": "ok",
            "delivery_status": "sent",
            "receipt_status": "pending",
            "reason_code": "ok",
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "elapsed_ms": 10,
        }
    )
    session["last_proactive_attempt"] = dict(old)
    session["proactive_attempt_pending"] = {
        old["attempt_id"]: {"diagnostic": dict(old), "started_at": 100.0}
    }

    plugin._refresh_last_attempt_receipt_status(
        session, old["attempt_id"], "acknowledged"
    )

    assert session["last_proactive_attempt"]["receipt_status"] == "acknowledged"
    assert (
        session["proactive_attempt_pending"][old["attempt_id"]]["diagnostic"][
            "receipt_status"
        ]
        == "acknowledged"
    )


def test_queued_receipt_does_not_overwrite_newer_attempt(plugin):
    from astrbot_plugin_kanjyou_module.units.proactive_diagnostics import safe_attempt

    session = _prepare(plugin)
    plugin.config["companion_enabled"] = True
    old_id = "11111111-1111-4111-8111-111111111111"
    new_id = "22222222-2222-4222-8222-222222222222"
    new = safe_attempt(
        {
            "source": "scheduled",
            "status": "completed",
            "generation_status": "generated",
            "generation_reason": "ok",
            "delivery_status": "sent",
            "receipt_status": "pending",
            "reason_code": "ok",
            "attempt_id": new_id,
            "elapsed_ms": 10,
        }
    )
    session["last_proactive_attempt"] = dict(new)
    session["proactive_attempt_pending"] = {
        old_id: {
            "diagnostic": {"receipt_status": "pending", "attempt_id": old_id},
            "started_at": 100.0,
        }
    }

    plugin._refresh_last_attempt_receipt_status(session, old_id, "acknowledged")

    assert session["last_proactive_attempt"]["receipt_status"] == "pending"
    assert (
        session["proactive_attempt_pending"][old_id]["diagnostic"]["receipt_status"]
        == "acknowledged"
    )


def test_generation_queue_full_is_typed_safe_diagnostic(plugin):
    session = _prepare(plugin)
    session["last_proactive_attempt"] = {
        "attempt_id": "33333333-3333-4333-8333-333333333333"
    }
    for index in range(8):
        plugin._proactive_generation_tasks.add(object())

    async def generate(*args, **kwargs):
        return "never generated"

    async def send(*args):
        raise AssertionError("queue full must not deliver")

    plugin._generate_proactive_text = generate
    plugin.context.send_message = send
    result = asyncio.run(
        plugin._send_proactive(UMO, None, KEY, 10, session, source="manual")
    )

    assert result[0] is False
    assert result.attempt["generation_status"] == "not_started"
    assert result.attempt["reason_code"] == "generation_queue_full"
    assert result.attempt["status"] == "blocked"
    assert result.attempt["delivery_status"] == "not_attempted"
    assert "PRIVATE" not in json.dumps(result.attempt)
    assert session["last_proactive_attempt"]["reason_code"] == "generation_queue_full"


def test_fallback_delivery_cancellation_is_recorded(plugin):
    session = _prepare(plugin)
    calls = 0

    async def send(_umo, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeError("MessageChain adapter mismatch")
        raise asyncio.CancelledError

    plugin.context.send_message = send
    result = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, session))

    assert result[0] is False
    assert calls == 2
    assert result.attempt["delivery_status"] == "unknown"
    assert result.attempt["reason_code"] == "delivery_cancelled"
    assert result.attempt["status"] == "cancelled"
    assert session["last_proactive_attempt"] == result.attempt
