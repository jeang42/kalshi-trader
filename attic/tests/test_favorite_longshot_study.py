"""Offline tests for the favorite-longshot mining backtest math."""

from __future__ import annotations

import pandas as pd

import favorite_longshot_study as fls


def _df(rows):
    # rows: (condition_id, event_id, predicted_prob, actual)
    return pd.DataFrame(rows, columns=[
        "condition_id", "event_id", "predicted_prob", "actual"])


class TestSimulate:
    def test_back_favorite_pnl_sign(self):
        # Priced 0.60, all resolve YES → backing wins (1-0.60) minus spread/fee.
        df = _df([(f"KX{i}-26", f"E{i}", 0.60, 1.0) for i in range(10)])
        r = fls.simulate(df, side="yes", price_lo=0.55, price_hi=0.85,
                         half_spread=0.0)
        assert r.n == 10 and r.win_rate == 1.0
        assert r.net_roi > 0          # winners net positive
        assert r.gross_roi > r.net_roi  # fees drag

    def test_fade_longshot_pnl_sign(self):
        # Priced 0.15 longshots that all resolve NO → fading (buy NO) wins.
        df = _df([(f"KX{i}-26", f"E{i}", 0.15, 0.0) for i in range(10)])
        r = fls.simulate(df, side="no", price_lo=0.02, price_hi=0.20,
                         half_spread=0.0)
        assert r.n == 10 and r.win_rate == 1.0
        assert r.net_roi > 0

    def test_fade_loses_when_longshot_hits(self):
        # If the longshots actually resolve YES, fading them loses.
        df = _df([(f"KX{i}-26", f"E{i}", 0.15, 1.0) for i in range(10)])
        r = fls.simulate(df, side="no", price_lo=0.02, price_hi=0.20,
                         half_spread=0.0)
        assert r.win_rate == 0.0 and r.net_roi < 0

    def test_spread_haircut_reduces_roi(self):
        df = _df([(f"KX{i}-26", f"E{i}", 0.60, 1.0) for i in range(10)])
        no_spread = fls.simulate(df, side="yes", price_lo=0.55, price_hi=0.85,
                                 half_spread=0.0)
        with_spread = fls.simulate(df, side="yes", price_lo=0.55, price_hi=0.85,
                                   half_spread=0.03)
        assert with_spread.net_roi < no_spread.net_roi

    def test_concentration_flags_single_event(self):
        # All trades share one event → top_event_share == 1.0 (correlated).
        df = _df([(f"KX{i}-26", "SAME", 0.60, 1.0) for i in range(8)])
        r = fls.simulate(df, side="yes", price_lo=0.55, price_hi=0.85,
                         half_spread=0.0)
        assert r.n_events == 1 and abs(r.top_event_share - 1.0) < 1e-9

    def test_empty_band_returns_none(self):
        df = _df([("KX1-26", "E", 0.60, 1.0)])
        assert fls.simulate(df, side="yes", price_lo=0.90, price_hi=0.99) is None


class TestGrid:
    def test_grid_runs_both_rules(self):
        rows = ([(f"L{i}", f"e{i}", 0.12, 0.0) for i in range(20)]
                + [(f"F{i}", f"e{i}", 0.65, 1.0) for i in range(20)])
        df = _df(rows)
        df["market_type"] = "binary"
        grid = fls.run_grid(df, half_spread=0.01)
        assert set(grid["strategy"]) == {"fade_longshot", "back_favorite"}
        txt = fls.format_report(grid, params={}, half_spread=0.01)
        assert "FAVORITE-LONGSHOT MINING" in txt
