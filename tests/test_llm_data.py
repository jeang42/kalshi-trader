"""Tests for the local-LLM (Ollama) forecasting layer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from pm.feeds import llm_data as ld

# ---------------------------------------------------------------------------
# Availability + model listing
# ---------------------------------------------------------------------------

class TestAvailability:
    @patch.object(ld._session, "get")
    def test_is_available_true_on_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        assert ld.is_available() is True

    @patch.object(ld._session, "get")
    def test_is_available_false_on_connection_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        assert ld.is_available() is False

    @patch.object(ld._session, "get")
    def test_list_models_returns_names(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:latest"},
                                     {"name": "qwen2.5:14b"}]},
        )
        mock_get.return_value.raise_for_status = lambda: None
        assert ld.list_models() == ["llama3.1:latest", "qwen2.5:14b"]

    @patch.object(ld._session, "get")
    def test_list_models_empty_on_unreachable(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("nope")
        assert ld.list_models() == []


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

class TestPromptBuilder:
    def test_question_only(self):
        prompt = ld.build_user_prompt(
            question="Will X happen?",
            category=None, market_yes_price=None, days_to_deadline=None,
        )
        assert "Will X happen?" in prompt
        assert '"probability"' in prompt  # asks for JSON

    def test_includes_all_fields(self):
        prompt = ld.build_user_prompt(
            question="Will BTC reach $200k by 2027?",
            category="Crypto", market_yes_price=0.42,
            days_to_deadline=180.0,
            as_of=datetime(2026, 5, 1, tzinfo=UTC),
        )
        assert "Will BTC reach $200k by 2027?" in prompt
        assert "Crypto" in prompt
        # market_yes_price is intentionally NOT in the prompt (blind forecast)
        assert "0.420" not in prompt
        assert "180" in prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_strict_json_object(self):
        raw = '{"probability": 0.62, "reasoning": "Anchored to market."}'
        f = ld._parse_response(raw, model="m1")
        assert f is not None
        assert f.probability == pytest.approx(0.62)
        assert "market" in f.reasoning.lower()

    def test_extra_prose_falls_back_to_regex(self):
        raw = ('Sure! Here is my analysis:\n'
               '{"probability": 0.30, "reasoning": "..."}\n'
               'I hope this helps.')
        f = ld._parse_response(raw, model="m1")
        assert f is not None
        assert f.probability == pytest.approx(0.30)

    def test_probability_out_of_range_is_rejected(self):
        raw = '{"probability": 1.7, "reasoning": "broken"}'
        assert ld._parse_response(raw, model="m1") is None

    def test_negative_probability_is_rejected(self):
        raw = '{"probability": -0.1, "reasoning": "broken"}'
        assert ld._parse_response(raw, model="m1") is None

    def test_missing_probability_returns_none(self):
        raw = '{"reasoning": "no number"}'
        assert ld._parse_response(raw, model="m1") is None

    def test_unparseable_garbage_returns_none(self):
        assert ld._parse_response("totally not json", model="m1") is None


# ---------------------------------------------------------------------------
# get_forecast — end-to-end with mocked HTTP
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect the LLM cache to a per-test tempfile so cached
    responses don't leak between tests. Autouse on this module so
    every test gets a clean cache without per-test boilerplate."""
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "llm_cache.sqlite"))


class TestGetForecast:
    @patch.object(ld._session, "post")
    def test_happy_path(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"probability": 0.71, "reasoning": "Within band."}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        f = ld.get_forecast(
            question="Will X happen?",
            model="llama3.1:latest",
            market_yes_price=0.65,
        )
        assert f is not None
        assert f.probability == pytest.approx(0.71)
        assert f.model == "llama3.1:latest"
        # Verify the request body shape
        kwargs = mock_post.call_args.kwargs
        body = kwargs["json"]
        assert body["model"] == "llama3.1:latest"
        assert body["format"] == "json"
        assert body["stream"] is False
        assert body["options"]["temperature"] == pytest.approx(0.2)
        assert any(m["role"] == "system" for m in body["messages"])

    @patch.object(ld._session, "post")
    def test_http_error_returns_none(self, mock_post):
        mock_post.side_effect = requests.RequestException("server down")
        assert ld.get_forecast(question="X?", model="m") is None

    @patch.object(ld._session, "post")
    def test_empty_content_returns_none(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"message": {"content": ""}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        assert ld.get_forecast(question="X?", model="m") is None

    @patch.object(ld._session, "post")
    def test_custom_options_merge(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"probability": 0.5, "reasoning": "x"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        ld.get_forecast(question="X?", model="m",
                        options={"temperature": 0.7, "num_ctx": 8192})
        sent = mock_post.call_args.kwargs["json"]
        # User options override the default temperature.
        assert sent["options"]["temperature"] == pytest.approx(0.7)
        assert sent["options"]["num_ctx"] == 8192


class TestForecastCaching:
    """Persistent cache wrapping `get_forecast`."""

    @patch.object(ld._session, "post")
    def test_second_call_skips_http(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"probability": 0.7, "reasoning": "first"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        kwargs = dict(question="Q?", model="m", market_yes_price=0.5,
                      days_to_deadline=30)
        first = ld.get_forecast(**kwargs)
        second = ld.get_forecast(**kwargs)
        assert first is not None and second is not None
        assert second.probability == pytest.approx(first.probability)
        assert second.reasoning == first.reasoning
        # Exactly one POST despite two identical calls.
        assert mock_post.call_count == 1

    @patch.object(ld._session, "post")
    def test_bypass_cache_forces_fresh_call(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"probability": 0.7, "reasoning": "x"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        kwargs = dict(question="Q?", model="m", market_yes_price=0.5,
                      days_to_deadline=30)
        ld.get_forecast(**kwargs)
        ld.get_forecast(**kwargs, bypass_cache=True)
        # Bypass means a fresh POST every time.
        assert mock_post.call_count == 2

    @patch.object(ld._session, "post")
    def test_different_market_price_hits_cache(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"probability": 0.5, "reasoning": "x"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        # market_yes_price is NOT in the cache key (blind forecast) —
        # same question at different market prices shares one cache entry.
        ld.get_forecast(question="Q?", model="m", market_yes_price=0.30,
                        days_to_deadline=30)
        ld.get_forecast(question="Q?", model="m", market_yes_price=0.70,
                        days_to_deadline=30)
        assert mock_post.call_count == 1

    @patch.object(ld._session, "post")
    def test_failed_call_does_not_poison_cache(self, mock_post):
        # First call: HTTP error → returns None, nothing stored.
        # Second call (same params, HTTP fixed): MUST still hit the wire
        # and return a fresh forecast.
        mock_post.side_effect = [
            requests.RequestException("transient"),
            MagicMock(status_code=200,
                      json=lambda: {"message": {"content":
                        '{"probability": 0.6, "reasoning": "x"}'}},
                      raise_for_status=lambda: None),
        ]
        kwargs = dict(question="Q?", model="m", market_yes_price=0.5,
                      days_to_deadline=30)
        first = ld.get_forecast(**kwargs)
        assert first is None
        second = ld.get_forecast(**kwargs)
        assert second is not None
        assert mock_post.call_count == 2
