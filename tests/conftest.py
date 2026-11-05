"""Shared pytest fixtures."""

import json

import pandas as pd
import pytest


@pytest.fixture
def portfolio_path(tmp_path, monkeypatch):
    """Redirect portfolio storage to a temp file for each test.

    Uses the env-var override that the lazy file lookup in portfolio.py
    consults on every call — same mechanism used by the profile system
    and by the `.env` config.
    """
    p = tmp_path / "portfolio.json"
    monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE", str(p))
    return p


@pytest.fixture
def sample_markets_df():
    """A small DataFrame matching get_markets() output format for scanner tests.

    Note: outcomes/outcome_prices are native Python lists (the get_markets()
    contract since the JSON-string bug fix), not JSON strings.
    """
    return pd.DataFrame([
        {
            "question": "Will Bitcoin hit $100k?",
            "condition_id": "0xaaa",
            "category": "Crypto",
            "volume_24hr": 500_000,
            "liquidity": 10_000,
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.42, 0.58],
            "spread": 0.002,
            "best_bid": 0.419,
            "best_ask": 0.421,
            "last_trade_price": 0.42,
        },
        {
            "question": "Will candidate A win?",
            "condition_id": "0xbbb",
            "category": "Politics",
            "volume_24hr": 200_000,
            "liquidity": 5_000,
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.60, 0.40],
            "spread": 0.005,
            "best_bid": 0.597,
            "best_ask": 0.602,
            "last_trade_price": 0.75,   # big divergence from mid
        },
        {
            "question": "Low liquidity market",
            "condition_id": "0xccc",
            "category": "Other",
            "volume_24hr": 10,
            "liquidity": 50,            # below most min_liquidity thresholds
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.50, 0.50],
            "spread": 0.05,
            "best_bid": 0.475,
            "best_ask": 0.525,
            "last_trade_price": 0.50,
        },
        {
            "question": "Multi-outcome: who wins the race?",
            "condition_id": "0xddd",
            "category": "Sports",
            "volume_24hr": 80_000,
            "liquidity": 3_000,
            "outcomes": ["Alice", "Bob", "Carol"],
            "outcome_prices": [0.50, 0.30, 0.10],  # sums to 0.90 — gap
            "spread": 0.003,
            "best_bid": 0.498,
            "best_ask": 0.501,
            "last_trade_price": 0.50,
        },
    ])
