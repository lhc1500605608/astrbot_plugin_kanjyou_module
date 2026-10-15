"""Synthetic trust-boundary regressions; no model, tools, or real delivery."""

import asyncio
import copy
import json

import pytest
from test_merge import _FakeEvent, _make_plugin
from test_platform_whitelist_boundary import _event


def _remember(plugin, event):
    row = plugin._get_or_create_session(event)
    plugin._sessions[plugin._session_key(event)] = row
    return row


def _message(umo, sender="42", role="member", group="42", text="ordinary"):
    event = _FakeEvent(text, umo=umo, sender_id=sender, group_id=group)
    event.role = role
    event.is_admin = lambda: event.role == "admin"
    return event


@pytest.mark.parametrize("other", ["adapter-B", "adapter-a"])
def test_full_umo_separates_platform_instances_and_preserves_case(plugin, other):
    first = _event("adapter-A")
    second = _event(other)
    row = _remember(plugin, first)
    row["recent_proactive_texts"] = ["FIRST_HISTORY"]
    new = _remember(plugin, second)
    assert plugin._session_key(first) != plugin._session_key(second)
    assert new["recent_proactive_texts"] == []
    assert row["unified_msg_origin"] == first.unified_msg_origin


def test_private_threads_keep_full_colon_suffix_and_sender_whitelist(plugin):
    first = _event("adapter", thread="thread:one")
    second = _event("adapter", thread="thread:two")
    one, two = _remember(plugin, first), _remember(plugin, second)
    assert one is not two
    assert plugin._session_key(first).endswith("thread:one")
    plugin.config["private_whitelist"] = [plugin._whitelist_entry("42", "adapter")]
    assert plugin._is_session_whitelisted(one["session_key"])
    assert plugin._is_session_whitelisted(two["session_key"])
    plugin.config["private_whitelist"] = [plugin._whitelist_entry("one", "adapter")]
    assert not plugin._is_session_whitelisted(one["session_key"])


@pytest.mark.parametrize("kind", ["private", "group"])
def test_legacy_state_and_whitelist_migrate_saved_origin_only(plugin, kind):
    origin = "instance:" + ("FriendMessage" if kind == "private" else "GroupMessage") + ":thread:42"
    raw = {kind + ":42": {"session_key": kind + ":42", "unified_msg_origin": origin, "recent_proactive_texts": ["HISTORY"]}}
    plugin.config[kind + "_whitelist"] = ["42"]
    plugin._state_path.write_text(json.dumps(raw))
    rows = plugin._load_state()
    key = kind + ":" + origin
    assert rows[key]["recent_proactive_texts"] == ["HISTORY"]
    assert rows[key]["identity"]["native_id"] == "42"
    assert plugin.config[kind + "_whitelist"] == [plugin._whitelist_entry("42", "instance", kind)]
    plugin._sessions = rows
    assert plugin._is_session_whitelisted(key)
    assert plugin._migrate_session_state(rows)[0] == rows


def test_legacy_unbound_config_never_binds_on_later_reload(plugin):
    plugin.config["private_whitelist"] = ["42"]
    assert plugin._load_state() == {}
    marker = copy.deepcopy(plugin.config["private_whitelist"])
    _remember(plugin, _event("new-instance"))
    plugin._save_state()
    plugin._sessions = plugin._load_state()
    assert plugin.config["private_whitelist"] == marker
    assert not plugin._is_whitelisted(_event("new-instance"))


def test_ambiguous_legacy_whitelist_remains_inert(plugin):
    one = _remember(plugin, _event("A"))
    two = _remember(plugin, _event("B"))
    plugin._save_state()
    plugin.config["private_whitelist"] = ["42"]
    plugin._sessions = plugin._load_state()
    assert not plugin._is_session_whitelisted(one["session_key"])
    assert not plugin._is_session_whitelisted(two["session_key"])


@pytest.mark.parametrize("origin", [None, "invalid", "A:Unknown:42", "A:FriendMessage:"])
def test_unattributable_state_retained_but_not_active(plugin, origin):
    original = {"private:42": {"session_key": "private:42", "unified_msg_origin": origin, "recent_proactive_texts": ["RECOVERABLE"]}}
    rows, count = plugin._migrate_session_state(original)
    assert count == 1
    key, row = next(iter(rows.items()))
    assert key.startswith("__quarantine__")
    assert row["recent_proactive_texts"] == ["RECOVERABLE"]
    assert not plugin._session_state_whitelisted(key, row)
    assert asyncio.run(plugin._process_session(key, row, plugin._now(), plugin._now().timestamp())) is False
    assert plugin._migrate_session_state(rows)[0] == rows


def test_collision_prefers_canonical_without_merging_histories(plugin):
    canonical = _remember(plugin, _event("A"))
    canonical["recent_proactive_texts"] = ["NEW"]
    old = {"session_key": "private:42", "unified_msg_origin": canonical["unified_msg_origin"], "recent_proactive_texts": ["OLD"]}
    source = {"private:42": old, canonical["session_key"]: canonical}
    rows, count = plugin._migrate_session_state(source)
    assert count == 1
    assert rows[canonical["session_key"]]["recent_proactive_texts"] == ["NEW"]
    assert any(row["recent_proactive_texts"] == ["OLD"] for key, row in rows.items() if key.startswith("__quarantine__"))
    assert plugin._migrate_session_state(rows)[0] == rows


