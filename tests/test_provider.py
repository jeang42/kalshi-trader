"""Tests for the market-provider abstraction layer."""

from unittest.mock import patch

import pandas as pd
import pytest

from pm.venue import polymarket_client as pm
from pm.venue import provider as mp

# ---------------------------------------------------------------------------
# Registry and selection
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_polymarket_registered_by_default(self):
        assert "polymarket" in mp.list_provider_names()
        assert mp.get_provider("polymarket").name == "polymarket"

    def test_kalshi_is_default(self, monkeypatch):
        monkeypatch.delenv(mp.ACTIVE_PROVIDER_ENV, raising=False)
        assert mp.active_provider_name() == "kalshi"

    def test_env_var_selects_provider(self, monkeypatch):
        monkeypatch.setenv(mp.ACTIVE_PROVIDER_ENV, "polymarket")
        assert mp.active_provider_name() == "polymarket"

    def test_unknown_provider_in_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(mp.ACTIVE_PROVIDER_ENV, "totally-fake-provider")
        # active_provider_name should silently fall back to default + log
        assert mp.active_provider_name() == mp.DEFAULT_PROVIDER

    def test_get_provider_with_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            mp.get_provider("totally-fake-provider")

    def test_set_active_provider(self, monkeypatch):
        monkeypatch.delenv(mp.ACTIVE_PROVIDER_ENV, raising=False)
        mp.set_active_provider("polymarket")
        assert mp.active_provider_name() == "polymarket"
        monkeypatch.delenv(mp.ACTIVE_PROVIDER_ENV, raising=False)

    def test_set_active_provider_unknown_raises(self):
        with pytest.raises(ValueError):
            mp.set_active_provider("nope")


# ---------------------------------------------------------------------------
# PolymarketProvider delegation
# ---------------------------------------------------------------------------

