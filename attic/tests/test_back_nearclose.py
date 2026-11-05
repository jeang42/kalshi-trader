"""Offline tests for back_nearclose (favorite-back paper arm).

Covers the class/band gates that define what the arm may trade, the
asset-hour de-correlation it inherited from the fade arm's canary bug, and the
resolution path that silently froze two sibling arms for weeks.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pm.arms import back_nearclose as bn


@pytest.fixture
def con(tmp_path, monkeypatch):
    # back_nearclose resolves its path lazily, so the env var IS the right knob
    # here (unlike fade_nearclose's import-time constant). Assert it landed.
    monkeypatch.setenv("BACK_NEARCLOSE_DB", str(tmp_path / "bn.sqlite"))
    assert bn._db_path().startswith(str(tmp_path))
    c = bn._con()
    yield c
    c.close()


def _cand(ticker, *, event_id, close_time, bid=0.93, ask=0.95,
          volume=500, category="Crypto"):
    return {
        "condition_id": ticker, "event_id": event_id, "category": category,
        "question": f"q {ticker}", "best_bid": bid, "best_ask": ask,
        "yes_mid": (bid + ask) / 2, "volume": volume, "close_time": close_time,
    }


class TestClassify:
    def test_continuous_classes_we_back(self):
        assert bn.classify("KXBTCD-26AUG0117-T62749.99") == "crypto"
        assert bn.classify("KXHYPE15M-x") == "crypto"          # not HYP + E
        assert bn.classify("KXGOLDD-26AUG01", "Commodities") == "commodity"
        assert bn.classify("KXINXU-x", "Financials") == "finance"

    def test_classes_we_refuse(self):
        # discrete measured -3.37c/ct; weather decays to negative; sports never
        assert bn.classify("KXNETFLIXRANKMOVIE-x", "Entertainment") == "discrete"
        assert bn.classify("KXTEMPNYC-x", "Climate") == "weather"
        assert bn.classify("KXRAIN-26AUG01-SATX", "Climate") == "weather"
        assert bn.classify("KXMLBGAME-x", "Sports") == "sports"

    def test_backable_gate(self):
        assert bn.backable("KXBTCD-x")
        assert not bn.backable("KXNETFLIXRANK-x", "Entertainment")
        assert not bn.backable("KXTEMPNYC-x", "Climate")
        assert not bn.backable("KXMLBGAME-x", "Sports")

    def test_excluded_underlyings(self):
        """WTI -6.43c/ct on n=78; NASDAQ -15c on n=5. Every series of each."""
        for t in ("KXWTI-26AUG0314-T78.49", "KXWTIH-26AUG0314-T79.39",
                  "KXWTI15M-26AUG041200-00"):
            assert not bn.backable(t, "Commodities"), t
        assert not bn.backable("KXNASDAQ100U-x", "Financials")
        assert not bn.backable("KXNASDAQ100-x", "Financials")
        # neighbours in the same class must survive the exclusion
        assert bn.backable("KXGOLDH-x", "Commodities")
        assert bn.backable("KXSILVERD-x", "Commodities")
        assert bn.backable("KXINXU-x", "Financials")


class TestBand:
    def test_accepts_mid_in_band_with_payable_ask(self):
        assert bn.in_band(0.94, 0.95)

    def test_rejects_ask_above_ev_cap(self):
        """ask >= 0.985 needs a >99.4% win rate — structurally -EV."""
        assert not bn.in_band(0.94, 0.98)
        assert not bn.in_band(0.96, 0.99)

    def test_rejects_mid_outside_band(self):
        assert not bn.in_band(0.99, 0.97)
        assert not bn.in_band(0.85, 0.86)

    def test_tolerates_junk(self):
        assert not bn.in_band(None, 0.95)
        assert not bn.in_band(0.94, None)


class TestOpen:
    def test_opens_a_backable_favorite(self, con):
        df = pd.DataFrame([_cand("KXBTCD-26AUG0117-T62749.99",
                                 event_id="KXBTCD-26AUG0117",
                                 close_time="2026-08-02T17:00:00Z")])
        assert bn._open(con, df) == 1
        row = con.execute(
            "SELECT ticker, kind, entry_cost, count FROM positions").fetchone()
        assert row[1] == "crypto"
        assert row[2] == pytest.approx(0.95)
        assert row[3] == int(bn.STAKE_DOLLARS / 0.95)

    def test_skips_excluded_underlying(self, con):
        df = pd.DataFrame([_cand("KXWTIH-26AUG0314-T79.39", event_id="e1",
                                 close_time="2026-08-03T18:00:00Z",
                                 category="Commodities")])
        assert bn._open(con, df) == 0

    def test_commodity_siblings_are_one_bet(self, con):
        """KXGOLDH + KXGOLDD at the same close-hour = one underlying."""
        df = pd.DataFrame([
            _cand("KXGOLDH-26AUG0400-T4039", event_id="e1",
                  close_time="2026-08-04T01:00:00Z", category="Commodities"),
            _cand("KXGOLDD-26AUG0400-T4041", event_id="e2",
                  close_time="2026-08-04T01:00:00Z", category="Commodities"),
        ])
        assert bn._open(con, df) == 1

    def test_skips_discrete_and_weather(self, con):
        df = pd.DataFrame([
            _cand("KXNETFLIXRANK-26AUG02-X", event_id="e1",
                  close_time="2026-08-02T17:00:00Z", category="Entertainment"),
            _cand("KXTEMPNYC-26AUG02-X", event_id="e2",
                  close_time="2026-08-02T17:00:00Z", category="Climate"),
        ])
        assert bn._open(con, df) == 0

    def test_skips_ask_above_cap(self, con):
        df = pd.DataFrame([_cand("KXBTCD-26AUG0117-T1", event_id="e1",
                                 close_time="2026-08-02T17:00:00Z",
                                 bid=0.96, ask=0.99)])
        assert bn._open(con, df) == 0

    def test_asset_hour_decorrelation(self, con):
        """Two crypto series on ONE asset settling the same hour = one bet."""
        df = pd.DataFrame([
            _cand("KXSOLD-26AUG0121-T71.9999", event_id="KXSOLD-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
            _cand("KXSOLE-26AUG0121-B72.125", event_id="KXSOLE-26AUG0121",
                  close_time="2026-08-02T01:00:00Z"),
        ])
        assert bn._open(con, df) == 1

    def test_missing_category_column_does_not_misclassify(self, con):
        """Regression: df.get('category','') would zip over a bare string."""
        rows = [_cand("KXBTCD-26AUG0117-T1", event_id="e1",
                      close_time="2026-08-02T17:00:00Z")]
        df = pd.DataFrame(rows).drop(columns=["category"])
        assert bn._open(con, df) == 1        # crypto resolves from the ticker


class TestZRecording:
    def test_crypto_entry_records_z(self, con, monkeypatch):
        monkeypatch.setattr(bn, "z_for_entry", lambda tk, ct: (64963.0, 0.003, 9.4))
        df = pd.DataFrame([_cand("KXBTCD-26AUG0117-T62749.99", event_id="e1",
                                 close_time="2026-08-02T17:00:00Z")])
        assert bn._open(con, df) == 1
        spot, sigma, z = con.execute(
            "SELECT spot, sigma, z FROM positions").fetchone()
        assert (spot, sigma, z) == (64963.0, 0.003, 9.4)

    def test_commodity_entry_records_z_too(self, con, monkeypatch):
        """Since 2026-08-08 (underlying_data feed) gold gets z as well —
        the commodity slice is the arm's largest flow and the whole point
        of extending the feed."""
        monkeypatch.setattr(bn, "z_for_entry", lambda tk, ct: (4399.7, 0.002, 2.1))
        df = pd.DataFrame([_cand("KXGOLDH-26AUG0400-T4039", event_id="e1",
                                 close_time="2026-08-04T01:00:00Z",
                                 category="Commodities")])
        assert bn._open(con, df) == 1
        spot, sigma, z = con.execute(
            "SELECT spot, sigma, z FROM positions").fetchone()
        assert (spot, sigma, z) == (4399.7, 0.002, 2.1)

    def test_z_failure_does_not_block_entry(self, con, monkeypatch):
        monkeypatch.setattr(bn, "z_for_entry", lambda tk, ct: (None, None, None))
        df = pd.DataFrame([_cand("KXBTCD-26AUG0117-T62749.99", event_id="e1",
                                 close_time="2026-08-02T17:00:00Z")])
        assert bn._open(con, df) == 1            # entry proceeds with NULL z


class TestResolve:
    def _seed(self, con, ticker="KXBTCD-26AUG0117-T1", cost=0.95, cnt=10):
        con.execute(
            "INSERT INTO positions (ticker,event_id,category,kind,question,"
            "yes_ask,yes_mid,entry_cost,count,stake,opened_at,close_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, "e", "Crypto", "crypto", "q", cost, cost, cost, cnt,
             10.0, 0, "2020-01-01T00:00:00Z"))
        con.commit()

    def test_yes_result_pays_out(self, con, monkeypatch):
        self._seed(con)
        monkeypatch.setattr(bn.kc, "_get", lambda p: {
            "market": {"status": "finalized", "result": "yes"}})
        assert bn._resolve(con) == 1
        pnl = con.execute("SELECT pnl FROM positions").fetchone()[0]
        assert pnl > 0                       # (1-0.95)*10 - small fee

    def test_no_result_loses_the_stake(self, con, monkeypatch):
        self._seed(con)
        monkeypatch.setattr(bn.kc, "_get", lambda p: {
            "market": {"status": "finalized", "result": "no"}})
        assert bn._resolve(con) == 1
        pnl = con.execute("SELECT pnl FROM positions").fetchone()[0]
        assert pnl == pytest.approx(-9.5, abs=0.1)

    def test_unsettled_and_scalar_are_skipped(self, con, monkeypatch):
        self._seed(con)
        monkeypatch.setattr(bn.kc, "_get", lambda p: {
            "market": {"status": "active", "result": ""}})
        assert bn._resolve(con) == 0
        monkeypatch.setattr(bn.kc, "_get", lambda p: {
            "market": {"status": "finalized", "result": "scalar"}})
        assert bn._resolve(con) == 0
        assert con.execute(
            "SELECT resolved FROM positions").fetchone()[0] == 0