def test_state_origin_and_identity_mismatch_never_authorizes(plugin):
    event = _event("A")
    row = _remember(plugin, event)
    key = row["session_key"]
    plugin.config["private_whitelist"] = [plugin._whitelist_entry("42", "B")]
    row["unified_msg_origin"] = "B:FriendMessage:42"
    row["identity"]["platform"] = "B"
    assert not plugin._session_state_whitelisted(key, row)
    fresh = _remember(plugin, event)
    assert fresh is not row
    assert any(k.startswith("__quarantine__") for k in plugin._sessions)


@pytest.mark.parametrize("roles", [("admin", "member"), ("member", "admin")])
def test_group_members_never_inherit_leader_authority(plugin, roles):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _message("A:GroupMessage:42", "leader", roles[0], text="LEADER")
        second = _message("A:GroupMessage:42", "follower", roles[1], text="FOLLOWER")
        assert await instance._evt_merge_gate(first)
        assert await instance._evt_merge_gate(second)
        instance._merge_shutdown()
        released = instance._merge_queue.items
        assert released == [first, second]
        assert [e.message_str for e in released] == ["LEADER", "FOLLOWER"]
        assert [e.is_admin() for e in released] == [r == "admin" for r in roles]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["role", "sender", "origin"])
def test_mutated_leader_is_split_before_requeue(plugin, change):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _message("A:GroupMessage:42", text="ONE")
        second = _message("A:GroupMessage:42", text="TWO")
        assert await instance._evt_merge_gate(first)
        assert await instance._evt_merge_gate(second)
        if change == "role":
            first.role = "admin"
        elif change == "sender":
            first.message_obj.sender.user_id = "different"
        else:
            first.unified_msg_origin = "B:GroupMessage:42"
        instance._merge_shutdown()
        assert instance._merge_queue.items == [first, second]
        assert [e.message_str for e in instance._merge_queue.items] == ["ONE", "TWO"]

    asyncio.run(scenario())


def test_same_sender_permission_change_starts_independent_batch(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _message("A:GroupMessage:42", role="admin", text="ONE")
        second = _message("A:GroupMessage:42", role="member", text="TWO")
        await instance._evt_merge_gate(first)
        await instance._evt_merge_gate(second)
        instance._merge_shutdown()
        assert instance._merge_queue.items == [first, second]
        assert second.message_str == "TWO"
        assert not second.is_admin()

    asyncio.run(scenario())


def test_known_context_merges_and_unknown_context_passes_through(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _message("A:GroupMessage:42", text="ONE")
        second = _message("A:GroupMessage:42", text="TWO")
        await instance._evt_merge_gate(first)
        await instance._evt_merge_gate(second)
        instance._merge_shutdown()
        assert instance._merge_queue.items == [first]
        assert first.message_str == "ONE\nTWO"
        unknown = _message("A:GroupMessage:42")
        del unknown.role
        assert not await instance._evt_merge_gate(unknown)
        assert not unknown.is_stopped()

    asyncio.run(scenario())


def test_typing_exact_origin_and_enabled_scope(plugin):
    instance = _make_plugin(plugin)
    instance.config["enabled"] = False

    async def scenario():
        event = _message("A:GroupMessage:42")
        assert await instance._evt_merge_gate(event)
        assert not instance._merge_note_typing(umo="B:GroupMessage:42")
        instance._merge_shutdown()
        instance.config["merge_enabled"] = False
        assert not await instance._evt_merge_gate(_message("A:GroupMessage:42"))

    asyncio.run(scenario())


def test_webui_requires_platform_for_both_kinds_and_survives_reload(plugin, monkeypatch):
    import sys

    webui = sys.modules[plugin.__module__.rsplit(".", 1)[0] + ".units.unit_webui"]
    for kind in ("private", "group"):
        payload = {"op": "add", "kind": kind, "id": "42"}
        async def body(p=payload):
            return p
        monkeypatch.setattr(plugin, "_webui_json_body", body)
        assert asyncio.run(plugin._web_api_whitelist())["status"] == "error"
        payload["platform"] = "A"
        assert asyncio.run(plugin._web_api_whitelist())["status"] == "ok"
        first = _event("A", group_id="42" if kind == "group" else "")
        second = _event("B", group_id="42" if kind == "group" else "")
        _remember(plugin, first)
        plugin._save_state()
        monkeypatch.setattr(plugin, "_webui_read_config_file", lambda: None)
        plugin._webui_reload()
        assert plugin._is_whitelisted(first)
        assert not plugin._is_whitelisted(second)
    assert webui is not None


@pytest.mark.parametrize("origin", ["B:GroupMessage:42", "A:GroupMessage:42:thread"])
def test_merge_never_crosses_full_origins(plugin, origin):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _message("A:GroupMessage:42", text="FIRST_ORIGIN")
        second = _message(origin, text="SECOND_ORIGIN")
        await instance._evt_merge_gate(first)
        await instance._evt_merge_gate(second)
        instance._merge_shutdown()
        assert instance._merge_queue.items == [first, second]
        assert first.message_str == "FIRST_ORIGIN"
        assert second.message_str == "SECOND_ORIGIN"
        assert [e.unified_msg_origin for e in instance._merge_queue.items] == ["A:GroupMessage:42", origin]

    asyncio.run(scenario())


def test_hard_deadline_consumes_follower_without_duplicate_default_call(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _message("A:GroupMessage:42", text="ONE")
        second = _message("A:GroupMessage:42", text="TWO")
        await instance._evt_merge_gate(first)
        instance._merge_sessions[instance._session_key(first)]["hard_deadline"] = 0
        assert await instance._evt_merge_gate(second)
        assert instance._merge_queue.items == [first]
        assert first.message_str == "ONE\nTWO"
        assert second.is_stopped()
        assert second.call_llm is True

    asyncio.run(scenario())
