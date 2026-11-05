"""Tests for the MLB book poller — discovers events, snapshots books,
writes to book_archive. Network mocked.

The poller is small enough that integration-style mocking covers it
well: we mock kalshi_client._get + get_order_book + parse_order_book +
book_archive.store_snapshot and assert the right calls happen in the
right order with the right shape."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.capture import mlb_book_poller


@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_open_mlb_event_tickers_parses_response(mock_get):
    mock_get.return_value = {"events": [
        {"event_ticker": "KXMLBGAME-26MAY231610NYMMIA"},
        {"event_ticker": "KXMLBGAME-26MAY231915TBNYY"},
        {"some_other_key": "ignored"},  # malformed entry skipped
    ]}
    out = mlb_book_poller._open_mlb_event_tickers()
    assert out == [
        "KXMLBGAME-26MAY231610NYMMIA",
        "KXMLBGAME-26MAY231915TBNYY",
    ]


@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_open_mlb_event_tickers_returns_empty_on_network_fail(mock_get):
    mock_get.side_effect = RuntimeError("kalshi down")
    out = mlb_book_poller._open_mlb_event_tickers()
    assert out == []


@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_market_tickers_for_event(mock_get):
    mock_get.return_value = {"event": {"markets": [
        {"ticker": "KXMLBGAME-26MAY231610NYMMIA-NYM"},
        {"ticker": "KXMLBGAME-26MAY231610NYMMIA-MIA"},
        {},  # missing ticker — skipped silently
    ]}}
    out = mlb_book_poller._market_tickers_for_event("KXMLBGAME-26MAY231610NYMMIA")
    assert out == [
        "KXMLBGAME-26MAY231610NYMMIA-NYM",
        "KXMLBGAME-26MAY231610NYMMIA-MIA",
    ]


@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_market_tickers_for_event_handles_failure(mock_get):
    mock_get.side_effect = RuntimeError("event detail 500")
    assert mlb_book_poller._market_tickers_for_event("X") == []


@patch("pm.capture.mlb_book_poller.book_archive.store_snapshot")
@patch("pm.capture.mlb_book_poller.parse_order_book")
@patch("pm.capture.mlb_book_poller.get_order_book")
@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_run_cycle_happy_path(mock_get, mock_book, mock_parse, mock_store):
    """One event → two markets → two successful snapshots."""
    mlb_book_poller._cached_markets = []
    mlb_book_poller._cache_ts = 0.0
    def kget(path, **kw):
        if path == "/events":
            return {"events": [{"event_ticker": "KXMLBGAME-26MAY231610NYMMIA"}]}
        return {"event": {"markets": [
            {"ticker": "KXMLBGAME-26MAY231610NYMMIA-NYM"},
            {"ticker": "KXMLBGAME-26MAY231610NYMMIA-MIA"},
        ]}}
    mock_get.side_effect = kget
    mock_book.return_value = {"bids": [], "asks": []}
    mock_parse.return_value = pd.DataFrame(columns=["side", "price", "size"])
    mock_store.return_value = True

    stats = mlb_book_poller.run_cycle()
    assert stats["events"] == 1
    assert stats["markets"] == 2
    assert stats["snapshots_ok"] == 2
    assert stats["snapshots_failed"] == 0
    assert mock_store.call_count == 2
    # Verify the store_snapshot call uses ticker for both market_id + token_id
    for call in mock_store.call_args_list:
        assert call.kwargs["venue"] == "kalshi"
        assert call.kwargs["market_id"] == call.kwargs["token_id"]


@patch("pm.capture.mlb_book_poller.book_archive.store_snapshot")
@patch("pm.capture.mlb_book_poller.parse_order_book")
@patch("pm.capture.mlb_book_poller.get_order_book")
@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_run_cycle_counts_failed_snapshots(mock_get, mock_book, mock_parse, mock_store):
    """When parse_order_book raises on one market, that one is counted
    failed but the cycle continues with the others."""
    mlb_book_poller._cached_markets = []
    mlb_book_poller._cache_ts = 0.0
    mock_get.side_effect = lambda path, **kw: (
        {"events": [{"event_ticker": "X"}]} if path == "/events"
        else {"event": {"markets": [
            {"ticker": "A"}, {"ticker": "B"},
        ]}}
    )
    mock_book.return_value = {}
    # First call succeeds, second raises
    mock_parse.side_effect = [
        pd.DataFrame(columns=["side", "price", "size"]),
        ValueError("malformed"),
    ]
    mock_store.return_value = True

    stats = mlb_book_poller.run_cycle()
    assert stats["snapshots_ok"] == 1
    assert stats["snapshots_failed"] == 1


@patch("pm.capture.mlb_book_poller._kalshi_get")
def test_run_cycle_with_no_events_short_circuits(mock_get):
    """No events open → cycle does not call book endpoints at all."""
    mlb_book_poller._cached_markets = []
    mlb_book_poller._cache_ts = 0.0
    mock_get.return_value = {"events": []}
    stats = mlb_book_poller.run_cycle()
    assert stats["events"] == 0
    assert stats["markets"] == 0
    assert stats["snapshots_ok"] == 0
    # _kalshi_get should have been called exactly once (the events fetch)
    assert mock_get.call_count == 1
