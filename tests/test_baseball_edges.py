"""Tests for the baseball edges adapter in edges.py.

The adapter is the single legitimate touchpoint between the market app
and the baseball sidecar — these tests pin the matching logic and the
ticker-parsing rules. We mock baseball.state directly so tests don't
hit the network or require a populated SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import edges
from pm.research import backtest as bt

# NOTE: patch edges internals at their REAL home (`edges.baseball.X`,
# `edges.tennis.X`), not `edges.X`. Since the R-24 package split the
# latter names this package's re-export; rebinding it would not affect
# the caller inside the submodule, and the test would exercise the real
# code path instead of the stub.

# ─── Ticker parser ────────────────────────────────────────────────────────


@pytest.mark.parametrize("ticker,expected", [
    ("KXMLBGAME-26MAY231610NYMMIA-NYM", ("2026-05-23", "NYMMIA", "NYM")),
    ("KXMLBGAME-26MAY231610NYMMIA-MIA", ("2026-05-23", "NYMMIA", "MIA")),
    ("KXMLBGAME-25JUN012205TEXLAA-TEX", ("2025-06-01", "TEXLAA", "TEX")),
    ("KXMLBGAME-26MAY222140ATHSD-ATH",  ("2026-05-22", "ATHSD",  "ATH")),
])
def test_kalshi_mlb_ticker_parse(ticker, expected):
    assert edges._parse_kalshi_mlb_ticker(ticker) == expected


@pytest.mark.parametrize("ticker", [
    "KXPRES-2024-DEM",                           # not MLB
    "KXMLBGAME-",                                # truncated
    "KXMLBGAME-26ZZZ231610NYMMIA-NYM",          # bad month
    "KXMLBGAME-26MAY32 1610NYMMIA-NYM".strip(), # malformed
    "",                                          # empty
    None,                                        # None is safe
])
def test_kalshi_mlb_ticker_parse_rejects_invalid(ticker):
    assert edges._parse_kalshi_mlb_ticker(ticker or "") is None


# ─── Match game by Kalshi ticker ─────────────────────────────────────────


@pytest.fixture
def sample_games():
    """Two games on 2026-05-23 where one team (NYM) only plays once."""
    return [
        {"home_abbr": "MIA", "away_abbr": "NYM",
         "home_name": "Marlins", "away_name": "Mets",
         "status": "Scheduled", "home_win_prob": 0.58},
        {"home_abbr": "PHI", "away_abbr": "CLE",
         "home_name": "Phillies", "away_name": "Guardians",
         "status": "Scheduled", "home_win_prob": 0.48},
    ]


def test_match_kalshi_yes_home(sample_games):
    """Ticker tail = home team → yes_side = 'home'."""
    with patch("edges.baseball._baseball_games_for_date", return_value=sample_games):
        g, side = edges._match_baseball_game(
            "New York M vs Miami Winner? — Miami",
            sample_games,
            market_id="KXMLBGAME-26MAY231610NYMMIA-MIA",
        )
        assert g["home_abbr"] == "MIA"
        assert side == "home"


def test_match_kalshi_yes_away(sample_games):
    """Ticker tail = away team → yes_side = 'away'."""
    with patch("edges.baseball._baseball_games_for_date", return_value=sample_games):
        g, side = edges._match_baseball_game(
            "New York M vs Miami Winner? — New York M",
            sample_games,
            market_id="KXMLBGAME-26MAY231610NYMMIA-NYM",
        )
        assert g["away_abbr"] == "NYM"
        assert side == "away"


def test_match_kalshi_no_games_on_date(sample_games):
    """Ticker is valid but the date isn't loaded → returns None
    (honest "no edge available" rather than wrong-day match)."""
    with patch("edges.baseball._baseball_games_for_date", return_value=[]):
        m = edges._match_baseball_game(
            "Texas vs Los Angeles A Winner? — Texas",
            sample_games,
            market_id="KXMLBGAME-26MAY252205TEXLAA-TEX",
        )
        assert m is None


def test_match_kalshi_opponent_validation_blocks_wrong_game():
    """The validation pass rejects a same-team-different-opponent match.
    Even if the date has ATH playing somewhere, only the game whose
    opponent matches the ticker's matchup segment can match."""
    bad_games = [
        # ATH plays LAA on this date but the ticker is for ATH @ SD.
        # The opponent check should reject this match.
        {"home_abbr": "LAA", "away_abbr": "ATH",
         "home_name": "Angels", "away_name": "Athletics",
         "status": "Scheduled", "home_win_prob": 0.50},
    ]
    with patch("edges.baseball._baseball_games_for_date", return_value=bad_games):
        m = edges._match_baseball_game(
            "A's vs San Diego Winner? — A's",
            bad_games,
            market_id="KXMLBGAME-26MAY232140ATHSD-ATH",
        )
        # Schedule has ATH but the matchup string is "ATHSD", not "ATHLAA".
        # LAA ∈ "ATHSD" is False → reject.
        assert m is None


