"""SQLite key/value cache for LLM forecasts and match verdicts.

Why a cache:
- ``llm_data.get_forecast`` and ``llm_matcher.verify_match`` each take
  0.5–1.5s on 7-13B local models, several seconds on 30B+.
- The Edges-page workflow involves re-tuning thresholds (min_gap,
  min_similarity, min_volume, ...) and re-running scans. Without
  caching, every tweak re-pays the full inference cost on questions
  that haven't changed.
- LLM responses are *deterministic enough* under low temperature
  (0.1–0.2) that a cached answer for `(question, model, key params)`
  is a reasonable proxy for re-asking.

Cache contract:
- Keys: SHA-256 of input parts joined by ``"|"``. Callers compose the
  parts to match what affects the response. Include a schema version
  prefix (e.g. ``"forecast/v1"``) so a future prompt revision can
  invalidate cleanly by bumping the version string.
- Values: any JSON-serializable dict — typically ``dataclasses.asdict``
  of an LLMForecast / MatchVerdict.
- TTL: per-entry, optional. ``None`` means "until purged."
- Storage: ``data/shared/llm_cache.sqlite``. Shared across profiles
  because the (question → LLM answer) mapping doesn't depend on which
  user is asking. Path override via ``LLM_CACHE_PATH``.

Thread safety: SQLite serialises writes via its default isolation
level; Streamlit pages run single-threaded per session. We use a fresh
connection per call rather than a module-level long-lived one because
the path can change at runtime via env-var override.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from pm.core import sqlite_schema

log = logging.getLogger(__name__)


DEFAULT_CACHE_PATH = Path("data/shared/llm_cache.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_expires_at ON cache(expires_at);
"""

# Versioned via sqlite_schema (P2/R-19): v1 adopts the shape above verbatim
# (every statement is IF NOT EXISTS, so it is the same for a fresh file and
# a legacy one at user_version 0). Future changes go on this list.
SCHEMA = [sqlite_schema.Migration(
    1, "adopt pre-versioning tables",
    [stmt.strip() for stmt in _SCHEMA.split(";") if stmt.strip()])]



# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _path() -> Path:
    """Resolved cache path. Env var ``LLM_CACHE_PATH`` overrides default."""
    return Path(os.getenv("LLM_CACHE_PATH", str(DEFAULT_CACHE_PATH)))


def _connect() -> sqlite3.Connection:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    sqlite_schema.ensure_schema(conn, store="llm_cache", migrations=SCHEMA, db_path=p)
    return conn


# ---------------------------------------------------------------------------
# Key building
# ---------------------------------------------------------------------------

def make_key(*parts: str) -> str:
    """SHA-256 over ``"|".join(parts)``. Callers compose the parts so
    every input that could affect the cached response is reflected in
    the key.

    The first part should be a schema version like ``"forecast/v1"`` —
    bumping it invalidates every entry of that kind without touching
    the others.
    """
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def get(key: str) -> dict | None:
    """Return the value dict for ``key`` if present and not expired,
    else None. Expired entries are *not* deleted on miss — call
    :func:`purge_expired` periodically.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?", (key,),
            ).fetchone()
    except sqlite3.Error as e:
        log.warning("llm_cache.get failed: %s", e)
        return None
    if row is None:
        return None
    value_json, expires_at = row
    if expires_at is not None and expires_at < time.time():
        return None
    try:
        return json.loads(value_json)
    except json.JSONDecodeError:
        log.warning("llm_cache: stored value isn't valid JSON for key %s", key[:10])
        return None


def set(key: str, value: dict, *, ttl_seconds: int | None = None) -> None:
    """Insert-or-replace ``value`` for ``key``. ``ttl_seconds=None``
    means the entry never expires automatically.
    """
    now = int(time.time())
    expires_at = now + int(ttl_seconds) if ttl_seconds else None
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (key, json.dumps(value), now, expires_at),
            )
            conn.commit()
    except sqlite3.Error as e:
        log.warning("llm_cache.set failed: %s", e)
    except (TypeError, ValueError) as e:
        log.warning("llm_cache.set: value not JSON-serializable: %s", e)


def purge_expired() -> int:
    """Delete entries past their ``expires_at``. Returns the count
    removed. Cheap enough to call from a settings-page button."""
    now = int(time.time())
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            conn.commit()
            return int(cur.rowcount)
    except sqlite3.Error as e:
        log.warning("llm_cache.purge_expired failed: %s", e)
        return 0


def clear() -> int:
    """Delete every entry. Returns count cleared. The on-disk file
    stays — we re-create the table after."""
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM cache")
            conn.commit()
            return int(cur.rowcount)
    except sqlite3.Error as e:
        log.warning("llm_cache.clear failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Stats (for the Settings page)
# ---------------------------------------------------------------------------

def stats() -> dict:
    """Counts + extremes for the Settings page panel.

    Returns ``{"entries", "expired", "oldest_age_s", "newest_age_s",
    "file_bytes"}``. Empty cache → zeros and ``None`` ages.
    """
    out: dict = {
        "entries": 0,
        "expired": 0,
        "oldest_age_s": None,
        "newest_age_s": None,
        "file_bytes": 0,
    }
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM cache",
            ).fetchone()
            out["entries"] = int(row[0])
            now = int(time.time())
            if row[1] is not None:
                out["oldest_age_s"] = now - int(row[1])
            if row[2] is not None:
                out["newest_age_s"] = now - int(row[2])
            expired_row = conn.execute(
                "SELECT COUNT(*) FROM cache "
                "WHERE expires_at IS NOT NULL AND expires_at < ?", (now,),
            ).fetchone()
            out["expired"] = int(expired_row[0])
    except sqlite3.Error as e:
        log.warning("llm_cache.stats failed: %s", e)

    p = _path()
    if p.exists():
        try:
            out["file_bytes"] = p.stat().st_size
        except OSError:
            pass
    return out


__all__ = [
    "DEFAULT_CACHE_PATH",
    "make_key",
    "get",
    "set",
    "purge_expired",
    "clear",
    "stats",
]
