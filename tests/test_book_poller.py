"""Tests for the order-book poller's per-cycle logic.

The loop itself isn't tested — that's just `while True: sleep`. The
interesting code is `run_cycle` / `_poll_one_provider`, which is
provider-bound and easy to mock.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.archive import book_archive
from pm.capture import book_poller


@pytest.fixture(autouse=True)
def _isolate_archive(tmp_path, monkeypatch):
    """Per-test SQLite tempfile so poller test runs don't pollute each
    other or the real archive."""
    monkeypatch.setenv("BOOK_ARCHIVE_PATH", str(tmp_path / "archive.sqlite"))


@pytest.fixture
def fake_provider():
    """Mock provider that returns 2 markets with 1 token each. Each
    get_order_book returns a non-empty raw book (will be passed through
    pm.parse_order_book by the poller)."""
    p = MagicMock()
    p.name = "fakevenue"
    p.get_markets.return_value = pd.DataFrame([
        {"condition_id": "m1", "id": "m1", "volume_24hr": 5000},
        {"condition_id": "m2", "id": "m2", "volume_24hr": 1000},
    ])
    p.list_outcome_tokens.side_effect = lambda cid: [
        {"token_id": f"tok-{cid}", "outcome": "Yes"},
    ]
    # parse_order_book ingests this shape (see Polymarket /book response)
    p.get_order_book.return_value = {
        "bids": [{"price": "0.39", "size": "100"}],
        "asks": [{"price": "0.41", "size": "100"}],
    }
    return p


# ---------------------------------------------------------------------------
# Happy path: every market produces a snapshot
# ---------------------------------------------------------------------------

class TestPollOneProvider:
    def test_stores_snapshot_per_token(self, fake_provider):
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.venue == "fakevenue"
        assert stats.fetched == 2
        assert stats.skipped == 0
        assert stats.errors == 0
        # And the archive actually has them
        s = book_archive.stats()
        assert s["entries"] == 2

    def test_max_markets_caps_provider_request(self, fake_provider):
        book_poller._poll_one_provider(
            fake_provider, max_markets=5, skip_if_recent_s=60,
        )
        # get_markets must have been called with limit=5
        kwargs = fake_provider.get_markets.call_args.kwargs
        assert kwargs.get("limit") == 5

    def test_skip_if_recent_avoids_redundant_fetch(self, fake_provider):
        # First cycle: stores 2 snapshots
        book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        # Reset the mock so call counts measure cycle-2 only
        fake_provider.get_order_book.reset_mock()
        # Second cycle within the skip window → 0 new fetches, 2 skips
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.fetched == 0
        assert stats.skipped == 2
        fake_provider.get_order_book.assert_not_called()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestPollerBatchesWrites:
    """The poller must flush all snapshots from one provider in ONE
    transaction — one fsync per cycle, not one per snapshot. Crucial
    for HDD performance once we're at 100+ markets per cycle."""

    @patch.object(book_archive, "store_snapshots", wraps=book_archive.store_snapshots)
    @patch.object(book_archive, "store_snapshot")
    def test_uses_batch_helper_not_per_row(
        self, mock_single, mock_batch, fake_provider,
    ):
        book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        # Single-row writes must NOT be used in the cycle path
        mock_single.assert_not_called()
        # Exactly one batch write per provider per cycle
        assert mock_batch.call_count == 1
        # And the batch payload should be the full 2-snapshot list
        payload = mock_batch.call_args.args[0]
        assert len(payload) == 2


class TestPollerErrorHandling:
    def test_get_markets_failure_counted_but_doesnt_crash(self, fake_provider):
        fake_provider.get_markets.side_effect = RuntimeError("boom")
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.errors >= 1
        assert stats.fetched == 0

    def test_list_outcome_tokens_failure_skips_market(self, fake_provider):
        # First market raises; second succeeds
        fake_provider.list_outcome_tokens.side_effect = [
            RuntimeError("bad market"),
            [{"token_id": "tok-m2", "outcome": "Yes"}],
        ]
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.errors >= 1
        assert stats.fetched == 1   # m2 still landed

    def test_get_order_book_failure_skips_token(self, fake_provider):
        fake_provider.get_order_book.side_effect = [
            RuntimeError("rate limited"),
            {"bids": [], "asks": [{"price": "0.5", "size": "10"}]},
        ]
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.errors >= 1
        assert stats.fetched == 1   # second one stored

    def test_empty_markets_short_circuits(self, fake_provider):
        fake_provider.get_markets.return_value = pd.DataFrame()
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.fetched == 0
        assert stats.errors == 0
        fake_provider.list_outcome_tokens.assert_not_called()


# ---------------------------------------------------------------------------
# Token de-dup: tokens with no token_id are skipped
# ---------------------------------------------------------------------------

class TestTokenSkipping:
    def test_empty_token_id_skipped(self, fake_provider):
        fake_provider.list_outcome_tokens.side_effect = lambda cid: [
            {"token_id": "", "outcome": "Yes"},   # bad
        ]
        stats = book_poller._poll_one_provider(
            fake_provider, max_markets=10, skip_if_recent_s=60,
        )
        assert stats.fetched == 0


# ---------------------------------------------------------------------------
# run_cycle integration across providers
# ---------------------------------------------------------------------------

class TestRunCycle:
    @patch.object(book_poller.provider_mod, "list_providers")
    def test_iterates_all_providers(self, mock_list, fake_provider):
        # Two providers, both with the same fake behavior
        p1 = fake_provider
        p2 = MagicMock()
        p2.name = "fake2"
        p2.get_markets.return_value = pd.DataFrame([
            {"condition_id": "m9", "id": "m9", "volume_24hr": 100},
        ])
        p2.list_outcome_tokens.return_value = [
            {"token_id": "tok-m9", "outcome": "Yes"},
        ]
        p2.get_order_book.return_value = {
            "bids": [], "asks": [{"price": "0.5", "size": "10"}],
        }
        mock_list.return_value = [p1, p2]

        results = book_poller.run_cycle(
            max_markets=10, skip_if_recent_s=60,
        )
        assert {r.venue for r in results} == {"fakevenue", "fake2"}
        # Total snapshots = 2 (from fake_provider) + 1 (from fake2)
        assert book_archive.stats()["entries"] == 3
