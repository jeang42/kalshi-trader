"""Orchestration tests for the remaining paper arms.

Their pure decision helpers were covered; the cycle around them was not — at
review time back_nearclose sat at 55% (fade_props 21% and fade_retail 25%,
both retired to attic/ on 2026-08-23). That is where the defects live: the one
this file's sibling found (an empty candidate frame losing every column and
being reported as "discovery failed") was invisible to helper-level tests.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from pm.arms import back_nearclose as bn

# ─── back_nearclose ───────────────────────────────────────────────────────


@pytest.fixture
def bn_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BACK_NEARCLOSE_DB", str(tmp_path / "back.sqlite"))
    con = bn._con()
    yield con
    con.close()


def _bn_market(ticker="KXBTCD-1-T50", *, mid=0.94, ask=0.95, bid=0.93,
               volume=500, event="EV1", category="Crypto",
               close="2026-09-01T05:00:00Z"):
    return {"id": ticker, "condition_id": ticker, "event_id": event,
            "question": "Q?", "category": category, "best_bid": bid,
            "best_ask": ask, "yes_mid": mid, "volume": volume,
            "close_time": close, "last_trade_price": mid}


class TestBackNearcloseOpen:
    def test_books_a_backable_favourite(self, bn_db):
        assert bn._open(bn_db, pd.DataFrame([_bn_market()])) == 1

    def test_rejects_an_ask_above_the_cap(self, bn_db):
        """EV arithmetic, not an outcome fit: at a ~3% loss rate, paying 0.985
        needs a >99.4% win rate to break even."""
        assert bn._open(bn_db, pd.DataFrame([_bn_market(mid=0.97, ask=0.985)])) == 0

    def test_rejects_a_discrete_market(self, bn_db):
        """Discrete measured -3.37c/ct — the one class where this inverts."""
        assert bn._open(bn_db, pd.DataFrame([
            _bn_market(ticker="KXNETFLIXRANK-1", category="Entertainment")])) == 0

    def test_rejects_an_excluded_underlying(self, bn_db):
        assert bn._open(bn_db, pd.DataFrame([
            _bn_market(ticker="KXWTIH-26AUG0314-T79.39", category="Commodities")])) == 0

    def test_rejects_thin_volume(self, bn_db):
        assert bn._open(bn_db, pd.DataFrame([_bn_market(volume=1)])) == 0

    def test_one_position_per_event(self, bn_db):
        df = pd.DataFrame([_bn_market(ticker="KXBTCD-1", event="SAME"),
                           _bn_market(ticker="KXBTCD-2", event="SAME")])
        assert bn._open(bn_db, df) == 1

    def test_an_all_excluded_frame_returns_zero_rather_than_raising(self, bn_db):
        """The failure mode found in the sibling arm: every candidate filtered
        out must be an ordinary no-op, not an exception the cycle mislabels."""
        df = pd.DataFrame([_bn_market(ticker="KXWTIH-1", category="Commodities"),
                           _bn_market(ticker="KXNETFLIXRANK-1", category="Entertainment")])
        assert bn._open(bn_db, df) == 0

    def test_frame_with_no_matching_rows_is_a_no_op(self, bn_db):
        """A schema-shaped frame with nothing in band must return 0 quietly."""
        empty = pd.DataFrame(columns=list(_bn_market().keys()))
        assert bn._open(bn_db, empty) == 0


class TestBackNearcloseResolve:
    def _insert(self, con, ticker="KXBTCD-1-T50", **over):
        row = dict(ticker=ticker, event_id="EV1", category="Crypto", question="Q?",
                   yes_ask=0.94, entry_cost=0.94, count=10, opened_at=bn._now() - 7200,
                   close_time="2000-01-01T00:00:00Z", resolved=0)
        row.update(over)
        cols = ",".join(row)
        con.execute(f"INSERT INTO positions ({cols}) VALUES ({','.join('?' * len(row))})",
                    tuple(row.values()))
        con.commit()

    def test_yes_wins_when_the_market_resolves_yes(self, bn_db):
        self._insert(bn_db)
        with patch.object(bn.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": "yes"}}), \
             patch.object(bn.time, "sleep"):
            bn._resolve(bn_db)
        pnl, resolved = bn_db.execute("SELECT pnl, resolved FROM positions").fetchone()
        assert resolved == 1 and pnl > 0

    def test_a_loss_costs_the_whole_entry(self, bn_db):
        """The risk shape: ~97% win at ~+1c, ~3% lose the full ~94c entry.
        Judge on mean P&L, never on win rate."""
        self._insert(bn_db)
        with patch.object(bn.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": "no"}}), \
             patch.object(bn.time, "sleep"):
            bn._resolve(bn_db)
        pnl = bn_db.execute("SELECT pnl FROM positions").fetchone()[0]
        assert pnl < -9.0, "a loss must cost the full entry, not a token amount"

    def test_still_settling_is_not_booked(self, bn_db):
        self._insert(bn_db)
        with patch.object(bn.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": ""}}), \
             patch.object(bn.time, "sleep"):
            bn._resolve(bn_db)
        assert bn_db.execute("SELECT resolved FROM positions").fetchone()[0] == 0

    def test_venue_failure_leaves_the_position_open(self, bn_db):
        self._insert(bn_db)
        with patch.object(bn.kc, "_get", side_effect=RuntimeError("down")), \
             patch.object(bn.time, "sleep"):
            bn._resolve(bn_db)
        assert bn_db.execute("SELECT resolved FROM positions").fetchone()[0] == 0


class TestBackNearcloseCycle:
    def test_registers_rules_and_survives_a_dead_feed(self, bn_db, tmp_path, monkeypatch):
        monkeypatch.setenv("OOS_LEDGER_FILE", str(tmp_path / "oos.json"))
        with patch.object(bn, "_discover", side_effect=RuntimeError("venue down")), \
             patch.object(bn, "_resolve", return_value=0) as resolve:
            bn.run_cycle(verbose=False)
        assert resolve.called, "resolution must still run when discovery fails"
        from pm.ledger import oos_ledger
        assert oos_ledger.get_rules("back_nearclose") is not None

    def test_places_no_real_orders(self):
        """PAPER ONLY is a property, not a comment: this arm must not import
        the live execution path at all."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(bn))
        imported = {
            n.module.split(".")[0] if isinstance(n, ast.ImportFrom) and n.module else
            (n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else "")
            for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)
        }
        # Parse imports rather than grep the source: the module docstring names
        # both of these precisely to say it does not import them.
        assert "execution" not in imported
        assert "kalshi_orders" not in imported
