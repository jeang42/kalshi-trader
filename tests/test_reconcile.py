"""Tests for portfolio.reconcile_positions — paper vs real position
drift detection."""

from __future__ import annotations

import pandas as pd

from pm.ledger import portfolio as pf


def _paper(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["question", "condition_id", "outcome", "shares",
                 "avg_price", "cost_basis", "added_at"]
    )


def _real(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ticker", "position", "market_exposure_cents",
                 "realized_pnl_cents", "total_traded_cents",
                 "resting_orders_count", "last_updated_ts"]
    )


# ---------------------------------------------------------------------------
# Empty cases
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_both_empty_returns_empty(self):
        df = pf.reconcile_positions(_paper([]), _real([]))
        assert df.empty
        # Column shape still intact
        for col in ["ticker", "paper_net", "real_net", "drift", "status"]:
            assert col in df.columns

    def test_paper_empty_real_present(self):
        df = pf.reconcile_positions(
            _paper([]),
            _real([{"ticker": "KX-1", "position": 10}]),
        )
        assert len(df) == 1
        assert df.iloc[0]["status"] == "real_only"
        assert df.iloc[0]["paper_net"] == 0.0
        assert df.iloc[0]["real_net"] == 10
        assert df.iloc[0]["drift"] == 10.0

    def test_paper_present_real_empty(self):
        df = pf.reconcile_positions(
            _paper([{"question": "Q", "condition_id": "KX-1",
                     "outcome": "Yes", "shares": 5,
                     "avg_price": 0.4, "cost_basis": 2.0, "added_at": ""}]),
            _real([]),
        )
        assert len(df) == 1
        assert df.iloc[0]["status"] == "paper_only"
        assert df.iloc[0]["paper_net"] == 5.0
        assert df.iloc[0]["real_net"] == 0


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

class TestMatching:
    def test_yes_matched(self):
        df = pf.reconcile_positions(
            _paper([{"question": "Q", "condition_id": "KX-1",
                     "outcome": "Yes", "shares": 10,
                     "avg_price": 0.5, "cost_basis": 5.0, "added_at": ""}]),
            _real([{"ticker": "KX-1", "position": 10}]),
        )
        row = df.iloc[0]
        assert row["status"] == "matched"
        assert row["drift"] == 0.0

    def test_no_matched_via_signed_position(self):
        # Paper holds 10 NO; Kalshi reports position=-10 (negative=NO long)
        df = pf.reconcile_positions(
            _paper([{"question": "Q", "condition_id": "KX-1",
                     "outcome": "No", "shares": 10,
                     "avg_price": 0.3, "cost_basis": 3.0, "added_at": ""}]),
            _real([{"ticker": "KX-1", "position": -10}]),
        )
        row = df.iloc[0]
        assert row["paper_net"] == -10.0
        assert row["real_net"] == -10
        assert row["status"] == "matched"

    def test_mismatch_in_count(self):
        df = pf.reconcile_positions(
            _paper([{"question": "Q", "condition_id": "KX-1",
                     "outcome": "Yes", "shares": 10,
                     "avg_price": 0.5, "cost_basis": 5.0, "added_at": ""}]),
            _real([{"ticker": "KX-1", "position": 7}]),
        )
        row = df.iloc[0]
        assert row["status"] == "mismatch"
        assert row["drift"] == -3.0  # real has 3 fewer than paper

    def test_netting_both_sides_held(self):
        # Paper has 10 YES + 4 NO → net +6. Kalshi reports +6.
        df = pf.reconcile_positions(
            _paper([
                {"question": "Q", "condition_id": "KX-1",
                 "outcome": "Yes", "shares": 10,
                 "avg_price": 0.5, "cost_basis": 5.0, "added_at": ""},
                {"question": "Q", "condition_id": "KX-1",
                 "outcome": "No", "shares": 4,
                 "avg_price": 0.5, "cost_basis": 2.0, "added_at": ""},
            ]),
            _real([{"ticker": "KX-1", "position": 6}]),
        )
        row = df.iloc[0]
        assert row["paper_yes"] == 10.0
        assert row["paper_no"] == 4.0
        assert row["paper_net"] == 6.0
        assert row["real_net"] == 6
        assert row["status"] == "matched"


# ---------------------------------------------------------------------------
# Multi-row mix
# ---------------------------------------------------------------------------

class TestMixed:
    def test_multiple_statuses_in_one_report(self):
        df = pf.reconcile_positions(
            _paper([
                {"question": "Q1", "condition_id": "KX-MATCH",
                 "outcome": "Yes", "shares": 5,
                 "avg_price": 0.5, "cost_basis": 2.5, "added_at": ""},
                {"question": "Q2", "condition_id": "KX-PAPER-ONLY",
                 "outcome": "Yes", "shares": 3,
                 "avg_price": 0.4, "cost_basis": 1.2, "added_at": ""},
                {"question": "Q3", "condition_id": "KX-MISMATCH",
                 "outcome": "Yes", "shares": 10,
                 "avg_price": 0.6, "cost_basis": 6.0, "added_at": ""},
            ]),
            _real([
                {"ticker": "KX-MATCH", "position": 5},
                {"ticker": "KX-REAL-ONLY", "position": 8},
                {"ticker": "KX-MISMATCH", "position": 4},
            ]),
        )
        by_status = dict(zip(df["ticker"], df["status"], strict=True))
        assert by_status["KX-MATCH"] == "matched"
        assert by_status["KX-PAPER-ONLY"] == "paper_only"
        assert by_status["KX-REAL-ONLY"] == "real_only"
        assert by_status["KX-MISMATCH"] == "mismatch"
        # Tickers sorted alphabetically for stable output
        assert list(df["ticker"]) == sorted(by_status.keys())


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_blank_condition_id_skipped(self):
        df = pf.reconcile_positions(
            _paper([{"question": "Q", "condition_id": "",
                     "outcome": "Yes", "shares": 5,
                     "avg_price": 0.5, "cost_basis": 2.5, "added_at": ""}]),
            _real([]),
        )
        assert df.empty

    def test_unknown_outcome_label_does_not_count(self):
        # A multi-outcome candidate-name row has no "Yes" / "No" prefix.
        # It should be silently dropped from the netting rather than
        # treated as YES or NO.
        df = pf.reconcile_positions(
            _paper([{"question": "Q", "condition_id": "KX-1",
                     "outcome": "Trump", "shares": 5,
                     "avg_price": 0.5, "cost_basis": 2.5, "added_at": ""}]),
            _real([{"ticker": "KX-1", "position": 0}]),
        )
        row = df.iloc[0]
        assert row["paper_net"] == 0.0
        assert row["real_net"] == 0
        # Status here is "matched" because neither side reports a net
        # position — exactly what we want for the netting case.
        assert row["status"] == "matched"
