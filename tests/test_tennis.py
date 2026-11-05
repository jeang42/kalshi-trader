"""Tests for the tennis sidecar: Elo engine (pure), model, ticker bridge,
state, data parsing. No network — data tests monkeypatch HTTP."""

from __future__ import annotations

import pytest

from tennis import data, elo, model

# ─── Elo engine (pure) ─────────────────────────────────────────────────────

def test_expected_score_equal_is_half():
    assert elo.expected_score(1500, 1500) == pytest.approx(0.5)


def test_expected_score_favors_higher():
    assert elo.expected_score(1700, 1500) == pytest.approx(0.76, abs=0.01)
    assert elo.expected_score(1500, 1700) == pytest.approx(0.24, abs=0.01)


def test_update_is_zero_sum():
    m = elo.EloModel()
    m.update(1, 2)
    # winner gained exactly what loser lost
    assert (m.rating(1) - 1500) == pytest.approx(1500 - m.rating(2))
    assert m.rating(1) > 1500 > m.rating(2)


def test_repeated_wins_raise_prob():
    m = elo.EloModel()
    for _ in range(5):
        m.update(1, 2)
    assert m.prob(1, 2) > 0.6
    assert m.n_played[1] == 5


def test_unseen_player_seeds_default():
    m = elo.EloModel()
    assert m.rating(999) == elo.DEFAULT_RATING
    assert m.prob(999, 888) == pytest.approx(0.5)


def test_seed_for_points_monotonic_and_clamped():
    assert round(elo.seed_for_points(1000)) == 1500
    assert elo.seed_for_points(None) == elo.UNRANKED_SEED
    assert elo.seed_for_points(14750) > elo.seed_for_points(5000) > elo.seed_for_points(1000)
    assert 1300.0 <= elo.seed_for_points(1) <= 2100.0   # clamped


def test_rank_seeding_separates_elite_from_fringe():
    """The Jodar≈Zverev fix: a true elite (high seed) stays above a
    hot-but-fringe player even after equal match counts, because seeding
    injects the prior. Without seeds they'd converge toward each other."""
    seeds = {1: 1850.0, 2: 1550.0}  # elite vs fringe
    m = elo.EloModel(seeds=seeds)
    assert m.rating(1) == 1850.0 and m.rating(2) == 1550.0
    # fringe wins a few — gains, but shouldn't leap past the elite's seed gap
    for _ in range(5):
        m.update(2, 99)   # player 2 beats unrelated unseeded players
    assert m.rating(1) > m.rating(2)


def test_k_decay_shrinks_with_matches():
    m = elo.EloModel(k_decay=True)
    early = m._k_for(7)               # 7 unseen → 0 played
    for _ in range(40):
        m.update(7, 8)
    late = m._k_for(7)               # 40 played
    assert late < early


def test_surface_aware_blend():
    m = elo.EloModel(surface_aware=True, surface_weight=0.5)
    # player 1 wins repeatedly on clay only
    for _ in range(8):
        m.update(1, 2, surface="clay")
    # on clay, prob(1,2) should exceed the global-only prob
    clay = m.prob(1, 2, surface="clay")
    glob = m.prob(1, 2, surface=None)
    assert clay >= glob > 0.5


# ─── model ─────────────────────────────────────────────────────────────────

def test_predict_match_from_rows():
    p1 = {"name": "A", "elo": 1700, "surface_elo": None}
    p2 = {"name": "B", "elo": 1500, "surface_elo": None}
    pred = model.predict_match(p1, p2)
    assert pred.p1_win_prob == pytest.approx(0.76, abs=0.01)
    assert pred.to_inputs()["p1_elo"] == 1700


def test_predict_match_missing_player_is_none():
    assert model.predict_match(None, {"name": "B", "elo": 1500}) is None


def test_predict_match_surface_blend():
    import json
    p1 = {"name": "A", "elo": 1500, "surface_elo": json.dumps({"clay": 1700})}
    p2 = {"name": "B", "elo": 1500, "surface_elo": json.dumps({"clay": 1500})}
    on_clay = model.predict_match(p1, p2, surface="clay")
    neutral = model.predict_match(p1, p2, surface="unknown")
    assert on_clay.p1_win_prob > neutral.p1_win_prob


# ─── ticker bridge (name parsing) ──────────────────────────────────────────

def test_name_from_question():
    import tennis
    q = "Will Matteo Berrettini win the Berrettini vs Arnaldi: Quarterfinal?"
    assert tennis._name_from_question(q) == "Matteo Berrettini"


def test_name_from_question_garbage():
    import tennis
    assert tennis._name_from_question("not a question") is None
    assert tennis._name_from_question("") is None


