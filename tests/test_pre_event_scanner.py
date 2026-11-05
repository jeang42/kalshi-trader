"""Tests for the pre-event lottery scanner + strategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pm.research import backtest as bt
from pm.research import pre_event_scanner as pe


def _market_row(*, cid="m1", price=0.05, days_to_res=0.5,
                category="Sports", volume=500.0, question="Q?"):
    return {
        "condition_id": cid, "id": cid, "question": question,
        "category": category,
        "best_bid": max(0.0, price - 0.005),
        "best_ask": price,
        "outcome_prices": [price, 1 - price],
        "volume": volume,
        "days_to_resolution": days_to_res,
        "end_date": None,
    }


class TestScanLotteryCandidates:
    def test_returns_empty_on_no_markets(self):
        fake = MagicMock()
        fake.get_markets_closing_within.return_value = pd.DataFrame()
        assert pe.scan_lottery_candidates(fake).empty

    def test_filters_by_max_price(self):
        fake = MagicMock()
        fake.get_markets_closing_within.return_value = pd.DataFrame([
            _market_row(cid="cheap", price=0.05),
            _market_row(cid="mid", price=0.30),
            _market_row(cid="expensive", price=0.85),
        ])
        df = pe.scan_lottery_candidates(fake, max_price=0.10)
        assert list(df["condition_id"]) == ["cheap"]

    def test_filters_by_max_days(self):
        fake = MagicMock()
        fake.get_markets_closing_within.return_value = pd.DataFrame([
            _market_row(cid="imminent", days_to_res=0.3),
            _market_row(cid="distant", days_to_res=5.0),
        ])
        df = pe.scan_lottery_candidates(fake, max_days=1.5)
        assert list(df["condition_id"]) == ["imminent"]

    def test_filters_by_category(self):
        fake = MagicMock()
        fake.get_markets_closing_within.return_value = pd.DataFrame([
            _market_row(cid="sports_ok", category="Sports"),
            _market_row(cid="other_skip", category="Other"),
        ])
        df = pe.scan_lottery_candidates(
            fake, categories=("Sports", "Politics"),
        )
        assert list(df["condition_id"]) == ["sports_ok"]

    def test_filters_by_min_volume(self):
        fake = MagicMock()
        fake.get_markets_closing_within.return_value = pd.DataFrame([
            _market_row(cid="alive", volume=500),
            _market_row(cid="dead", volume=10),
        ])
        df = pe.scan_lottery_candidates(fake, min_volume=100.0)
        assert list(df["condition_id"]) == ["alive"]

    def test_sorted_by_entry_price_ascending(self):
        fake = MagicMock()
        fake.get_markets_closing_within.return_value = pd.DataFrame([
            _market_row(cid="m_mid", price=0.07),
            _market_row(cid="m_cheap", price=0.03),
            _market_row(cid="m_high", price=0.09),
        ])
        df = pe.scan_lottery_candidates(fake, max_price=0.10)
        assert list(df["condition_id"]) == ["m_cheap", "m_mid", "m_high"]

    def test_falls_back_to_outcome_prices_when_ask_missing(self):
        fake = MagicMock()
        row = _market_row(cid="m1", price=0.05)
        row["best_ask"] = None
        fake.get_markets_closing_within.return_value = pd.DataFrame([row])
        df = pe.scan_lottery_candidates(fake, max_price=0.10)
        assert len(df) == 1
        assert df.iloc[0]["entry_price"] == pytest.approx(0.05)

    def test_provider_error_returns_empty(self):
        fake = MagicMock()
        fake.get_markets_closing_within.side_effect = RuntimeError("API down")
        assert pe.scan_lottery_candidates(fake).empty


class TestConcentrationSummary:
    def test_empty_returns_zeros(self):
        c = pe.concentration_summary(pd.DataFrame())
        assert c["n_markets"] == 0 and c["n_series"] == 0

    def test_one_series_dominates(self):
        # Five Drake tracker markets, one stray market
        df = pd.DataFrame({
            "condition_id": [
                "KXALBUMEQUIV-ICE26MAY21-7",
                "KXALBUMEQUIV-ICE26MAY21-8",
                "KXALBUMEQUIV-ICE26MAY21-9",
                "KXPUREALBUMS-ICE26MAY21-3",
                "KXPUREALBUMS-ICE26MAY21-4",
                "KXTRUMPACT-26MAY17-T15",
            ],
        })
        c = pe.concentration_summary(df)
        assert c["n_markets"] == 6
        assert c["top_series"] == "KXALBUMEQUIV"
        assert c["top_series_share"] == pytest.approx(3 / 6)
        # 3 distinct series: KXALBUMEQUIV, KXPUREALBUMS, KXTRUMPACT
        assert c["n_series"] == 3


class TestStrategyPreEventLottery:
    def _snap(self, *, cid="m1", price=0.05, category="Sports",
              days_remaining=0.5):
        now = datetime.now(UTC)
        end = now + timedelta(days=days_remaining)
        prices = pd.DataFrame({
            "timestamp": [now - timedelta(hours=1)],
            "price": [price],
        })
        return bt.MarketSnapshot(
            condition_id=cid, question="Q", category=category,
            as_of=now, prices=prices, end_date=end,
        )

    def test_emits_yes_when_qualifies(self):
        sig = pe.strategy_pre_event_lottery(self._snap(price=0.05))
        assert sig is not None
        assert sig.direction == "YES"
        # probability = price + edge
        assert sig.probability == pytest.approx(0.07)

    def test_skips_when_price_above_max(self):
        sig = pe.strategy_pre_event_lottery(
            self._snap(price=0.20), max_price=0.10,
        )
        assert sig is None

    def test_skips_when_too_far_from_resolution(self):
        sig = pe.strategy_pre_event_lottery(
            self._snap(days_remaining=5.0), max_days=1.5,
        )
        assert sig is None

    def test_skips_wrong_category(self):
        sig = pe.strategy_pre_event_lottery(
            self._snap(category="Tech"),
            categories=("Sports", "Politics"),
        )
        assert sig is None

    def test_empty_category_passes_through(self):
        """A snapshot with no category falls through the category gate
        — we don't want to drop edge cases just because metadata is
        missing."""
        snap = self._snap(category="")
        sig = pe.strategy_pre_event_lottery(snap)
        assert sig is not None

    def test_no_end_date_skips(self):
        now = datetime.now(UTC)
        prices = pd.DataFrame({"timestamp": [now], "price": [0.05]})
        snap = bt.MarketSnapshot(
            condition_id="m1", question="Q", category="Sports",
            as_of=now, prices=prices, end_date=None,
        )
        assert pe.strategy_pre_event_lottery(snap) is None

    def test_already_resolved_skips(self):
        sig = pe.strategy_pre_event_lottery(
            self._snap(days_remaining=-1.0),
        )
        assert sig is None

    def test_edge_parameter_controls_probability(self):
        sig = pe.strategy_pre_event_lottery(
            self._snap(price=0.05), edge=0.10,
        )
        assert sig is not None
        assert sig.probability == pytest.approx(0.15)

    def test_registered_in_strategy_registry(self):
        assert "pre_event_lottery" in bt.STRATEGIES
        name, fn = bt.STRATEGIES["pre_event_lottery"]
        assert fn is pe.strategy_pre_event_lottery
        assert "UNTESTED" in name  # honest labeling
