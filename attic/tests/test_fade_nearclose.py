"""Offline tests for fade_nearclose: asset-level correlation control.

Focused on the 2026-08-02 fix — `event_of` de-correlates one-winner fields but
not two series written on the SAME underlying settling the same hour, which the
live canary proved resolve together (KXSOLD+KXSOLE @01:00, KXXRPD+KXXRP @05:00
each lost as a pair).
"""

from __future__ import annotations

import pandas as pd
import pytest

from pm.arms import fade_nearclose as fn


@pytest.fixture
def con(tmp_path, monkeypatch):
    # The env var now works: fade_nearclose resolves its path per call via
    # _db_path() instead of freezing it at import into a module-level `DB`.
    # That frozen constant is why this fixture used to patch the module
    # attribute — with setenv a no-op, _con() opened the REAL production
    # database and inserted a synthetic row into it, twice.
    monkeypatch.setenv("FADE_NEARCLOSE_DB", str(tmp_path / "fn.sqlite"))
    c = fn._con()
    assert fn._db_path().startswith(str(tmp_path))   # guard: never the live DB
    yield c
    c.close()


def _cand(ticker, *, event_id, close_time, bid=0.15, ask=0.17, volume=500):
    """One discovery row shaped like _discover()'s output."""
    return {
        "condition_id": ticker, "event_id": event_id, "category": "Crypto",
        "question": f"q {ticker}", "best_bid": bid, "best_ask": ask,
        "yes_mid": (bid + ask) / 2, "volume": volume, "close_time": close_time,
    }


class TestAssetOf:
    def test_collapses_sibling_crypto_series(self):
        # the exact pairs that lost together in the live canary
        assert fn.asset_of("KXSOLD-26AUG0121-T71.9999") == "SOL"
        assert fn.asset_of("KXSOLE-26AUG0121-B72.125") == "SOL"
        assert fn.asset_of("KXXRPD-26AUG0201-T1.0799") == "XRP"
        assert fn.asset_of("KXXRP-26AUG0201-B1.0899500") == "XRP"

    def test_cadence_and_qualifier_suffixes(self):
        assert fn.asset_of("KXSOL15M-26AUG020000-00") == "SOL"
        assert fn.asset_of("KXXRPMINMON-XRP-26JUL31-100") == "XRP"
        assert fn.asset_of("KXETHD-26AUG0117-T1849.99") == "ETH"

    def test_longest_match_wins(self):
        # HYPE must not parse as HYP + "E"
        assert fn.asset_of("KXHYPE15M-x") == "HYPE"
        assert fn.asset_of("KXHYPED-x") == "HYPE"

    def test_non_crypto_falls_back_to_series(self):
        assert fn.asset_of("KXRAIN-26AUG01-SATX") == "KXRAIN"
        assert fn.asset_of("KXNETFLIXRANKMOVIE-x") == "KXNETFLIXRANKMOVIE"

    def test_collapses_commodity_and_index_series(self):
        """2026-08-06 regression: three series, one barrel of crude."""
        for t in ("KXWTI-26AUG0314-T78.49", "KXWTIH-26AUG0314-T79.39",
                  "KXWTI15M-26AUG041200-00"):
            assert fn.asset_of(t) == "WTI", t
        assert fn.asset_of("KXNASDAQ100U-26AUG04H160") == "NASDAQ"
        assert fn.asset_of("KXNASDAQ100-26AUG04H1600") == "NASDAQ"
        assert fn.asset_of("KXGOLDH-x") == fn.asset_of("KXGOLDD-x") == "GOLD"
        assert fn.asset_of("KXSILVERH-x") == fn.asset_of("KXSILVERD-x") == "SILVER"

    def test_commodity_pair_is_one_bet_same_hour(self, tmp_path, monkeypatch):
        """The observed duplicate: KXWTI + KXWTIH at the same close-hour."""
        monkeypatch.setenv("FADE_NEARCLOSE_DB", str(tmp_path / "fn.sqlite"))
        c = fn._con()
        try:
            a = ("KXWTI-26AUG0314-T78.49", "2026-08-03T18:00:00Z")
            b = ("KXWTIH-26AUG0314-T79.39", "2026-08-03T18:00:00Z")
            assert (fn.asset_of(a[0]), fn.close_hour(a[1])) == \
                   (fn.asset_of(b[0]), fn.close_hour(b[1]))
        finally:
            c.close()


