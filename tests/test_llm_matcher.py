"""Tests for the LLM-assisted semantic matcher."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from pm.feeds import llm_data
from pm.feeds import llm_matcher as lm


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect llm_cache to a per-test tempfile so cached verdicts
    don't leak between tests."""
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "llm_cache.sqlite"))

# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestParseVerdict:
    def test_strict_json_true(self):
        raw = '{"same_question": true, "confidence": 0.9, "reasoning": "Identical."}'
        v = lm._parse_verdict(raw, model="m")
        assert v is not None
        assert v.same_question is True
        assert v.confidence == pytest.approx(0.9)
        assert "Identical" in v.reasoning

    def test_strict_json_false(self):
        raw = '{"same_question": false, "confidence": 0.85, "reasoning": "Opposite directions."}'
        v = lm._parse_verdict(raw, model="m")
        assert v is not None
        assert v.same_question is False

    def test_prose_wrapped_json_regex_fallback(self):
        raw = ('I think these questions are different:\n'
               '{"same_question": false, "confidence": 0.75, "reasoning": "..."}\n'
               'Hope that helps.')
        v = lm._parse_verdict(raw, model="m")
        assert v is not None
        assert v.same_question is False
        assert v.confidence == pytest.approx(0.75)

    def test_missing_same_question_key_returns_none(self):
        assert lm._parse_verdict('{"confidence": 0.9}', model="m") is None

    def test_non_bool_same_question_returns_none(self):
        # JSON "1" is truthy but not bool — reject strictly
        assert lm._parse_verdict(
            '{"same_question": 1, "confidence": 0.9, "reasoning": "x"}',
            model="m",
        ) is None

    def test_confidence_clamped_to_unit_interval(self):
        v = lm._parse_verdict(
            '{"same_question": true, "confidence": 1.5, "reasoning": "x"}',
            model="m",
        )
        assert v is not None
        assert v.confidence == 1.0

        v = lm._parse_verdict(
            '{"same_question": true, "confidence": -0.2, "reasoning": "x"}',
            model="m",
        )
        assert v is not None
        assert v.confidence == 0.0

    def test_unparseable_returns_none(self):
        assert lm._parse_verdict("totally not json", model="m") is None

    def test_default_confidence_when_missing(self):
        v = lm._parse_verdict(
            '{"same_question": true, "reasoning": "x"}', model="m",
        )
        assert v is not None
        assert v.confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# verify_match end-to-end with mocked Ollama HTTP
# ---------------------------------------------------------------------------

class TestVerifyMatch:
    @patch.object(llm_data._session, "post")
    def test_happy_path_same(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"same_question": true, "confidence": 0.9, "reasoning": "ok"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        v = lm.verify_match("Q1?", "Q2?", model="m")
        assert v is not None
        assert v.same_question is True
        # The request body should be the chat shape with format=json
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == "m"
        assert body["format"] == "json"
        assert body["options"]["temperature"] == pytest.approx(0.1)
        # System prompt is the matcher one, not the forecaster one
        assert any("question-matching judge" in m["content"]
                   for m in body["messages"] if m["role"] == "system")

    @patch.object(llm_data._session, "post")
    def test_happy_path_different(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"same_question": false, "confidence": 0.8, "reasoning": "opposites"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        v = lm.verify_match("Will Fed cut?", "Will Fed hike?", model="m")
        assert v is not None
        assert v.same_question is False

    @patch.object(llm_data._session, "post")
    def test_http_failure_returns_none(self, mock_post):
        mock_post.side_effect = requests.RequestException("connection refused")
        assert lm.verify_match("A?", "B?", model="m") is None

    @patch.object(llm_data._session, "post")
    def test_non_json_envelope_returns_none(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json.side_effect = ValueError("bad")
        assert lm.verify_match("A?", "B?", model="m") is None

    @patch.object(llm_data._session, "post")
    def test_empty_content_returns_none(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content": ""}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        assert lm.verify_match("A?", "B?", model="m") is None

    def test_empty_question_short_circuits(self):
        # Both inputs blank — no HTTP call should happen, no model needed
        with patch.object(llm_data._session, "post") as mock_post:
            assert lm.verify_match("", "B?", model="m") is None
            assert lm.verify_match("A?", "", model="m") is None
            assert lm.verify_match("   ", "  ", model="m") is None
            mock_post.assert_not_called()

    @patch.object(llm_data._session, "post")
    def test_caller_options_override_defaults(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content":
                '{"same_question": true, "confidence": 0.9, "reasoning": "x"}'}},
        )
        mock_post.return_value.raise_for_status = lambda: None
        lm.verify_match("A?", "B?", model="m",
                         options={"temperature": 0.7, "num_ctx": 4096})
        body = mock_post.call_args.kwargs["json"]
        assert body["options"]["temperature"] == pytest.approx(0.7)
        assert body["options"]["num_ctx"] == 4096


class TestMatchCaching:
    """Persistent cache wrapping `verify_match`."""

    def _mock_response(self, mock_post, content: str):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content": content}},
        )
        mock_post.return_value.raise_for_status = lambda: None

    @patch.object(llm_data._session, "post")
    def test_second_call_skips_http(self, mock_post):
        self._mock_response(
            mock_post,
            '{"same_question": true, "confidence": 0.9, "reasoning": "ok"}',
        )
        first = lm.verify_match("Q1?", "Q2?", model="m")
        second = lm.verify_match("Q1?", "Q2?", model="m")
        assert first is not None and second is not None
        assert first.same_question == second.same_question
        assert mock_post.call_count == 1

    @patch.object(llm_data._session, "post")
    def test_cache_key_is_symmetric(self, mock_post):
        # verify_match(A, B) and verify_match(B, A) should share an
        # entry since the question pair is unordered for matching
        # purposes.
        self._mock_response(
            mock_post,
            '{"same_question": true, "confidence": 0.9, "reasoning": "x"}',
        )
        lm.verify_match("Q1?", "Q2?", model="m")
        lm.verify_match("Q2?", "Q1?", model="m")
        assert mock_post.call_count == 1

    @patch.object(llm_data._session, "post")
    def test_bypass_cache_forces_fresh_call(self, mock_post):
        self._mock_response(
            mock_post,
            '{"same_question": true, "confidence": 0.9, "reasoning": "x"}',
        )
        lm.verify_match("Q1?", "Q2?", model="m")
        lm.verify_match("Q1?", "Q2?", model="m", bypass_cache=True)
        assert mock_post.call_count == 2

    @patch.object(llm_data._session, "post")
    def test_failed_call_does_not_poison_cache(self, mock_post):
        import requests as _req
        mock_post.side_effect = [
            _req.RequestException("transient"),
            MagicMock(status_code=200,
                      json=lambda: {"message": {"content":
                        '{"same_question": true, "confidence": 0.9, '
                        '"reasoning": "x"}'}},
                      raise_for_status=lambda: None),
        ]
        first = lm.verify_match("Q1?", "Q2?", model="m")
        assert first is None
        second = lm.verify_match("Q1?", "Q2?", model="m")
        assert second is not None
        assert mock_post.call_count == 2
