"""Crash-safe JSON persistence helpers for plugin state files.

The helpers deliberately keep the destination and temporary file in the same
directory so ``os.replace`` remains an atomic rename on the same filesystem.
No function in this module replaces a malformed primary state with an invented
empty state: recovery is only allowed from an independently parsed backup, and
the corrupt primary is retained first.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


class StateStorageError(RuntimeError):
    """Raised when state cannot be read or safely persisted."""


class CorruptStateError(StateStorageError):
    """Raised when the primary state is malformed and no safe backup exists."""


class StateWriteCommittedError(OSError):
    """Raised when replacement committed but its directory sync failed."""

    committed = True


def read_json(path: str | os.PathLike[str]) -> Any:
    """Read a UTF-8 JSON document, accepting a UTF-8 BOM used by older files."""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json_atomic(
    path: str | os.PathLike[str], data: Any, *, fsync_directory: bool = True
) -> None:
    """Atomically replace *path* with a JSON representation of *data*.

    A same-directory temporary file is used so a crash during serialization or
    fsync cannot truncate the existing destination.  Every failure path removes
    the temporary file; ``os.replace`` is only called after a successful flush
    and fsync.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        if fsync_directory:
            try:
                _fsync_directory(target.parent)
            except OSError as exc:
                # The rename has happened. Make the uncertain durability
                # explicit so callers do not mistake this for a rolled-back
                # write and attempt an empty-state fallback.
                raise StateWriteCommittedError(
                    exc.errno, f"state replaced but directory sync failed: {target}"
                ) from exc
    except BaseException:
        # BaseException is intentional: interrupting serialization must also
        # clean the temporary file and leave the previous destination intact.
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Best-effort cleanup; the original state is still untouched
            # because replace has not happened or failed.
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        # Windows does not provide a portable way to fsync an open directory.
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {
                errno.EINVAL,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
                errno.ENOSYS,
            }:
                raise
    finally:
        os.close(fd)


def _retain_corrupt_state(primary: Path) -> Path:
    """Copy a malformed primary aside before backup recovery may proceed."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidates = [
        primary.with_name(f"{primary.name}.corrupt-{stamp}-{os.getpid()}"),
        primary.with_name(f"{primary.name}.corrupt-{stamp}-{os.getpid()}-1"),
        primary.with_name(f"{primary.name}.corrupt-{stamp}-{os.getpid()}-2"),
    ]
    retained: Path | None = None
    for candidate in candidates:
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            with primary.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        retained = candidate
        break

    if retained is None:
        raise CorruptStateError(f"cannot retain corrupt state file {primary}")

    # Keep only the five most recent corruption snapshots to bound disk usage.
    snapshots = sorted(
        (item for item in primary.parent.glob(primary.name + ".corrupt-*")),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
    )
    for stale in snapshots[:-5]:
        try:
            stale.unlink()
        except OSError:
            pass
    return retained


def load_json_state(
    primary_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> tuple[dict[str, Any], Path | None]:
    """Load state, recovering from a validated backup only when it is safe.

    A malformed primary is never overwritten in memory by an invented empty
    state.  Recovery requires a backup that parses as a JSON object; the raw
    primary bytes are copied aside first.  If no valid backup exists,
    ``CorruptStateError`` is raised and callers must disable state writes.
    """
    primary = Path(primary_path)
    backup = Path(backup_path)

    try:
        data = read_json(primary)
    except FileNotFoundError:
        if not backup.is_file():
            return {}, None
        try:
            recovered = read_json(backup)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptStateError(
                f"primary state is missing and backup is malformed: {primary}"
            ) from exc
        if not isinstance(recovered, dict):
            raise CorruptStateError(
                f"primary state is missing and backup is not a JSON object: {backup}"
            ) from None
        return recovered, None
    except (OSError, UnicodeError, json.JSONDecodeError):
        if not backup.is_file():
            raise CorruptStateError(f"state file is malformed: {primary}") from None

        try:
            recovered = read_json(backup)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptStateError(
                f"state file and backup are malformed: {primary}"
            ) from exc
        if not isinstance(recovered, dict):
            raise CorruptStateError(
                f"state backup is not a JSON object: {backup}"
            ) from None

        retained = _retain_corrupt_state(primary)
        return recovered, retained

    if not isinstance(data, dict):
        # A JSON scalar/array is not a valid session state.  Preserve it and
        # only recover when an independently validated backup exists.
        if not backup.is_file():
            raise CorruptStateError(f"state file is not a JSON object: {primary}")

        try:
            recovered = read_json(backup)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptStateError(
                f"state file is invalid and backup is malformed: {primary}"
            ) from exc
        if not isinstance(recovered, dict):
            raise CorruptStateError(
                f"state backup is not a JSON object: {backup}"
            ) from None

        retained = _retain_corrupt_state(primary)
        return recovered, retained

    return data, None


def save_json_state(
    primary_path: str | os.PathLike[str], data: dict[str, Any]
) -> tuple[Path, Path | None]:
    """Persist state atomically and maintain a same-format validated backup.

    The primary write happens first.  A backup failure never invalidates the
    new primary, but the old backup remains available for corruption recovery.
    """
    primary = Path(primary_path)
    write_json_atomic(primary, data)

    backup = primary.with_name(primary.name + ".bak")
    backup_error: OSError | None = None
    try:
        write_json_atomic(backup, data)
    except OSError as exc:
        backup_error = exc

    return primary, None if backup_error is not None else backup