class TestZFeature:
    def test_strike_of_t_strikes(self):
        assert fn.strike_of("KXBTCD-26AUG0117-T62749.99") == 62749.99
        assert fn.strike_of("KXXRPD-26AUG0201-T1.0799") == 1.0799

    def test_strike_of_rejects_b_and_nonstrike(self):
        assert fn.strike_of("KXSOLE-26AUG0121-B72.125") is None
        assert fn.strike_of("KXRAIN-26AUG01-SATX") is None

    def test_compute_z_sign_and_scale(self):
        # 5% above strike, 1% hourly vol, 4h left -> 5% / 2% = 2.5 sigmas-ish
        z = fn.compute_z(105.0, 100.0, 0.01, 4.0)
        assert 2.3 < z < 2.5
        assert fn.compute_z(95.0, 100.0, 0.01, 4.0) < 0

    def test_compute_z_degenerate_inputs(self):
        assert fn.compute_z(100, 100, 0.0, 4.0) is None      # zero vol
        assert fn.compute_z(100, 100, 0.01, 0.0) is None     # expired
        assert fn.compute_z(None, 100, 0.01, 4.0) is None

    def test_z_for_entry_never_raises_on_junk(self, monkeypatch):
        # network paths mocked out — must degrade to Nones, not raise
        from pm.feeds import crypto_data
        monkeypatch.setattr(crypto_data, "get_spot_price",
                            lambda a: (_ for _ in ()).throw(RuntimeError("down")))
        assert fn.z_for_entry("KXBTCD-x-T100", "not-a-timestamp") == (None, None, None)
        assert fn.z_for_entry("KXBTCD-x-T100", "") == (None, None, None)

    def test_open_records_z_for_crypto(self, con, monkeypatch):
        monkeypatch.setattr(fn, "z_for_entry", lambda tk, ct: (74.8, 0.004, 7.5))
        df = pd.DataFrame([_cand("KXSOLD-26AUG0121-T71.9999",
                                 event_id="KXSOLD-26AUG0121",
                                 close_time="2026-08-02T01:00:00Z")])
        assert fn._open(con, df) == 1
        spot, sigma, z = con.execute(
            "SELECT spot, sigma, z FROM positions").fetchone()
        assert (spot, sigma, z) == (74.8, 0.004, 7.5)


class TestCloseHour:
    def test_buckets_to_hour(self):
        assert fn.close_hour("2026-08-02T01:00:00Z") == "2026-08-02T01"
        assert fn.close_hour("2026-08-02T01:45:00Z") == "2026-08-02T01"

    def test_tolerates_empty(self):
        assert fn.close_hour("") == ""
        assert fn.close_hour(None) == ""


class TestAssetHourCap:
    def test_blocks_sibling_series_same_asset_same_hour(self, con):
        """The regression: two SOL series settling 01:00 are ONE bet."""
        df = pd.DataFrame([
            _cand("KXSOLD-26AUG0121-T71.9999", event_id="KXSOLD-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
            _cand("KXSOLE-26AUG0121-B72.125", event_id="KXSOLE-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
        ])
        assert fn._open(con, df) == 1
        held = [r[0] for r in con.execute("SELECT ticker FROM positions")]
        assert len(held) == 1

    def test_allows_same_asset_different_hour(self, con):
        """Serial exposure is deliberately NOT capped — only same-hour dupes."""
        df = pd.DataFrame([
            _cand("KXSOLD-26AUG0121-T71.9999", event_id="KXSOLD-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
            _cand("KXSOLD-26AUG0122-T72.4999", event_id="KXSOLD-26AUG0122",
                  close_time="2026-08-02T02:00:00Z"),
        ])
        assert fn._open(con, df) == 2

    def test_allows_different_assets_same_hour(self, con):
        df = pd.DataFrame([
            _cand("KXSOLD-26AUG0121-T71.9999", event_id="KXSOLD-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
            _cand("KXETHD-26AUG0121-T1849.99", event_id="KXETHD-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
        ])
        assert fn._open(con, df) == 2

    def test_cap_persists_across_cycles(self, con):
        """A later cycle must still see the earlier cycle's asset-hour claim."""
        first = pd.DataFrame([
            _cand("KXSOLD-26AUG0121-T71.9999", event_id="KXSOLD-26AUG0121",
                  close_time="2026-08-02T01:00:00Z")])
        second = pd.DataFrame([
            _cand("KXSOLE-26AUG0121-B72.125", event_id="KXSOLE-26AUG0121",
                  close_time="2026-08-02T01:00:00Z")])
        assert fn._open(con, first) == 1
        assert fn._open(con, second) == 0