def test_tour_from_ticker():
    import tennis
    assert tennis._tour_from_ticker("KXATPMATCH-26JUN03BERARN-BER") == "atp"
    assert tennis._tour_from_ticker("KXWTAMATCH-26JUN03SABSHN-SAB") == "wta"
    assert tennis._tour_from_ticker("KXMLBGAME-X") is None


# ─── value strategy guardrail (the K-decay protection) ─────────────────────

class _Snap:
    """Minimal MarketSnapshot stand-in: only latest_price/condition_id/
    question are read by the strategy."""
    def __init__(self, price, ticker="KXATPMATCH-X-Y", question="q"):
        self._p = price
        self.condition_id = ticker
        self.question = question

    @property
    def latest_price(self):
        return self._p


# NOTE: patch edges internals at their REAL home (`edges.baseball.X`,
# `edges.tennis.X`), not `edges.X`. Since the R-24 package split the
# latter names this package's re-export; rebinding it would not affect
# the caller inside the submodule, and the test would exercise the real
# code path instead of the stub.
def _patch_pred(monkeypatch, pred):
    import edges
    monkeypatch.setattr(edges.tennis, "_tennis_pred", lambda snap: pred)


def test_value_backs_underpriced_side(monkeypatch):
    import edges
    # model 0.60 vs market 0.45 → +15pp edge on a comparably-ranked player
    _patch_pred(monkeypatch, {
        "yes_prob": 0.60, "yes_name": "A", "opponent": "B",
        "yes_matches": 40, "opp_matches": 40, "yes_rank": 8, "opp_rank": 12,
    })
    sig = edges.strategy_tennis_value(_Snap(0.45), min_gap=0.08)
    assert sig is not None and sig.direction == "YES"


def test_value_skips_small_gap(monkeypatch):
    import edges
    _patch_pred(monkeypatch, {
        "yes_prob": 0.52, "yes_name": "A", "opponent": "B",
        "yes_matches": 40, "opp_matches": 40, "yes_rank": 8, "opp_rank": 12,
    })
    assert edges.strategy_tennis_value(_Snap(0.50), min_gap=0.08) is None


def test_value_guard_rejects_kdecay_low_rank_vs_top(monkeypatch):
    import edges
    # The Jodar/Zverev case: model wants to back YES (#29) over market;
    # fading the #3 favorite on Elo alone → must be refused.
    _patch_pred(monkeypatch, {
        "yes_prob": 0.47, "yes_name": "Jodar", "opponent": "Zverev",
        "yes_matches": 40, "opp_matches": 39, "yes_rank": 29, "opp_rank": 3,
    })
    # market underprices YES (0.26) → +21pp edge, but guard 3 rejects it
    assert edges.strategy_tennis_value(_Snap(0.26), min_gap=0.08) is None


def test_value_guard_rejects_thin_sample(monkeypatch):
    import edges
    _patch_pred(monkeypatch, {
        "yes_prob": 0.65, "yes_name": "Rookie", "opponent": "B",
        "yes_matches": 5, "opp_matches": 40, "yes_rank": 40, "opp_rank": 45,
    })
    assert edges.strategy_tennis_value(_Snap(0.45), min_gap=0.08) is None


# ─── state (real SQLite at temp path) ──────────────────────────────────────

@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TENNIS_STATE_PATH", str(tmp_path / "tennis.sqlite"))
    from tennis import state
    state.migrate()
    return state


def test_migrate_idempotent(temp_state):
    temp_state.migrate()
    assert temp_state.ratings_table() == []


def test_player_roundtrip_and_name_lookup(temp_state):
    s = temp_state
    s.upsert_player(pid=1, tour="atp", name="Jannik Sinner", elo=1850,
                    rank=1, points=14750, matches_played=40)
    rows = s.find_player_by_name("jannik sinner", "atp")  # case-insensitive
    assert len(rows) == 1 and rows[0]["elo"] == 1850
    table = s.ratings_table("atp")
    assert table[0]["name"] == "Jannik Sinner"


def test_prediction_and_brier(temp_state):
    s = temp_state
    s.record_prediction("KXATPMATCH-X-SIN", 0.80, "Jannik Sinner",
                        "Joe Q", "Jannik Sinner", "0.1.0", {})
    s.settle_prediction("KXATPMATCH-X-SIN", True)  # yes won; predicted 0.80
    summ = s.get_brier_summary()
    assert summ["n"] == 1
    assert summ["brier"] == pytest.approx((0.80 - 1.0) ** 2)


# ─── data parsing (mocked) ─────────────────────────────────────────────────

def test_get_rankings_parses(monkeypatch):
    fake = {"rankings": [{"ranks": [
        {"current": 1, "points": 14750,
         "athlete": {"id": "3623", "displayName": "Jannik Sinner"}},
        {"current": 2, "points": 11960,
         "athlete": {"id": "3782", "displayName": "Carlos Alcaraz"}},
    ]}]}
    monkeypatch.setattr(data, "_get", lambda *a, **k: fake)
    players = data.get_rankings("atp")
    assert len(players) == 2
    assert players[0].name == "Jannik Sinner" and players[0].id == 3623


