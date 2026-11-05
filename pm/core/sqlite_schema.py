"""Schema versioning for the arm and capture SQLite stores (P2 / R-19).

The three sidecars (``baseball``, ``wnba``, ``tennis``) version their state
databases with ``PRAGMA user_version`` and an idempotent ``migrate()``. The
arm and capture stores at the root did not: each ``_con()`` ran
``CREATE TABLE IF NOT EXISTS`` plus a try/except ``ALTER TABLE ADD COLUMN``
loop, so a store's migration generation was unknowable without reading
source, and nothing could ever REMOVE or TIGHTEN anything. This module is
the one place that changes.

Contract
--------
A store declares an ordered list of :class:`Migration` — version 1, 2, 3 … —
and calls :func:`ensure_schema` on every connection it opens (cheap: one
``PRAGMA user_version`` read when nothing is pending). Migration ``n`` takes
a database at ``user_version = n-1`` to ``n``, runs inside its own
``BEGIN IMMEDIATE`` transaction, and stamps the version in the same
transaction — so a crash mid-migration leaves the file at ``n-1`` with the
old schema, never half-way.

* **Version 1 is the adoption step** and must be idempotent for BOTH a
  fresh file and a legacy file whose tables already exist at
  ``user_version = 0`` (every store on disk today). In practice: the
  store's existing ``CREATE TABLE IF NOT EXISTS`` + ``add_column_if_missing``
  calls, verbatim.
* A migration that rebuilds a table (the only way SQLite can add ``CHECK`` /
  ``NOT NULL`` to an existing table) sets ``backup=True``; the previous file
  is copied to ``<db>.pre-v<n>.bak`` via the online backup API before the
  transaction opens. Refused above :data:`BACKUP_MAX_BYTES` — a rebuild that
  large should not be a ``_con()`` side effect.
* **Fails closed.** A file whose ``user_version`` is HIGHER than the code
  knows raises :class:`SchemaError` rather than proceed on a schema written
  by newer code; a failing migration rolls back and raises. Preserve that
  direction — on a store that records money, not knowing is not fine.

Helpers :func:`add_column_if_missing` and :func:`rebuild_table` are the two
operations every migration so far has needed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Above this a table rebuild is an operation you schedule, not something a
# daemon does on the way to its next cycle. Deliberately well below the
# smallest capture store that would need it (near_close_capture, ~44 MB).
BACKUP_MAX_BYTES = 64 * 1024 * 1024


def set_wal(conn: sqlite3.Connection, *, attempts: int = 20,
            wait_s: float = 0.1) -> str:
    """Switch ``conn`` to WAL, retrying transient SQLITE_BUSY.

    Why this exists (R-27 found it): the DELETE→WAL conversion on a FRESH
    database takes an exclusive lock that the sqlite3 busy handler does NOT
    cover — six processes racing ``PRAGMA journal_mode=WAL`` on a new file
    reproduced ``database is locked`` in ~2/3 of trials, while
    ``ensure_schema``'s own BEGIN IMMEDIATE path survived 30/30. On an
    already-WAL file the pragma is a cheap read, so the race window is first
    creation only — but every store's ``_con()`` runs this pragma, and a
    capture daemon and the control plane do open the same store.

    Degrades rather than raises: after ``attempts`` the store runs in its
    current (delete) journal mode this session — slower under concurrency,
    not broken — and the next opener converts it. Returns the final mode.
    """
    import time
    for i in range(attempts):
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            return str(row[0]) if row else "unknown"
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == attempts - 1:
                log.warning("set_wal: giving up after %d attempts (%s); "
                            "continuing in the current journal mode", i + 1, e)
                break
        time.sleep(wait_s)
    with contextlib.suppress(sqlite3.Error):
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]) if row else "unknown"
    return "unknown"


class SchemaError(RuntimeError):
    """A store's schema could not be brought to the version this code needs."""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: str | Sequence[str] | Callable[[sqlite3.Connection], None]
    backup: bool = False   # copy the file aside first (table rebuilds)


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names of ``table`` in declared order; ``[]`` if it doesn't exist."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def add_column_if_missing(conn: sqlite3.Connection, table: str,
                          column: str, decl: str) -> bool:
    """``ALTER TABLE ADD COLUMN`` guarded by ``PRAGMA table_info`` — the
    idempotent form the stores used to spell as try/except OperationalError.
    Returns True if the column was added."""
    if column in table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def rebuild_table(conn: sqlite3.Connection, table: str, new_ddl: str, *,
                  post_sql: Sequence[str] = ()) -> int:
    """Rebuild ``table`` under ``new_ddl`` (which must create a table named
    ``{table}__new``), copying every column the two shapes share, then swap.
    The only way to add ``CHECK`` / ``NOT NULL`` constraints in SQLite. Runs
    ``post_sql`` (indexes, etc.) after the rename. Returns rows copied.
    Caller supplies the transaction (``ensure_schema`` does)."""
    tmp = f"{table}__new"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.execute(new_ddl)
    if tmp not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        raise SchemaError(f"rebuild_table: new_ddl did not create {tmp}")
    old_cols = table_columns(conn, table)
    new_cols = table_columns(conn, tmp)
    shared = [c for c in new_cols if c in old_cols]
    n = 0
    if old_cols:
        cols = ", ".join(shared)
        cur = conn.execute(f"INSERT INTO {tmp} ({cols}) SELECT {cols} FROM {table}")
        n = int(cur.rowcount)
        conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    for stmt in post_sql:
        conn.execute(stmt)
    return n


