"""Tests for the transaction-based portfolio model in portfolio.py."""

import json

import pandas as pd
import pytest

from pm.ledger import portfolio as pf


def _add_buy(question="Test", cid="0xaaa", outcome="Yes", shares=100.0, price=0.40):
    return pf.add_buy(question, cid, outcome, shares, price)


def _add_sell(question="Test", cid="0xaaa", outcome="Yes", shares=50.0, price=0.50):
    return pf.add_sell(question, cid, outcome, shares, price)


# ---------------------------------------------------------------------------
# Transactions: recording & validation
# ---------------------------------------------------------------------------

class TestRecordTransaction:
    def test_buy_recorded(self, portfolio_path):
        _add_buy(shares=100, price=0.40)
        df = pf.get_transactions()
        assert len(df) == 1
        assert df.iloc[0]["type"] == "buy"

    def test_sell_after_buy(self, portfolio_path):
        _add_buy(shares=100, price=0.40)
        _add_sell(shares=50, price=0.50)
        df = pf.get_transactions()
        assert len(df) == 2
        assert df.iloc[1]["type"] == "sell"

    def test_sell_with_no_position_raises(self, portfolio_path):
        with pytest.raises(ValueError, match="no open positions"):
            _add_sell()

    def test_sell_for_unknown_market_raises(self, portfolio_path):
        _add_buy(cid="0xaaa", outcome="Yes")
        with pytest.raises(ValueError, match="no open position"):
            pf.add_sell("X", "0xother", "Yes", 10, 0.5)

    def test_sell_more_than_held_raises(self, portfolio_path):
        _add_buy(shares=10, price=0.40)
        with pytest.raises(ValueError, match="only 10"):
            _add_sell(shares=20, price=0.50)

    def test_invalid_type_raises(self, portfolio_path):
        with pytest.raises(ValueError, match="tx_type"):
            pf.record_transaction("transfer", "Q", "0x1", "Yes", 10, 0.5)

    def test_zero_shares_raises(self, portfolio_path):
        with pytest.raises(ValueError, match="shares"):
            pf.add_buy("Q", "0x1", "Yes", 0, 0.5)

    def test_invalid_price_raises(self, portfolio_path):
        with pytest.raises(ValueError, match="price"):
            pf.add_buy("Q", "0x1", "Yes", 10, 1.5)

    def test_transaction_has_uuid_and_timestamp(self, portfolio_path):
        tx = _add_buy()
        assert "id" in tx and tx["id"]
        assert "timestamp" in tx and tx["timestamp"]


# ---------------------------------------------------------------------------
# Derived positions: averaging
# ---------------------------------------------------------------------------

class TestDerivedPositions:
    def test_single_buy(self, portfolio_path):
        _add_buy(shares=100, price=0.40)
        pos = pf.get_positions()
        assert len(pos) == 1
        assert pos.iloc[0]["shares"] == pytest.approx(100)
        assert pos.iloc[0]["avg_price"] == pytest.approx(0.40)
        assert pos.iloc[0]["cost_basis"] == pytest.approx(40.0)

    def test_two_buys_average(self, portfolio_path):
        # buy 100 @ 0.40, then 50 @ 0.60
        # avg = (40 + 30) / 150 = 0.4667
        _add_buy(shares=100, price=0.40)
        _add_buy(shares=50, price=0.60)
        pos = pf.get_positions()
        assert len(pos) == 1
        assert pos.iloc[0]["shares"] == pytest.approx(150)
        assert pos.iloc[0]["avg_price"] == pytest.approx(70 / 150, rel=1e-3)

    def test_separate_outcomes_separate_positions(self, portfolio_path):
        _add_buy(outcome="Yes", shares=100, price=0.40)
        _add_buy(outcome="No",  shares=50,  price=0.55)
        pos = pf.get_positions()
        assert len(pos) == 2

    def test_separate_markets_separate_positions(self, portfolio_path):
        _add_buy(cid="0xaaa", shares=100, price=0.40)
        _add_buy(cid="0xbbb", shares=100, price=0.30)
        pos = pf.get_positions()
        assert len(pos) == 2

    def test_partial_sell_reduces_shares(self, portfolio_path):
        _add_buy(shares=100, price=0.40)
        _add_sell(shares=30, price=0.50)
        pos = pf.get_positions()
        assert pos.iloc[0]["shares"] == pytest.approx(70)

    def test_full_sell_closes_position(self, portfolio_path):
        _add_buy(shares=100, price=0.40)
        _add_sell(shares=100, price=0.50)
        pos = pf.get_positions()
        assert pos.empty

    def test_cost_basis_reduces_proportionally_on_sell(self, portfolio_path):
        # buy 100 @ 0.40 → cost basis 40
        # sell 25 @ 0.55 → remaining 75 shares, cost basis should be 30 (75/100 of 40)
        _add_buy(shares=100, price=0.40)
        _add_sell(shares=25, price=0.55)
        pos = pf.get_positions()
        assert pos.iloc[0]["cost_basis"] == pytest.approx(30.0, rel=1e-3)
        assert pos.iloc[0]["avg_price"] == pytest.approx(0.40, rel=1e-3)  # unchanged


