"""Tests for the web-context fetchers (Google News RSS + Wikipedia).

HTTP is mocked — these tests don't hit the network. Live coverage
comes from the smoke test (``scripts/smoke_test.py``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pm.feeds import web_context as wc

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.sqlite"))


_GOOGLE_NEWS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test feed</title>
    <item>
      <title>Massie loses primary - WaPo</title>
      <link>https://example.com/wapo/1</link>
      <pubDate>Tue, 19 May 2026 22:00:00 GMT</pubDate>
      <source url="https://wapo.com">The Washington Post</source>
    </item>
    <item>
      <title>Live results - NBC News</title>
      <link>https://example.com/nbc/2</link>
      <pubDate>Tue, 19 May 2026 21:30:00 GMT</pubDate>
      <source url="https://nbcnews.com">NBC News</source>
    </item>
  </channel>
</rss>
"""

_WIKI_SUMMARY_JSON = {
    "type": "standard",
    "title": "Thomas Massie",
    "extract": "Thomas Harold Massie is an American politician and engineer.",
    "content_urls": {
        "desktop": {"page": "https://en.wikipedia.org/wiki/Thomas_Massie"},
    },
}


# ---------------------------------------------------------------------------
# search_news
# ---------------------------------------------------------------------------

class TestSearchNews:
    @patch.object(wc._session, "get")
    def test_parses_rss_into_news_items(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=_GOOGLE_NEWS_XML,
        )
        mock_get.return_value.raise_for_status = lambda: None
        items = wc.search_news("Thomas Massie", max_items=5)
        assert len(items) == 2
        assert items[0].title.startswith("Massie loses")
        assert items[0].source == "The Washington Post"
        assert items[0].url.startswith("https://example.com/wapo")

    @patch.object(wc._session, "get")
    def test_respects_max_items(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=_GOOGLE_NEWS_XML,
        )
        mock_get.return_value.raise_for_status = lambda: None
        items = wc.search_news("Thomas Massie", max_items=1)
        assert len(items) == 1

    @patch.object(wc._session, "get")
    def test_empty_query_short_circuits(self, mock_get):
        items = wc.search_news("", max_items=5)
        assert items == []
        mock_get.assert_not_called()

    @patch.object(wc._session, "get")
    def test_network_failure_returns_empty(self, mock_get):
        import requests
        mock_get.side_effect = requests.RequestException("nope")
        items = wc.search_news("Thomas Massie")
        assert items == []

    @patch.object(wc._session, "get")
    def test_caches_result_and_short_circuits_second_call(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=_GOOGLE_NEWS_XML,
        )
        mock_get.return_value.raise_for_status = lambda: None
        first = wc.search_news("Massie")
        mock_get.reset_mock()
        second = wc.search_news("Massie")
        assert first == second
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# wikipedia_summary
# ---------------------------------------------------------------------------

class TestWikipediaSummary:
    @patch.object(wc._session, "get")
    def test_returns_summary_on_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = _WIKI_SUMMARY_JSON
        w = wc.wikipedia_summary("Thomas Massie")
        assert w is not None
        assert w.title == "Thomas Massie"
        assert "American politician" in w.extract
        assert w.url.endswith("Thomas_Massie")

    @patch.object(wc._session, "get")
    def test_404_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        # raise_for_status should not be called when we early-return on 404
        w = wc.wikipedia_summary("Definitely Not A Real Person")
        assert w is None

    @patch.object(wc._session, "get")
    def test_disambiguation_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = {
            "type": "disambiguation", "title": "Jeff", "extract": "list",
        }
        assert wc.wikipedia_summary("Jeff") is None

    @patch.object(wc._session, "get")
    def test_empty_extract_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = {
            "type": "standard", "title": "X", "extract": "",
        }
        assert wc.wikipedia_summary("X") is None

    @patch.object(wc._session, "get")
    def test_empty_title_short_circuits(self, mock_get):
        assert wc.wikipedia_summary("") is None
        mock_get.assert_not_called()

    @patch.object(wc._session, "get")
    def test_path_uses_quote_not_quote_plus(self, mock_get):
        """Regression guard for the Phase-B day-1 bug: quote_plus put
        '+' for spaces, Wikipedia REST 404'd. The path segment must use
        '%20'-style encoding."""
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = _WIKI_SUMMARY_JSON
        wc.wikipedia_summary("Thomas Massie")
        called_url = mock_get.call_args[0][0]
        assert "Thomas%20Massie" in called_url
        assert "Thomas+Massie" not in called_url


# ---------------------------------------------------------------------------
# format_*_for_prompt
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_news_empty(self):
        assert "no recent headlines" in wc.format_news_for_prompt([])

    def test_news_includes_source_in_brackets(self):
        items = [wc.NewsItem("Massie loses", "WaPo", "x", "https://x")]
        out = wc.format_news_for_prompt(items)
        assert "Massie loses" in out
        assert "[WaPo]" in out

    def test_wiki_empty(self):
        assert "no Wikipedia" in wc.format_wiki_for_prompt(None)

    def test_wiki_truncates_long_extract(self):
        w = wc.WikiSummary("X", "word " * 500, "https://x")
        out = wc.format_wiki_for_prompt(w, max_chars=100)
        assert out.endswith("…")
        # Trim point preserves whole words
        assert "word word" in out
