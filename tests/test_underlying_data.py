"""Offline tests for underlying_data (Yahoo commodity/index/FX quotes) and the
z_for_entry routing/staleness policy built on it. All network mocked."""

from __future__ import annotations

import pytest

from pm.feeds import underlying_data as ud


def _chart_payload(price, qtime, closes=None):
    return {
        "meta": {"regularMarketPrice": price, "regularMarketTime": qtime},
        "indicators": {"quote": [{"close": closes or []}]},
    }


class TestSupported:
    def test_roots_map_to_symbols(self):
        assert ud.supported("GOLD") and ud.supported("WTI")
        assert ud.supported("KXUST30A")
        assert not ud.supported("SOL")       # crypto belongs to crypto_data
        assert not ud.supported("KXRAIN")


class TestGetSpot:
    def test_returns_price_and_age(self, monkeypatch):
        import time
        now = time.time()
        monkeypatch.setattr(ud, "_chart",
                            lambda s, **k: _chart_payload(4399.7, now - 120))
        price, age = ud.get_spot("GOLD")
        assert price == 4399.7
        assert 100 < age < 200

    def test_stale_quote_still_reported_with_age(self, monkeypatch):
        """Staleness POLICY lives at the caller; the feed reports honestly."""
        import time
        monkeypatch.setattr(ud, "_chart",
                            lambda s, **k: _chart_payload(78.2, time.time() - 72000))
        price, age = ud.get_spot("WTI")
        assert age > 70000

    def test_yield_sanity_band(self, monkeypatch):
        """A silent x10 scale flip on treasury yields must become None."""
        import time
        monkeypatch.setattr(ud, "_chart",
                            lambda s, **k: _chart_payload(52.1, time.time()))
        assert ud.get_spot("KXUST30A") is None      # 52.1% is not a yield
        monkeypatch.setattr(ud, "_chart",
                            lambda s, **k: _chart_payload(5.21, time.time()))
        assert ud.get_spot("KXUST30A")[0] == 5.21

    def test_shape_surprises_degrade_to_none(self, monkeypatch):
        monkeypatch.setattr(ud, "_chart", lambda s, **k: None)
        assert ud.get_spot("GOLD") is None
        monkeypatch.setattr(ud, "_chart", lambda s, **k: {"meta": {}})
        assert ud.get_spot("GOLD") is None


class TestHourlySigma:
    def test_sigma_from_closes(self, monkeypatch):
        closes = [100 + (i % 3) for i in range(60)]      # oscillating series
        monkeypatch.setattr(ud, "_chart",
                            lambda s, **k: _chart_payload(100, 0, closes))
        ud._SIGMA_CACHE.clear()
        sig = ud.hourly_sigma("GOLD")
        assert sig and 0 < sig < 0.1

    def test_too_few_candles_is_none(self, monkeypatch):
        monkeypatch.setattr(ud, "_chart",
                            lambda s, **k: _chart_payload(100, 0, [100.0] * 5))
        ud._SIGMA_CACHE.clear()
        assert ud.hourly_sigma("SILVER") is None


# TestZForEntryRouting moved to attic/tests/test_nearclose_fragments.py on
# 2026-08-24 — it tested fade_nearclose.z_for_entry's ROUTING into this
# module (commodity → underlying_data, crypto → crypto_data, unsupported →
# refuse), and the arm retired. The pure underlying_data behavior above is
# still fully covered here.
