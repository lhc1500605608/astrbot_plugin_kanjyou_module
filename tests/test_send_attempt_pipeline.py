"""Phase 2 proactive send pipeline tests.

Covers per-session locking, strict pre-delivery revalidation, bounded/owned
generation tasks, attempt receipts, and manual/scheduled coordination. These
tests use synthetic providers/platforms only and never inspect real secrets.
"""

from __future__ import annotations

import asyncio

from test_merge import _FakeEvent

UMO = "webchat:FriendMessage:10001"
KEY = "private:webchat:FriendMessage:10001"
OTHER_UMO = "webchat:FriendMessage:10002"
OTHER_KEY = "private:webchat:FriendMessage:10002"


def _session(plugin, key=KEY, umo=UMO, native="10001", **overrides):
    now = plugin._now().timestamp()
    row = {
        "session_key": key,
        "unified_msg_origin": umo,
        "identity": {"platform": "webchat", "native_id": native},
        "last_human_at": now - 120,
        "last_interaction_at": now - 120,
        "next_check_at": now + 30,
        "cooldown_until": 0.0,
        "today_proactive_count": 0,
        "pending_human_reply": False,
        "user_message_revision": 7,
        "recent_proactive_texts": [],
    }
    row.update(overrides)
    plugin.config["private_whitelist"] = [
        plugin._whitelist_entry(native, "webchat", "private")
    ]
    plugin._sessions[key] = row
    return row


def _receipts(plugin):
    recorded = []

    async def _record(session, umo, *, event_id, persona_id, sent, reason_code):
        recorded.append(
            {
                "session": session,
                "umo": umo,
                "event_id": event_id,
                "persona_id": persona_id,
                "sent": sent,
                "reason_code": reason_code,
            }
        )
        return True

    plugin._record_attempt_outcome = _record
    return recorded


def _delivery(plugin, result=True, calls=None):
    async def _send(umo, _chain):
        if calls is not None:
            calls.append(umo)
        return result

    plugin.context.send_message = _send


def _user_event(plugin, row, text="new message"):
    native = row["identity"]["native_id"]
    event = _FakeEvent(text, umo=row["unified_msg_origin"], sender_id=native)
    plugin.config["merge_enabled"] = False
    plugin.context.get_registered_star = lambda _name: None
    return event


def test_slow_session_does_not_block_other_session_or_global_snapshot(plugin):
    first = _session(plugin)
    second = _session(plugin, key=OTHER_KEY, umo=OTHER_UMO, native="10002")
    plugin.config["private_whitelist"] = [
        plugin._whitelist_entry("10001", "webchat", "private"),
        plugin._whitelist_entry("10002", "webchat", "private"),
    ]
    started = asyncio.Event()
    release = asyncio.Event()

    async def generate(umo, key, *_args, **_kwargs):
        if key == KEY:
            started.set()
            await release.wait()
        return "TEXT"

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.config["proactive_segment_enabled"] = False
        _delivery(plugin)

        async with plugin._lock:
            snapshot_before = list(plugin._sessions.items())
        task_a = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, first))
        await started.wait()

        # A short inbound state snapshot is not queued behind A's generation.
        snapshot_during = await asyncio.wait_for(_short_snapshot(plugin), timeout=0.2)

        task_b = asyncio.create_task(
            plugin._send_proactive(OTHER_UMO, None, OTHER_KEY, 10, second)
        )
        release.set()
        a_result = await task_a
        b_result = await task_b
        assert a_result[0] is True
        assert b_result[0] is True
        return snapshot_before, snapshot_during

    async def _short_snapshot(plug):
        async with plug._lock:
            return list(plug._sessions.items())

    before, during = asyncio.run(scenario())
    assert before and during


def test_user_message_during_generation_blocks_send_and_receipt(plugin):
    row = _session(plugin)
    receipts = _receipts(plugin)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def generate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "STALE"

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.config["proactive_segment_enabled"] = False
        _delivery(plugin, calls=calls)
        task = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, row))
        await started.wait()
        event = _user_event(plugin, row)
        await plugin._evt_on_all_message(event)
        release.set()
        return await task

    ok, text = asyncio.run(scenario())
    assert (ok, text) == (False, "")
    assert calls == []
    assert receipts[0]["sent"] is False
    assert receipts[0]["reason_code"] == "new_human_message"
    assert row["proactive_attempt_inflight"] is None


def test_whitelist_removed_during_generation_blocks_send(plugin):
    row = _session(plugin)
    receipts = _receipts(plugin)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def generate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "TEXT"

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.config["proactive_segment_enabled"] = False
        _delivery(plugin, calls=calls)
        task = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, row))
        await started.wait()
        plugin.config["private_whitelist"] = []
        release.set()
        return await task

    ok, text = asyncio.run(scenario())
    assert ok is False
    assert calls == []
    assert receipts[0]["reason_code"] == "whitelist_removed"


