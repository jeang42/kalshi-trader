"""Tests for the persistent LLM cache (SQLite K/V with TTL)."""

from __future__ import annotations

import time

import pytest

from pm.feeds import llm_cache

# ---------------------------------------------------------------------------
# Fixture: redirect the cache file to a tempdir via the env-var override
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    p = tmp_path / "test_llm_cache.sqlite"
    monkeypatch.setenv("LLM_CACHE_PATH", str(p))
    return p


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------

class TestMakeKey:
    def test_stable(self):
        a = llm_cache.make_key("forecast/v1", "model-x", "Q1?")
        b = llm_cache.make_key("forecast/v1", "model-x", "Q1?")
        assert a == b

    def test_parts_matter(self):
        a = llm_cache.make_key("forecast/v1", "model-x", "Q1?")
        b = llm_cache.make_key("forecast/v1", "model-x", "Q2?")
        assert a != b

    def test_version_separates(self):
        a = llm_cache.make_key("forecast/v1", "m", "Q?")
        b = llm_cache.make_key("forecast/v2", "m", "Q?")
        assert a != b


# ---------------------------------------------------------------------------
# get / set / clear
# ---------------------------------------------------------------------------

class TestGetSet:
    def test_roundtrip(self, cache_path):
        llm_cache.set("k1", {"hello": "world", "n": 42})
        assert llm_cache.get("k1") == {"hello": "world", "n": 42}

    def test_miss_returns_none(self, cache_path):
        assert llm_cache.get("not-here") is None

    def test_overwrite_existing(self, cache_path):
        llm_cache.set("k1", {"v": 1})
        llm_cache.set("k1", {"v": 2})
        assert llm_cache.get("k1") == {"v": 2}

    def test_clear_wipes_everything(self, cache_path):
        llm_cache.set("k1", {"v": 1})
        llm_cache.set("k2", {"v": 2})
        n = llm_cache.clear()
        assert n == 2
        assert llm_cache.get("k1") is None
        assert llm_cache.get("k2") is None

    def test_non_serializable_value_is_dropped_quietly(self, cache_path):
        # Lambda is not JSON-serializable; set() logs a warning and skips.
        llm_cache.set("k1", {"f": lambda x: x})  # type: ignore[dict-item]
        assert llm_cache.get("k1") is None


# ---------------------------------------------------------------------------
# TTL + expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_ttl_none_never_expires(self, cache_path):
        llm_cache.set("k1", {"v": 1}, ttl_seconds=None)
        # Even after a small wait — and with purge_expired called — the
        # entry survives.
        time.sleep(0.05)
        llm_cache.purge_expired()
        assert llm_cache.get("k1") == {"v": 1}

    def test_expired_entry_returns_none(self, cache_path, monkeypatch):
        llm_cache.set("k1", {"v": 1}, ttl_seconds=10)
        # Pretend it's now 30s in the future — entry should be considered
        # expired by get() without being deleted yet.
        future = time.time() + 30
        monkeypatch.setattr(time, "time", lambda: future)
        assert llm_cache.get("k1") is None

    def test_purge_expired_removes_only_expired(self, cache_path, monkeypatch):
        llm_cache.set("k_expired", {"v": 1}, ttl_seconds=10)
        llm_cache.set("k_live",    {"v": 2}, ttl_seconds=10_000)
        llm_cache.set("k_forever", {"v": 3}, ttl_seconds=None)

        future = time.time() + 30
        monkeypatch.setattr(time, "time", lambda: future)
        n = llm_cache.purge_expired()
        assert n == 1
        # The live one and the forever one still get served (using the
        # mocked time, both are still in the future).
        assert llm_cache.get("k_live") == {"v": 2}
        assert llm_cache.get("k_forever") == {"v": 3}
        # And the expired key is genuinely gone (purged, not just hidden).
        assert llm_cache.get("k_expired") is None


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_empty(self, cache_path):
        s = llm_cache.stats()
        assert s["entries"] == 0
        assert s["expired"] == 0
        assert s["oldest_age_s"] is None
        assert s["newest_age_s"] is None

    def test_counts_after_inserts(self, cache_path):
        llm_cache.set("k1", {"v": 1})
        llm_cache.set("k2", {"v": 2})
        s = llm_cache.stats()
        assert s["entries"] == 2
        assert s["expired"] == 0
        assert s["oldest_age_s"] is not None
        assert s["newest_age_s"] is not None
        assert s["file_bytes"] > 0

    def test_expired_count_separate_from_total(self, cache_path, monkeypatch):
        llm_cache.set("k_expired", {"v": 1}, ttl_seconds=10)
        llm_cache.set("k_live",    {"v": 2}, ttl_seconds=10_000)

        future = time.time() + 30
        monkeypatch.setattr(time, "time", lambda: future)
        s = llm_cache.stats()
        assert s["entries"] == 2
        assert s["expired"] == 1


# ---------------------------------------------------------------------------
# File creation
# ---------------------------------------------------------------------------

class TestFileCreation:
    def test_parent_dir_created_on_first_write(self, tmp_path, monkeypatch):
        # Point at a non-existent deeply-nested path; first set() should
        # mkdir parents.
        path = tmp_path / "nested" / "deep" / "cache.sqlite"
        monkeypatch.setenv("LLM_CACHE_PATH", str(path))
        llm_cache.set("k1", {"v": 1})
        assert path.exists()
        assert llm_cache.get("k1") == {"v": 1}
