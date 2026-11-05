"""Tests for backtest_history persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from pm.research import backtest as bt
from pm.research import backtest_history as bh

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_history_dir(tmp_path, monkeypatch):
    """Every test gets its own empty history directory."""
    d = tmp_path / "backtest_runs"
    monkeypatch.setenv("BACKTEST_HISTORY_DIR", str(d))
    yield d


def _price_history(prices, start=None):
    start = start or datetime(2026, 5, 1, tzinfo=UTC)
    return pd.DataFrame({
        "timestamp": [start + timedelta(hours=i) for i in range(len(prices))],
        "price": prices,
    })


def _real_result() -> bt.BacktestResult:
    """Build a real BacktestResult by running a tiny one-trade backtest."""
    as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    snap = bt.MarketSnapshot(
        "x", "q", "Sports", as_of,
        _price_history([0.4], start=as_of - timedelta(hours=1)),
    )
    res = bt.Resolution("x", True, as_of + timedelta(hours=12))
    dataset = [(snap, res)]

    def strat(snap):
        return bt.Signal(direction="YES", probability=0.5)

    return bt.run_backtest(
        strat, dataset,
        bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
        strategy_name="test_strategy",
    )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKTEST_HISTORY_DIR", str(tmp_path / "custom"))
        assert bh._dir() == tmp_path / "custom"

    def test_list_runs_on_missing_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKTEST_HISTORY_DIR", str(tmp_path / "does_not_exist"))
        df = bh.list_runs()
        assert df.empty

    def test_stats_on_missing_dir_safe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKTEST_HISTORY_DIR", str(tmp_path / "nope"))
        s = bh.stats()
        assert s["runs"] == 0
        assert s["bytes"] == 0
        assert s["oldest_age_s"] is None
        assert s["newest_age_s"] is None


# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------

class TestRoundtrip:
    def test_save_then_load_preserves_scalars(self):
        result = _real_result()
        run_id = bh.save_run(result, params={"bankroll": 1000.0}, venue="kalshi")
        assert run_id

        loaded = bh.load_run(run_id)
        assert loaded is not None
        r2, meta = loaded

        assert r2.strategy_name == result.strategy_name
        assert r2.n_trades == result.n_trades
        assert r2.n_wins == result.n_wins
        assert r2.total_pnl == pytest.approx(result.total_pnl)
        assert r2.max_drawdown == pytest.approx(result.max_drawdown)
        assert meta["venue"] == "kalshi"
        assert meta["params"] == {"bankroll": 1000.0}

    def test_save_then_load_preserves_trades_dataframe(self):
        result = _real_result()
        run_id = bh.save_run(result)
        r2, _ = bh.load_run(run_id)

        assert len(r2.trades) == len(result.trades)
        # PnL roundtrips as a float
        assert r2.trades.iloc[0]["pnl"] == pytest.approx(
            result.trades.iloc[0]["pnl"]
        )

    def test_save_then_load_preserves_hit_rate_ci_tuple(self):
        result = _real_result()
        run_id = bh.save_run(result)
        r2, _ = bh.load_run(run_id)
        # JSON serializes tuples as lists; _dict_to_result coerces back
        if result.hit_rate_ci is not None:
            assert isinstance(r2.hit_rate_ci, tuple)
            assert r2.hit_rate_ci == pytest.approx(result.hit_rate_ci)

    def test_save_failure_returns_empty_string(self, monkeypatch):
        """If the file write blows up, save_run returns "" and doesn't crash."""
        result = _real_result()

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", boom)
        assert bh.save_run(result) == ""


# ---------------------------------------------------------------------------
# list_runs metadata
# ---------------------------------------------------------------------------

class TestListRuns:
    def test_list_runs_returns_one_row_per_save(self):
        for _ in range(3):
            bh.save_run(_real_result())
        df = bh.list_runs()
        assert len(df) == 3

    def test_list_runs_sorted_newest_first(self):
        rid1 = bh.save_run(_real_result())
        # Ensure mtime ordering differs
        import time as _t
        _t.sleep(0.005)
        rid2 = bh.save_run(_real_result())
        df = bh.list_runs()
        # rid2 was saved later, should appear first
        assert df.iloc[0]["run_id"] == rid2
        assert df.iloc[1]["run_id"] == rid1

    def test_list_runs_exposes_summary_metrics(self):
        result = _real_result()
        bh.save_run(result)
        df = bh.list_runs()
        row = df.iloc[0]
        assert row["strategy_name"] == "test_strategy"
        assert row["n_trades"] == result.n_trades
        assert row["total_pnl"] == pytest.approx(result.total_pnl)

    def test_list_runs_skips_malformed_files(self, isolated_history_dir):
        # Save one valid run
        bh.save_run(_real_result())
        # Drop a malformed file into the same dir
        isolated_history_dir.mkdir(exist_ok=True)
        (isolated_history_dir / "00_garbage.json").write_text("{not json")
        df = bh.list_runs()
        # The garbage one is skipped; the valid one survives
        assert len(df) == 1