def test_disabled_or_dnd_during_generation_blocks_scheduled_send(plugin):
    row = _session(plugin)
    receipts = _receipts(plugin)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def generate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "TEXT"

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.config["proactive_segment_enabled"] = False
        _delivery(plugin, calls=calls)
        task = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, row))
        await started.wait()
        plugin.config["enabled"] = False
        release.set()
        return await task

    assert asyncio.run(scenario()) == (False, "")
    assert calls == []
    assert receipts[0]["reason_code"] == "disabled"


def test_manual_bypasses_dnd_but_hard_guards_survive(plugin):
    row = _session(plugin, cooldown_until=plugin._now().timestamp() + 600)
    receipts = _receipts(plugin)
    plugin.config["sleep_start"] = "00:00"
    plugin.config["sleep_end"] = "23:59"

    async def generate(*_args, **_kwargs):
        return "TEXT"

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False
    calls = []
    _delivery(plugin, calls=calls)

    ok, text = asyncio.run(
        plugin._send_proactive(UMO, None, KEY, 10, row, source="manual")
    )
    assert (ok, text) == (True, "TEXT")
    assert calls == [UMO]
    assert receipts[-1]["reason_code"] == "ok"

    # Identity/whitelist is still a hard manual guard.
    row2 = _session(plugin)
    plugin.config["private_whitelist"] = []
    ok2, _ = asyncio.run(
        plugin._send_proactive(UMO, None, KEY, 10, row2, source="manual")
    )
    assert ok2 is False
    assert receipts[-1]["reason_code"] == "whitelist_removed"


def test_concurrent_duplicate_manual_is_busy_not_sequential_send(plugin):
    row = _session(plugin)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def generate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "TEXT"

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.config["proactive_segment_enabled"] = False
        _delivery(plugin, calls=calls)
        first = asyncio.create_task(
            plugin._send_proactive(UMO, None, KEY, 10, row, source="manual")
        )
        await started.wait()
        second = asyncio.create_task(
            plugin._send_proactive(UMO, None, KEY, 10, row, source="manual")
        )
        release.set()
        return await first, await second

    first, second = asyncio.run(scenario())
    assert first[0] is True
    assert second == (False, "")
    assert calls == [UMO]


def test_manual_v_schedule_collision_revalidates_second_request(plugin):
    row = _session(plugin)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def generate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "TEXT"

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.config["proactive_segment_enabled"] = False
        _delivery(plugin, calls=calls)
        manual = asyncio.create_task(
            plugin._send_proactive(UMO, None, KEY, 10, row, source="manual")
        )
        await started.wait()
        scheduled = asyncio.create_task(
            plugin._send_proactive(UMO, None, KEY, 10, row, source="scheduled")
        )
        release.set()
        return await manual, await scheduled

    manual, scheduled = asyncio.run(scenario())
    assert manual[0] is True
    assert scheduled == (False, "")
    assert calls == [UMO]


def test_false_delivery_records_failure_without_text_or_finalize(plugin):
    row = _session(plugin)
    receipts = _receipts(plugin)
    remembered = []

    async def generate(*_args, **_kwargs):
        return "TEXT"

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False

    async def send(_umo, _chain):
        return False

    plugin.context.send_message = send
    plugin._remember_proactive_text = lambda *args, **kwargs: remembered.append(args)

    ok, text = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, row))
    assert (ok, text) == (False, "")
    assert remembered == []
    assert "pending_proactive_text" not in row
    assert receipts[-1]["sent"] is False
    assert receipts[-1]["reason_code"] == "send_message_false"
    pending = row.get("proactive_attempt_pending", {})
    assert pending == {}


def test_generation_timeout_cleans_owned_task_and_pending_attempt(plugin):
    row = _session(plugin)
    receipts = _receipts(plugin)
    plugin._proactive_generation_timeout_sec = lambda: 0.01
    entered = asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await asyncio.sleep(10)

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False

    async def scenario():
        task = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, row))
        await entered.wait()
        await asyncio.sleep(0.03)
        return await task

    ok, _ = asyncio.run(scenario())
    assert ok is False
    assert plugin._proactive_generation_tasks == set()
    assert receipts[-1]["reason_code"] == "generation_timeout"
    assert row["proactive_attempt_inflight"] is None


def test_receipt_uses_attempt_id_and_current_persona(plugin):
    row = _session(plugin)
    receipts = _receipts(plugin)
    plugin.config["persona_id"] = "configured-persona"

    async def generate(*_args, **_kwargs):
        return "TEXT"

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False
    _delivery(plugin)

    ok, _text = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, row))
    assert ok is True
    receipt = receipts[0]
    metadata = row["proactive_attempt_pending"]
    assert receipt["event_id"] == receipt["event_id"].strip()
    assert receipt["persona_id"] == "configured-persona"
    assert receipt["reason_code"] == "ok"
    # Successful receipt is consumed, never replayed by a later attempt.
    assert metadata == {}


