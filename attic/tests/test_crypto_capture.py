"""Offline tests for crypto_capture: ticker mapping, selection, store."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

import crypto_capture as cc

NOW = datetime(2026, 6, 19, 18, 0, tzinfo=UTC)


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setenv("CRYPTO_CAPTURE_DB", str(tmp_path / "cc.sqlite"))
    cc.migrate()
    return tmp_path


class TestPure:
    def test_asset_from_ticker_longest_prefix(self):
        assert cc.asset_from_ticker("KXBTC15M-26JUN191600-00") == "BTC"
        assert cc.asset_from_ticker("KXBTC-26JUN1917-B59250") == "BTC"
        assert cc.asset_from_ticker("KXETHD-26JUN19-T3000") == "ETH"
        assert cc.asset_from_ticker("KXDOGE15M-26JUN19-X") == "DOGE"
        assert cc.asset_from_ticker("KXMLBGAME-26JUN19-NYY") is None

    def test_mins_to_close(self):
        assert cc._mins_to_close("2026-06-19T19:00:00Z", NOW) == pytest.approx(60.0)
        assert cc._mins_to_close("bad", NOW) is None

    def test_yes_mid(self):
        assert cc._yes_mid(0.40, 0.44, 0.3) == pytest.approx(0.42)
        assert cc._yes_mid(None, None, 0.31) == pytest.approx(0.31)


class TestSelect:
    def test_filters_to_near_money_crypto(self):
        df = pd.DataFrame([
            {"condition_id": "KXBTC-1-A", "best_bid": 0.40, "best_ask": 0.44,
             "last_trade_price": 0.4, "volume": 100},   # near-money crypto ✓
            {"condition_id": "KXBTC-1-B", "best_bid": 0.0, "best_ask": 0.02,
             "last_trade_price": 0.01, "volume": 100},  # deep-OTM → drop
            {"condition_id": "KXMLBGAME-1-NYY", "best_bid": 0.5, "best_ask": 0.52,
             "last_trade_price": 0.5, "volume": 100},   # not crypto → drop
        ])
        sel = cc.select_markets(df)
        assert list(sel["condition_id"]) == ["KXBTC-1-A"]
        assert sel.iloc[0]["asset"] == "BTC"

    def test_empty(self):
        assert cc.select_markets(pd.DataFrame()).empty


class TestStore:
    def _row(self, ticker, ts):
        return {"ticker": ticker, "ts": ts, "asset": "BTC",
                "close_time": "2026-06-19T19:00:00+00:00", "mins_to_close": 60.0,
                "spot": 60000.0, "yes_bid": 0.4, "yes_ask": 0.44, "yes_mid": 0.42,
                "volume": 100}

    def test_store_timeseries_dedup(self, tmpdb):
        assert cc.store_snapshots([self._row("KXBTC-1-A", "2026-06-19T18:00:00+00:00")]) == 1
        assert cc.store_snapshots([self._row("KXBTC-1-A", "2026-06-19T18:00:00+00:00")]) == 0
        assert cc.store_snapshots([self._row("KXBTC-1-A", "2026-06-19T18:00:20+00:00")]) == 1
        s = cc.stats()
        assert s["snapshots"] == 2 and s["markets"] == 1 and s["by_asset"]["BTC"] == 2