# ---------------------------------------------------------------------------
# Realized P&L
# ---------------------------------------------------------------------------

class TestRealizedPnL:
    def test_no_sells_no_realized(self, portfolio_path):
        _add_buy(shares=100, price=0.40)
        assert pf.get_realized_pnl().empty

    def test_simple_profit(self, portfolio_path):
        # buy 100 @ 0.40, sell 100 @ 0.50 → +$10
        _add_buy(shares=100, price=0.40)
        _add_sell(shares=100, price=0.50)
        r = pf.get_realized_pnl()
        assert r.iloc[0]["realized_pnl"] == pytest.approx(10.0)

    def test_simple_loss(self, portfolio_path):
        _add_buy(shares=100, price=0.60)
        _add_sell(shares=100, price=0.40)
        r = pf.get_realized_pnl()
        assert r.iloc[0]["realized_pnl"] == pytest.approx(-20.0)

    def test_partial_sell_realized(self, portfolio_path):
        # buy 100 @ 0.40, sell 50 @ 0.50 → +$5 realized
        _add_buy(shares=100, price=0.40)
        _add_sell(shares=50, price=0.50)
        r = pf.get_realized_pnl()
        assert r.iloc[0]["realized_pnl"] == pytest.approx(5.0)

    def test_realized_uses_average_cost(self, portfolio_path):
        # buy 100 @ 0.40 (cost 40), buy 100 @ 0.60 (cost 60); total 200 shares, cost 100, avg 0.50
        # sell 50 @ 0.70 → realized = 50 * (0.70 - 0.50) = 10
        _add_buy(shares=100, price=0.40)
        _add_buy(shares=100, price=0.60)
        _add_sell(shares=50, price=0.70)
        r = pf.get_realized_pnl()
        assert r.iloc[0]["realized_pnl"] == pytest.approx(10.0, rel=1e-3)

    def test_realized_per_market(self, portfolio_path):
        # different markets each have their own realized PnL
        _add_buy(cid="0xaaa", shares=100, price=0.40)
        _add_sell(cid="0xaaa", shares=100, price=0.55)  # +15
        _add_buy(cid="0xbbb", shares=100, price=0.30)
        _add_sell(cid="0xbbb", shares=100, price=0.20)  # -10
        r = pf.get_realized_pnl()
        assert len(r) == 2
        assert r["realized_pnl"].sum() == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Migration from v1 (positions) → v2 (transactions)
# ---------------------------------------------------------------------------

class TestMigration:
    def test_v1_file_migrated_on_read(self, portfolio_path):
        # Write a v1-format file (no "version", has "positions")
        v1 = {
            "positions": [
                {
                    "question": "Old market",
                    "condition_id": "0xold",
                    "outcome": "Yes",
                    "shares": 50,
                    "avg_price": 0.40,
                    "cost_basis": 20.0,
                    "added_at": "2025-01-01T00:00:00+00:00",
                }
            ]
        }
        portfolio_path.write_text(json.dumps(v1))

        # Triggers migration on read
        pos = pf.get_positions()
        assert len(pos) == 1
        assert pos.iloc[0]["shares"] == pytest.approx(50)

        # File should now be v2 on disk
        data = json.loads(portfolio_path.read_text())
        assert data["version"] == 2
        assert "transactions" in data
        assert len(data["transactions"]) == 1
        assert data["transactions"][0]["type"] == "buy"

    def test_v2_file_unchanged(self, portfolio_path):
        _add_buy()
        snapshot = portfolio_path.read_text()
        # Reading again should not modify
        pf.get_positions()
        assert portfolio_path.read_text() == snapshot


# ---------------------------------------------------------------------------
# Backward-compat: add_position / remove_position still work
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_add_position_creates_buy(self, portfolio_path):
        pf.add_position("Q", "0xaaa", "Yes", 100, 0.40)
        pos = pf.get_positions()
        assert len(pos) == 1
        # Underlying is a buy transaction
        tx = pf.get_transactions()
        assert tx.iloc[0]["type"] == "buy"

    def test_remove_position_deletes_transaction(self, portfolio_path):
        pf.add_position("Q", "0xaaa", "Yes", 100, 0.40)
        pf.remove_position(0)
        assert pf.get_positions().empty
        assert pf.get_transactions().empty

    def test_remove_position_out_of_range_noop(self, portfolio_path):
        pf.add_position("Q", "0xaaa", "Yes", 100, 0.40)
        pf.remove_position(99)
        assert len(pf.get_positions()) == 1


