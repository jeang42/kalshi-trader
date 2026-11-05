"""Tests for the live-order-book slippage helper."""

from __future__ import annotations

import pandas as pd
import pytest

from pm.venue import slippage


def _book(ask_levels: list[tuple[float, float]] | None = None,
          bid_levels: list[tuple[float, float]] | None = None) -> pd.DataFrame:
    """Build a parsed-order-book DataFrame for testing.
    `ask_levels` and `bid_levels` are lists of (price, size) tuples;
    callers pass them in arbitrary order to make sure the helper
    re-sorts robustly."""
    rows = []
    for price, size in (ask_levels or []):
        rows.append({"side": "ask", "price": price, "size": size,
                     "cumulative_size": 0.0})
    for price, size in (bid_levels or []):
        rows.append({"side": "bid", "price": price, "size": size,
                     "cumulative_size": 0.0})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# walk_book_for_stake — ASK side (buying)
# ---------------------------------------------------------------------------

class TestWalkAsks:
    def test_fully_fillable_within_top_level(self):
        # $20 stake against 100 shares at $0.40 → 50 shares for $20,
        # avg fill exactly the best price, no slippage.
        book = _book(ask_levels=[(0.40, 100.0)])
        est = slippage.walk_book_for_stake(book, side="ask", stake_dollars=20.0)
        assert est is not None
        assert est.fully_filled is True
        assert est.shares_filled == pytest.approx(50.0)
        assert est.avg_fill_price == pytest.approx(0.40)
        assert est.best_price == pytest.approx(0.40)
        assert est.slippage_per_share == pytest.approx(0.0)
        assert est.levels_consumed == 1

    def test_walks_multiple_levels(self):
        # $50 stake against (0.40, 100sh) + (0.42, 200sh)
        # Top level: 100 shares × $0.40 = $40 → 100 shares for $40.
        # Remaining: $10 left at $0.42 → 10/0.42 ≈ 23.81 shares.
        # Total: ~123.81 shares for $50 → avg ≈ 0.4038.
        book = _book(ask_levels=[(0.40, 100.0), (0.42, 200.0)])
        est = slippage.walk_book_for_stake(book, side="ask", stake_dollars=50.0)
        assert est is not None
        assert est.levels_consumed == 2
        assert est.shares_filled == pytest.approx(123.81, abs=0.05)
        assert est.avg_fill_price == pytest.approx(0.4038, abs=0.0005)
        assert est.slippage_per_share == pytest.approx(0.0038, abs=0.0005)

    def test_sorts_unsorted_input_robustly(self):
        # Same levels but passed in reverse order — helper must re-sort.
        book = _book(ask_levels=[(0.42, 200.0), (0.40, 100.0)])
        est = slippage.walk_book_for_stake(book, side="ask", stake_dollars=50.0)
        assert est is not None
        assert est.best_price == pytest.approx(0.40)

    def test_thin_book_partial_fill(self):
        # $500 stake against only 100 shares × $0.40 = $40 of depth.
        # Fillable = $40; remaining stake unmet.
        book = _book(ask_levels=[(0.40, 100.0)])
        est = slippage.walk_book_for_stake(book, side="ask", stake_dollars=500.0)
        assert est is not None
        assert est.fully_filled is False
        assert est.fillable_stake == pytest.approx(40.0)
        assert est.shares_filled == pytest.approx(100.0)

    def test_zero_stake_returns_none(self):
        book = _book(ask_levels=[(0.40, 100.0)])
        assert slippage.walk_book_for_stake(book, side="ask", stake_dollars=0.0) is None

    def test_empty_book_returns_none(self):
        assert slippage.walk_book_for_stake(
            pd.DataFrame(columns=["side", "price", "size", "cumulative_size"]),
            side="ask", stake_dollars=10.0,
        ) is None

    def test_book_with_no_ask_levels_returns_none(self):
        # bid-only book — buying isn't possible
        book = _book(bid_levels=[(0.40, 100.0)])
        assert slippage.walk_book_for_stake(book, side="ask", stake_dollars=10.0) is None

    def test_degenerate_zero_price_level_is_skipped(self):
        # A bad data row (price=0) shouldn't crash; helper skips it
        # and walks the next level instead.
        book = _book(ask_levels=[(0.0, 100.0), (0.40, 100.0)])
        est = slippage.walk_book_for_stake(book, side="ask", stake_dollars=20.0)
        assert est is not None
        assert est.avg_fill_price == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# walk_book_for_stake — BID side (selling)
