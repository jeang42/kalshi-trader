"""Orchestration tests for the near-close fade arm.

Its pure decision helpers were already covered. What was not was everything
around them — discovery, the correlation controls that decide what gets
booked, resolution, and the live-canary gate — which is where a defect
actually costs money. This is a live-capable arm; at review time it sat at 44%.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from pm.arms import fade_nearclose as fn


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("FADE_NEARCLOSE_DB", str(tmp_path / "fade.sqlite"))
    con = fn._con()
    yield con
    con.close()


def _settled(result: str) -> dict:
    """A settled-market payload in the venue's RESPONSE vocabulary."""
    return {"market": {"status": "finalized", "result": result}}


def _market(ticker="KXBTCD-1-T50", *, mid=0.15, bid=0.14, ask=0.16,
            volume=500, event="EV1", category="Crypto",
            close="2026-09-01T05:00:00Z"):
    return {
        "id": ticker, "condition_id": ticker, "event_id": event,
        "question": "Q?", "category": category,
        "best_bid": bid, "best_ask": ask, "yes_mid": mid,
        "volume": volume, "close_time": close, "last_trade_price": mid,
    }


class TestDiscovery:
    def test_computes_mid_from_top_of_book(self):
        df = pd.DataFrame([{"best_bid": 0.10, "best_ask": 0.20,
                            "volume": 1, "last_trade_price": 0.5}])
        with patch.object(fn.kc, "get_markets_closing_within", return_value=df):
            out = fn._discover()
        assert out.iloc[0]["yes_mid"] == pytest.approx(0.15)

    def test_falls_back_to_last_trade_when_a_side_is_missing(self):
        """A one-sided book is common near close; without the fallback those
        markets vanish from discovery entirely."""
        df = pd.DataFrame([{"best_bid": 0.0, "best_ask": 0.20,
                            "volume": 1, "last_trade_price": 0.17}])
        with patch.object(fn.kc, "get_markets_closing_within", return_value=df):
            out = fn._discover()
        assert out.iloc[0]["yes_mid"] == pytest.approx(0.17)

    def test_empty_feed_returns_empty_frame_not_none(self):
        with patch.object(fn.kc, "get_markets_closing_within",
                          return_value=pd.DataFrame()):
            assert fn._discover().empty

    def test_none_from_the_venue_is_tolerated(self):
        with patch.object(fn.kc, "get_markets_closing_within", return_value=None):
            assert fn._discover().empty


class TestOpen:
    def test_books_a_qualifying_market(self, db):
        n = fn._open(db, pd.DataFrame([_market()]))
        assert n == 1
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1

    def test_skips_a_ticker_already_held(self, db):
        fn._open(db, pd.DataFrame([_market()]))
        assert fn._open(db, pd.DataFrame([_market()])) == 0

    def test_rejects_a_mid_outside_the_band(self, db):
        assert fn._open(db, pd.DataFrame([_market(mid=0.50)])) == 0
        assert fn._open(db, pd.DataFrame([_market(mid=0.05)])) == 0

    def test_rejects_thin_volume(self, db):
        assert fn._open(db, pd.DataFrame([_market(volume=1)])) == 0

    def test_rejects_an_excluded_series(self, db):
        """Weather and commodity settles were measured as the whole loss and
        are excluded; booking one would silently reintroduce it."""
        assert fn._open(db, pd.DataFrame([
            _market(ticker="KXTEMPNYC-1-T50", category="Climate")])) == 0

    def test_rejects_an_excluded_category(self, db):
        """Sports near-close are in-game books — a different game entirely, and
        measured as not this edge."""
        assert fn._open(db, pd.DataFrame([
            _market(ticker="KXSOMETHING-1-T50", category="Sports")])) == 0
        assert fn._open(db, pd.DataFrame([
            _market(ticker="KXSOMETHING-2-T50", category="Finance")])) == 0

    def test_one_position_per_event(self, db):
        """Members of one event resolve together — booking two is one bet
        counted twice, which is what makes a sample look bigger than it is."""
        df = pd.DataFrame([
            _market(ticker="KXA-1", event="SAME"),
            _market(ticker="KXA-2", event="SAME"),
        ])
        assert fn._open(db, df) == 1

    def test_one_position_per_asset_and_close_hour(self, db):
        """Two series on the same underlying settling the same hour are one
        bet: measured, 90% of such clusters resolved identically."""
        df = pd.DataFrame([
            _market(ticker="KXBTCD-1-T50", event="EV1", close="2026-09-01T05:00:00Z"),
            _market(ticker="KXBTC-9-T50", event="EV2", close="2026-09-01T05:30:00Z"),
        ])
        assert fn._open(db, df) == 1, "same asset, same close hour is one position"

    def test_different_close_hours_on_one_asset_are_allowed(self, db):
        df = pd.DataFrame([
            _market(ticker="KXBTCD-1-T50", event="EV1", close="2026-09-01T05:00:00Z"),
            _market(ticker="KXBTCD-2-T50", event="EV2", close="2026-09-01T09:00:00Z"),
        ])
        assert fn._open(db, df) == 2

    def test_records_the_no_side_entry_cost(self, db):
        """The arm buys NO at (1 - yes_bid); recording the yes price instead
        would make every P&L wrong by the complement."""
        fn._open(db, pd.DataFrame([_market(bid=0.14)]))
        row = db.execute("SELECT no_entry_cost FROM positions").fetchone()
        assert row[0] == pytest.approx(1 - 0.14)

    def test_rejects_a_market_with_no_real_bid(self, db):
        assert fn._open(db, pd.DataFrame([_market(bid=0.0, mid=0.15)])) == 0


