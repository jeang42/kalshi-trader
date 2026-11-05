"""Offline tests for news_capture pure logic + store (temp DB, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pm.capture import news_capture as nc

NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_CAPTURE_DB", str(tmp_path / "news.sqlite"))
    nc.migrate()
    return tmp_path


class TestQueryDerivation:
    def test_strips_will_and_question(self):
        assert nc.news_query_for("Will Trump impose new tariffs?") == \
            "Trump impose new tariffs"

    def test_drops_trailing_deadline(self):
        q = nc.news_query_for("Will the Fed cut rates by July 2026?")
        assert "Fed cut rates" in q and "2026" not in q

    def test_short_or_empty(self):
        assert nc.news_query_for("") == ""


class TestPublishedParsing:
    def test_rfc822(self):
        dt = nc.parse_published("Wed, 17 Jun 2026 18:01:00 +0000")
        assert dt is not None and dt.year == 2026 and dt.tzinfo is not None

    def test_garbage(self):
        assert nc.parse_published("not a date") is None
        assert nc.parse_published(None) is None


class TestFreshness:
    def test_fresh_within_window(self):
        assert nc.is_fresh(NOW - timedelta(hours=2), NOW, 6.0) is True

    def test_stale_excluded(self):
        assert nc.is_fresh(NOW - timedelta(hours=20), NOW, 6.0) is False

    def test_future_excluded(self):
        assert nc.is_fresh(NOW + timedelta(hours=1), NOW, 6.0) is False

    def test_none_excluded(self):
        assert nc.is_fresh(None, NOW, 6.0) is False


class TestStore:
    def _row(self, ticker, url):
        return {"ticker": ticker, "news_url": url, "question": "q",
                "category": "Politics", "news_title": "t", "news_source": "s",
                "news_published": NOW.isoformat(), "captured_at": NOW.isoformat(),
                "lag_minutes": 30.0, "yes_bid": 0.4, "yes_ask": 0.44,
                "yes_mid": 0.42, "last_price": 0.41, "volume": 5000}

    def test_dedup_on_ticker_url(self, tmpdb):
        assert nc.store_events([self._row("KX-1", "http://a")]) == 1
        assert nc.store_events([self._row("KX-1", "http://a")]) == 0  # dup
        assert nc.store_events([self._row("KX-1", "http://b")]) == 1  # new url
        assert nc.store_events([self._row("KX-2", "http://a")]) == 1  # new ticker
        s = nc.stats()
        assert s["total"] == 3 and s["markets"] == 2

    def test_yes_mid_fallbacks(self):
        assert nc._yes_mid(0.40, 0.44, 0.30) == pytest.approx(0.42)
        assert nc._yes_mid(None, None, 0.31) == pytest.approx(0.31)
