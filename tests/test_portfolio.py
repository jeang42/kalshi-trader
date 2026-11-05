"""Tests for portfolio.py — all I/O redirected to tmp files via the portfolio_path fixture."""

import json
from datetime import timezone

import pandas as pd
import pytest

from pm.ledger import portfolio as pf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_one(question="Test market", cid="0xabc", outcome="Yes", shares=100.0, price=0.40):
    pf.add_position(question, cid, outcome, shares, price)


# ---------------------------------------------------------------------------
# add_position / get_positions
# ---------------------------------------------------------------------------

class TestAddAndGet:
    def test_adds_single_position(self, portfolio_path):
        _add_one()
        df = pf.get_positions()
        assert len(df) == 1

    def test_fields_stored_correctly(self, portfolio_path):
        _add_one(question="Q1", cid="0x1", outcome="Yes", shares=50.0, price=0.60)
        row = pf.get_positions().iloc[0]
        assert row["question"] == "Q1"
        assert row["condition_id"] == "0x1"
        assert row["outcome"] == "Yes"
        assert row["shares"] == pytest.approx(50.0)
        assert row["avg_price"] == pytest.approx(0.60)

    def test_cost_basis_computed(self, portfolio_path):
        _add_one(shares=200.0, price=0.25)
        row = pf.get_positions().iloc[0]
        assert row["cost_basis"] == pytest.approx(50.0)

    def test_multiple_positions(self, portfolio_path):
        # Different markets → separate positions
        _add_one(question="A", cid="0xaaa")
        _add_one(question="B", cid="0xbbb")
        _add_one(question="C", cid="0xccc")
        assert len(pf.get_positions()) == 3

    def test_same_market_buys_aggregate(self, portfolio_path):
        """The new transactional model averages multiple buys into one position."""
        _add_one(cid="0xaaa", shares=10, price=0.40)
        _add_one(cid="0xaaa", shares=10, price=0.60)
        pos = pf.get_positions()
        assert len(pos) == 1
        assert pos.iloc[0]["shares"] == pytest.approx(20)
        assert pos.iloc[0]["avg_price"] == pytest.approx(0.50)

    def test_added_at_is_utc_iso(self, portfolio_path):
        _add_one()
        ts = pf.get_positions().iloc[0]["added_at"]
        # should be parseable as ISO datetime with timezone info
        from dateutil.parser import parse
        dt = parse(ts)
        assert dt.tzinfo is not None

    def test_persists_across_calls(self, portfolio_path):
        _add_one(question="Persistent")
        # Second independent call to get_positions reads from same file
        df = pf.get_positions()
        assert any("Persistent" in q for q in df["question"])

    def test_empty_portfolio_returns_empty_df(self, portfolio_path):
        df = pf.get_positions()
        assert df.empty
        assert "question" in df.columns  # schema columns present even when empty


# ---------------------------------------------------------------------------
# remove_position
# ---------------------------------------------------------------------------

class TestRemovePosition:
    def test_removes_by_index(self, portfolio_path):
        _add_one(question="Keep")
        _add_one(question="Remove me")
        pf.remove_position(1)
        df = pf.get_positions()
        assert len(df) == 1
        assert df.iloc[0]["question"] == "Keep"

    def test_remove_first(self, portfolio_path):
        _add_one(question="Gone")
        _add_one(question="Stay")
        pf.remove_position(0)
        df = pf.get_positions()
        assert len(df) == 1
        assert df.iloc[0]["question"] == "Stay"

    def test_out_of_range_index_is_noop(self, portfolio_path):
        _add_one()
        pf.remove_position(99)   # no-op — should not raise
        assert len(pf.get_positions()) == 1

    def test_negative_index_is_noop(self, portfolio_path):
        _add_one()
        pf.remove_position(-1)   # no-op — should not raise
        assert len(pf.get_positions()) == 1

    def test_remove_from_empty_is_noop(self, portfolio_path):
        pf.remove_position(0)    # no-op — should not raise
        assert pf.get_positions().empty


# ---------------------------------------------------------------------------
# enrich_with_current_prices
# ---------------------------------------------------------------------------