class TestResolve:
    def _insert(self, con, ticker="KXBTCD-1-T50", **over):
        row = dict(ticker=ticker, event_id="EV1", category="Crypto",
                   question="Q?", yes_bid=0.15, yes_mid=0.15,
                   no_entry_cost=0.85, count=11, opened_at=fn._now() - 7200,
                   close_time="2000-01-01T00:00:00Z", resolved=0)
        row.update(over)
        cols = ",".join(row)
        con.execute(f"INSERT INTO positions ({cols}) VALUES ({','.join('?' * len(row))})",
                    tuple(row.values()))
        con.commit()

    def test_no_wins_when_the_market_resolved_no(self, db):
        self._insert(db)
        with patch.object(fn.kc, "_get", return_value=_settled("no")), \
             patch.object(fn.time, "sleep"):
            fn._resolve(db)
        pnl = db.execute("SELECT pnl, resolved FROM positions").fetchone()
        assert pnl[1] == 1
        assert pnl[0] > 0, "a NO position must profit when the market resolves NO"

    def test_no_loses_when_the_market_resolved_yes(self, db):
        self._insert(db)
        with patch.object(fn.kc, "_get", return_value=_settled("yes")), \
             patch.object(fn.time, "sleep"):
            fn._resolve(db)
        assert db.execute("SELECT pnl FROM positions").fetchone()[0] < 0

    def test_unresolved_markets_are_left_alone(self, db):
        """status="active", or finalized with result="" (still settling), must
        not be booked as an outcome — gotcha #12 in the project's API notes."""
        self._insert(db)
        with patch.object(fn.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": ""}}), \
             patch.object(fn.time, "sleep"):
            fn._resolve(db)
        assert db.execute("SELECT resolved FROM positions").fetchone()[0] == 0

    def test_a_lookup_failure_does_not_crash_the_cycle(self, db):
        self._insert(db)
        with patch.object(fn.kc, "_get", side_effect=RuntimeError("venue down")), \
             patch.object(fn.time, "sleep"):
            fn._resolve(db)          # must not raise
        assert db.execute("SELECT resolved FROM positions").fetchone()[0] == 0

    def test_pnl_is_net_of_the_settlement_fee(self, db):
        """Gross-of-fee P&L is how a losing arm looks profitable."""
        self._insert(db, count=100, no_entry_cost=0.85)
        with patch.object(fn.kc, "_get", return_value=_settled("no")), \
             patch.object(fn.time, "sleep"):
            fn._resolve(db)
        pnl = db.execute("SELECT pnl FROM positions").fetchone()[0]
        gross = (1.0 - 0.85) * 100
        assert pnl < gross, "P&L must be net of fees"


class TestCanaryGate:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("NEARCLOSE_CANARY_ENABLED", raising=False)
        assert fn._canary_enabled() is False

    def test_enabled_only_by_an_explicit_truthy_value(self, monkeypatch):
        for val, want in (("true", True), ("1", True), ("yes", True),
                          ("false", False), ("", False), ("maybe", False)):
            monkeypatch.setenv("NEARCLOSE_CANARY_ENABLED", val)
            assert fn._canary_enabled() is want, val

    def test_deployed_total_sums_only_live_rows(self, db):
        # v2 schema (R-19): a position row must carry its entry cost, count
        # and open time — the shape every real row has always had.
        base = "no_entry_cost, count, opened_at"
        db.execute(f"INSERT INTO positions (ticker, event_id, {base}, live_cost) "
                   "VALUES ('A','E',0.85,11,1,10.0)")
        db.execute(f"INSERT INTO positions (ticker, event_id, {base}, live_cost) "
                   "VALUES ('B','E',0.85,11,1,5.0)")
        db.execute(f"INSERT INTO positions (ticker, event_id, {base}) "
                   "VALUES ('C','E',0.85,11,1)")
        db.commit()
        assert fn._canary_deployed(db) == pytest.approx(15.0)

    def test_ceiling_falls_back_to_the_default_on_a_bad_value(self, monkeypatch):
        monkeypatch.setenv("NEARCLOSE_CANARY_MAX_TOTAL", "not-a-number")
        assert fn._canary_max_total() == fn.CANARY_MAX_TOTAL_DEFAULT


class TestRunCycle:
    def test_registers_its_rules_with_the_oos_ledger(self, db, tmp_path, monkeypatch):
        monkeypatch.setenv("OOS_LEDGER_FILE", str(tmp_path / "oos.json"))
        with patch.object(fn, "_discover", return_value=pd.DataFrame()), \
             patch.object(fn, "_resolve"):
            fn.run_cycle(verbose=False)
        from pm.ledger import oos_ledger
        assert oos_ledger.get_rules("fade_nearclose") is not None

    def test_a_discovery_failure_does_not_stop_resolution(self, db, tmp_path, monkeypatch):
        """Resolution is how open positions ever close; a dead discovery call
        must not strand them."""
        monkeypatch.setenv("OOS_LEDGER_FILE", str(tmp_path / "oos.json"))
        with patch.object(fn, "_discover", side_effect=RuntimeError("down")), \
             patch.object(fn, "_resolve") as resolve:
            fn.run_cycle(verbose=False)
        assert resolve.called

    def test_summary_is_readable_on_an_empty_database(self, db):
        s = fn.summary()
        assert isinstance(s, dict)
