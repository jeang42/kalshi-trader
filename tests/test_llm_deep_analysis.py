"""Tests for the deep-analysis module — parse, context formatting,
cache behaviour. The actual Ollama call is mocked."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.feeds import llm_deep_analysis as lda

# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestParseResponse:
    def _good(self):
        return json.dumps({
            "probability_yes": 0.65,
            "confidence": "medium",
            "key_drivers": ["incumbent", "favorable polling"],
            "key_risks": ["scandal possible"],
            "evidence_used": ["price history", "siblings"],
            "recommended_action": "BUY_YES",
            "reasoning": "Looks moderately favourable.",
        })

    def test_happy_path(self):
        a = lda._parse_response(
            self._good(), model="m", ticker="T",
            question="Q?", context_summary="CTX",
        )
        assert a is not None
        assert a.probability == 0.65
        assert a.confidence == "medium"
        assert a.recommended_action == "BUY_YES"
        assert "incumbent" in a.key_drivers
        assert a.context_summary == "CTX"

    def test_rejects_non_json(self):
        a = lda._parse_response(
            "not json {{{", model="m", ticker="T", question="Q?",
            context_summary="",
        )
        assert a is None

    def test_rejects_probability_out_of_range(self):
        bad = json.dumps({"probability_yes": 1.5, "confidence": "low"})
        a = lda._parse_response(bad, model="m", ticker="T", question="Q?",
                                 context_summary="")
        assert a is None

    def test_unknown_confidence_clamps_to_low(self):
        weird = json.dumps({"probability_yes": 0.4, "confidence": "yolo"})
        a = lda._parse_response(weird, model="m", ticker="T", question="Q?",
                                 context_summary="")
        assert a is not None
        assert a.confidence == "low"

    def test_unknown_action_clamps_to_wait(self):
        weird = json.dumps({"probability_yes": 0.5, "recommended_action": "YOLO"})
        a = lda._parse_response(weird, model="m", ticker="T", question="Q?",
                                 context_summary="")
        assert a is not None
        assert a.recommended_action == "WAIT"

    def test_str_lists_coerced_and_capped(self):
        long_list = json.dumps({
            "probability_yes": 0.5,
            "key_drivers": [f"d{i}" for i in range(20)],
        })
        a = lda._parse_response(long_list, model="m", ticker="T",
                                 question="Q?", context_summary="")
        assert a is not None
        assert len(a.key_drivers) == 5   # capped at 5


# ---------------------------------------------------------------------------
# Context summarizers
# ---------------------------------------------------------------------------

class TestSummarizers:
    def test_price_history_empty(self):
        assert "no history" in lda._summarize_price_history(pd.DataFrame())

    def test_price_history_one_line(self):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=5),
            "price": [0.3, 0.32, 0.35, 0.38, 0.40],
        })
        s = lda._summarize_price_history(df)
        assert "started 0.30" in s
        assert "ended 0.40" in s
        assert "Δ=+0.10" in s
        assert "5 samples" in s

    def test_order_book_empty(self):
        assert "empty" in lda._summarize_order_book(pd.DataFrame()).lower()

    def test_order_book_normal(self):
        df = pd.DataFrame([
            {"side": "bid", "price": 0.40, "size": 100, "cumulative_size": 100},
            {"side": "bid", "price": 0.38, "size": 200, "cumulative_size": 300},
            {"side": "ask", "price": 0.42, "size": 50, "cumulative_size": 50},
            {"side": "ask", "price": 0.45, "size": 150, "cumulative_size": 200},
        ])
        s = lda._summarize_order_book(df)
        assert "best bid 0.400" in s
        assert "best ask 0.420" in s
        assert "spread 0.020" in s

    def test_siblings_filters_self(self):
        df = pd.DataFrame([
            {"condition_id": "A", "question": "Q1", "outcome_prices": [0.3, 0.7]},
            {"condition_id": "B", "question": "Q2", "outcome_prices": [0.5, 0.5]},
        ])
        s = lda._summarize_siblings(df, this_ticker="A")
        assert "Q2" in s
        assert "Q1" not in s

    def test_siblings_no_others(self):
        df = pd.DataFrame([
            {"condition_id": "A", "question": "Q1", "outcome_prices": [0.3, 0.7]},
        ])
        s = lda._summarize_siblings(df, this_ticker="A")
        assert "(no siblings" in s.lower()


# ---------------------------------------------------------------------------
# Recommended-model sorting + default selection
# ---------------------------------------------------------------------------

class TestRecommendedSorting:
    def test_pins_recommended_to_top_in_priority_order(self):
        installed = ["random:7b", "qwen2.5:32b", "weird:latest",
                     "qwen3:32b", "llama3.1:latest"]
        out = lda.sort_models_recommended_first(installed)
        # qwen3:32b before qwen2.5:32b before llama3.1:latest
        i_q3 = out.index("qwen3:32b")
        i_q25 = out.index("qwen2.5:32b")
        i_llama = out.index("llama3.1:latest")
        assert i_q3 < i_q25 < i_llama
        # Non-recommended ones still present
        assert "random:7b" in out
        assert "weird:latest" in out

    def test_no_recommended_installed_preserves_order(self):
        installed = ["random:7b", "weird:latest"]
        assert lda.sort_models_recommended_first(installed) == installed

    def test_empty_list(self):
        assert lda.sort_models_recommended_first([]) == []

    def test_default_model_picks_first_recommended_present(self):
        # qwen3 missing, qwen2.5 present → qwen2.5
        installed = ["random:7b", "qwen2.5:32b", "llama3.1:latest"]
        assert lda.default_model(installed) == "qwen2.5:32b"

    def test_default_model_falls_back_to_first_installed(self):
        installed = ["random:7b", "weird:latest"]
        assert lda.default_model(installed) == "random:7b"

    def test_default_model_none_when_empty(self):
        assert lda.default_model([]) is None


# ---------------------------------------------------------------------------
# analyze_market end-to-end with mocked Ollama
# ---------------------------------------------------------------------------

class TestAnalyzeMarket:
    def _provider(self):
        p = MagicMock()
        p.get_market.return_value = {
            "rules_primary": "Market resolves YES if X happens by date.",
        }
        p.get_price_history.return_value = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=10),
            "price": [0.4 + i*0.01 for i in range(10)],
        })
        p.get_order_book.return_value = {}
        return p

    def _df_market(self, ticker="KX-1"):
        return pd.DataFrame([{
            "condition_id": ticker, "question": "Q?",
            "category": "Politics", "days_to_resolution": 30.0,
            "best_bid": 0.40, "best_ask": 0.42, "last_trade_price": 0.41,
            "event_id": "EV", "outcome_prices": [0.41, 0.59],
        }])

    @patch("pm.feeds.llm_data._session")
    def test_happy_path_returns_parsed_analysis(self, mock_session, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.sqlite"))
        mock_session.post.return_value.json.return_value = {
            "message": {"content": json.dumps({
                "probability_yes": 0.55, "confidence": "high",
                "key_drivers": ["d1"], "key_risks": ["r1"],
                "evidence_used": ["e1"],
                "recommended_action": "BUY_YES",
                "reasoning": "x",
            })}
        }
        mock_session.post.return_value.raise_for_status = lambda: None
        result = lda.analyze_market(
            provider=self._provider(),
            df_markets=self._df_market(),
            ticker="KX-1", model="testmodel",
        )
        assert result is not None
        assert result.probability == 0.55
        assert result.market_id == "KX-1"

    @patch("pm.feeds.llm_data._session")
    def test_transport_failure_returns_none(self, mock_session, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.sqlite"))
        import requests
        mock_session.post.side_effect = requests.RequestException("boom")
        result = lda.analyze_market(
            provider=self._provider(),
            df_markets=self._df_market(),
            ticker="KX-1", model="testmodel",
        )
        assert result is None

    @patch("pm.feeds.llm_data._session")
    def test_unparseable_content_returns_none(self, mock_session, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.sqlite"))
        mock_session.post.return_value.json.return_value = {
            "message": {"content": "not json"}
        }
        mock_session.post.return_value.raise_for_status = lambda: None
        result = lda.analyze_market(
            provider=self._provider(),
            df_markets=self._df_market(),
            ticker="KX-1", model="testmodel",
        )
        assert result is None

    @patch("pm.feeds.llm_data._session")
    def test_caches_result_and_short_circuits_on_second_call(
        self, mock_session, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.sqlite"))
        mock_session.post.return_value.json.return_value = {
            "message": {"content": json.dumps({
                "probability_yes": 0.42, "confidence": "low",
                "recommended_action": "WAIT",
            })}
        }
        mock_session.post.return_value.raise_for_status = lambda: None
        a = lda.analyze_market(
            provider=self._provider(), df_markets=self._df_market(),
            ticker="KX-1", model="testmodel",
        )
        assert a is not None
        # Second call: should hit cache, no new POST
        mock_session.post.reset_mock()
        a2 = lda.analyze_market(
            provider=self._provider(), df_markets=self._df_market(),
            ticker="KX-1", model="testmodel",
        )
        assert a2 is not None
        assert a2.probability == pytest.approx(0.42)
        mock_session.post.assert_not_called()