class TestPolymarketProvider:
    """Each provider method must delegate to the corresponding function in
    polymarket_client.py. The behaviour of those functions is tested in
    test_client.py — here we just verify the wiring."""

    def test_has_required_attrs(self):
        provider = mp.PolymarketProvider()
        assert provider.name == "polymarket"
        assert provider.display_name == "Polymarket"

    @patch.object(pm, "get_markets")
    def test_get_markets_delegates(self, mock_fn):
        mock_fn.return_value = pd.DataFrame()
        mp.PolymarketProvider().get_markets(limit=10, active=True)
        mock_fn.assert_called_once_with(limit=10, active=True, order="volume24hr")

    @patch.object(pm, "get_market")
    def test_get_market_delegates(self, mock_fn):
        mock_fn.return_value = {}
        mp.PolymarketProvider().get_market("0xabc")
        mock_fn.assert_called_once_with("0xabc")

    @patch.object(pm, "get_events")
    def test_get_events_delegates(self, mock_fn):
        mock_fn.return_value = pd.DataFrame()
        mp.PolymarketProvider().get_events(limit=100)
        mock_fn.assert_called_once_with(limit=100)

    @patch.object(pm, "enrich_categories_from_events")
    def test_enrich_delegates(self, mock_fn):
        df_m, df_e = pd.DataFrame(), pd.DataFrame()
        mp.PolymarketProvider().enrich_categories_from_events(df_m, df_e)
        mock_fn.assert_called_once_with(df_m, df_e)

    @patch.object(pm, "get_price_history")
    def test_get_price_history_delegates(self, mock_fn):
        mock_fn.return_value = pd.DataFrame()
        mp.PolymarketProvider().get_price_history("token1", days=7, fidelity=15)
        mock_fn.assert_called_once_with("token1", days=7, fidelity=15)

    @patch.object(pm, "get_order_book")
    def test_get_order_book_delegates(self, mock_fn):
        mock_fn.return_value = {}
        mp.PolymarketProvider().get_order_book("token1")
        mock_fn.assert_called_once_with("token1")

    @patch.object(pm, "get_midpoint")
    def test_get_midpoint_delegates(self, mock_fn):
        mock_fn.return_value = 0.42
        result = mp.PolymarketProvider().get_midpoint("token1")
        mock_fn.assert_called_once_with("token1")
        assert result == 0.42

    @patch.object(pm, "get_closed_markets")
    def test_get_closed_markets_delegates(self, mock_fn):
        mock_fn.return_value = pd.DataFrame()
        mp.PolymarketProvider().get_closed_markets(limit=50)
        mock_fn.assert_called_once_with(limit=50)

    @patch.object(pm, "get_market")
    def test_list_outcome_tokens_parses_clob_and_outcomes(self, mock_fn):
        # Gamma encodes both fields as JSON strings; the provider method
        # must parse them and emit one entry per token with the matching
        # outcome label.
        mock_fn.return_value = {
            "clobTokenIds": '["77000000000001", "77000000000002"]',
            "outcomes": '["Yes", "No"]',
        }
        tokens = mp.PolymarketProvider().list_outcome_tokens("0xabc")
        assert tokens == [
            {"token_id": "77000000000001", "outcome": "Yes"},
            {"token_id": "77000000000002", "outcome": "No"},
        ]

    @patch.object(pm, "get_market")
    def test_list_outcome_tokens_empty_when_no_clob_ids(self, mock_fn):
        mock_fn.return_value = {"clobTokenIds": None, "outcomes": None}
        assert mp.PolymarketProvider().list_outcome_tokens("0xabc") == []

    @patch.object(pm, "get_market_calibration_data")
    def test_get_market_calibration_data_delegates(self, mock_fn):
        mock_fn.return_value = pd.DataFrame()
        df = pd.DataFrame()
        mp.PolymarketProvider().get_market_calibration_data(
            df, lead_hours=12, max_markets=20,
        )
        mock_fn.assert_called_once_with(
            df, lead_hours=12, max_markets=20, progress_callback=None,
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_polymarket_provider_satisfies_protocol(self):
        """Runtime isinstance check against MarketProvider Protocol."""
        provider = mp.PolymarketProvider()
        assert isinstance(provider, mp.MarketProvider)

    def test_get_provider_returns_protocol(self):
        provider = mp.get_provider("polymarket")
        assert isinstance(provider, mp.MarketProvider)


# ---------------------------------------------------------------------------
# Custom provider registration (Phase 2 prep — verifies the registry can
# accept a new provider without code changes to provider.py)
# ---------------------------------------------------------------------------

class _StubProvider:
    """Minimal Protocol-conforming stub for testing registration."""
    name = "stub"
    display_name = "Stub Provider"

    def get_markets(self, limit=500, active=True, order="volume24hr"):
        return pd.DataFrame()
    def get_market(self, market_id): return {}
    def get_events(self, limit=500): return pd.DataFrame()
    def enrich_categories_from_events(self, df_markets, df_events):
        return df_markets
    def get_price_history(self, token_id, days=30, fidelity=60):
        return pd.DataFrame()
    def get_order_book(self, token_id): return {}
    def get_midpoint(self, token_id): return None
    def list_outcome_tokens(self, market_id): return []
    def get_closed_markets(self, limit=100): return pd.DataFrame()
    def get_closed_markets_in_range(self, *, since_days, until_days=0.0,
                                     max_items=10000, min_volume=0.0,
                                     progress_callback=None):
        return pd.DataFrame()
    def get_markets_closing_within(self, max_days, *, limit=500, min_days=0.0):
        return pd.DataFrame()
    def lookup_markets_by_id(self, market_or_event_id):
        return pd.DataFrame()
    def get_market_calibration_data(self, df_closed, lead_hours=24,
                                     max_markets=100, progress_callback=None):
        return pd.DataFrame()


class TestRegisterProvider:
    def test_can_register_and_retrieve(self, monkeypatch):
        # Snapshot existing registry so we can restore
        original = dict(mp._PROVIDERS)
        try:
            mp.register_provider(_StubProvider())
            assert "stub" in mp.list_provider_names()
            assert mp.get_provider("stub").name == "stub"
        finally:
            mp._PROVIDERS.clear()
            mp._PROVIDERS.update(original)

    def test_registered_stub_satisfies_protocol(self, monkeypatch):
        original = dict(mp._PROVIDERS)
        try:
            stub = _StubProvider()
            mp.register_provider(stub)
            assert isinstance(stub, mp.MarketProvider)
        finally:
            mp._PROVIDERS.clear()
            mp._PROVIDERS.update(original)
