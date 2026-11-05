"""Offline tests for the executable cross-market arb math."""

from __future__ import annotations

import pandas as pd

from pm.arms import arb_scan


def _df(rows):
    # rows: (event_id, condition_id, question, best_bid, best_ask)
    df = pd.DataFrame(rows, columns=[
        "event_id", "condition_id", "question", "best_bid", "best_ask"])
    df["event_title"] = df["event_id"]
    df["volume"] = 1000.0
    return df


class TestFindArbs:
    def test_overround_flagged_buy_no(self):
        # Three-way field, YES bids sum to 1.20 → overround arb (buy NO all).
        df = _df([
            ("EV", "KXA-26", "A?", 0.50, 0.52),
            ("EV", "KXB-26", "B?", 0.40, 0.42),
            ("EV", "KXC-26", "C?", 0.30, 0.32),
        ])  # bid sum 1.20, mid sum ~1.23
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01,
                                 me_band=(0.9, 1.3))
        assert len(out) == 1
        r = out.iloc[0]
        assert r["best_side"] == "overround"
        assert r["best_edge"] > 0          # ~0.20 minus small fees

    def test_underround_flagged_buy_yes(self):
        # Complete field (bids sum 0.90 ≥ floor), asks sum 0.93 < 1 → underround.
        df = _df([
            ("EV", "KXA-26", "A?", 0.30, 0.31),
            ("EV", "KXB-26", "B?", 0.30, 0.31),
            ("EV", "KXC-26", "C?", 0.30, 0.31),
        ])  # bid sum 0.90, ask sum 0.93
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01,
                                 me_band=(0.7, 1.1))
        assert len(out) == 1 and out.iloc[0]["best_side"] == "underround"
        assert out.iloc[0]["best_edge"] > 0

    def test_incomplete_underround_rejected(self):
        # Low bid-sum = missing members (the dominant outcome) → not trusted.
        df = _df([
            ("EV", "KXA-26", "A?", 0.05, 0.07),
            ("EV", "KXB-26", "B?", 0.08, 0.10),
            ("EV", "KXC-26", "C?", 0.06, 0.08),
        ])  # ask sum 0.25 (looks like huge arb) but bid sum 0.19 → incomplete
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01,
                                 me_band=(0.0, 2.0))
        assert out.empty

    def test_efficient_field_no_arb(self):
        # Tight field summing to ~1.0 → no arb after fees.
        df = _df([
            ("EV", "KXA-26", "A?", 0.49, 0.51),
            ("EV", "KXB-26", "B?", 0.49, 0.51),
        ])
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01)
        assert out.empty

    def test_non_mutually_exclusive_excluded_by_band(self):
        # Two independent ~0.5 markets sum to ~1.0 mid — but make them sum high
        # (1.6) so the ME band rejects them as not one-winner.
        df = _df([
            ("EV", "KXA-26", "A?", 0.78, 0.82),
            ("EV", "KXB-26", "B?", 0.78, 0.82),
        ])  # mid sum ~1.6 → outside default band
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01)
        assert out.empty

    def test_skips_markets_without_two_sided_quote(self):
        df = _df([
            ("EV", "KXA-26", "A?", 0.0, 0.0),   # no book
            ("EV", "KXB-26", "B?", 0.40, 0.42),
        ])
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01)
        assert out.empty   # only 1 valid member < min_members

    def test_report_renders_with_candidate(self):
        df = _df([("EV", "KXA-26", "A?", 0.50, 0.52),
                  ("EV", "KXB-26", "B?", 0.40, 0.42),
                  ("EV", "KXC-26", "C?", 0.30, 0.32)])
        out = arb_scan.find_arbs(df, min_members=2, me_band=(0.9, 1.3))
        txt = arb_scan.format_report(out, n_markets=3, n_fields=1)
        assert "LIVE CROSS-MARKET ARB SCAN" in txt and "overround" in txt

    def test_report_renders_empty(self):
        txt = arb_scan.format_report(
            pd.DataFrame(columns=["best_side"]), n_markets=0, n_fields=0)
        assert "No arbitrage candidates" in txt