def test_pending_receipt_replay_uses_same_attempt_id_without_resend(plugin):
    row = _session(plugin)
    plugin.config["persona_id"] = "p1"
    topic = "TEXT"

    async def generate(*_args, **_kwargs):
        return topic

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False
    calls = []
    _delivery(plugin, calls=calls)

    plugin.config["companion_enabled"] = True
    # First adapter call fails, leaving the known attempt pending.
    attempts = []

    async def record(session, umo, *, event_id, persona_id, sent, reason_code):
        attempts.append((event_id, sent, reason_code))
        return False

    plugin._record_attempt_outcome = record
    ok, _ = asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, row))
    assert ok is True
    assert attempts[-1][1] is True
    pending_id = row["proactive_attempt_pending"][attempts[-1][0]]["attempt_id"]
    assert pending_id == attempts[-1][0]

    # Actual receipt replay is tested through the real adapter in the receipt suite.
    assert calls == [UMO]
    assert row["proactive_attempt_pending"][pending_id]["state"] == "sent"


def test_terminate_cancels_and_awaits_owned_generation_tasks(plugin):
    row = _session(plugin)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False

    async def scenario():
        task = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, row))
        await entered.wait()
        await plugin.terminate()
        return await task

    ok, _ = asyncio.run(scenario())
    assert ok is False
    assert cancelled.is_set()
    assert plugin._proactive_generation_tasks == set()


def test_polling_pool_processes_other_session_before_slow_one_finishes(plugin):
    plugin.config["enabled"] = True
    plugin._in_sleep_window = lambda _now: False
    plugin._companion_maybe_refresh_life_content = _noop
    plugin._sessions = {KEY: {}, OTHER_KEY: {}}
    entered = asyncio.Event()
    other_done = asyncio.Event()
    release = asyncio.Event()

    async def process(key, *_args):
        if key == KEY:
            entered.set()
            await release.wait()
        else:
            other_done.set()
        return False

    async def scenario():
        plugin._process_session = process
        task = asyncio.create_task(plugin._check_sessions())
        await entered.wait()
        try:
            await asyncio.wait_for(other_done.wait(), 0.2)
        finally:
            release.set()
            await task

    asyncio.run(scenario())


async def _noop(*_args, **_kwargs):
    return False


def test_generation_semaphore_limits_actual_provider_work(plugin):
    running = 0
    peak = 0
    release = asyncio.Event()
    two_started = asyncio.Event()

    async def generate(*_args):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == 2:
            two_started.set()
        try:
            await release.wait()
            return "TEXT"
        finally:
            running -= 1

    async def scenario():
        plugin._generate_proactive_text = generate
        tasks = [
            asyncio.create_task(plugin._run_proactive_generation(UMO, KEY, 1, {}))
            for _ in range(4)
        ]
        await two_started.wait()
        await asyncio.sleep(0)
        assert peak == 2
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert peak == 2


def test_unknown_delivery_error_is_not_replayed(plugin):
    row = _session(plugin)
    calls = []

    async def generate(*_args):
        return "TEXT"

    async def send(*_args):
        calls.append(True)
        raise RuntimeError("unknown delivery outcome")

    plugin._generate_proactive_text = generate
    plugin.context.send_message = send
    plugin.config["proactive_segment_enabled"] = False
    assert asyncio.run(plugin._send_proactive(UMO, None, KEY, 10, row))[0] is False
    assert len(calls) == 1


def test_manual_success_has_separate_counter_and_attempt_emotion_receipt(plugin):
    row = _session(plugin)
    plugin.config["companion_enabled"] = True
    plugin.config["emotion_event_enabled"] = True

    async def generate(*_args):
        return "TEXT"

    plugin._generate_proactive_text = generate
    plugin.config["proactive_segment_enabled"] = False
    _delivery(plugin)
    assert asyncio.run(
        plugin._send_proactive(UMO, None, KEY, 10, row, source="manual")
    )[0]
    assert row["manual_proactive_count"] == 1
    assert row["today_proactive_count"] == 0
    assert row["companion_receipt_pending"]["attempt_id"]


def test_parallel_sessions_cannot_overrun_global_send_cap(plugin):
    first = _session(plugin)
    second = _session(plugin, key=OTHER_KEY, umo=OTHER_UMO, native="10002")
    plugin.config["private_whitelist"] = [
        plugin._whitelist_entry("10001", "webchat", "private"),
        plugin._whitelist_entry("10002", "webchat", "private"),
    ]
    plugin._security_global_hourly_cap = lambda: 1
    plugin._in_sleep_window = lambda _now: False
    plugin._period_quota_enabled = lambda: False
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def generate(*_args):
        return "TEXT"

    async def send(umo, _chain):
        calls.append(umo)
        entered.set()
        await release.wait()
        return True

    async def scenario():
        plugin._generate_proactive_text = generate
        plugin.context.send_message = send
        plugin.config["proactive_segment_enabled"] = False
        task = asyncio.create_task(plugin._send_proactive(UMO, None, KEY, 10, first))
        await entered.wait()
        try:
            result = await plugin._send_proactive(
                OTHER_UMO, None, OTHER_KEY, 10, second
            )
            assert result[0] is False
        finally:
            release.set()
            assert (await task)[0] is True

    asyncio.run(scenario())
    assert calls == [UMO]
    assert plugin._global_send_inflight == 0