def test_match_derivative_keywords_skipped(sample_games):
    """O/U, spread, props — model says nothing about these."""
    with patch("edges.baseball._baseball_games_for_date", return_value=sample_games):
        for q in [
            "Mets vs Marlins total runs over 8.5",
            "Mets vs Marlins run-line spread",
            "Mets vs Marlins first to score?",
        ]:
            assert edges._match_baseball_game(
                q, sample_games, market_id="KXMLBGAME-26MAY231610NYMMIA-NYM",
            ) is None


def test_match_alias_fallback_when_not_kalshi(sample_games):
    """For non-Kalshi market IDs, fall back to alias matching with
    the 'first team in question is YES' convention."""
    g, side = edges._match_baseball_game(
        "Will the Mets beat the Marlins tonight?",
        sample_games,
        market_id="0xdeadbeef-not-a-kalshi-mlb-ticker",
    )
    assert g["away_abbr"] == "NYM"
    assert side == "away"  # Mets appear first → YES = Mets = away


# ─── End-to-end find_baseball_edges ──────────────────────────────────────


@pytest.fixture
def fake_market_df():
    """Two markets representing both YES sides of a single Kalshi game."""
    return pd.DataFrame([
        {
            "condition_id": "KXMLBGAME-26MAY231610NYMMIA-NYM",
            "question": "New York M vs Miami Winner? — New York M",
            "outcome_prices": [0.50, 0.50],   # market 50/50
            "outcomes": ["Yes", "No"],
            "category": "Sports",
            "end_date": "",
            "best_bid": 0.49, "best_ask": 0.51,
        },
        {
            "condition_id": "KXMLBGAME-26MAY231610NYMMIA-MIA",
            "question": "New York M vs Miami Winner? — Miami",
            "outcome_prices": [0.50, 0.50],
            "outcomes": ["Yes", "No"],
            "category": "Sports",
            "end_date": "",
            "best_bid": 0.49, "best_ask": 0.51,
        },
    ])


def test_find_baseball_edges_produces_consistent_direction(fake_market_df, sample_games):
    """Both per-side markets on the same game should produce edges whose
    YES probabilities sum to ~1.0 — a sanity invariant."""
    with patch("edges.baseball._baseball_today_schedule", return_value=sample_games), \
         patch("edges.baseball._baseball_games_for_date", return_value=sample_games):
        out = edges.find_baseball_edges(fake_market_df, min_gap=0.0)
    assert len(out) == 2
    # model_implied for NYM-YES + MIA-YES should sum to ~1.0
    total = sum(e.model_implied for e in out)
    assert abs(total - 1.0) < 1e-9


def test_find_baseball_edges_min_gap_filter(fake_market_df, sample_games):
    """min_gap=0.10 should drop edges where |gap| < 10pp.
    Market is 0.50 / 0.50; model is 0.42 / 0.58 → gaps ±8pp → all filtered."""
    with patch("edges.baseball._baseball_today_schedule", return_value=sample_games), \
         patch("edges.baseball._baseball_games_for_date", return_value=sample_games):
        out = edges.find_baseball_edges(fake_market_df, min_gap=0.10)
    assert out == []


def test_find_baseball_edges_source_tag(fake_market_df, sample_games):
    """All emitted edges carry source="baseball" so the calibration log
    keeps them separate from the ESPN-consensus 'sports' source."""
    with patch("edges.baseball._baseball_today_schedule", return_value=sample_games), \
         patch("edges.baseball._baseball_games_for_date", return_value=sample_games):
        out = edges.find_baseball_edges(fake_market_df, min_gap=0.0)
    assert all(e.source == "baseball" for e in out)


def test_find_baseball_edges_empty_df():
    assert edges.find_baseball_edges(pd.DataFrame()) == []


def test_find_baseball_edges_no_schedule(fake_market_df):
    """Sidecar empty / unreachable → graceful return rather than crash."""
    with patch("edges.baseball._baseball_today_schedule", return_value=[]):
        assert edges.find_baseball_edges(fake_market_df) == []


# ─── Backtest strategy registration ──────────────────────────────────────


def test_strategy_registered():
    assert "baseball_winprob" in bt.STRATEGIES
    name, fn = bt.STRATEGIES["baseball_winprob"]
    assert callable(fn)
    assert "baseball" in name.lower()


def test_source_registered():
    assert "baseball" in edges.SOURCES
    name, fn = edges.SOURCES["baseball"]
    assert callable(fn)


def test_strategy_returns_none_when_no_match():
    """Backtest signal is None when the snapshot's market doesn't map
    to any game in the live schedule. Same caveat as sports_winprob —
    historical replay isn't built yet."""
    snap = bt.MarketSnapshot(
        condition_id="some-random-id",
        question="Something unrelated to baseball",
        category="Politics",
        as_of=datetime.now(UTC),
        prices=pd.DataFrame(columns=["timestamp", "price"]),
    )
    with patch("edges.baseball._baseball_today_schedule", return_value=[]):
        assert edges.strategy_baseball_winprob(snap) is None
