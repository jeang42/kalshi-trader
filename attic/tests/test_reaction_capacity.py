"""Offline tests for reaction_capacity pure helpers."""

from __future__ import annotations

import json

import pandas as pd

import reaction_capacity as rc


class TestExecutableDepth:
    def test_sums_within_window(self):
        book = json.dumps([
            {"side": "ask", "price": 0.50, "size": 100},
            {"side": "ask", "price": 0.52, "size": 200},   # within 3c of 0.50
            {"side": "ask", "price": 0.60, "size": 999},   # outside → excluded
            {"side": "bid", "price": 0.48, "size": 500},   # wrong side
        ])
        c, d = rc.executable_depth(book, within_cents=0.03)
        assert c == 300
        assert d == round(100 * 0.50 + 200 * 0.52, 6)

    def test_empty_or_bad(self):
        assert rc.executable_depth("", within_cents=0.03) == (0.0, 0.0)
        assert rc.executable_depth("not json") == (0.0, 0.0)
        assert rc.executable_depth(json.dumps([{"side": "bid", "price": 0.4,
                                                "size": 10}])) == (0.0, 0.0)


class TestHoldPnl:
    def test_win_and_loss_net_of_fee(self):
        # buy 0.60, win → +0.40 minus fee; lose → -0.60 minus fee
        win = rc._hold_pnl(0.60, True, ticker="KXMLBGAME-x")
        loss = rc._hold_pnl(0.60, False, ticker="KXMLBGAME-x")
        assert 0.35 < win < 0.40
        assert -0.62 < loss < -0.60


class TestBucket:
    def test_summarize(self):
        df = pd.DataFrame({
            "wpa": [0.11, 0.13, 0.20, 0.35],
            "pnl": [0.1, -0.2, 0.3, 0.4],
            "won": [1, 0, 1, 1],
            "depth_contracts": [10, 20, 30, 40],
            "edge_below_fair": [0.05, -0.1, 0.2, 0.3],
        })
        out = rc.summarize_by_bucket(df, "wpa", [0, 0.12, 0.18, 0.30, 1.0],
                                     ["a", "b", "c", "d"])
        assert set(out["bucket"]) == {"a", "b", "c", "d"}
        assert out[out.bucket == "a"]["n"].iloc[0] == 1