def _make_markets_df(cid, outcomes, prices):
    """Mimics the get_markets() output where outcomes/prices are NATIVE LISTS,
    not JSON strings. This is the post-fix contract."""
    return pd.DataFrame([{
        "condition_id": cid,
        "outcomes": list(outcomes),
        "outcome_prices": list(prices),
    }])


class TestEnrichWithCurrentPrices:
    def test_adds_current_price(self, portfolio_path):
        _add_one(cid="0xaaa", outcome="Yes", shares=10, price=0.30)
        df_pos = pf.get_positions()
        df_mkt = _make_markets_df("0xaaa", ["Yes", "No"], [0.55, 0.45])
        enriched = pf.enrich_with_current_prices(df_pos, df_mkt)
        assert enriched.iloc[0]["current_price"] == pytest.approx(0.55)

    def test_regression_json_strings_iterate_safely(self, portfolio_path):
        """Regression: pre-fix the market data had outcomes/prices as JSON strings,
        which made zip() iterate characters. Even if someone reintroduces the bug
        upstream, enrich should not crash."""
        _add_one(cid="0xaaa", outcome="Yes", shares=10, price=0.30)
        df_pos = pf.get_positions()
        # Bad input: JSON strings instead of lists
        df_mkt = pd.DataFrame([{
            "condition_id": "0xaaa",
            "outcomes": '["Yes", "No"]',           # bad: should be a list
            "outcome_prices": '["0.55", "0.45"]',  # bad: should be a list
        }])
        # Should not raise; should fall back to avg_price → P&L of 0
        enriched = pf.enrich_with_current_prices(df_pos, df_mkt)
        assert enriched.iloc[0]["unrealized_pnl"] == pytest.approx(0.0)

    def test_unrealized_pnl(self, portfolio_path):
        _add_one(cid="0xaaa", outcome="Yes", shares=100, price=0.30)
        df_pos = pf.get_positions()
        df_mkt = _make_markets_df("0xaaa", ["Yes", "No"], [0.50, 0.50])
        enriched = pf.enrich_with_current_prices(df_pos, df_mkt)
        # cost_basis = 30, current_value = 50 → pnl = +20
        assert enriched.iloc[0]["unrealized_pnl"] == pytest.approx(20.0)

    def test_pnl_pct(self, portfolio_path):
        _add_one(cid="0xaaa", outcome="Yes", shares=100, price=0.30)
        df_pos = pf.get_positions()
        df_mkt = _make_markets_df("0xaaa", ["Yes", "No"], [0.60, 0.40])
        enriched = pf.enrich_with_current_prices(df_pos, df_mkt)
        # cost_basis=30, current_value=60 → +100%
        assert enriched.iloc[0]["pnl_pct"] == pytest.approx(100.0)

    def test_unknown_market_falls_back_to_avg_price(self, portfolio_path):
        _add_one(cid="0xunknown", outcome="Yes", shares=10, price=0.40)
        df_pos = pf.get_positions()
        df_mkt = _make_markets_df("0xdifferent", ["Yes", "No"], [0.80, 0.20])
        enriched = pf.enrich_with_current_prices(df_pos, df_mkt)
        # Falls back to avg_price → pnl should be 0
        assert enriched.iloc[0]["unrealized_pnl"] == pytest.approx(0.0)

    def test_no_outcome_match_falls_back(self, portfolio_path):
        _add_one(cid="0xaaa", outcome="Yes", shares=10, price=0.40)
        df_pos = pf.get_positions()
        # Market uses different outcome labels
        df_mkt = _make_markets_df("0xaaa", ["Agree", "Disagree"], [0.70, 0.30])
        enriched = pf.enrich_with_current_prices(df_pos, df_mkt)
        assert enriched.iloc[0]["unrealized_pnl"] == pytest.approx(0.0)

    def test_empty_positions_returns_unchanged(self, portfolio_path):
        df_pos = pf.get_positions()  # empty
        df_mkt = _make_markets_df("0xaaa", ["Yes", "No"], [0.50, 0.50])
        result = pf.enrich_with_current_prices(df_pos, df_mkt)
        assert result.empty

    def test_empty_markets_returns_unchanged(self, portfolio_path):
        _add_one()
        df_pos = pf.get_positions()
        result = pf.enrich_with_current_prices(df_pos, pd.DataFrame())
        # No enrichment columns added, original df returned unchanged
        assert "current_price" not in result.columns