def _backup_file(conn: sqlite3.Connection, dest: str) -> None:
    """Consistent copy of the open database via the online backup API."""
    with sqlite3.connect(dest) as out:
        conn.backup(out)
    out.close()


def ensure_schema(conn: sqlite3.Connection, *, store: str,
                  migrations: Sequence[Migration],
                  db_path: str | os.PathLike[str] | None = None) -> int:
    """Bring ``conn`` to the highest declared version. Returns that version.

    ``db_path`` is where a pre-rebuild backup goes (``<db_path>.pre-v<n>.bak``);
    in-memory or path-less connections skip the backup with a warning rather
    than refuse — tests use ``:memory:``.
    """
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaError(f"{store}: migrations must be numbered 1..n, got {versions}")
    target = len(versions)
    current = user_version(conn)
    if current > target:
        raise SchemaError(
            f"{store}: database is at schema v{current} but this code knows only "
            f"v{target} — written by newer code; refusing to touch it")
    if current == target:
        return current
    for m in migrations[current:]:
        if m.backup:
            path = str(db_path) if db_path is not None else None
            if not path or path == ":memory:" or not os.path.exists(path):
                log.warning("%s: v%d rebuild without a file backup (no path)",
                            store, m.version)
            else:
                size = os.path.getsize(path)
                if size > BACKUP_MAX_BYTES:
                    raise SchemaError(
                        f"{store}: v{m.version} rebuilds a {size / 1e6:.0f} MB "
                        f"file; above the {BACKUP_MAX_BYTES / 1e6:.0f} MB limit "
                        "for an in-daemon rebuild — run it deliberately")
                dest = f"{path}.pre-v{m.version}.bak"
                _backup_file(conn, dest)
                log.info("%s: backed up to %s before v%d", store, dest, m.version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Re-check under the write lock: another process (an arm and the
            # control plane both open the same store) may have applied this
            # step between our unlocked read and here.
            if user_version(conn) >= m.version:
                conn.execute("COMMIT")
                continue
            if callable(m.apply):
                m.apply(conn)
            elif isinstance(m.apply, str):
                conn.execute(m.apply)      # NOT executescript: it implicitly COMMITs first
            else:
                for stmt in m.apply:
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(m.version)}")
            conn.execute("COMMIT")
        except Exception as e:  # noqa: BLE001 — every failure must roll back + surface
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise SchemaError(
                f"{store}: migration v{m.version} ({m.description}) failed: {e}") from e
        log.info("%s: schema v%d applied — %s", store, m.version, m.description)
    return target
