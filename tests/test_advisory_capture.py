"""Tests for pm/capture/advisory_capture.py (the candidate-A instrument).

Contract from the spec (edge log §2026-08-24 late-3): advisory detection is
once-per-(storm, product, advNum) with baseline rows flagged; books store on
change + heartbeat, best-first regardless of venue ordering; watchlist takes
only ACTIVE storms' per-storm members; stop requests break loops before the
next network call; feed failures degrade quietly.
"""
from __future__ import annotations

import signal
from unittest.mock import patch

import pytest

from pm.capture import advisory_capture as ac
from pm.core import shutdown


@pytest.fixture(autouse=True)
def _fresh_shutdown():
    shutdown.reset()
    yield
    shutdown.reset()


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("ADVISORY_CAPTURE_DB", str(tmp_path / "ac.sqlite"))
    c = ac._con()
    yield c
    c.close()


def _nhc_payload(adv="005", fut="2026-08-24T14:33:13.480Z"):
    return {"activeStorms": [{
        "id": "ep092026", "name": "Iselle", "classification": "TS",
        "publicAdvisory": {"advNum": adv, "issuance": "2026-08-24T15:00:00.000Z",
                           "fileUpdateTime": fut, "url": "https://x/adv.shtml"},
    }]}


class TestAdvisoryDetection:
    def test_new_advisory_recorded_once(self, con):
        with patch.object(ac.circuit_breaker.get("nhc"), "call",
                          side_effect=[_nhc_payload(), "RAW TEXT",
                                       _nhc_payload()]):
            n1, named = ac.poll_advisories(con)
            n2, _ = ac.poll_advisories(con)
        assert n1 == 1 and n2 == 0
        assert [s.name for s in named] == ["Iselle"]
        row = con.execute("SELECT adv_num, baseline, raw_text FROM advisories").fetchone()
        assert row == ("005", 0, "RAW TEXT")

    def test_baseline_rows_flagged_and_skip_text_fetch(self, con):
        with patch.object(ac.circuit_breaker.get("nhc"), "call",
                          side_effect=[_nhc_payload()]) as call:
            n, _ = ac.poll_advisories(con, baseline=True)
        assert n == 1
        assert call.call_count == 1, "baseline load must not fetch product text"
        assert con.execute("SELECT baseline, raw_text FROM advisories").fetchone() == (1, None)

    def test_new_adv_num_on_same_storm_is_a_new_detection(self, con):
        with patch.object(ac.circuit_breaker.get("nhc"), "call",
                          side_effect=[_nhc_payload("005"), "T5",
                                       _nhc_payload("006"), "T6"]):
            ac.poll_advisories(con)
            n, _ = ac.poll_advisories(con)
        assert n == 1
        assert con.execute("SELECT COUNT(*) FROM advisories").fetchone()[0] == 2

    def test_nhc_failure_degrades_quietly(self, con):
        import requests
        with patch.object(ac.circuit_breaker.get("nhc"), "call",
                          side_effect=requests.ConnectionError("down")):
            assert ac.poll_advisories(con) == (0, [])


class TestHotWindow:
    def test_slot_windows(self):
        from datetime import UTC, datetime
        def t(h, m):
            return int(datetime(2026, 8, 24, h, m, tzinfo=UTC).timestamp())
        assert ac.in_hot_window(t(14, 20))       # 45min before the 15Z slot
        assert ac.in_hot_window(t(16, 25))       # 85min after
        assert not ac.in_hot_window(t(13, 0))
        assert not ac.in_hot_window(t(17, 0))

    def test_detection_forces_hot(self):
        now = 1_700_000_000
        assert ac.in_hot_window(now, last_detection=now - 100)
        assert not ac.in_hot_window(now, last_detection=now - ac.HOT_AFTER_S - 1) \
            or ac.in_hot_window(now)  # unless a slot window covers it anyway


