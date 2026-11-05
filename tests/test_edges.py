"""Tests for the external-data edge layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

import edges
from pm.feeds import crypto_data as cd
from pm.feeds import llm_data as ld
from pm.feeds import llm_matcher as lm
from pm.feeds import macro_data as macd
from pm.feeds import manifold_data as md
from pm.feeds import sports_data as sd
from pm.research import backtest as bt

# ---------------------------------------------------------------------------
# Crypto question parser
# ---------------------------------------------------------------------------

class TestParseCryptoQuestion:
    DEADLINE = datetime(2026, 8, 1, tzinfo=UTC)

    def test_bitcoin_reach_strike(self):
        q = edges.parse_crypto_question(
            "Will Bitcoin reach $150,000 by August 1, 2026?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.asset == "BTC"
        assert q.strike_usd == pytest.approx(150_000.0)
        assert q.direction == "above"

    def test_btc_abbreviated_with_k_suffix(self):
        q = edges.parse_crypto_question(
            "Will BTC hit $200k by year-end?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.asset == "BTC"
        assert q.strike_usd == pytest.approx(200_000.0)

    def test_ethereum_above(self):
        q = edges.parse_crypto_question(
            "Will Ethereum close above $5,000 in 2026?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.asset == "ETH"
        assert q.direction == "above"

    def test_below_threshold(self):
        q = edges.parse_crypto_question(
            "Will BTC fall below $50,000 by July?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.direction == "below"
        assert q.strike_usd == pytest.approx(50_000.0)

    def test_m_suffix(self):
        q = edges.parse_crypto_question(
            "Will Bitcoin exceed $1M by 2030?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.strike_usd == pytest.approx(1_000_000.0)

    def test_no_asset(self):
        assert edges.parse_crypto_question(
            "Will the Fed cut rates above 4%?", deadline=self.DEADLINE,
        ) is None

    def test_no_amount(self):
        assert edges.parse_crypto_question(
            "Will Bitcoin rally this year?", deadline=self.DEADLINE,
        ) is None

    def test_no_deadline_returns_none(self):
        assert edges.parse_crypto_question(
            "Will BTC reach $200k?", deadline=None,
        ) is None

    def test_dip_to_means_below(self):
        # "Will Bitcoin dip to $74,000 by May 17?" — barrier-from-above,
        # NOT direction=above. This was a real bug discovered on live
        # Polymarket data.
        q = edges.parse_crypto_question(
            "Will Bitcoin dip to $74,000 by May 17, 2026?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.direction == "below"
        assert q.strike_usd == pytest.approx(74_000.0)

    def test_fall_to_means_below(self):
        q = edges.parse_crypto_question(
            "Will Ethereum fall to $1,500 in 2026?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.direction == "below"

    def test_drop_to_means_below(self):
        q = edges.parse_crypto_question(
            "Will Bitcoin drop to $50,000 by August?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.direction == "below"

    def test_be_above_is_terminal_model(self):
        # "Will the price of Bitcoin be above $76,000 on May 18?"
        # → terminal close-on-date, not barrier
        q = edges.parse_crypto_question(
            "Will the price of Bitcoin be above $76,000 on May 18, 2026?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.model_type == "terminal"
        assert q.direction == "above"

    def test_close_above_is_terminal(self):
        q = edges.parse_crypto_question(
            "Will Ethereum close above $5,000 on December 31?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.model_type == "terminal"

    def test_reach_is_barrier(self):
        q = edges.parse_crypto_question(
            "Will Bitcoin reach $150,000 by August?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.model_type == "barrier"

    def test_bare_to_without_falling_verb_means_above(self):
        # "Will Bitcoin to $300,000 by 2027?" — informal but means "rise to"
        q = edges.parse_crypto_question(
            "Bitcoin to $300,000 by 2027?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.direction == "above"

    def test_longest_alias_wins(self):
        # "ether" must not match inside "bitcoin"; here the word "Ethereum"
        # should beat a spurious "BTC" mention via case-insensitive match.
        q = edges.parse_crypto_question(
            "Will Ethereum reach $10,000 by August?", deadline=self.DEADLINE,
        )
        assert q is not None and q.asset == "ETH"


# ---------------------------------------------------------------------------
# strategy_crypto_spot_lognormal (Tier 3b integration)
# ---------------------------------------------------------------------------

_MISSING = object()

class TestCryptoStrategy:
    def _snap(self, *, question: str, market_yes_price: float,
              as_of: datetime | None = None, end_date=_MISSING) -> bt.MarketSnapshot:
        as_of = as_of or datetime(2026, 5, 1, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=1)],
            "price": [market_yes_price],
        })
        # Sentinel default so callers can explicitly pass end_date=None
        # to test the no-deadline branch — `end_date or fallback` would
        # have swallowed it.
        if end_date is _MISSING:
            end_date = as_of + timedelta(days=180)
        return bt.MarketSnapshot(
            condition_id="x", question=question, category="Crypto",
            as_of=as_of, prices=prices, end_date=end_date,
        )

    def test_no_signal_when_question_unparseable(self):
        snap = self._snap(question="Will the SP500 hit 7000?", market_yes_price=0.3)
        assert edges.strategy_crypto_spot_lognormal(snap) is None

    def test_no_signal_when_no_end_date(self):
        snap = self._snap(
            question="Will BTC reach $200k by 2026?", market_yes_price=0.3,
            end_date=None,
        )
        assert edges.strategy_crypto_spot_lognormal(snap) is None

    @patch.object(cd, "realized_volatility_at", return_value=0.5)
    @patch.object(cd, "spot_at", return_value=100_000.0)
    def test_emits_yes_when_model_above_market(self, _spot, _vol):
        # Spot $100k, strike $110k, 50% vol, 180d → model ~52%
        # Market = 20% → gap +32% → signal=YES
        snap = self._snap(
            question="Will Bitcoin reach $110,000 by August?",
            market_yes_price=0.20,
        )
        sig = edges.strategy_crypto_spot_lognormal(snap, min_gap=0.10)
        assert sig is not None
        assert sig.direction == "YES"
        assert sig.probability > 0.30

    @patch.object(cd, "realized_volatility_at", return_value=0.3)
    @patch.object(cd, "spot_at", return_value=100_000.0)
    def test_emits_no_when_model_below_market(self, _spot, _vol):
        # Spot $100k, strike $250k, 30% vol, 180d — model ~very low.
        # Market priced 50% → gap is hugely negative → signal=NO
        snap = self._snap(
            question="Will Bitcoin reach $250,000 by August?",
            market_yes_price=0.50,
        )
        sig = edges.strategy_crypto_spot_lognormal(snap, min_gap=0.10)
        assert sig is not None
        assert sig.direction == "NO"

    @patch.object(cd, "realized_volatility_at", return_value=0.3)
    @patch.object(cd, "spot_at", return_value=100_000.0)
    def test_no_signal_within_gap_band(self, _spot, _vol):
        snap = self._snap(
            question="Will Bitcoin reach $110,000 by August?",
            market_yes_price=0.50,
        )
        # min_gap=0.99 → essentially nothing qualifies
        assert edges.strategy_crypto_spot_lognormal(snap, min_gap=0.99) is None

    @patch.object(cd, "spot_at", return_value=None)
    def test_no_signal_when_spot_unavailable(self, _spot):
        snap = self._snap(
            question="Will BTC reach $150k by August?", market_yes_price=0.3,
        )
        assert edges.strategy_crypto_spot_lognormal(snap) is None


# ---------------------------------------------------------------------------
# find_crypto_edges — live path
# ---------------------------------------------------------------------------

class TestFindCryptoEdges:
    def _markets(self) -> pd.DataFrame:
        future = datetime.now(UTC) + timedelta(days=120)
        return pd.DataFrame([
            {
                "condition_id": "m1",
                "question": "Will BTC reach $150,000 by August 2026?",
                "category": "Crypto",
                "outcome_prices": [0.10, 0.90],
                "end_date": future.isoformat(),
            },
            {
                "condition_id": "m2",
                "question": "Will the Fed cut rates in 2026?",
                "category": "Macro",
                "outcome_prices": [0.40, 0.60],
                "end_date": future.isoformat(),
            },
            {
                "condition_id": "m3",
                "question": "Will Bitcoin be above $40,000 by August?",
                "category": "Crypto",
                "outcome_prices": [0.50, 0.50],
                "end_date": future.isoformat(),
            },
        ])

    @patch.object(cd, "realized_volatility", return_value=0.50)
    @patch.object(cd, "get_klines", return_value=pd.DataFrame({
        "timestamp": [datetime.now(UTC)], "open": [100.0], "high": [100.0],
        "low": [100.0], "close": [100_000.0], "volume": [1.0],
    }))
    @patch.object(cd, "get_spot_price", return_value=100_000.0)
    def test_emits_edges_for_crypto_questions_only(self, _spot, _kl, _vol):
        df = self._markets()
        out = edges.find_crypto_edges(df, min_gap=0.05)
        # m2 is Macro and unparseable → skipped
        cids = [e.market_id for e in out]
        assert "m2" not in cids
        # m1 (10% YES, model says higher) → big positive gap → flagged YES
        # m3 (50% YES on already-deep-ITM strike) → big positive gap → flagged YES
        assert all(e.source == "crypto_spot" for e in out)
        assert all(e.note for e in out)

    @patch.object(cd, "get_spot_price", return_value=None)
    def test_no_edges_when_spot_unavailable(self, _spot):
        out = edges.find_crypto_edges(self._markets(), min_gap=0.05)
        assert out == []

    def test_empty_input_returns_empty(self):
        assert edges.find_crypto_edges(pd.DataFrame()) == []
        assert edges.find_crypto_edges(None) == []  # type: ignore[arg-type]

    @patch.object(cd, "realized_volatility", return_value=0.50)
    @patch.object(cd, "get_klines", return_value=pd.DataFrame({
        "timestamp": [datetime.now(UTC)], "open": [100.0], "high": [100.0],
        "low": [100.0], "close": [100_000.0], "volume": [1.0],
    }))
    @patch.object(cd, "get_spot_price", return_value=100_000.0)
    def test_results_sorted_by_absolute_gap_descending(self, *_):
        out = edges.find_crypto_edges(self._markets(), min_gap=0.01)
        if len(out) >= 2:
            assert abs(out[0].gap) >= abs(out[1].gap)


# ---------------------------------------------------------------------------
# Registry + back-reference to backtest.STRATEGIES
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_sources_lists_crypto_spot(self):
        names = dict(edges.list_sources())
        assert "crypto_spot" in names

    def test_get_finder_returns_callable(self):
        assert callable(edges.get_finder("crypto_spot"))

    def test_strategy_registered_in_backtest(self):
        # Importing edges has the side effect of registering the
        # crypto-spot strategy on backtest.STRATEGIES — this test guards
        # that wiring so a future refactor doesn't silently break it.
        assert "crypto_spot_lognormal" in bt.STRATEGIES
        display, fn = bt.STRATEGIES["crypto_spot_lognormal"]
        assert "external" in display.lower() or "spot" in display.lower()
        assert callable(fn)


# ---------------------------------------------------------------------------
# Edge dataclass + DataFrame coercion
# ---------------------------------------------------------------------------

class TestLLMStrategy:
    def _snap(self, *, question="?", market_yes_price=0.40,
              category="Crypto", end_date_offset_days: int | None = 30):
        as_of = datetime(2026, 5, 1, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=1)],
            "price": [market_yes_price],
        })
        end = (as_of + timedelta(days=end_date_offset_days)
               if end_date_offset_days is not None else None)
        return bt.MarketSnapshot(
            condition_id="x", question=question, category=category,
            as_of=as_of, prices=prices, end_date=end,
        )

    @patch.object(ld, "get_forecast")
    def test_emits_yes_when_llm_above_market(self, mock_fc):
        mock_fc.return_value = ld.LLMForecast(
            model="m", probability=0.65, reasoning="..."
        )
        sig = edges.strategy_llm_forecast(
            self._snap(market_yes_price=0.30), min_gap=0.10, model="m",
        )
        assert sig is not None
        assert sig.direction == "YES"
        assert sig.probability == pytest.approx(0.65)

    @patch.object(ld, "get_forecast")
    def test_emits_no_when_llm_below_market(self, mock_fc):
        mock_fc.return_value = ld.LLMForecast(
            model="m", probability=0.20, reasoning="..."
        )
        sig = edges.strategy_llm_forecast(
            self._snap(market_yes_price=0.60), min_gap=0.10, model="m",
        )
        assert sig is not None
        assert sig.direction == "NO"

    @patch.object(ld, "get_forecast")
    def test_no_signal_inside_gap_band(self, mock_fc):
        mock_fc.return_value = ld.LLMForecast(
            model="m", probability=0.45, reasoning="anchored"
        )
        assert edges.strategy_llm_forecast(
            self._snap(market_yes_price=0.40), min_gap=0.10, model="m",
        ) is None

    @patch.object(ld, "get_forecast", return_value=None)
    def test_no_signal_when_llm_unavailable(self, _fc):
        assert edges.strategy_llm_forecast(
            self._snap(), min_gap=0.10, model="m",
        ) is None

    @patch.object(ld, "get_forecast")
    def test_skips_short_dated_markets(self, mock_fc):
        # If we skipped on short-dated, mock_fc should never be called.
        snap = self._snap(end_date_offset_days=0)
        edges.strategy_llm_forecast(snap, model="m", min_days_to_expiry=1.0)
        mock_fc.assert_not_called()


class TestFindLLMEdges:
    def _markets(self):
        future = datetime.now(UTC) + timedelta(days=120)
        return pd.DataFrame([
            {"condition_id": f"m{i}", "question": f"Q {i}",
             "category": "Crypto", "outcome_prices": [0.30, 0.70],
             "end_date": future.isoformat(), "volume_24hr": 1000 - i}
            for i in range(5)
        ])

    @patch.object(ld, "get_forecast")
    def test_max_markets_caps_scoring(self, mock_fc):
        mock_fc.return_value = ld.LLMForecast(
            model="m", probability=0.50, reasoning="x",
        )
        edges.find_llm_edges(self._markets(), max_markets=2, min_gap=0.05, model="m")
        # 5 markets in fixture; cap at 2 → only 2 calls
        assert mock_fc.call_count == 2

    @patch.object(ld, "get_forecast")
    def test_sorts_most_liquid_first(self, mock_fc):
        # All 5 rows scored; verify the first call is for the highest-volume row
        mock_fc.return_value = ld.LLMForecast(
            model="m", probability=0.50, reasoning="x",
        )
        edges.find_llm_edges(self._markets(), max_markets=5, min_gap=0.05, model="m")
        first_call_question = mock_fc.call_args_list[0].kwargs["question"]
        assert first_call_question == "Q 0"  # volume_24hr=1000 is largest

    @patch.object(ld, "get_forecast")
    def test_emits_edges_sorted_by_abs_gap(self, mock_fc):
        # Two markets, different LLM probs → both flagged, sorted by |gap|.
        mock_fc.side_effect = [
            ld.LLMForecast(model="m", probability=0.10, reasoning="r"),
            ld.LLMForecast(model="m", probability=0.40, reasoning="r"),
        ]
        df = pd.DataFrame([
            {"condition_id": "m1", "question": "Q1", "category": "Crypto",
             "outcome_prices": [0.30, 0.70], "volume_24hr": 1000,
             "end_date": (datetime.now(UTC) + timedelta(days=60)).isoformat()},
            {"condition_id": "m2", "question": "Q2", "category": "Crypto",
             "outcome_prices": [0.30, 0.70], "volume_24hr": 500,
             "end_date": (datetime.now(UTC) + timedelta(days=60)).isoformat()},
        ])
        out = edges.find_llm_edges(df, max_markets=2, min_gap=0.05, model="m")
        assert len(out) == 2
        assert abs(out[0].gap) >= abs(out[1].gap)
        assert all(e.source == "llm" for e in out)

    @patch.object(ld, "get_forecast", return_value=None)
    def test_handles_llm_failure_gracefully(self, _fc):
        out = edges.find_llm_edges(self._markets(), max_markets=3,
                                    min_gap=0.05, model="m")
        assert out == []

    def test_empty_input(self):
        assert edges.find_llm_edges(pd.DataFrame(), model="m") == []


class TestSportsMatching:
    def _event(self, *, home="Los Angeles Lakers", away="Miami Heat",
               home_prob=0.65, away_prob=0.35, league="NBA",
               eid="abc", state="pre"):
        return sd.SportsEvent(
            league=league, event_id=eid,
            name=f"{away} at {home}", state=state,
            home=sd.TeamSide(name=home, short_name=home.split()[-1],
                             abbreviation="HOM", is_home=True),
            away=sd.TeamSide(name=away, short_name=away.split()[-1],
                             abbreviation="AWY", is_home=False),
            home_winprob=home_prob, away_winprob=away_prob,
        )

    def test_match_assigns_yes_to_first_team_mentioned(self):
        ev = self._event()
        # "Lakers vs. Heat" → Lakers first → YES = home
        match = edges._match_event("Lakers vs. Heat", [ev])
        assert match is not None
        assert match[1] == "home"

        # "Heat vs. Lakers" → away first → YES = away
        match = edges._match_event("Heat vs. Lakers", [ev])
        assert match is not None
        assert match[1] == "away"

    def test_match_uses_full_team_name(self):
        ev = self._event()
        match = edges._match_event(
            "Los Angeles Lakers vs. Miami Heat tonight", [ev],
        )
        assert match is not None
        assert match[0].event_id == ev.event_id

    def test_match_returns_none_when_one_team_missing(self):
        ev = self._event()
        assert edges._match_event("Lakers won big", [ev]) is None

    def test_match_returns_none_when_multiple_events_match(self):
        # Two events sharing the same two teams (rare but possible across
        # leagues) — we can't tell which one the question means.
        ev1 = self._event(eid="1")
        ev2 = self._event(eid="2")
        assert edges._match_event("Lakers vs. Heat", [ev1, ev2]) is None

    def test_derivative_markets_are_skipped(self):
        # Real Polymarket title that produced a false positive during
        # the live scan: an O/U (totals) market on the same teams.
        # ESPN moneyline doesn't apply, must skip.
        ev = self._event()
        assert edges._match_event("Lakers vs. Heat: O/U 220.5", [ev]) is None
        assert edges._match_event("Lakers vs. Heat spread", [ev]) is None
        assert edges._match_event("Lakers vs. Heat over 220", [ev]) is None
        assert edges._match_event("Lakers vs. Heat prop bet", [ev]) is None
        # Plain head-to-head still matches
        assert edges._match_event("Lakers vs. Heat", [ev]) is not None

    def test_winprob_for_side(self):
        ev = self._event(home_prob=0.7, away_prob=0.3)
        assert edges._winprob_for_side(ev, "home") == pytest.approx(0.7)
        assert edges._winprob_for_side(ev, "away") == pytest.approx(0.3)

    def test_team_aliases_includes_full_short_and_abbrev(self):
        side = sd.TeamSide(name="Los Angeles Lakers", short_name="Lakers",
                           abbreviation="LAL", is_home=True)
        aliases = edges._team_aliases(side)
        assert "los angeles lakers" in aliases
        assert "lakers" in aliases
        assert "lal" in aliases


class TestFindSportsEdges:
    def _markets(self):
        return pd.DataFrame([
            {"condition_id": "m1",
             "question": "Will the Lakers beat the Heat tonight?",
             "category": "Sports",
             "outcome_prices": [0.40, 0.60]},
            {"condition_id": "m2",
             "question": "Who will be the next Pope?",
             "category": "Politics",
             "outcome_prices": [0.30, 0.70]},
        ])

    def _event(self, *, home_prob=0.65, away_prob=0.35, state="pre"):
        return sd.SportsEvent(
            league="NBA", event_id="abc",
            name="Miami Heat at Los Angeles Lakers", state=state,
            home=sd.TeamSide(name="Los Angeles Lakers", short_name="Lakers",
                             abbreviation="LAL", is_home=True),
            away=sd.TeamSide(name="Miami Heat", short_name="Heat",
                             abbreviation="MIA", is_home=False),
            home_winprob=home_prob, away_winprob=away_prob,
        )

    @patch.object(sd, "get_events_with_probabilities")
    def test_emits_edge_when_market_disagrees(self, mock_evs):
        # Lakers (home) at 65% by ESPN; market YES (Lakers) at 40% → +25pp gap, YES
        mock_evs.side_effect = lambda league: [self._event()] if league == "NBA" else []
        out = edges.find_sports_edges(
            self._markets(), min_gap=0.10, leagues=["NBA"],
        )
        assert len(out) == 1
        assert out[0].source == "sports"
        assert out[0].direction == "YES"
        assert out[0].gap == pytest.approx(0.25, abs=0.01)
        assert "NBA" in out[0].note

    @patch.object(sd, "get_events_with_probabilities")
    def test_no_emit_when_within_band(self, mock_evs):
        mock_evs.side_effect = lambda league: [self._event(home_prob=0.45)] if league == "NBA" else []
        # Market YES = 0.40, ESPN home = 0.45 → 5pp gap; min_gap=0.10 → no flag
        out = edges.find_sports_edges(
            self._markets(), min_gap=0.10, leagues=["NBA"],
        )
        assert out == []

    @patch.object(sd, "get_events_with_probabilities")
    def test_post_state_events_skipped(self, mock_evs):
        # Final games never produce edges (settled already)
        mock_evs.side_effect = lambda league: [self._event(state="post")] if league == "NBA" else []
        out = edges.find_sports_edges(
            self._markets(), min_gap=0.10, leagues=["NBA"],
        )
        assert out == []

    @patch.object(sd, "get_events_with_probabilities", return_value=[])
    def test_no_events_returns_empty(self, _ev):
        assert edges.find_sports_edges(self._markets(), leagues=["NBA"]) == []

    def test_empty_input_returns_empty(self):
        assert edges.find_sports_edges(pd.DataFrame(), leagues=["NBA"]) == []

    @patch.object(sd, "get_events_with_probabilities")
    def test_unmatched_market_skipped(self, mock_evs):
        # Polit market — no team names → skipped silently
        mock_evs.side_effect = lambda league: [self._event()] if league == "NBA" else []
        out = edges.find_sports_edges(
            self._markets().tail(1), min_gap=0.05, leagues=["NBA"],
        )
        assert out == []


class TestFindManifoldEdges:
    def _mm(self, *, q="Q?", prob=0.50, mid="m1", vol=1000.0, resolved=False):
        return md.ManifoldMarket(
            market_id=mid, question=q, probability=prob,
            volume=vol, is_resolved=resolved, close_time_ms=None,
        )

    def _markets(self):
        future = datetime.now(UTC) + timedelta(days=60)
        return pd.DataFrame([
            {"condition_id": "p1",
             "question": "Will Trump be impeached by the House in 2026?",
             "category": "Politics",
             "outcome_prices": [0.10, 0.90],
             "end_date": future.isoformat(),
             "volume_24hr": 5000},
            {"condition_id": "p2",
             "question": "Random unrelated long-tail question text",
             "category": "Other",
             "outcome_prices": [0.50, 0.50],
             "end_date": future.isoformat(),
             "volume_24hr": 200},
        ])

    @patch.object(md, "search_markets")
    def test_emits_edge_for_high_similarity_disagreement(self, mock_search):
        # Polymarket says 10%, Manifold says 25% — 15pp gap → YES
        mock_search.side_effect = lambda q, **kw: [
            self._mm(q="Will Trump be impeached by the House in 2026?",
                     prob=0.25),
        ]
        out = edges.find_manifold_edges(
            self._markets(), min_gap=0.10, min_similarity=0.30,
            max_markets=1,
        )
        assert len(out) == 1
        assert out[0].source == "manifold"
        assert out[0].direction == "YES"
        assert out[0].gap == pytest.approx(0.15, abs=0.01)

    @patch.object(md, "search_markets")
    def test_skips_low_similarity_match(self, mock_search):
        # Search returns something unrelated → Jaccard low → skip
        mock_search.side_effect = lambda q, **kw: [
            self._mm(q="Bitcoin to $200k by 2027?", prob=0.95),
        ]
        out = edges.find_manifold_edges(
            self._markets(), min_gap=0.05, min_similarity=0.30,
            max_markets=1,
        )
        assert out == []

    @patch.object(md, "search_markets")
    def test_skips_resolved_and_thin_volume(self, mock_search):
        mock_search.side_effect = lambda q, **kw: [
            self._mm(q="Same question text Trump impeached House 2026",
                     prob=0.50, vol=5, resolved=False),   # too-thin volume
            self._mm(q="Same question text Trump impeached House 2026",
                     prob=0.50, vol=1000, resolved=True),  # resolved
        ]
        out = edges.find_manifold_edges(
            self._markets(), min_gap=0.10, min_similarity=0.30,
            min_volume=100.0, max_markets=1,
        )
        # Both candidates filtered → best_match returns None
        assert out == []

    @patch.object(md, "search_markets", return_value=[])
    def test_no_search_results_returns_empty(self, _ms):
        assert edges.find_manifold_edges(self._markets(), max_markets=2) == []

    def test_empty_input(self):
        assert edges.find_manifold_edges(pd.DataFrame()) == []

    @patch.object(md, "search_markets")
    def test_sorts_by_absolute_gap_descending(self, mock_search):
        # Two markets, both with strong matches but different gaps
        responses = [
            [self._mm(q="Will Trump be impeached by the House in 2026?",
                      prob=0.80)],   # market_p=0.10 → +70pp gap
            [self._mm(q="Random unrelated long-tail question text",
                      prob=0.60)],   # market_p=0.50 → +10pp gap
        ]
        call_count = {"n": 0}
        def _side(q, **kw):
            r = responses[call_count["n"] % len(responses)]
            call_count["n"] += 1
            return r
        mock_search.side_effect = _side
        out = edges.find_manifold_edges(
            self._markets(), min_gap=0.05, min_similarity=0.20,
            max_markets=2, min_volume=0,
        )
        assert len(out) == 2
        assert abs(out[0].gap) >= abs(out[1].gap)


class TestManifoldLLMVerify:
    """Wiring of the optional LLM verification step into find_manifold_edges
    and strategy_manifold_community. The matcher itself is exercised in
    tests/test_llm_matcher.py — here we only verify it gates correctly."""

    def _market(self, *, q="Will Trump be impeached in 2026?", prob=0.55,
                vol=1000.0, mid="m1", resolved=False):
        return md.ManifoldMarket(
            market_id=mid, question=q, probability=prob,
            volume=vol, is_resolved=resolved, close_time_ms=None,
        )

    def _markets_df(self):
        future = datetime.now(UTC) + timedelta(days=60)
        return pd.DataFrame([
            {"condition_id": "p1",
             "question": "Will Trump be impeached in 2026?",
             "category": "Politics",
             "outcome_prices": [0.20, 0.80],
             "end_date": future.isoformat(),
             "volume_24hr": 5000},
        ])

    @patch.object(md, "search_markets")
    @patch.object(lm, "verify_match")
    def test_llm_accept_emits_edge(self, mock_verify, mock_search):
        mock_search.side_effect = lambda q, **kw: [self._market()]
        mock_verify.return_value = lm.MatchVerdict(
            same_question=True, confidence=0.9,
            reasoning="Same event", model="m",
        )
        out = edges.find_manifold_edges(
            self._markets_df(), min_gap=0.10, min_similarity=0.10,
            llm_verify=True, llm_verify_model="m", max_markets=1,
        )
        assert len(out) == 1
        assert "LLM-verified" in out[0].note
        assert "Same event" in out[0].note

    @patch.object(md, "search_markets")
    @patch.object(lm, "verify_match")
    def test_llm_reject_suppresses_edge(self, mock_verify, mock_search):
        mock_search.side_effect = lambda q, **kw: [self._market()]
        # LLM says "different question" — the framework must drop the edge
        mock_verify.return_value = lm.MatchVerdict(
            same_question=False, confidence=0.85,
            reasoning="Semantic opposites", model="m",
        )
        out = edges.find_manifold_edges(
            self._markets_df(), min_gap=0.10, min_similarity=0.10,
            llm_verify=True, llm_verify_model="m", max_markets=1,
        )
        assert out == []

    @patch.object(md, "search_markets")
    @patch.object(lm, "verify_match")
    def test_llm_low_confidence_suppresses_edge(self, mock_verify, mock_search):
        # LLM says SAME but with confidence below the floor → reject
        mock_search.side_effect = lambda q, **kw: [self._market()]
        mock_verify.return_value = lm.MatchVerdict(
            same_question=True, confidence=0.3,
            reasoning="Unsure", model="m",
        )
        out = edges.find_manifold_edges(
            self._markets_df(), min_gap=0.10, min_similarity=0.10,
            llm_verify=True, llm_verify_model="m",
            llm_min_confidence=0.6, max_markets=1,
        )
        assert out == []

    @patch.object(md, "search_markets")
    @patch.object(lm, "verify_match", return_value=None)
    def test_llm_unreachable_suppresses_edge(self, _verify, mock_search):
        # When verify_match returns None (transport failure / unparseable),
        # the framework must refuse the edge — don't fabricate.
        mock_search.side_effect = lambda q, **kw: [self._market()]
        out = edges.find_manifold_edges(
            self._markets_df(), min_gap=0.10, min_similarity=0.10,
            llm_verify=True, llm_verify_model="m", max_markets=1,
        )
        assert out == []

    @patch.object(md, "search_markets")
    @patch.object(lm, "verify_match")
    def test_llm_skipped_when_flag_disabled(self, mock_verify, mock_search):
        mock_search.side_effect = lambda q, **kw: [self._market()]
        # llm_verify=False (default) → verify_match must never be called
        out = edges.find_manifold_edges(
            self._markets_df(), min_gap=0.10, min_similarity=0.10,
            llm_verify=False, max_markets=1,
        )
        assert len(out) == 1
        mock_verify.assert_not_called()


class TestParseMacroQuestion:
    DEADLINE = datetime(2026, 8, 1, tzinfo=UTC)

    def test_unemployment_above(self):
        q = edges.parse_macro_question(
            "Will unemployment exceed 5% by August 2026?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.series_key == "unemployment"
        assert q.direction == "above"
        assert q.threshold == pytest.approx(5.0)

    def test_unemployment_below(self):
        q = edges.parse_macro_question(
            "Will unemployment fall below 3.5% by Aug?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.direction == "below"
        assert q.threshold == pytest.approx(3.5)

    def test_10y_treasury_above(self):
        q = edges.parse_macro_question(
            "Will the 10-year Treasury yield exceed 5% by year-end?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.series_key == "10y_treasury"

    def test_2y_treasury_below(self):
        q = edges.parse_macro_question(
            "Will the 2-year yield drop to 3% by August?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.series_key == "2y_treasury"
        assert q.direction == "below"

    def test_fed_funds(self):
        q = edges.parse_macro_question(
            "Will the fed funds rate be above 4% by December?",
            deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.series_key == "fed_funds"

    def test_no_macro_keyword(self):
        assert edges.parse_macro_question(
            "Will Bitcoin reach $200k?", deadline=self.DEADLINE,
        ) is None

    def test_no_threshold(self):
        assert edges.parse_macro_question(
            "Will unemployment rise this year?", deadline=self.DEADLINE,
        ) is None

    def test_no_deadline(self):
        assert edges.parse_macro_question(
            "Will unemployment exceed 5%?", deadline=None,
        ) is None

    def test_percent_optional(self):
        # "above 5" should parse the same as "above 5%"
        q = edges.parse_macro_question(
            "Will unemployment go above 5 by Dec?", deadline=self.DEADLINE,
        )
        assert q is not None
        assert q.threshold == pytest.approx(5.0)


class TestMacroStrategy:
    def _snap(self, *, question, market_yes_price=0.40,
              end_date_offset_days=120):
        as_of = datetime(2026, 5, 1, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=1)],
            "price": [market_yes_price],
        })
        end = as_of + timedelta(days=end_date_offset_days)
        return bt.MarketSnapshot(
            condition_id="x", question=question, category="Economics",
            as_of=as_of, prices=prices, end_date=end,
        )

    @patch.object(macd, "realized_volatility_at", return_value=0.5)
    @patch.object(macd, "value_at",
                  return_value=(4.3, datetime(2026, 4, 1, tzinfo=UTC)))
    def test_strategy_uses_as_of_lookups(self, mock_value_at, mock_vol_at):
        """When the strategy runs against a backtest snapshot, the macro
        model must use the historical (value_at / realized_volatility_at)
        APIs, not the live ones — otherwise vol/level reflect today, not
        snap.as_of, and the backtest gate is lying."""
        snap = self._snap(
            question="Will unemployment exceed 6% by August 2026?",
            market_yes_price=0.50,
        )
        sig = edges.strategy_macro_threshold(snap, min_gap=0.10)
        # The historical APIs were called with snap.as_of
        mock_value_at.assert_called_once()
        called_as_of = mock_value_at.call_args.args[1]
        assert called_as_of == snap.as_of
        mock_vol_at.assert_called_once()
        called_as_of_vol = mock_vol_at.call_args.args[1]
        assert called_as_of_vol == snap.as_of
        # And we still get a sensible signal out the other side
        assert sig is not None
        assert sig.direction == "NO"

    @patch.object(macd, "realized_volatility_at", return_value=0.5)
    @patch.object(macd, "value_at",
                  return_value=(4.3, datetime(2026, 4, 1, tzinfo=UTC)))
    def test_emits_no_when_model_far_below_market(self, _lv, _vol):
        # Latest unemployment 4.3%, threshold 6%, vol 0.5/yr over ~120d
        # → model probability of crossing very low; market priced 50%
        # → big negative gap → emit NO
        snap = self._snap(
            question="Will unemployment exceed 6% by August 2026?",
            market_yes_price=0.50,
        )
        sig = edges.strategy_macro_threshold(snap, min_gap=0.10)
        assert sig is not None
        assert sig.direction == "NO"

    @patch.object(macd, "realized_volatility_at", return_value=0.5)
    @patch.object(macd, "value_at",
                  return_value=(4.3, datetime(2026, 4, 1, tzinfo=UTC)))
    def test_no_signal_for_unparseable(self, _lv, _vol):
        snap = self._snap(question="Will Bitcoin reach $200k?",
                          market_yes_price=0.50)
        assert edges.strategy_macro_threshold(snap) is None

    @patch.object(macd, "value_at", return_value=None)
    def test_no_signal_when_no_data(self, _lv):
        snap = self._snap(
            question="Will unemployment exceed 5% by August?",
            market_yes_price=0.50,
        )
        assert edges.strategy_macro_threshold(snap) is None

    @patch.object(macd, "realized_volatility_at", return_value=0.5)
    @patch.object(macd, "value_at",
                  return_value=(4.3, datetime(2026, 4, 1, tzinfo=UTC)))
    def test_no_signal_for_short_dated(self, _lv, _vol):
        snap = self._snap(
            question="Will unemployment exceed 5% by August?",
            market_yes_price=0.50, end_date_offset_days=0,
        )
        assert edges.strategy_macro_threshold(snap, min_days_to_expiry=1.0) is None


class TestFindMacroEdges:
    def _markets(self):
        future = datetime.now(UTC) + timedelta(days=120)
        return pd.DataFrame([
            {"condition_id": "m1",
             "question": "Will unemployment exceed 6% by August 2026?",
             "category": "Economics",
             "outcome_prices": [0.50, 0.50],
             "end_date": future.isoformat()},
            {"condition_id": "m2",
             "question": "Will Bitcoin reach $200k?",
             "category": "Crypto",
             "outcome_prices": [0.40, 0.60],
             "end_date": future.isoformat()},
        ])

    @patch.object(macd, "realized_volatility_for", return_value=0.4)
    @patch.object(macd, "latest_value",
                  return_value=(4.3, datetime(2026, 4, 1, tzinfo=UTC)))
    def test_emits_only_for_macro_questions(self, _lv, _vol):
        out = edges.find_macro_edges(self._markets(), min_gap=0.10)
        cids = [e.market_id for e in out]
        assert "m2" not in cids  # crypto question skipped

    def test_empty_input(self):
        assert edges.find_macro_edges(pd.DataFrame()) == []


class TestSourceRegistryIncludesSports:
    def test_sports_in_sources(self):
        names = dict(edges.list_sources())
        assert "sports" in names
        assert callable(edges.get_finder("sports"))

    def test_sports_strategy_registered_in_backtest(self):
        assert "sports_winprob" in bt.STRATEGIES
        _, fn = bt.STRATEGIES["sports_winprob"]
        assert callable(fn)


class TestLogEdgesToCalibration:
    """Bulk-log helper: edges → calibration.add_forecast records.

    Uses the env-var override so each test gets its own temp forecasts.json
    (matches the pattern in tests/test_calibration.py)."""

    @pytest.fixture
    def forecasts_path(self, tmp_path, monkeypatch):
        p = tmp_path / "forecasts.json"
        monkeypatch.setenv("POLYMARKET_FORECASTS_FILE", str(p))
        return p

    def _edge(self, *, source="llm", note="llama3.1:latest: anchored",
              market=0.30, model=0.55, market_id="m1"):
        gap = model - market
        return edges.Edge(
            source=source, market_id=market_id, question="Q?",
            category="Crypto",
            market_yes_price=market, model_implied=model, gap=gap,
            direction="YES" if gap > 0 else "NO",
            severity="medium", note=note,
        )

    def test_records_one_forecast_per_edge(self, forecasts_path):
        from pm.ledger import calibration as cal
        out = [self._edge(market_id="m1"), self._edge(market_id="m2")]
        n = edges.log_edges_to_calibration(out)
        assert n == 2
        df = cal.get_forecasts()
        assert len(df) == 2

    def test_llm_source_tag_includes_model(self, forecasts_path):
        from pm.ledger import calibration as cal
        edges.log_edges_to_calibration([
            self._edge(source="llm", note="llama3.1:latest: x"),
        ])
        df = cal.get_forecasts()
        assert df.iloc[0]["source"] == "llm:llama3.1:latest"

    def test_crypto_source_tag_stays_bare(self, forecasts_path):
        from pm.ledger import calibration as cal
        edges.log_edges_to_calibration([
            self._edge(source="crypto_spot",
                       note="BTC spot $77k; touch $80k from above..."),
        ])
        df = cal.get_forecasts()
        assert df.iloc[0]["source"] == "crypto_spot"

    def test_market_price_and_probability_persisted(self, forecasts_path):
        from pm.ledger import calibration as cal
        edges.log_edges_to_calibration([
            self._edge(market=0.30, model=0.65),
        ])
        df = cal.get_forecasts()
        assert df.iloc[0]["market_price"] == pytest.approx(0.30)
        assert df.iloc[0]["your_probability"] == pytest.approx(0.65)

    def test_skips_edges_with_invalid_probabilities(self, forecasts_path):
        from pm.ledger import calibration as cal
        # Construct an edge with an out-of-range model probability — the
        # helper should skip it rather than crash the batch.
        bad = self._edge()
        bad = edges.Edge(**{**bad.__dict__, "model_implied": 1.5})
        good = self._edge(market_id="m2")
        n = edges.log_edges_to_calibration([bad, good])
        assert n == 1
        df = cal.get_forecasts()
        assert len(df) == 1
        assert df.iloc[0]["condition_id"] == "m2"


class TestEdgeDataframe:
    def test_empty_yields_typed_columns(self):
        df = edges.edges_to_dataframe([])
        assert list(df.columns) == [
            "source", "category", "market_id", "question",
            "market_yes_price", "model_implied", "gap",
            "direction", "severity", "note",
        ]
        assert df.empty

    def test_round_trip(self):
        e = edges.Edge(
            source="crypto_spot", market_id="m1", question="?",
            category="Crypto", market_yes_price=0.3, model_implied=0.6,
            gap=0.3, direction="YES", severity="high", note="-",
        )
        df = edges.edges_to_dataframe([e])
        assert df.iloc[0]["gap"] == pytest.approx(0.3)
        assert df.iloc[0]["severity"] == "high"


# ---------------------------------------------------------------------------
# find_all_edges — unified Recommendations aggregator
# ---------------------------------------------------------------------------

class TestFindAllEdges:
    def _stub_finder(self, edges_to_return):
        """Build a stub finder closure that ignores df_markets and returns
        a fixed edge list. Signature mimics the real finders."""
        def f(df, *, min_gap=0.1, min_days_to_expiry=1.0, **kwargs):
            return edges_to_return
        return f

    def test_runs_every_registered_source(self, monkeypatch):
        # Stub the whole registry with two trivial sources
        e1 = edges.Edge(source="a", market_id="m1", question="q1",
                         category="X", market_yes_price=0.4,
                         model_implied=0.6, gap=0.2,
                         direction="YES", severity="medium", note="-")
        e2 = edges.Edge(source="b", market_id="m2", question="q2",
                         category="Y", market_yes_price=0.7,
                         model_implied=0.5, gap=-0.2,
                         direction="NO", severity="medium", note="-")
        monkeypatch.setattr(edges, "SOURCES", {
            "a": ("Source A", self._stub_finder([e1])),
            "b": ("Source B", self._stub_finder([e2])),
        })
        out = edges.find_all_edges(pd.DataFrame())
        assert set(out.keys()) == {"a", "b"}
        assert len(out["a"]) == 1 and out["a"][0] is e1
        assert len(out["b"]) == 1 and out["b"][0] is e2

    def test_sources_filter_limits_dispatch(self, monkeypatch):
        called = []
        def make_finder(name):
            def f(df, *, min_gap=0.1, min_days_to_expiry=1.0, **kw):
                called.append(name)
                return []
            return f
        monkeypatch.setattr(edges, "SOURCES", {
            "a": ("A", make_finder("a")),
            "b": ("B", make_finder("b")),
            "c": ("C", make_finder("c")),
        })
        out = edges.find_all_edges(pd.DataFrame(), sources=["a", "c"])
        assert set(out.keys()) == {"a", "c"}
        assert called == ["a", "c"]

    def test_broken_source_does_not_break_run(self, monkeypatch):
        def broken(df, **kw):
            raise RuntimeError("source crashed")
        def good(df, **kw):
            return [edges.Edge(source="good", market_id="m", question="q",
                                category="X", market_yes_price=0.5,
                                model_implied=0.7, gap=0.2,
                                direction="YES", severity="medium", note="-")]
        monkeypatch.setattr(edges, "SOURCES", {
            "broken": ("Broken", broken),
            "good": ("Good", good),
        })
        out = edges.find_all_edges(pd.DataFrame())
        assert out["broken"] == []        # caught + empty
        assert len(out["good"]) == 1      # other source still ran

    def test_progress_callbacks_invoked(self, monkeypatch):
        starts: list[str] = []
        dones: list[tuple[str, int]] = []
        e = edges.Edge(source="x", market_id="m", question="q",
                       category="Y", market_yes_price=0.4,
                       model_implied=0.6, gap=0.2,
                       direction="YES", severity="medium", note="-")
        monkeypatch.setattr(edges, "SOURCES", {
            "x": ("X", self._stub_finder([e])),
        })
        edges.find_all_edges(
            pd.DataFrame(),
            on_source_start=starts.append,
            on_source_done=lambda k, n: dones.append((k, n)),
        )
        assert starts == ["x"]
        assert dones == [("x", 1)]

    def test_per_source_kwargs_override(self, monkeypatch):
        seen = {}
        def f(df, *, min_gap=0.1, min_days_to_expiry=1.0, **kw):
            seen["min_gap"] = min_gap
            return []
        monkeypatch.setattr(edges, "SOURCES", {
            "x": ("X", f),
        })
        edges.find_all_edges(
            pd.DataFrame(),
            min_gap=0.10,
            per_source_kwargs={"x": {"min_gap": 0.25}},
        )
        assert seen["min_gap"] == 0.25

    def test_unknown_kwargs_dropped(self, monkeypatch):
        # The finder doesn't accept `min_days_to_expiry` — aggregator
        # must drop it rather than crash.
        def f(df, *, min_gap=0.1):
            return []
        monkeypatch.setattr(edges, "SOURCES", {
            "x": ("X", f),
        })
        # Should not raise
        out = edges.find_all_edges(pd.DataFrame(), min_gap=0.1,
                                     min_days_to_expiry=5.0)
        assert out == {"x": []}


class TestEdgesSeverity:
    def test_severity_uses_abs_gap(self):
        e = edges.Edge(source="a", market_id="m", question="q",
                       category="X", market_yes_price=0.4,
                       model_implied=0.6, gap=-0.3,
                       direction="NO", severity="medium", note="-")
        assert edges.edges_severity(e) == pytest.approx(0.3)


class TestSportsConsensusForTicker:
    """edges.sports_consensus_for_ticker — Kalshi MLB ticker → ESPN de-vigged
    consensus P(YES). (prior-art-survey #1)"""

    def _mlb_event(self, *, home_abbr, away_abbr, home_prob, away_prob,
                   state="pre"):
        return sd.SportsEvent(
            league="MLB", event_id="g1",
            name=f"{away_abbr} at {home_abbr}", state=state,
            home=sd.TeamSide(name=home_abbr, short_name=home_abbr,
                             abbreviation=home_abbr, is_home=True),
            away=sd.TeamSide(name=away_abbr, short_name=away_abbr,
                             abbreviation=away_abbr, is_home=False),
            home_winprob=home_prob, away_winprob=away_prob,
        )

    @patch.object(sd, "get_events_with_probabilities")
    def test_returns_yes_side_consensus(self, mock_evs):
        # HOU (home) 0.58, TEX (away) 0.42. Ticker YES = HOU → 0.58.
        mock_evs.return_value = [self._mlb_event(
            home_abbr="HOU", away_abbr="TEX", home_prob=0.58, away_prob=0.42)]
        p = edges.sports_consensus_for_ticker("KXMLBGAME-26MAY282005HOUTEX-HOU")
        assert p == pytest.approx(0.58)

    @patch.object(sd, "get_events_with_probabilities")
    def test_returns_away_side_for_away_yes_ticker(self, mock_evs):
        mock_evs.return_value = [self._mlb_event(
            home_abbr="HOU", away_abbr="TEX", home_prob=0.58, away_prob=0.42)]
        p = edges.sports_consensus_for_ticker("KXMLBGAME-26MAY282005HOUTEX-TEX")
        assert p == pytest.approx(0.42)

    @patch.object(sd, "get_events_with_probabilities")
    def test_none_when_no_consensus(self, mock_evs):
        # Event present but no moneyline → winprobs None → skip.
        mock_evs.return_value = [self._mlb_event(
            home_abbr="HOU", away_abbr="TEX", home_prob=None, away_prob=None)]
        assert edges.sports_consensus_for_ticker(
            "KXMLBGAME-26MAY282005HOUTEX-HOU") is None

    @patch.object(sd, "get_events_with_probabilities")
    def test_none_when_game_finished(self, mock_evs):
        mock_evs.return_value = [self._mlb_event(
            home_abbr="HOU", away_abbr="TEX", home_prob=0.58, away_prob=0.42,
            state="post")]
        assert edges.sports_consensus_for_ticker(
            "KXMLBGAME-26MAY282005HOUTEX-HOU") is None

    @patch.object(sd, "get_events_with_probabilities")
    def test_none_when_no_matching_event(self, mock_evs):
        mock_evs.return_value = [self._mlb_event(
            home_abbr="NYM", away_abbr="MIA", home_prob=0.55, away_prob=0.45)]
        assert edges.sports_consensus_for_ticker(
            "KXMLBGAME-26MAY282005HOUTEX-HOU") is None

    def test_none_on_non_mlb_ticker(self):
        assert edges.sports_consensus_for_ticker("KXNBA-XYZ-LAL") is None