# ---------------------------------------------------------------------------
# Delete / clear
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_run_removes_file(self):
        rid = bh.save_run(_real_result())
        assert bh.delete_run(rid) is True
        assert bh.load_run(rid) is None

    def test_delete_unknown_returns_false(self):
        assert bh.delete_run("does_not_exist") is False

    def test_clear_history_removes_all(self):
        for _ in range(3):
            bh.save_run(_real_result())
        assert bh.clear_history() == 3
        assert bh.list_runs().empty


# ---------------------------------------------------------------------------
# stats()
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_counts_and_bytes(self):
        bh.save_run(_real_result())
        bh.save_run(_real_result())
        s = bh.stats()
        assert s["runs"] == 2
        assert s["bytes"] > 0
        assert s["oldest_age_s"] is not None
        assert s["newest_age_s"] is not None
        assert s["oldest_age_s"] >= s["newest_age_s"]


# ---------------------------------------------------------------------------
# Schema-fallback / load_run resilience
# ---------------------------------------------------------------------------

class TestLoadResilience:
    def test_load_unknown_returns_none(self):
        assert bh.load_run("nonexistent_id") is None

    def test_load_malformed_returns_none(self, isolated_history_dir):
        isolated_history_dir.mkdir(exist_ok=True)
        (isolated_history_dir / "bad.json").write_text("{not json")
        assert bh.load_run("bad") is None

    def test_load_round_trips_through_disk(self, isolated_history_dir):
        # Inspect the saved payload looks like real JSON, not pickle
        rid = bh.save_run(_real_result())
        contents = (isolated_history_dir / f"{rid}.json").read_text()
        parsed = json.loads(contents)  # no exception = valid JSON
        assert parsed["run_id"] == rid
        assert "result" in parsed


# ---------------------------------------------------------------------------
# Market-baseline gate fields: persistence + backward compatibility
# ---------------------------------------------------------------------------

class TestGateFieldPersistence:
    def test_real_result_carries_the_gate(self):
        # Sanity: _real_result (prob 0.5 vs market 0.4, resolves YES) has
        # strategy Brier 0.25 vs baseline (0.4-1)² = 0.36 → passes.
        r = _real_result()
        assert r.market_baseline_brier == pytest.approx(0.36)
        assert r.market_baseline_n == 1
        assert r.beats_market_baseline is True

    def test_round_trip_preserves_gate_fields(self):
        result = _real_result()
        rid = bh.save_run(result)
        loaded, _meta = bh.load_run(rid)
        assert loaded.market_baseline_brier == pytest.approx(
            result.market_baseline_brier
        )
        assert loaded.market_baseline_n == result.market_baseline_n
        assert loaded.beats_market_baseline is True

    def test_legacy_payload_without_gate_fields_loads_with_defaults(
        self, isolated_history_dir,
    ):
        # Simulate a run saved before the schema carried the gate: strip
        # the three fields from the stored JSON and reload.
        rid = bh.save_run(_real_result())
        path = Path(isolated_history_dir) / f"{rid}.json"
        payload = json.loads(path.read_text())
        for key in ("market_baseline_brier", "market_baseline_n",
                    "beats_market_baseline"):
            payload["result"].pop(key, None)
        path.write_text(json.dumps(payload, default=str))

        loaded, _meta = bh.load_run(rid)
        assert loaded is not None
        assert loaded.market_baseline_brier is None
        assert loaded.market_baseline_n == 0
        assert loaded.beats_market_baseline is None

    def test_list_runs_includes_gate_columns(self):
        bh.save_run(_real_result())
        df = bh.list_runs()
        assert "market_baseline_brier" in df.columns
        assert "beats_market_baseline" in df.columns
        assert bool(df.iloc[0]["beats_market_baseline"]) is True
        assert df.iloc[0]["market_baseline_brier"] == pytest.approx(0.36)