class TestCumulativeExclusion:
    def test_before_buckets_rejected(self):
        # "Before <date>" cumulative buckets sum >1 but aren't one-winner.
        df = _df([
            ("EV", "KXA-26", "Released before Jan 2027?", 0.05, 0.08),
            ("EV", "KXB-26", "Released before Jun 2027?", 0.20, 0.26),
            ("EV", "KXC-26", "Released before Dec 2027?", 0.72, 0.79),
        ])  # bid sum ~0.97 but cumulative → must be skipped
        out = arb_scan.find_arbs(df, min_members=2, me_band=(0.5, 1.5))
        assert out.empty

    def test_disjoint_between_buckets_not_rejected(self):
        # "between X and Y" ARE disjoint/mutually exclusive → allowed.
        df = _df([
            ("EV", "KXA-26", "Vote share between 40% and 45%?", 0.50, 0.52),
            ("EV", "KXB-26", "Vote share between 45% and 50%?", 0.40, 0.42),
            ("EV", "KXC-26", "Vote share between 50% and 55%?", 0.30, 0.32),
        ])
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01,
                                 me_band=(0.9, 1.3))
        assert len(out) == 1 and out.iloc[0]["best_side"] == "overround"


class TestAlerting:
    def test_alert_writes_jsonl_and_dedups(self, tmp_path, monkeypatch):
        import json as _json
        monkeypatch.setattr(arb_scan, "ARB_LOG", tmp_path / "arb.jsonl")
        df = _df([("EV", "KXA-26", "A?", 0.50, 0.52),
                  ("EV", "KXB-26", "B?", 0.40, 0.42),
                  ("EV", "KXC-26", "C?", 0.30, 0.32)])
        arbs = arb_scan.find_arbs(df, min_members=2, me_band=(0.9, 1.3))
        seen: set[str] = set()
        assert arb_scan._alert_candidates(arbs, seen=seen) == 1   # new
        assert arb_scan._alert_candidates(arbs, seen=seen) == 0   # deduped
        lines = (tmp_path / "arb.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2                                    # both logged
        rec = _json.loads(lines[0])
        assert rec["best_side"] == "overround" and "ts" in rec


class TestMutualExclusiveFlag:
    def _dfme(self, rows, me):
        df = _df(rows)
        df["mutually_exclusive"] = me
        return df

    def test_flag_true_allows_arb(self):
        # me=True field, bids sum 1.20 → real overround arb, even outside band.
        df = self._dfme([("EV", "KXA-26", "A?", 0.50, 0.52),
                         ("EV", "KXB-26", "B?", 0.40, 0.42),
                         ("EV", "KXC-26", "C?", 0.30, 0.32)], me=True)
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01)
        assert len(out) == 1 and out.iloc[0]["best_side"] == "overround"

    def test_flag_false_excludes_independent_field(self):
        # me=False (independent "Will X resign" markets) → never an arb.
        df = self._dfme([("EV", "KXA-26", "Will Thomas resign?", 0.30, 0.33),
                         ("EV", "KXB-26", "Will Alito resign?", 0.40, 0.43),
                         ("EV", "KXC-26", "Will Sotomayor resign?", 0.40, 0.43)],
                        me=False)
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01)
        assert out.empty

    def test_flag_true_overrides_band(self):
        # me=True but mid-sum 1.6 (would fail heuristic band) — flag wins.
        df = self._dfme([("EV", "KXA-26", "A?", 0.78, 0.82),
                         ("EV", "KXB-26", "B?", 0.78, 0.82)], me=True)
        out = arb_scan.find_arbs(df, min_members=2, min_edge=0.01)
        assert len(out) == 1   # not dropped by band when flag present