# ---------------------------------------------------------------------------
# delete_transaction
# ---------------------------------------------------------------------------

class TestDeleteTransaction:
    def test_returns_true_when_deleted(self, portfolio_path):
        _add_buy()
        assert pf.delete_transaction(0) is True

    def test_returns_false_when_index_invalid(self, portfolio_path):
        assert pf.delete_transaction(0) is False
        assert pf.delete_transaction(-1) is False
        _add_buy()
        assert pf.delete_transaction(99) is False


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

class TestAddSnapshot:
    def test_persists(self, portfolio_path):
        pf.add_snapshot(cost_basis=100, current_value=120,
                        unrealized_pnl=20, realized_pnl=5, open_positions=3)
        df = pf.get_snapshots()
        assert len(df) == 1
        assert df.iloc[0]["cost_basis"] == pytest.approx(100)
        assert df.iloc[0]["current_value"] == pytest.approx(120)
        assert df.iloc[0]["realized_pnl"] == pytest.approx(5)
        assert df.iloc[0]["open_positions"] == 3

    def test_multiple_in_order(self, portfolio_path):
        pf.add_snapshot(100, 100, 0, 0, 1)
        pf.add_snapshot(150, 160, 10, 0, 2)
        pf.add_snapshot(150, 170, 20, 5, 2)
        df = pf.get_snapshots()
        assert len(df) == 3
        assert df["current_value"].tolist() == pytest.approx([100, 160, 170])

    def test_derived_columns_present(self, portfolio_path):
        pf.add_snapshot(100, 120, 20, 5, 1)
        df = pf.get_snapshots()
        # net_value = current_value + realized_pnl
        assert df.iloc[0]["net_value"] == pytest.approx(125)
        # total_pnl = unrealized + realized
        assert df.iloc[0]["total_pnl"] == pytest.approx(25)


class TestGetSnapshots:
    def test_empty_returns_empty_frame(self, portfolio_path):
        df = pf.get_snapshots()
        assert df.empty
        assert "cost_basis" in df.columns


class TestMaybeSnapshot:
    def test_first_call_records(self, portfolio_path):
        recorded = pf.maybe_snapshot(100, 120, 20, 5, 1, min_interval_hours=1.0)
        assert recorded is True
        assert len(pf.get_snapshots()) == 1

    def test_immediate_repeat_is_skipped(self, portfolio_path):
        pf.maybe_snapshot(100, 120, 20, 5, 1, min_interval_hours=1.0)
        recorded = pf.maybe_snapshot(100, 120, 20, 5, 1, min_interval_hours=1.0)
        assert recorded is False
        assert len(pf.get_snapshots()) == 1

    def test_skips_when_portfolio_is_empty(self, portfolio_path):
        recorded = pf.maybe_snapshot(0, 0, 0, 0, 0)
        assert recorded is False
        assert pf.get_snapshots().empty

    def test_records_if_only_realized_pnl_nonzero(self, portfolio_path):
        # User closed all positions but has realized P&L — still worth tracking
        recorded = pf.maybe_snapshot(0, 0, 0, 50.0, 0)
        assert recorded is True

    def test_short_interval_allows_repeat(self, portfolio_path):
        pf.maybe_snapshot(100, 120, 20, 5, 1, min_interval_hours=0)
        pf.maybe_snapshot(100, 130, 30, 5, 1, min_interval_hours=0)
        assert len(pf.get_snapshots()) == 2


class TestSnapshotsBackwardCompat:
    def test_v2_file_without_snapshots_key_still_loads(self, portfolio_path):
        """A pre-snapshots v2 portfolio.json — load should add an empty list."""
        import json as _json
        portfolio_path.write_text(_json.dumps({
            "version": 2,
            "transactions": [{
                "id": "x", "timestamp": "2026-01-01T00:00:00+00:00",
                "type": "buy", "question": "Q", "condition_id": "0x1",
                "outcome": "Yes", "shares": 10, "price": 0.5,
            }]
            # no "snapshots" key
        }))
        # get_snapshots should return empty, not crash
        assert pf.get_snapshots().empty
        # transactions still work
        assert len(pf.get_transactions()) == 1

    def test_v1_migration_includes_empty_snapshots(self, portfolio_path):
        """v1 → v2 migration should write a snapshots:[] key."""
        import json as _json
        portfolio_path.write_text(_json.dumps({
            "positions": [{
                "question": "Q", "condition_id": "0x1", "outcome": "Yes",
                "shares": 10, "avg_price": 0.5, "cost_basis": 5.0,
                "added_at": "2025-01-01T00:00:00+00:00",
            }]
        }))
        pf.get_positions()  # triggers migration
        data = _json.loads(portfolio_path.read_text())
        assert "snapshots" in data
        assert data["snapshots"] == []
