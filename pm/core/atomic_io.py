"""Crash-safe, concurrency-safe JSON persistence for the per-profile ledgers.

Why this module exists
----------------------
Every profile-scoped store (``portfolio.json``, ``forecasts.json``,
``alerts.json``, ``settings.json``, ``position_meta.json``) used to be written
with the shortest thing that works::

    with open(path, "w") as f:        # <-- truncates the file IMMEDIATELY
        json.dump(data, f, indent=2)  # <-- then streams up to 3.8 MB into it

That has two defects, and this project hits both:

1. **Torn writes.** ``open(path, "w")`` truncates before a single byte is
   written. A crash, a full disk, or a SIGKILL mid-``json.dump`` leaves a
   truncated file. The read path then raised ``JSONDecodeError``, and because
   the arms run under ``Restart=on-failure`` the result was a restart loop
   against permanently corrupt state — the ledger being simultaneously the
   record of what we own and the input to reconciliation against the real
   account.

2. **Lost updates.** Every mutation is a read-modify-write. Two writers
   interleaving (``snapshot_runner`` walks *every* profile 4x/day while each
   arm daemon writes its own profile on a 15-minute cycle, and the UI writes on
   user action) silently drops one side's changes even if neither write tears.

The fix is the standard pair, and both halves are needed — atomicity alone does
not prevent lost updates, and locking alone does not prevent torn files:

* **Atomic publish** — write a sibling temp file, ``fsync`` it, then
  ``os.replace()`` onto the target. ``os.replace`` is atomic on POSIX, so a
  concurrent reader observes either the complete old file or the complete new
  one, never a partial one. The directory is fsynced too, so the rename itself
  survives a power loss.
* **An advisory lock around the whole read-modify-write** — see :func:`mutate`,
  which is the API every mutation site should use.

A ``.bak`` of the previous contents is kept as a recovery path for the case
atomicity cannot help with: a bug that writes well-formed but wrong JSON.

Scope note: SQLite stores are already crash-safe via WAL and are not this
module's problem. This is for the plain-JSON ledgers only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # POSIX advisory locking. Absent on Windows; we degrade to no locking.
    import fcntl
except ImportError:  # pragma: no cover - project targets Linux
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: How long :func:`locked` waits for a competing writer before giving up.
DEFAULT_LOCK_TIMEOUT = 10.0


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def backup_path(path: Path) -> Path:
    """Sibling holding the previous contents of ``path``.

    >>> backup_path(Path("/tmp/portfolio.json")).name
    'portfolio.json.bak'
    """
    return path.with_name(path.name + ".bak")


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive advisory lock covering ``path``.

    The lock is taken on a separate ``<name>.lock`` file rather than on the
    ledger itself, because the ledger is replaced by rename on every write —
    a descriptor held on the old inode would stop excluding anyone the moment
    the rename lands.

    Blocks up to ``timeout`` seconds, then proceeds unlocked with a warning:
    a slow lock must not be able to stop an arm from recording a fill it has
    already placed. Losing an update is bad; failing to record a real position
    is worse.
    """
    if fcntl is None:  # pragma: no cover - project targets Linux
        yield
        return

    lp = _lock_path(path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning(
                        "atomic_io: timed out after %.1fs waiting for the lock on %s; "
                        "proceeding UNLOCKED (a concurrent write may be lost)", timeout, path)
                    break
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from ``path``, tolerating a missing or damaged file.

    Resolution order: the file, then its ``.bak``, then ``default``. A damaged
    primary is preserved as ``<name>.corrupt-<epoch>`` rather than discarded —
    the operator will want it, and it is the only evidence of what happened.

    Never raises for content reasons: callers are daemons whose job is to keep
    running. ``default`` is deep-copied per call by the caller if it is
    mutable; this function returns it as given.
    """
    if not path.exists():
        return default

    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        log.error("atomic_io: %s is unreadable (%s); attempting .bak recovery", path, e)

    quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    with contextlib.suppress(OSError):
        os.replace(path, quarantine)
        log.error("atomic_io: preserved the damaged file as %s", quarantine)

    bak = backup_path(path)
    if bak.exists():
        try:
            with open(bak) as f:
                data = json.load(f)
            log.warning("atomic_io: recovered %s from its .bak", path)
            return data
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            log.error("atomic_io: .bak for %s is also unreadable (%s)", path, e)

    log.error("atomic_io: no usable copy of %s; falling back to the empty default. "
              "The damaged file is retained for inspection.", path)
    return default


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Publish ``data`` to ``path`` atomically and durably.

    Temp file in the same directory (so the rename stays within one
    filesystem), flushed and fsynced, then ``os.replace``d onto the target.
    The previous contents are retained as ``.bak`` first.

    Serialization happens BEFORE anything on disk is touched, so a value that
    cannot be serialized raises without having disturbed the existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent)  # may raise — deliberately, first

    if path.exists():
        with contextlib.suppress(OSError):
            _copy_file(path, backup_path(path))

    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # fdopen takes ownership of fd; closing the wrapper closes the fd.
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())          # the bytes are on the platter...
        os.replace(tmp, path)             # ...before the name points at them
        _fsync_dir(path.parent)           # ...and the rename is durable too
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def mutate(path: Path, default: Any = None, *,
           timeout: float = DEFAULT_LOCK_TIMEOUT, indent: int = 2) -> Iterator[Any]:
    """Lock, read, yield for mutation, then publish atomically.

    **This is the API every mutation site should use.** Using
    :func:`read_json` and :func:`write_json` separately reintroduces the
    lost-update race this module exists to close, because another writer can
    land in between.

    The write happens only on clean exit — raising inside the block leaves the
    store exactly as it was::

        with atomic_io.mutate(path, {"transactions": []}) as store:
            store["transactions"].append(txn)
    """
    with locked(path, timeout=timeout):
        data = read_json(path, default)
        yield data
        write_json(path, data, indent=indent)


def _copy_file(src: Path, dst: Path) -> None:
    with open(src, "rb") as fsrc:
        payload = fsrc.read()
    tmp = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    with open(tmp, "wb") as fdst:
        fdst.write(payload)
        fdst.flush()
        os.fsync(fdst.fileno())
    os.replace(tmp, dst)


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename survives power loss. Best-effort: some
    filesystems refuse O_RDONLY fsync on directories, and that is not a reason
    to fail a write that already landed."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


__all__ = ["locked", "mutate", "read_json", "write_json", "backup_path",
           "DEFAULT_LOCK_TIMEOUT"]
