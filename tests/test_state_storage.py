"""Fault-injection and compatibility tests for atomic JSON state storage."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_kanjyou_module.units import state_storage  # noqa: E402
from astrbot_plugin_kanjyou_module.units.state_storage import (  # noqa: E402
    CorruptStateError,
    StateWriteCommittedError,
    load_json_state,
    write_json_atomic,
)


def test_atomic_write_replaces_file_and_has_no_temporary_left(tmp_path):
    target = tmp_path / "nested" / "state.json"
    original = {"version": 1, "value": "original"}
    write_json_atomic(target, original)

    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob("**/.state.json.*.tmp"))
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600

    updated = {"version": 2, "value": "updated"}
    write_json_atomic(target, updated)
    assert json.loads(target.read_text(encoding="utf-8")) == updated
    assert not list(tmp_path.glob("**/.state.json.*.tmp"))


def test_atomic_write_failure_before_replace_preserves_original_and_cleans_temporary(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    original = {"value": "KEEP ME"}
    write_json_atomic(target, original)
    target.chmod(0o644)
    before_mode = target.stat().st_mode

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_json_atomic(target, {"value": "NEW"})

    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert target.stat().st_mode == before_mode
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_post_replace_failure_reports_without_deleting_primary(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    write_json_atomic(target, {"value": "OLD"})

    real_replace = os.replace

    def replace_then_fail(*args, **kwargs):
        real_replace(*args, **kwargs)
        raise OSError("injected post-replace failure")

    monkeypatch.setattr(os, "replace", replace_then_fail)
    with pytest.raises(OSError, match="injected"):
        write_json_atomic(target, {"value": "NEW"})

    # The rename itself committed; cleanup must not roll back that durable write.
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "NEW"}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_directory_sync_failure_reports_that_primary_was_committed(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_text('{"value":"OLD"}', encoding="utf-8")

    def fail_directory_sync(*args, **kwargs):
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(state_storage, "_fsync_directory", fail_directory_sync)
    with pytest.raises(StateWriteCommittedError) as error:
        write_json_atomic(target, {"value": "NEW"})

    assert error.value.committed is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "NEW"}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_cleans_temporary_when_serialization_fails(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("ORIGINAL", encoding="utf-8")
    original = target.read_text(encoding="utf-8")

    def fail_dump(*args, **kwargs):
        raise ValueError("injected serialization failure")

    monkeypatch.setattr(json, "dump", fail_dump)
    with pytest.raises(ValueError, match="injected serialization"):
        write_json_atomic(target, {"bad": object()})
    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_load_state_rejects_malformed_primary_without_backup(tmp_path):
    primary = tmp_path / "state.json"
    primary.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(CorruptStateError):
        load_json_state(primary, tmp_path / "state.json.bak")

    assert primary.read_text(encoding="utf-8") == "{ this is not json"
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_load_state_recovers_missing_primary_from_valid_backup(tmp_path):
    backup = tmp_path / "state.json.bak"
    backup.write_text(json.dumps({"valid": "backup"}), encoding="utf-8")

    assert load_json_state(tmp_path / "state.json", backup) == (
        {"valid": "backup"},
        None,
    )


@pytest.mark.parametrize("contents", ["[", "[]", "null"])
def test_missing_primary_with_invalid_backup_is_not_treated_as_empty(
    tmp_path, contents
):
    backup = tmp_path / "state.json.bak"
    backup.write_text(contents, encoding="utf-8")

    with pytest.raises(CorruptStateError):
        load_json_state(tmp_path / "state.json", backup)


def test_load_state_recovers_only_from_valid_backup_and_retains_corrupt_primary(
    tmp_path,
):
    primary = tmp_path / "state.json"
    backup = tmp_path / "state.json.bak"
    primary.write_text("{ this is not json", encoding="utf-8")
    backup.write_text(json.dumps({"valid": "backup"}), encoding="utf-8")

    data, retained = load_json_state(primary, backup)

    assert data == {"valid": "backup"}
    assert retained is not None
    assert retained.read_text(encoding="utf-8") == "{ this is not json"
    assert primary.read_text(encoding="utf-8") == "{ this is not json"


def test_load_state_does_not_recover_from_malformed_backup(tmp_path):
    primary = tmp_path / "state.json"
    backup = tmp_path / "state.json.bak"
    primary.write_text("[", encoding="utf-8")
    backup.write_text("]", encoding="utf-8")

    with pytest.raises(CorruptStateError):
        load_json_state(primary, backup)

    assert primary.read_text(encoding="utf-8") == "["
    assert backup.read_text(encoding="utf-8") == "]"
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_load_state_rejects_backup_that_is_not_an_object(tmp_path):
    primary = tmp_path / "state.json"
    backup = tmp_path / "state.json.bak"
    primary.write_text("not json", encoding="utf-8")
    backup.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(CorruptStateError):
        load_json_state(primary, backup)
    assert primary.read_text(encoding="utf-8") == "not json"


def test_load_state_accepts_legacy_utf8_bom(tmp_path):
    primary = tmp_path / "state.json"
    primary.write_bytes(b"\xef\xbb\xbf" + json.dumps({"legacy": "bom"}).encode("utf-8"))
    assert load_json_state(primary, tmp_path / "state.json.bak") == (
        {"legacy": "bom"},
        None,
    )


def test_load_state_rejects_non_object_primary_without_backup(tmp_path):
    primary = tmp_path / "state.json"
    primary.write_text(json.dumps(["legacy scalar"]), encoding="utf-8")

    with pytest.raises(CorruptStateError):
        load_json_state(primary, tmp_path / "state.json.bak")

    assert primary.read_text(encoding="utf-8") == json.dumps(["legacy scalar"])
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_load_state_recovers_from_backup_for_non_object_primary(tmp_path):
    primary = tmp_path / "state.json"
    backup = tmp_path / "state.json.bak"
    primary.write_text(json.dumps(["legacy scalar"]), encoding="utf-8")
    backup.write_text(json.dumps({"valid": "backup"}), encoding="utf-8")

    data, retained = load_json_state(primary, backup)

    assert data == {"valid": "backup"}
    assert retained is not None
    assert retained.read_text(encoding="utf-8") == json.dumps(["legacy scalar"])
    assert primary.read_text(encoding="utf-8") == json.dumps(["legacy scalar"])


def test_plugin_save_and_reload_round_trip_with_backup(plugin, tmp_path):
    plugin._sessions = {
        "private:instance:FriendMessage:42": {
            "session_key": "private:instance:FriendMessage:42",
            "unified_msg_origin": "instance:FriendMessage:42",
            "identity": {"platform": "instance", "native_id": "42"},
            "recent_proactive_texts": ["HISTORY"],
        }
    }
    plugin._save_state()

    primary = tmp_path / "idle_state.json"
    backup = tmp_path / "idle_state.json.bak"
    assert json.loads(primary.read_text(encoding="utf-8")) == plugin._sessions
    assert json.loads(backup.read_text(encoding="utf-8")) == plugin._sessions
    assert not list(tmp_path.glob(".idle_state.json.*.tmp"))
    assert plugin._load_state() == plugin._sessions


def test_plugin_corrupt_state_is_retained_and_save_is_blocked(plugin, tmp_path):
    primary = tmp_path / "idle_state.json"
    primary.write_text("{ corrupt", encoding="utf-8")
    plugin._load_state()

    plugin._sessions = {"new": {"session_key": "new"}}
    plugin._save_state()

    assert primary.read_text(encoding="utf-8") == "{ corrupt"
    assert not (tmp_path / "idle_state.json.bak").exists()
    assert not list(tmp_path.glob(".idle_state.json.*.tmp"))


def test_plugin_migration_failure_keeps_state_writes_blocked(
    plugin, tmp_path, monkeypatch
):
    primary = tmp_path / "idle_state.json"
    original = {"legacy": {"session_key": "legacy"}}
    primary.write_text(json.dumps(original), encoding="utf-8")

    def fail_migration(_data):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(plugin, "_migrate_session_state", fail_migration)
    assert plugin._load_state() == {}
    assert plugin._state_storage_blocked is True

    plugin._sessions = {"empty": {"session_key": "empty"}}
    plugin._save_state()
    assert json.loads(primary.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "idle_state.json.bak").exists()


def test_plugin_reloads_backup_after_corruption(plugin, tmp_path, monkeypatch):
    primary = tmp_path / "idle_state.json"
    backup = tmp_path / "idle_state.json.bak"
    backup.write_text(json.dumps({}), encoding="utf-8")
    primary.write_text("{ corrupt", encoding="utf-8")

    assert plugin._load_state() == {}
    assert primary.read_text(encoding="utf-8") == "{ corrupt"

    plugin._sessions = {}
    plugin._save_state()
    assert json.loads(primary.read_text(encoding="utf-8")) == {}
    retained = list(primary.parent.glob("idle_state.json.corrupt-*"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "{ corrupt"


def test_plugin_backup_failure_does_not_roll_back_valid_primary(
    plugin, tmp_path, monkeypatch
):
    primary = tmp_path / "idle_state.json"
    backup = tmp_path / "idle_state.json.bak"
    backup.write_text(json.dumps({"old": "backup"}), encoding="utf-8")
    original_backup = backup.read_text(encoding="utf-8")

    real_replace = os.replace
    calls = 0

    def fail_second_replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected backup replace failure")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    plugin._sessions = {"new": "primary"}
    plugin._save_state()

    assert json.loads(primary.read_text(encoding="utf-8")) == {"new": "primary"}
    assert backup.read_text(encoding="utf-8") == original_backup
    assert calls == 2
    # The failed backup temporary was cleaned up.
    assert not list(tmp_path.glob(".idle_state.json.bak.*.tmp"))