# ---------------------------------------------------------------------------

class TestWalkBids:
    def test_bids_sorted_highest_first(self):
        # Pass bids in low-to-high order; helper must sort highest-first
        # because that's the best sell price.
        book = _book(bid_levels=[(0.38, 100.0), (0.40, 100.0)])
        est = slippage.walk_book_for_stake(book, side="bid", stake_dollars=20.0)
        assert est is not None
        assert est.best_price == pytest.approx(0.40)
        assert est.avg_fill_price == pytest.approx(0.40)   # fully filled at top

    def test_walks_through_thin_bids(self):
        # $50 against (0.40, 100sh) + (0.38, 100sh) (best-first display
        # order, but we pass in reverse to exercise sorting)
        book = _book(bid_levels=[(0.38, 100.0), (0.40, 100.0)])
        est = slippage.walk_book_for_stake(book, side="bid", stake_dollars=50.0)
        assert est is not None
        assert est.levels_consumed == 2
        # 100 shares at $0.40 = $40, then 26.32 shares at $0.38 = $10
        # → ~126.32 shares for $50, avg ≈ 0.3958
        assert est.avg_fill_price == pytest.approx(0.3958, abs=0.0005)
        assert est.slippage_per_share == pytest.approx(0.0042, abs=0.0005)


# ---------------------------------------------------------------------------
# Signed effective edge (YES vs NO convention)
# ---------------------------------------------------------------------------

class TestEffectiveEdge:
    def test_yes_edge_positive_when_model_above_fill(self):
        # Strategy thinks YES is 0.55 fair; actual fill 0.45 → +10pp
        est = slippage.SlippageEstimate(
            side="ask", requested_stake=20.0, fillable_stake=20.0,
            shares_filled=44.44, avg_fill_price=0.45,
            best_price=0.45, slippage_per_share=0.0,
            levels_consumed=1, fully_filled=True,
        )
        gap = slippage.effective_edge(0.55, est)
        assert gap == pytest.approx(0.10)

    def test_yes_edge_collapses_after_slippage(self):
        # Model fair 0.55, top-of-book 0.45 → claim is +10pp.
        # If avg fill is 0.50 after walking the book, effective gap is +5pp.
        est = slippage.SlippageEstimate(
            side="ask", requested_stake=200.0, fillable_stake=200.0,
            shares_filled=400.0, avg_fill_price=0.50,
            best_price=0.45, slippage_per_share=0.05,
            levels_consumed=3, fully_filled=True,
        )
        gap = slippage.effective_edge(0.55, est)
        assert gap == pytest.approx(0.05)

    def test_no_edge_via_bid_walk_buyer_gets_discount(self):
        # Model fair (YES) 0.30 → fair NO = 1 − 0.30 = 0.70.
        # We sell YES at avg 0.32 → NO buyer paid 1 − 0.32 = 0.68.
        # 0.68 < 0.70 → buyer got a 2pp discount → +0.02 edge.
        # helper formula on bid walk: avg − model = 0.32 − 0.30 = +0.02.
        est = slippage.SlippageEstimate(
            side="bid", requested_stake=20.0, fillable_stake=20.0,
            shares_filled=62.5, avg_fill_price=0.32,
            best_price=0.32, slippage_per_share=0.0,
            levels_consumed=1, fully_filled=True,
        )
        assert slippage.effective_edge(0.30, est) == pytest.approx(0.02)

    def test_no_edge_negative_when_yes_sells_low(self):
        # Selling YES at 0.50 when model thinks YES fair = 0.60 →
        # NO buyer paid 0.50, fair NO = 0.40 → overpaid 10pp.
        # Edge = -0.10. helper: avg − model = 0.50 − 0.60 = −0.10. ✓
        est = slippage.SlippageEstimate(
            side="bid", requested_stake=20.0, fillable_stake=20.0,
            shares_filled=40.0, avg_fill_price=0.50,
            best_price=0.50, slippage_per_share=0.0,
            levels_consumed=1, fully_filled=True,
        )
        assert slippage.effective_edge(0.60, est) == pytest.approx(-0.10)
