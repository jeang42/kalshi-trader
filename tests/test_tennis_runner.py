"""Tests for tennis_runner — the match-level dedup that stops the value arm
from double-betting both sides of a match."""

from __future__ import annotations

import pandas as pd

from pm.arms import tennis_runner as tr


def _two_sided(*matches: tuple[str, str, str]) -> pd.DataFrame:
    """Build a discovery-shaped frame with both tickers per match."""
    rows = []
    for prefix, a, b in matches:
        rows.append({"condition_id": f"{prefix}-{a}", "best_ask": 0.30})
        rows.append({"condition_id": f"{prefix}-{b}", "best_ask": 0.70})
    return pd.DataFrame(rows)


class TestOneTickerPerMatch:
    def test_collapses_both_sides_to_one(self):
        df = _two_sided(("KXATPMATCH-26JUN03BERARN", "BER", "ARN"),
                        ("KXWTAMATCH-26JUN03SABSHN", "SAB", "SHN"))
        out = tr._one_ticker_per_match(df)
        assert len(out) == 2  # 2 matches, not 4 sides
        keys = {c.rsplit("-", 1)[0] for c in out["condition_id"]}
        assert keys == {"KXATPMATCH-26JUN03BERARN", "KXWTAMATCH-26JUN03SABSHN"}

    def test_keeps_one_side_per_match(self):
        df = _two_sided(("KXATPMATCH-26JUN03BERARN", "BER", "ARN"))
        out = tr._one_ticker_per_match(df)
        assert len(out) == 1
        # deterministic: first occurrence (the -BER side) is kept
        assert out.iloc[0]["condition_id"] == "KXATPMATCH-26JUN03BERARN-BER"

    def test_distinct_matches_all_kept(self):
        df = _two_sided(("KXATPMATCH-26JUN03AAABBB", "AAA", "BBB"),
                        ("KXATPMATCH-26JUN03CCCDDD", "CCC", "DDD"),
                        ("KXWTAMATCH-26JUN03EEEFFF", "EEE", "FFF"))
        out = tr._one_ticker_per_match(df)
        assert len(out) == 3

    def test_empty_frame_safe(self):
        assert tr._one_ticker_per_match(pd.DataFrame()).empty

    def test_missing_column_safe(self):
        df = pd.DataFrame([{"foo": 1}])
        out = tr._one_ticker_per_match(df)
        assert len(out) == 1  # no condition_id → returned unchanged
