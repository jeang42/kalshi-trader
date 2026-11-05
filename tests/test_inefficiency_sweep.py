"""Offline tests for the inefficiency sweep's pure analysis."""

from __future__ import annotations

import pandas as pd

from pm.research import inefficiency_sweep as isweep


def _cal_df(rows):
    return pd.DataFrame(rows, columns=["category", "predicted_prob", "actual"])


def _cal_df_t(rows):
    """With condition_id + question so classify() works.
    rows: (condition_id, question, category, predicted_prob, actual)."""
    return pd.DataFrame(rows, columns=[
        "condition_id", "question", "category", "predicted_prob", "actual"])


class TestClassify:
    def test_groups_multi_outcome_by_ticker_prefix(self):
        df = _cal_df_t([
            ("KXSENATE-26-JWOO", "Will Woo win?", "Elections", 0.5, 1.0),
            ("KXSENATE-26-ALAF", "Will Alaf win?", "Elections", 0.3, 0.0),
            ("KXSENATE-26-JG", "Will JG win?", "Elections", 0.2, 0.0),
            ("KXSERENA-26", "Will Serena retire?", "Sports", 0.6, 1.0),
        ])
        out = isweep.classify(df)
        senate = out[out.group_key == "KXSENATE-26"]
        assert (senate["market_type"] == "multi_outcome").all()
        assert (senate["group_size"] == 3).all()
        serena = out[out.condition_id == "KXSERENA-26"]
        assert serena["market_type"].iloc[0] == "binary"

    def test_scalar_flag(self):
        df = _cal_df_t([
            ("KXVOTE-26-A", "Will X get between 40% and 45% of the vote?",
             "Elections", 0.2, 0.0),
            ("KXYESNO-26", "Will it rain tomorrow?", "Other", 0.5, 1.0),
        ])
        out = isweep.classify(df)
        assert bool(out.loc[out.condition_id == "KXVOTE-26-A", "is_scalar"].iloc[0])
        assert not bool(out.loc[out.condition_id == "KXYESNO-26", "is_scalar"].iloc[0])


class TestOverround:
    def test_positive_overround_is_fadeable(self):
        # one group whose member prices sum to 1.30 → +0.30 vig, one winner.
        rows = []
        for i in range(6):  # 6 groups, each 3 members summing to 1.30
            rows += [
                (f"KXG{i}-26-A", "Will A win?", "Awards", 0.6, 1.0),
                (f"KXG{i}-26-B", "Will B win?", "Awards", 0.5, 0.0),
                (f"KXG{i}-26-C", "Will C win?", "Awards", 0.2, 0.0),
            ]
        out = isweep.classify(_cal_df_t(rows))
        ov = isweep.multi_outcome_overround(out, min_groups=3)
        r = ov[ov.category == "Awards"].iloc[0]
        assert abs(r["mean_overround"] - 0.30) < 1e-9
        assert r["pct_fadeable"] == 1.0
        assert r["n_excl_groups"] == 6
        assert r["dropped_incomplete"] == 0 and r["dropped_nonexcl"] == 0

    def test_nonexclusive_groups_dropped(self):
        # A group where 2 members win (independent yes/no, not a one-winner
        # field) must be excluded from overround scoring, counted as nonexcl.
        rows = []
        for i in range(5):  # 5 clean one-winner groups
            rows += [(f"KXG{i}-26-A", "q", "Aw", 0.6, 1.0),
                     (f"KXG{i}-26-B", "q", "Aw", 0.5, 0.0)]
        rows += [("KXIND-26-A", "q", "Aw", 0.6, 1.0),   # 2 winners → nonexcl
                 ("KXIND-26-B", "q", "Aw", 0.6, 1.0)]
        ov = isweep.multi_outcome_overround(isweep.classify(_cal_df_t(rows)),
                                            min_groups=3)
        r = ov[ov.category == "Aw"].iloc[0]
        assert r["n_excl_groups"] == 5
        assert r["dropped_nonexcl"] == 1

    def test_empty_when_no_multi_outcome(self):
        df = isweep.classify(_cal_df_t([
            ("KXA-26", "q", "X", 0.5, 1.0), ("KXB-26", "q", "X", 0.5, 0.0)]))
        assert isweep.multi_outcome_overround(df).empty


class TestCategoryEdges:
    def test_fair_category_has_no_edge(self):
        # Priced 0.20, resolves YES exactly 20% → gap ~0.
        df = _cal_df([("Fair", 0.20, 1.0)] * 8 + [("Fair", 0.20, 0.0)] * 32)
        e = isweep.category_edges(df, min_cat_n=10, min_bin_n=5)
        assert (e["category"] == "Fair").any()
        assert e.loc[e.category == "Fair", "edge_score"].iloc[0] < 0.02

    def test_mispriced_category_surfaces_signed_gap(self):
        # Priced 0.20 but resolves YES 50% → +0.30 gap, buy YES.
        df = _cal_df([("Hot", 0.20, 1.0)] * 20 + [("Hot", 0.20, 0.0)] * 20)
        e = isweep.category_edges(df, min_cat_n=10, min_bin_n=5)
        row = e.loc[e.category == "Hot"].iloc[0]
        assert row["edge_score"] > 0.2
        assert row["worst_bin_gap"] > 0.2          # observed > priced → buy YES

    def test_ranking_puts_most_mispriced_first(self):
        fair = [("Fair", 0.50, 1.0)] * 20 + [("Fair", 0.50, 0.0)] * 20
        mis = [("Mis", 0.30, 1.0)] * 30 + [("Mis", 0.30, 0.0)] * 10
        e = isweep.category_edges(_cal_df(fair + mis), min_cat_n=10, min_bin_n=5)
        assert e.iloc[0]["category"] == "Mis"

    def test_small_category_dropped(self):
        df = _cal_df([("Tiny", 0.3, 1.0)] * 5)
        e = isweep.category_edges(df, min_cat_n=30)
        assert e.empty or "Tiny" not in set(e["category"])

    def test_empty_input(self):
        e = isweep.category_edges(pd.DataFrame(
            columns=["category", "predicted_prob", "actual"]))
        assert e.empty


class TestReport:
    def test_report_renders_without_network(self):
        rows = [(f"KXC{i}-26", "Will it happen?", "Crypto", 0.2,
                 1.0 if i < 20 else 0.0) for i in range(40)]
        df = isweep.classify(_cal_df_t(rows))
        binary = df[df.market_type == "binary"]
        e = isweep.category_edges(binary, min_cat_n=10, min_bin_n=5)
        ov = isweep.multi_outcome_overround(df)
        txt = isweep.format_report(df, e, ov, params={"x": 1}, n_fetched=100)
        assert "KALSHI INEFFICIENCY SWEEP" in txt
        assert "BINARY MARKET MISPRICING" in txt
        assert "ONE-WINNER FIELD OVERROUND" in txt
        assert "VENUE-WIDE CALIBRATION" in txt