class TestBooks:
    def test_top3_is_best_first_regardless_of_venue_order(self):
        bids = [{"price": "0.01", "size": "5"}, {"price": "0.12", "size": "7"},
                {"price": "0.08", "size": "3"}]
        assert ac._top3(bids, reverse=True)[0] == (0.12, 7.0)
        asks = [{"price": "0.9", "size": "1"}, {"price": "0.2", "size": "2"}]
        assert ac._top3(asks, reverse=False)[0] == (0.2, 2.0)

    def test_store_on_change_plus_heartbeat(self, con):
        book1 = {"bids": [{"price": "0.10", "size": "5"}], "asks": [{"price": "0.2", "size": "4"}]}
        book2 = {"bids": [{"price": "0.11", "size": "5"}], "asks": [{"price": "0.2", "size": "4"}]}
        last, hb = {}, {}
        with patch.object(ac.kc, "get_order_book", return_value=book1):
            n1 = ac.store_books(con, ["T1"], last, hb)
            n2 = ac.store_books(con, ["T1"], last, hb)   # unchanged, hb fresh
        with patch.object(ac.kc, "get_order_book", return_value=book2):
            n3 = ac.store_books(con, ["T1"], last, hb)   # changed
        assert (n1, n2, n3) == (1, 0, 1)
        rows = con.execute("SELECT bid1, heartbeat FROM books ORDER BY ts, bid1").fetchall()
        assert len(rows) == 2 and {r[0] for r in rows} == {0.10, 0.11}

    def test_book_failure_skips_ticker(self, con):
        with patch.object(ac.kc, "get_order_book", side_effect=RuntimeError("x")):
            assert ac.store_books(con, ["T1"], {}, {}) == 0


class TestWatchlist:
    def test_only_active_storm_members_from_per_storm_series(self, con):
        from pm.feeds.nhc_data import Storm
        named = [Storm(id="ep092026", name="Iselle", classification="TS", basin="EP")]

        def fake_paginate(path, params, **kw):
            s = params["series_ticker"]
            if s == "KXHURCAT":
                return [{"ticker": "KXHURCAT-26ISELLE-T3", "title": "i", "close_time": "c"},
                        {"ticker": "KXHURCAT-26YOLANDA-T3", "title": "y", "close_time": "c"}]
            if s == "KXTROPSTORM":
                return [{"ticker": "KXTROPSTORM-26DEC01-T20", "title": "t", "close_time": "c"}]
            return []
        with patch.object(ac.kc, "_paginate", side_effect=fake_paginate):
            tickers = ac.refresh_watchlist(con, named)
        assert "KXHURCAT-26ISELLE-T3" in tickers
        assert "KXHURCAT-26YOLANDA-T3" not in tickers, "inactive storm excluded"
        assert "KXTROPSTORM-26DEC01-T20" in tickers

    def test_series_failure_skips_that_series_only(self, con):
        def fake_paginate(path, params, **kw):
            if params["series_ticker"] == "KXTROPSTORM":
                raise RuntimeError("down")
            return []
        with patch.object(ac.kc, "_paginate", side_effect=fake_paginate):
            assert ac.refresh_watchlist(con, []) == []


class TestShutdown:
    def test_store_books_breaks_on_stop_without_touching_the_venue(self, con):
        shutdown._handler(signal.SIGTERM, None)
        with patch.object(ac.kc, "get_order_book",
                          side_effect=AssertionError("venue must not be called")):
            assert ac.store_books(con, ["T1", "T2"], {}, {}) == 0

    def test_resolve_breaks_on_stop(self, con):
        shutdown._handler(signal.SIGTERM, None)
        con.execute("INSERT INTO markets (ticker, series, close_time, first_seen) "
                    "VALUES ('T1','S','2020-01-01T00:00:00+00:00',1)")
        con.commit()
        with patch.object(ac.kc, "get_market",
                          side_effect=AssertionError("venue must not be called")):
            assert ac.resolve_cycle(con) == 0
