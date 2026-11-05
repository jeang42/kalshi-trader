"""Tests for the MLB gemma3 deep primer — pure selection logic + a
run_cycle pass with mocked discovery/forecast/LLM (no network, no GPU)."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

import mlb_deep_primer as primer


def _df(*tickers: str) -> pd.DataFrame:
    return pd.DataFrame({
        "condition_id": list(tickers),
        "question": [f"{t} wins" for t in tickers],
        "outcome_prices": [[0.5, 0.5] for _ in tickers],
        "days_to_resolution": [0.3 for _ in tickers],
    })


class TestEventOf:
    def test_strips_team_suffix(self):
        assert primer._event_of("KXMLBGAME-26MAY282005HOUTEX-HOU") == \
            "KXMLBGAME-26MAY282005HOUTEX"


class TestSelectPrimingTickers:
    BAND = dict(prefilter_lo=0.35, prefilter_hi=0.65)

    def test_keeps_both_sides_of_coinflip_game(self):
        df = _df("KXMLBGAME-G1-HOU", "KXMLBGAME-G1-TEX")
        fc = {"KXMLBGAME-G1-HOU": 0.52, "KXMLBGAME-G1-TEX": 0.48}
        out = primer.select_priming_tickers(df, lambda t: fc[t], **self.BAND)
        assert set(out) == {"KXMLBGAME-G1-HOU", "KXMLBGAME-G1-TEX"}

    def test_drops_lopsided_game(self):
        df = _df("KXMLBGAME-G2-NYY-x", "KXMLBGAME-G2-COL")
        # NYY 0.78 / COL 0.22 → out of band → skip the whole game.
        fc = {"KXMLBGAME-G2-NYY-x": 0.78, "KXMLBGAME-G2-COL": 0.22}
        assert primer.select_priming_tickers(df, lambda t: fc.get(t), **self.BAND) == []

    def test_one_forecast_call_per_game(self):
        df = _df("KXMLBGAME-G3-A", "KXMLBGAME-G3-B")
        calls: list[str] = []

        def fc(t):
            calls.append(t)
            return 0.5
        primer.select_priming_tickers(df, fc, **self.BAND)
        assert len(calls) == 1  # decided on the first side

    def test_skips_game_with_no_forecast(self):
        df = _df("KXMLBGAME-G4-A", "KXMLBGAME-G4-B")
        assert primer.select_priming_tickers(df, lambda t: None, **self.BAND) == []

    def test_mixed_slate(self):
        df = _df("KXMLBGAME-C-A", "KXMLBGAME-C-B",   # close
                 "KXMLBGAME-L-A", "KXMLBGAME-L-B")   # lopsided
        fc = {"KXMLBGAME-C-A": 0.50, "KXMLBGAME-C-B": 0.50,
              "KXMLBGAME-L-A": 0.80, "KXMLBGAME-L-B": 0.20}
        out = primer.select_priming_tickers(df, lambda t: fc[t], **self.BAND)
        assert set(out) == {"KXMLBGAME-C-A", "KXMLBGAME-C-B"}

    def test_empty_df(self):
        assert primer.select_priming_tickers(pd.DataFrame(), lambda t: 0.5,
                                             **self.BAND) == []


class TestRunCycle:
    def test_primes_only_coinflip_games(self, tmp_path, monkeypatch):
        # Pin the deep profile to tmp so we don't touch real data.
        monkeypatch.setenv("POLYMARKET_FORECASTS_FILE", str(tmp_path / "f.json"))
        monkeypatch.setattr(primer.profiles, "initialize_profiles",
                            lambda *a, **k: None)
        monkeypatch.setattr(primer.profiles, "active_profile", lambda: "agentic_deep")
        monkeypatch.setattr(primer.profiles, "list_profiles", lambda: ["agentic_deep"])
        monkeypatch.setattr(primer.profiles, "set_active_profile", lambda n: None)
        monkeypatch.setattr(primer.profiles, "create_profile", lambda n: True)

        df = _df("KXMLBGAME-G1-HOU", "KXMLBGAME-G1-TEX",   # close
                 "KXMLBGAME-G2-NYY", "KXMLBGAME-G2-COL")   # lopsided
        monkeypatch.setattr(primer, "_todays_mlb_markets", lambda *, max_days: df)
        fc = {"KXMLBGAME-G1-HOU": 0.51, "KXMLBGAME-G1-TEX": 0.49,
              "KXMLBGAME-G2-NYY": 0.80, "KXMLBGAME-G2-COL": 0.20}
        monkeypatch.setattr(primer, "_pythag_forecast", lambda t: fc.get(t))
        monkeypatch.setattr(primer.bda, "_recent_forecast_exists",
                            lambda *a, **k: False)
        monkeypatch.setattr(primer.mp, "get_provider", lambda: object())

        class _Fake:
            probability = 0.55
            confidence = "med"
            reasoning = "toss-up"
        monkeypatch.setattr(primer.lda, "analyze_market",
                            lambda **k: _Fake())
        logged: list = []
        monkeypatch.setattr(primer.cal, "add_forecast",
                            lambda **k: logged.append(k))

        stats = primer.run_cycle(profile="agentic_deep")
        assert stats["games_found"] == 2
        assert stats["games_close"] == 1
        assert stats["logged"] == 2          # both sides of the close game
        assert {r["condition_id"] for r in logged} == {
            "KXMLBGAME-G1-HOU", "KXMLBGAME-G1-TEX"}
        assert all(r["source"] == "llm_deep:gemma3:27b" for r in logged)
