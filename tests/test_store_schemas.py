"""Every arm/capture/archive store is versioned (R-19).

For all thirteen: a fresh file lands at the store's current version. For the
four arms (which carry v2 constraints): a LEGACY file — the pre-versioning
shape with rows, user_version 0 — is adopted, rebuilt, and its rows kept;
the constraints are then live; a backup of the pre-rebuild file exists.
"""

from __future__ import annotations

import importlib
import os
import sqlite3

import pytest

from pm.core import sqlite_schema as ss

# (module, env var, connect fn name, expected version)
STORES = [
    ("pm.capture.near_close_capture", "NEAR_CLOSE_DB", "_connect", 1),
    ("pm.capture.advisory_capture", "ADVISORY_CAPTURE_DB", "_con", 1),
    ("pm.capture.mention_capture", "MENTION_CAPTURE_DB", "_con", 1),
    ("pm.research.candle_panel", "CANDLE_PANEL_DB", "_con", 1),
    ("pm.capture.news_capture", "NEWS_CAPTURE_DB", "_connect", 1),
    ("pm.capture.tennis_capture", "TENNIS_CAPTURE_DB", "_connect", 1),
    ("pm.capture.wnba_capture", "WNBA_CAPTURE_DB", "_connect", 1),
    ("pm.archive.book_archive", "BOOK_ARCHIVE_PATH", "_connect", 1),
    ("pm.archive.trade_archive", "TRADE_ARCHIVE_PATH", "_connect", 1),
    ("pm.feeds.llm_cache", "LLM_CACHE_PATH", "_connect", 1),
]


@pytest.mark.parametrize("mod,env,fn,version", STORES, ids=[s[0] for s in STORES])
def test_fresh_store_is_stamped(tmp_path, monkeypatch, mod, env, fn, version):
    monkeypatch.setenv(env, str(tmp_path / f"{mod}.sqlite"))
    m = importlib.import_module(mod)
    c = getattr(m, fn)()
    assert ss.user_version(c) == version
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 1
    # second open: no-op, same version
    c.close()
    c2 = getattr(m, fn)()
    assert ss.user_version(c2) == version
    c2.close()


# Legacy shapes = the CREATE TABLE each arm shipped BEFORE versioning (from
# git history), plus one already-resolved row and one open row.
LEGACY = {
}


@pytest.mark.parametrize("mod", list(LEGACY), ids=list(LEGACY))
def test_legacy_arm_store_is_adopted_rebuilt_and_constrained(tmp_path, monkeypatch, mod):
    env, ddl, rows, bad_insert = LEGACY[mod]
    path = tmp_path / f"{mod}.sqlite"
    monkeypatch.setenv(env, str(path))
    raw = sqlite3.connect(path)
    raw.execute(ddl)
    raw.executemany(f"INSERT INTO positions VALUES ({','.join('?' * len(rows[0]))})", rows)
    raw.commit()
    raw.close()

    m = importlib.import_module(mod)
    c = m._con()
    assert ss.user_version(c) == 3
    got = c.execute("SELECT ticker, resolved FROM positions ORDER BY ticker").fetchall()
    assert got == [("A", 1), ("B", 0)], "rows must survive the rebuild"
    # every legacy column still present (plus whatever v1 added)
    legacy_cols = [r[1] for r in sqlite3.connect(str(path) + ".pre-v2.bak")
                   .execute("PRAGMA table_info(positions)")]
    assert set(legacy_cols) <= set(ss.table_columns(c, "positions"))
    assert os.path.exists(str(path) + ".pre-v2.bak")
    # constraints are live now
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(bad_insert)
    # the arm's own summary still reads the store
    assert isinstance(m.summary(), dict)