def test_surface_from_name():
    assert data._surface_from_name("Roland Garros") == "clay"
    assert data._surface_from_name("Wimbledon") == "grass"
    assert data._surface_from_name("US Open") == "hard"
    assert data._surface_from_name("Some Exhibition") == "unknown"


def test_get_matches_parses_inline(monkeypatch):
    fake_event = {
        "name": "Roland Garros",
        "competitions": [{
            "id": "m1", "date": "2026-05-18T08:05Z",
            "round": {"displayName": "Round of 128"},
            "competitors": [
                {"winner": True, "athlete": {"id": "3897"}},
                {"winner": False, "athlete": {"id": "2567"}},
            ],
        }],
    }
    monkeypatch.setattr(data, "_get", lambda *a, **k: fake_event)
    matches = data.get_matches_for_tournament("ref", "atp")
    assert len(matches) == 1
    m = matches[0]
    assert m.p1_id == 3897 and m.winner_id == 3897 and m.surface == "clay"


# ─── resolve_pending_tickers ───────────────────────────────────────────────

def _seed_resolved_match(s, *, tour="atp", p1=(1, "Aryna Sabalenka"),
                         p2=(2, "Naomi Osaka"), winner_idx=0,
                         date="2026-06-02T14:00:00+00:00"):
    s.upsert_player(pid=p1[0], tour=tour, name=p1[1], elo=1700)
    s.upsert_player(pid=p2[0], tour=tour, name=p2[1], elo=1600)

    class _M:  # minimal Match-shaped object for upsert_match
        match_id = "m1"
        def __init__(self):
            self.tour = tour
            self.p1_id = p1[0]
            self.p2_id = p2[0]
            self.winner_id = (p1[0] if winner_idx == 0 else p2[0])
            self.surface = "clay"
            self.tournament = "Test Open"
            self.round_name = "QF"
            from datetime import datetime
            self.date = datetime.fromisoformat(date)
    s.upsert_match(_M())


def test_resolve_winner_and_loser_tickers(temp_state):
    import tennis
    _seed_resolved_match(temp_state, winner_idx=0)  # Sabalenka beats Osaka
    # ET date for 14:00Z on 2026-06-02 is still June 2 in New York
    win = "KXATPMATCH-26JUN02SABOSA-SAB"   # YES=winner → True
    lose = "KXATPMATCH-26JUN02SABOSA-OSA"  # YES=loser  → False
    res = tennis.resolve_pending_tickers([win, lose])
    assert res == {win: True, lose: False}


def test_resolve_skips_unknown_date(temp_state):
    import tennis
    _seed_resolved_match(temp_state)
    # a ticker for a different day → no finished match → omitted
    assert tennis.resolve_pending_tickers(["KXATPMATCH-26JUN09SABOSA-SAB"]) == {}


def test_resolve_skips_non_tennis_and_garbage(temp_state):
    import tennis
    _seed_resolved_match(temp_state)
    assert tennis.resolve_pending_tickers(
        ["KXMLBGAME-26JUN02NYMMIA-NYM", "garbage", ""]) == {}


def test_resolve_skips_ambiguous_code(temp_state):
    import tennis
    # both players' surnames start with the same 3 letters → code matches
    # both → must skip, not guess
    _seed_resolved_match(temp_state, p1=(1, "Bob Smithson"),
                         p2=(2, "Al Smithers"), winner_idx=0)
    # blob SMISMI, code SMI matches both
    assert tennis.resolve_pending_tickers(["KXATPMATCH-26JUN02SMISMI-SMI"]) == {}


# ─── walk-forward Brier (model accuracy) ───────────────────────────────────

def test_walk_forward_brier_empty():
    assert elo.walk_forward_brier([])["brier"] is None


def test_walk_forward_brier_coin_flip_at_equal_seeds():
    # equal seeds → first prediction 0.5; p1 wins → (0.5-1)^2 = 0.25
    r = elo.walk_forward_brier([(1, 2, 1, None)])
    assert r["n"] == 1
    assert round(r["brier"], 3) == 0.25


def test_walk_forward_brier_rewards_skill():
    # a much higher-seeded player who keeps winning → low (good) Brier,
    # well below the 0.25 coin-flip baseline.
    seeds = {1: 2100.0, 2: 1300.0}
    matches = [(1, 2, 1, None)] * 5   # the favorite wins every time
    r = elo.walk_forward_brier(matches, seeds=seeds)
    assert r["n"] == 5
    assert r["brier"] < 0.10


def test_walk_forward_brier_skips_bad_winner():
    # winner not one of the two players → skipped, not crashed
    assert elo.walk_forward_brier([(1, 2, 99, None)])["n"] == 0
