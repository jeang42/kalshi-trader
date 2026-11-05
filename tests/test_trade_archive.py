"""Tests for the executed-trades layer: kalshi_client trade/candlestick
parsing, trade_archive CRUD, and trade_poller fetch logic. No network —
``kalshi_client._get`` is mocked."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from pm.archive import trade_archive
from pm.venue import kalshi_client as kc

# ---------------------------------------------------------------------------
# Sample raw responses (shape verified against the live API on 2026-05-29)
# ---------------------------------------------------------------------------

RAW_TRADE_A = {
    "trade_id": "aaaa-1",
    "ticker": "KXMLBGAME-26MAY28OKCSAS-OKC",
    "created_time": "2026-05-29T01:19:22.525275Z",
    "count_fp": "31.63",
    "yes_price_dollars": "0.6100",
    "no_price_dollars": "0.3900",
    "taker_side": "yes",
    "taker_outcome_side": "yes",
}
RAW_TRADE_B = {
    "trade_id": "aaaa-2",
    "ticker": "KXMLBGAME-26MAY28OKCSAS-OKC",
    "created_time": "2026-05-29T01:20:00Z",
    "count_fp": "0.10",
    "yes_price_dollars": "0.6200",
    "no_price_dollars": "0.3800",
    "taker_side": "no",
    "taker_outcome_side": "no",
}


@pytest.fixture
def archive_path(tmp_path, monkeypatch):
    p = tmp_path / "test_trade_archive.sqlite"
    monkeypatch.setenv("TRADE_ARCHIVE_PATH", str(p))
    return p


# ---------------------------------------------------------------------------
# kalshi_client: trade normalization + pagination
# ---------------------------------------------------------------------------

class TestNormalizeTrade:
    def test_parses_dollars_and_count_fp(self):
        row = kc._normalize_trade(RAW_TRADE_A)
        assert row["trade_id"] == "aaaa-1"
        assert row["ticker"].endswith("-OKC")
        assert row["count"] == pytest.approx(31.63)
        assert row["yes_price"] == pytest.approx(0.61)
        assert row["no_price"] == pytest.approx(0.39)
        assert row["taker_side"] == "yes"
        # 2026-05-29T01:19:22.525275Z → unix seconds (sub-second truncated)
        assert row["ts_unix"] == 1780017562

    def test_missing_id_or_ticker_returns_none(self):
        assert kc._normalize_trade({"ticker": "X", "created_time": "2026-01-01T00:00:00Z"}) is None
        assert kc._normalize_trade({"trade_id": "x", "created_time": "2026-01-01T00:00:00Z"}) is None

    def test_unparseable_time_returns_none(self):
        bad = dict(RAW_TRADE_A, created_time="not-a-time")
        assert kc._normalize_trade(bad) is None

    def test_falls_back_to_integer_count(self):
        d = dict(RAW_TRADE_A)
        del d["count_fp"]
        d["count"] = 5
        assert kc._normalize_trade(d)["count"] == pytest.approx(5.0)


class TestGetAllTrades:
    @patch.object(kc, "_get")
    def test_paginates_and_normalizes(self, mock_get):
        mock_get.side_effect = [
            {"trades": [RAW_TRADE_A], "cursor": "next"},
            {"trades": [RAW_TRADE_B], "cursor": ""},
        ]
        df = kc.get_all_trades("KXMLBGAME-26MAY28OKCSAS-OKC")
        assert list(df["trade_id"]) == ["aaaa-1", "aaaa-2"]
        # Sorted oldest-first by ts_unix
        assert df["ts_unix"].is_monotonic_increasing
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    @patch.object(kc, "_get")
    def test_empty_returns_typed_frame(self, mock_get):
        mock_get.return_value = {"trades": [], "cursor": ""}
        df = kc.get_all_trades("KX-NONE")
        assert df.empty
        assert "taker_side" in df.columns

    @patch.object(kc, "_get")
    def test_passes_time_window_params(self, mock_get):
        mock_get.return_value = {"trades": [], "cursor": ""}
        kc.get_all_trades("KX-X", since_ts=100, until_ts=200)
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["min_ts"] == 100
        assert kwargs["params"]["max_ts"] == 200
        assert kwargs["params"]["ticker"] == "KX-X"


class TestGetTrades:
    @patch.object(kc, "_get")
    def test_clamps_limit_and_builds_params(self, mock_get):
        mock_get.return_value = {"trades": [], "cursor": ""}
        kc.get_trades("KX-X", limit=99999, min_ts=5, cursor="c")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["limit"] == 1000
        assert kwargs["params"]["min_ts"] == 5
        assert kwargs["params"]["cursor"] == "c"


class TestGetCandlesticks:
    @patch.object(kc, "_get")
    def test_surfaces_volume_and_open_interest(self, mock_get):
        mock_get.return_value = {"candlesticks": [
            {
                "end_period_ts": 1779023962,
                "yes_bid": {"close_dollars": "0.60"},
                "yes_ask": {"close_dollars": "0.62"},
                "volume": 1234,
                "open_interest": 5678,
            },
        ]}
        df = kc.get_candlesticks("KXMLBGAME-26MAY28OKCSAS-OKC")
        assert df.iloc[0]["price"] == pytest.approx(0.61)
        assert df.iloc[0]["volume"] == pytest.approx(1234)
        assert df.iloc[0]["open_interest"] == pytest.approx(5678)

    @patch.object(kc, "_get")
    def test_reads_fixed_point_volume_fields(self, mock_get):
        # The live payload shape (captured 2026-10-13): volume/OI arrive ONLY
        # as *_fp strings. Reading the bare keys silently returned 0.
        mock_get.return_value = {"candlesticks": [
            {
                "end_period_ts": 1790715600,
                "yes_bid": {"close_dollars": "0.8900"},
                "yes_ask": {"close_dollars": "0.9000"},
                "price": {"close_dollars": "0.9000"},
                "volume_fp": "8025.37",
                "open_interest_fp": "12001.00",
            },
        ]}
        df = kc.get_candlesticks("KXNASDAQ100U-26SEP30H1600-T29349.99")
        assert df.iloc[0]["volume"] == pytest.approx(8025.37)
        assert df.iloc[0]["open_interest"] == pytest.approx(12001.0)


# ---------------------------------------------------------------------------
# trade_archive: CRUD + dedup
# ---------------------------------------------------------------------------

def _rows() -> list[dict]:
    return [kc._normalize_trade(RAW_TRADE_A), kc._normalize_trade(RAW_TRADE_B)]


class TestTradeArchive:
    def test_store_and_range_query(self, archive_path):
        n = trade_archive.store_trades(_rows())
        assert n == 2
        df = trade_archive.get_trades_in_range(
            "KXMLBGAME-26MAY28OKCSAS-OKC", since_ts=0, until_ts=2_000_000_000,
        )
        assert len(df) == 2
        assert df["ts_unix"].is_monotonic_increasing
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    def test_dedup_on_trade_id(self, archive_path):
        assert trade_archive.store_trades(_rows()) == 2
        # Re-storing the same trades inserts nothing new.
        assert trade_archive.store_trades(_rows()) == 0

    def test_store_df_roundtrip(self, archive_path):
        with patch.object(kc, "_get") as mock_get:
            mock_get.return_value = {"trades": [RAW_TRADE_A, RAW_TRADE_B], "cursor": ""}
            df = kc.get_all_trades("KXMLBGAME-26MAY28OKCSAS-OKC")
        assert trade_archive.store_trades_df(df) == 2

    def test_latest_trade_ts(self, archive_path):
        assert trade_archive.latest_trade_ts("KX-NONE") is None
        trade_archive.store_trades(_rows())
        assert trade_archive.latest_trade_ts("KXMLBGAME-26MAY28OKCSAS-OKC") == 1780017600

    def test_range_excludes_out_of_window(self, archive_path):
        trade_archive.store_trades(_rows())
        df = trade_archive.get_trades_in_range(
            "KXMLBGAME-26MAY28OKCSAS-OKC", since_ts=0, until_ts=1780017562,
        )
        assert len(df) == 1  # only trade A at exactly 1780017562

    def test_skips_bad_rows(self, archive_path):
        good = kc._normalize_trade(RAW_TRADE_A)
        assert trade_archive.store_trades([good, {"oops": 1}]) == 1

    def test_stats_and_clear(self, archive_path):
        trade_archive.store_trades(_rows())
        s = trade_archive.stats()
        assert s["entries"] == 2
        assert s["markets"] == 1
        assert trade_archive.clear() == 2
        assert trade_archive.stats()["entries"] == 0

    def test_empty_range_typed(self, archive_path):
        df = trade_archive.get_trades_in_range("KX-X", since_ts=0, until_ts=1)
        assert df.empty
        assert "ts" in df.columns


# ---------------------------------------------------------------------------
# trade_poller: incremental vs backfill fetch logic
# ---------------------------------------------------------------------------

class TestTradePoller:
    def test_poll_ticker_uses_latest_ts_as_since(self, archive_path):
        from pm.capture import trade_poller
        trade_archive.store_trades(_rows())
        captured = {}

        def fake_get_all_trades(ticker, *, since_ts=None, until_ts=None, **kw):
            captured["since"] = since_ts
            return pd.DataFrame()

        with patch.object(trade_poller.kalshi_client, "get_all_trades",
                          side_effect=fake_get_all_trades):
            trade_poller.poll_ticker("KXMLBGAME-26MAY28OKCSAS-OKC")
        # Incremental poll starts at the latest archived trade.
        assert captured["since"] == trade_archive.latest_trade_ts(
            "KXMLBGAME-26MAY28OKCSAS-OKC")

    def test_backfill_ticker_stores(self, archive_path):
        from pm.capture import trade_poller
        df = pd.DataFrame(_rows())
        df["ts"] = pd.to_datetime(df["ts_unix"], unit="s", utc=True)
        with patch.object(trade_poller.kalshi_client, "get_all_trades",
                          return_value=df):
            r = trade_poller.backfill_ticker("KXMLBGAME-26MAY28OKCSAS-OKC", days=3)
        assert r["fetched"] == 2
        assert r["stored"] == 2


# ---------------------------------------------------------------------------
# kalshi_ws: trade-message normalization (cents vs dollars tolerance)
# ---------------------------------------------------------------------------

class TestWsTradeNormalize:
    def test_dollars_schema(self):
        from pm.capture import kalshi_ws
        # Real WS trade shape (verified 2026-05): == REST, with count_fp + ts.
        row = kalshi_ws._normalize_ws_trade({
            "market_ticker": "KX-X", "ts": 1779023962,
            "yes_price_dollars": "0.62", "no_price_dollars": "0.38",
            "count_fp": "15.29", "taker_side": "yes", "trade_id": "z1",
        })
        assert row["yes_price"] == pytest.approx(0.62)
        assert row["count"] == pytest.approx(15.29)  # count_fp, not count
        assert row["trade_id"] == "z1"

    def test_count_fp_preferred_over_count(self):
        from pm.capture import kalshi_ws
        row = kalshi_ws._normalize_ws_trade({
            "market_ticker": "KX-X", "ts": 1, "yes_price_dollars": "0.5",
            "count_fp": "7.5", "count": 7, "taker_side": "no",
        })
        assert row["count"] == pytest.approx(7.5)

    def test_cents_schema_divided(self):
        from pm.capture import kalshi_ws
        row = kalshi_ws._normalize_ws_trade({
            "market_ticker": "KX-X", "ts": 1779023962,
            "yes_price": 62, "count": 4, "taker_side": "yes",
        })
        assert row["yes_price"] == pytest.approx(0.62)
        # no_price derived from yes when absent
        assert row["no_price"] == pytest.approx(0.38)

    def test_synthesizes_trade_id_when_absent(self):
        from pm.capture import kalshi_ws
        row = kalshi_ws._normalize_ws_trade({
            "market_ticker": "KX-X", "ts": 100, "yes_price": 50, "count": 1,
        })
        assert "KX-X:100:" in row["trade_id"]

    def test_no_ticker_returns_none(self):
        from pm.capture import kalshi_ws
        assert kalshi_ws._normalize_ws_trade({"yes_price": 50}) is None
