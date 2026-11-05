"""Tests for the lineup-OPS pathway: model adjustment + state plumbing
+ runner integration with the min-AB filter."""

from __future__ import annotations

import pytest

from baseball import data, model, runner, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    return db


# ─── Model unit tests ─────────────────────────────────────────────────────


def test_lineup_logodds_adj_zero_when_missing():
    assert model.lineup_logodds_adj(None) == 0.0


def test_lineup_logodds_adj_league_avg_zero():
    assert model.lineup_logodds_adj(model.LEAGUE_AVG_OPS) == pytest.approx(0.0)


def test_lineup_logodds_adj_above_avg_positive():
    """A lineup OPS of 0.800 (well above 0.720 league avg) should
    produce a positive log-odds boost."""
    adj = model.lineup_logodds_adj(0.800)
    assert adj > 0
    # Magnitude: 0.080 × 3.0 = 0.24 log-odds
    assert adj == pytest.approx(0.080 * model.LINEUP_LOGODDS_PER_OPS)


def test_lineup_logodds_adj_below_avg_negative():
    adj = model.lineup_logodds_adj(0.640)
    assert adj < 0


def test_predict_matchup_lineup_symmetric_swing():
    """Equal everything except lineups: positive home lineup +
    matching negative away should produce a measurable home boost."""
    pred = model.predict_matchup(
        home_strength=0.5, away_strength=0.5,
        home_field=False,
        home_lineup_avg_ops=0.760,  # +0.040 over avg
        away_lineup_avg_ops=0.680,  # -0.040 below avg
    )
    assert pred.home_win_prob > 0.5
    # Symmetric reverse:
    pred2 = model.predict_matchup(
        home_strength=0.5, away_strength=0.5,
        home_field=False,
        home_lineup_avg_ops=0.680,
        away_lineup_avg_ops=0.760,
    )
    assert pred2.home_win_prob == pytest.approx(1.0 - pred.home_win_prob, abs=1e-9)


def test_prediction_inputs_carry_lineup_adj():
    """to_inputs() now includes home/away lineup adjustments."""
    pred = model.predict_matchup(
        home_strength=0.5, away_strength=0.5,
        home_field=False,
        home_lineup_avg_ops=0.800,
    )
    inputs = pred.to_inputs()
    assert "home_lineup_adj" in inputs
    assert "away_lineup_adj" in inputs
    assert inputs["home_lineup_adj"] > 0


# ─── State plumbing ───────────────────────────────────────────────────────


def test_upsert_get_batter(temp_state):
    b = data.Batter(id=123, name="Test Player", season=2026,
                     ops=0.812, avg=0.275, home_runs=14, at_bats=200)
    state.upsert_batter(b)
    row = state.get_batter(123, 2026)
    assert row is not None
    assert row["ops"] == 0.812
    assert row["at_bats"] == 200


def test_upsert_get_lineup_roundtrip(temp_state):
    ids = [111, 222, 333, 444, 555]
    state.upsert_lineup(game_pk=9999, side="home", player_ids=ids)
    assert state.get_lineup(9999, "home") == ids
    # Other side returns None
    assert state.get_lineup(9999, "away") is None


def test_upsert_lineup_rejects_bad_side(temp_state):
    with pytest.raises(ValueError):
        state.upsert_lineup(game_pk=1, side="visiting", player_ids=[1, 2])


def test_lineup_idempotent_upsert(temp_state):
    state.upsert_lineup(1, "home", [1, 2, 3])
    state.upsert_lineup(1, "home", [4, 5, 6])  # overwrite
    assert state.get_lineup(1, "home") == [4, 5, 6]


# ─── Runner _avg_lineup_ops helper ────────────────────────────────────────


def test_avg_lineup_ops_returns_none_no_lineup(temp_state):
    assert runner._avg_lineup_ops(9999, "home", 2026) is None


def test_avg_lineup_ops_filters_low_AB(temp_state):
    """Batters with < min_AB at-bats are filtered. If fewer than 5
    remain we return None — better than averaging noise."""
    # Lineup of 9 players. 4 have enough AB; 5 don't.
    pids = list(range(1, 10))
    state.upsert_lineup(100, "home", pids)
    # First 4 have AB >= 50; rest have 10 (below threshold)
    for i in range(4):
        state.upsert_batter(data.Batter(
            id=i+1, name=f"Star{i}", season=2026,
            ops=0.800, avg=0.280, home_runs=10, at_bats=150,
        ))
    for i in range(4, 9):
        state.upsert_batter(data.Batter(
            id=i+1, name=f"Bench{i}", season=2026,
            ops=0.500, avg=0.200, home_runs=0, at_bats=10,
        ))
    # Only 4 batters meet the min-AB threshold → returns None (need ≥5)
    assert runner._avg_lineup_ops(100, "home", 2026) is None


def test_avg_lineup_ops_computes_when_enough_batters(temp_state):
    """5+ batters with ≥50 AB → real average."""
    pids = list(range(1, 10))
    state.upsert_lineup(101, "home", pids)
    for i in range(6):
        state.upsert_batter(data.Batter(
            id=i+1, name=f"P{i}", season=2026,
            ops=0.750 + i * 0.010,  # 0.750, 0.760, 0.770, ...
            avg=None, home_runs=None, at_bats=200,
        ))
    avg = runner._avg_lineup_ops(101, "home", 2026)
    assert avg == pytest.approx((0.750 + 0.760 + 0.770 + 0.780 + 0.790 + 0.800) / 6)


def test_avg_lineup_ops_ignores_missing_batter_data(temp_state):
    """If a player_id has no batters row, they're skipped silently —
    no crash. Same outcome whether 'missing' or 'low AB'."""
    state.upsert_lineup(102, "home", [777])  # batter 777 not in batters table
    assert runner._avg_lineup_ops(102, "home", 2026) is None
