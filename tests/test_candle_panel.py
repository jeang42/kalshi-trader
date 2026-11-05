"""Offline tests for candle_panel: store bounds, idempotency, resumability,
panel reconstruction. Network fully mocked."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from pm.research import candle_panel as cp

CLOSE = "2026-08-08T20:00:00Z"
CLOSE_TS = int(datetime(2026, 8, 8, 20, tzinfo=UTC).timestamp())


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("CANDLE_PANEL_DB", str(tmp_path / "cp.sqlite"))
    assert cp._db_path().startswith(str(tmp_path))
    c = cp._con()
    yield c
    c.close()


def _candles(hours_before_close, *, bid=0.15, ask=0.17):
    rows = []
    for h in hours_before_close:
        ts = CLOSE_TS - int(h * 3600)
        rows.append({"timestamp": pd.Timestamp(ts, unit="s", tz="UTC"),
                     "price": (bid + ask) / 2, "yes_bid_close": bid,
                     "yes_ask_close": ask, "volume": 10.0,
                     "open_interest": 100.0})
    return pd.DataFrame(rows)


def _closed_df(tickers, resolved=True):
    return pd.DataFrame([{
        "id": t, "event_id": t.rsplit("-", 1)[0], "category": "Crypto",
        "question": f"q {t}", "closed_time": CLOSE, "resolved_yes": resolved,
        "volume": 500.0,
    } for t in tickers])


class TestStoreBounds:
    def test_keeps_only_final_hours(self, con):
        df = _candles([1, 2, 50, cp.FINAL_HOURS + 5])   # last one out of range
        n = cp._store_candles(con, "T1", CLOSE_TS, df)
        assert n == 3

    def test_idempotent(self, con):
        df = _candles([1, 2])
        assert cp._store_candles(con, "T1", CLOSE_TS, df) == 2
        cp._store_candles(con, "T1", CLOSE_TS, df)
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 2


class TestCycle:
    def test_fetches_and_is_resumable(self, con, monkeypatch):
        monkeypatch.setattr(cp.kc, "get_closed_markets_in_range",
                            lambda **k: _closed_df(["A-1", "B-1", "C-1"]))
        calls = []
        monkeypatch.setattr(cp.kc, "get_candlesticks",
                            lambda tk, **k: calls.append(tk) or _candles([1, 2]))
        r1 = cp.run_cycle(max_markets=2, verbose=False)
        assert r1["fetched"] == 2 and r1["remaining"] == 1
        r2 = cp.run_cycle(max_markets=10, verbose=False)
        assert r2["fetched"] == 1                # only the remainder
        assert sorted(calls) == ["A-1", "B-1", "C-1"]

    def test_skips_non_binary(self, con, monkeypatch):
        df = _closed_df(["A-1"], resolved=None)  # scalar / unsettled
        monkeypatch.setattr(cp.kc, "get_closed_markets_in_range", lambda **k: df)
        monkeypatch.setattr(cp.kc, "get_candlesticks",
                            lambda tk, **k: _candles([1]))
        assert cp.run_cycle(verbose=False)["fetched"] == 0

    def test_empty_feed_is_loud_not_silent(self, con, monkeypatch, caplog):
        monkeypatch.setattr(cp.kc, "get_closed_markets_in_range",
                            lambda **k: pd.DataFrame())
        import logging
        with caplog.at_level(logging.WARNING):
            r = cp.run_cycle(verbose=False)
        assert r["fetched"] == 0
        assert any("ZERO markets" in m for m in caplog.messages)

    def test_candle_failure_skips_market_not_cycle(self, con, monkeypatch):
        monkeypatch.setattr(cp.kc, "get_closed_markets_in_range",
                            lambda **k: _closed_df(["A-1", "B-1"]))
        def boom_then_ok(tk, **k):
            if tk == "A-1":
                raise RuntimeError("500")
            return _candles([1])
        monkeypatch.setattr(cp.kc, "get_candlesticks", boom_then_ok)
        r = cp.run_cycle(verbose=False)
        assert r["fetched"] == 1                 # B fetched despite A failing


class TestLoadPanel:
    def test_reconstructs_at_lead(self, con, monkeypatch):
        monkeypatch.setattr(cp.kc, "get_closed_markets_in_range",
                            lambda **k: _closed_df(["A-1"]))
        monkeypatch.setattr(cp.kc, "get_candlesticks",
                            lambda tk, **k: _candles([1, 2, 3], bid=0.90, ask=0.94))
        cp.run_cycle(verbose=False)
        p = cp.load_panel(2.0)
        assert len(p) == 1
        row = p.iloc[0]
        assert row.yes_bid == 0.90 and row.yes_ask == 0.94
        assert abs(row.lead_hours - 2.0) < 0.01
        assert row.resolved_yes == True          # noqa: E712

    def test_missing_lead_dropped(self, con, monkeypatch):
        monkeypatch.setattr(cp.kc, "get_closed_markets_in_range",
                            lambda **k: _closed_df(["A-1"]))
        monkeypatch.setattr(cp.kc, "get_candlesticks",
                            lambda tk, **k: _candles([1]))    # only 1h lead
        cp.run_cycle(verbose=False)
        assert len(cp.load_panel(48.0)) == 0     # no candle near 48h
