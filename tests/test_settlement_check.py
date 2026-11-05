"""Tests for pm/ops/settlement_check.py.

Contract: scope defaults to tickers OUR system placed (never a shared
account's manual trades); anything placed but not yet in the settlements feed
is reported PENDING; realized total sums only settled rows.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from pm.ops import settlement_check as sc


def _settlements():
    return pd.DataFrame([
        {"ticker": "KXA-1", "market_result": "yes", "revenue_cents": 100,
         "settled_time": "2026-09-10T06:30:00Z"},
        {"ticker": "KXB-2", "market_result": "no", "revenue_cents": 0,
         "settled_time": "2026-09-10T06:30:00Z"},
        {"ticker": "KXMANUAL-9", "market_result": "yes", "revenue_cents": 500,
         "settled_time": "2026-09-10T06:30:00Z"},
    ])


class TestCheck:
    def test_splits_settled_from_pending_and_sums_revenue(self):
        with patch("pm.venue.kalshi_client.get_settlements", return_value=_settlements()):
            r = sc.check({"KXA-1", "KXB-2", "KXPEND-3"})
        assert [x["ticker"] for x in r["settled"]] == ["KXA-1", "KXB-2"]
        assert r["pending"] == ["KXPEND-3"]
        assert r["realized_cents"] == 100

    def test_never_reports_tickers_we_did_not_place(self):
        """A shared live account carries the user's own manual trades."""
        with patch("pm.venue.kalshi_client.get_settlements", return_value=_settlements()):
            r = sc.check({"KXA-1"})
        assert [x["ticker"] for x in r["settled"]] == ["KXA-1"]
        assert "KXMANUAL-9" not in r["pending"]

    def test_no_targets_is_not_an_error(self):
        assert sc.check(set()) == {"settled": [], "pending": [], "realized_cents": 0}

    def test_empty_settlements_feed_means_all_pending(self):
        with patch("pm.venue.kalshi_client.get_settlements", return_value=pd.DataFrame()):
            r = sc.check({"KXA-1"})
        assert r["pending"] == ["KXA-1"] and r["settled"] == []


class TestFormat:
    def test_report_names_result_and_payout(self):
        with patch("pm.venue.kalshi_client.get_settlements", return_value=_settlements()):
            r = sc.check({"KXA-1", "KXPEND-3"})
        out = sc.format_report(r)
        assert "KXA-1" in out and "yes" in out and "$1.00" in out
        assert "pending" in out and "KXPEND-3" in out


class TestWaitMode:
    def test_polls_until_settled_then_stops(self, monkeypatch):
        """--wait-hours must re-check rather than trust a guessed timer."""
        calls = {"n": 0}

        def fake_check(targets, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"settled": [], "pending": ["KXA-1"], "realized_cents": 0}
            return {"settled": [{"ticker": "KXA-1", "result": "yes",
                                 "revenue_cents": 100, "settled_time": "t"}],
                    "pending": [], "realized_cents": 100}

        # main() assigns POLYMARKET_ACTIVE_PROFILE directly into os.environ.
        # Claim it through monkeypatch FIRST so the original is restored on
        # teardown — without this the profile leaks and breaks the sandbox
        # tests in test_shared.py later in the run.
        monkeypatch.setenv("POLYMARKET_ACTIVE_PROFILE", "test-profile")
        monkeypatch.setattr(sc, "check", fake_check)
        monkeypatch.setattr("pm.core.shutdown.sleep", lambda s: False)
        monkeypatch.setattr("pm.core.shutdown.install", lambda: None)
        monkeypatch.setattr("pm.venue.kalshi_auth.is_configured", lambda: True)
        rc = sc.main(["--ticker", "KXA-1", "--wait-hours", "1", "--poll-minutes", "0.01"])
        assert rc == 0
        assert calls["n"] == 3, "should have re-checked until settled"
