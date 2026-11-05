"""Pure Elo rating engine for tennis.

The standard, well-validated approach to tennis win-probability (used by
FiveThirtyEight, Tennis Abstract, etc.). Elo covers the *whole field* —
every player gets a rating that updates from results — unlike a
rank-points model that only knows the top ~150. New/unseen players seed
at ``DEFAULT_RATING`` and converge as they play.

P(A beats B) = 1 / (1 + 10**((Eb - Ea) / 400))

Ratings update after each match:
    E_winner += K * (1 - p_win)
    E_loser  -= K * (1 - p_win)         [zero-sum]

where ``p_win`` is the winner's pre-match expected probability. A fixed K
is the simple v1; tennis-specific refinements (K decaying with matches
played, surface-specific ladders) are supported via parameters but default
to the simple global ladder.

Everything here is pure: feed it chronological matches, get a ratings
dict + per-match prediction log back. No IO, no network — unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_RATING = 1500.0
DEFAULT_K = 32.0
UNRANKED_SEED = 1400.0   # outside the top-N ranking → below the ranked field


def expected_score(rating_a: float, rating_b: float) -> float:
    """P(A beats B) from two Elo ratings."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def seed_for_points(points: float | None) -> float:
    """Initial Elo for a player from their ATP/WTA ranking points.

    **This is the load-bearing fix for the model.** Without it everyone
    starts at 1500, so after a season a #29 on a hot streak rates ≈ a true
    #3 (equal match counts → equal updates) and the model manufactures fake
    edges. Seeding from ranking points injects the prior "this player is
    actually elite / actually fringe" so the ladder starts ordered.

    Walk-forward Brier on the backfill: no-seed 0.2405 → rank-seed 0.2198.
    (K-decay alone gave 0.2403 — no help — which is why it's off by default.)

    Maps log(points/1000) to Elo points, clamped to a sane band. ~1000
    ranking points → 1500; the #1 (~14750) → ~1965; fringe → ~1300.
    ``None`` (unranked) → ``UNRANKED_SEED``.

    >>> round(seed_for_points(1000))
    1500
    >>> seed_for_points(None)
    1400.0
    >>> seed_for_points(14750) > seed_for_points(5000) > seed_for_points(1000)
    True
    """
    if not points or points <= 0:
        return UNRANKED_SEED
    import math
    return max(1300.0, min(2100.0, 1500.0 + 173.0 * math.log(points / 1000.0)))


@dataclass
class EloModel:
    """Mutable Elo ladder. Process matches chronologically with
    :meth:`update`; query with :meth:`prob` / :meth:`rating`.

    ``surface_aware``: when True, also maintains per-surface ladders and
    blends the global + surface rating (weight ``surface_weight``) for
    predictions — clay specialists rate higher on clay. Off by default
    (v1 = global ladder only; turn on once there's enough per-surface data).

    ``seeds``: ``{player_id: initial_rating}`` (from ``seed_for_points``).
    A player's first appearance uses their seed instead of the flat
    ``default_rating`` — the load-bearing accuracy fix (see
    ``seed_for_points``). Players absent from ``seeds`` fall back to
    ``default_rating``.

    ``k_decay``: when True, K shrinks as a player accumulates matches
    (``k_decay_c / (n+5)**k_decay_p``) so established ratings move less.
    **Off by default** — walk-forward Brier showed it didn't help once
    seeding was in place (0.2198 seed-only vs 0.2221 seed+decay). Kept as
    an option for re-evaluation on more data.
    """

    k: float = DEFAULT_K
    default_rating: float = DEFAULT_RATING
    surface_aware: bool = False
    surface_weight: float = 0.5
    seeds: dict[int, float] = field(default_factory=dict)
    k_decay: bool = False
    k_decay_c: float = 130.0
    k_decay_p: float = 0.4
    ratings: dict[int, float] = field(default_factory=dict)
    surface_ratings: dict[str, dict[int, float]] = field(default_factory=dict)
    n_played: dict[int, int] = field(default_factory=dict)
    n_matches: int = 0

    def _seed(self, player_id: int) -> float:
        return self.seeds.get(player_id, self.default_rating)

    def rating(self, player_id: int) -> float:
        return self.ratings.get(player_id, self._seed(player_id))

    def _k_for(self, player_id: int) -> float:
        if not self.k_decay:
            return self.k
        return self.k_decay_c / ((self.n_played.get(player_id, 0) + 5) ** self.k_decay_p)

    def _surface_rating(self, surface: str, player_id: int) -> float:
        return self.surface_ratings.get(surface, {}).get(player_id, self._seed(player_id))

    def _blended(self, player_id: int, surface: str | None) -> float:
        base = self.rating(player_id)
        if not self.surface_aware or not surface or surface == "unknown":
            return base
        surf = self._surface_rating(surface, player_id)
        return (1 - self.surface_weight) * base + self.surface_weight * surf

    def prob(self, p1_id: int, p2_id: int, *, surface: str | None = None) -> float:
        """P(p1 beats p2), optionally surface-adjusted."""
        return expected_score(self._blended(p1_id, surface),
                              self._blended(p2_id, surface))

    def update(self, winner_id: int, loser_id: int, *,
               surface: str | None = None) -> None:
        """Apply one finished match to the ladder(s)."""
        ew = self.rating(winner_id)
        el = self.rating(loser_id)
        p_win = expected_score(ew, el)
        # symmetric K so the ladder stays zero-sum even under decay (both
        # players move by the same amount — the average of their K's).
        k = ((self._k_for(winner_id) + self._k_for(loser_id)) / 2
             if self.k_decay else self.k)
        delta = k * (1.0 - p_win)
        self.ratings[winner_id] = ew + delta
        self.ratings[loser_id] = el - delta
        self.n_played[winner_id] = self.n_played.get(winner_id, 0) + 1
        self.n_played[loser_id] = self.n_played.get(loser_id, 0) + 1
        self.n_matches += 1

        if self.surface_aware and surface and surface != "unknown":
            sd = self.surface_ratings.setdefault(surface, {})
            sw = sd.get(winner_id, self._seed(winner_id))
            sl = sd.get(loser_id, self._seed(loser_id))
            sp = expected_score(sw, sl)
            sdelta = k * (1.0 - sp)
            sd[winner_id] = sw + sdelta
            sd[loser_id] = sl - sdelta

    def process(self, matches: list[tuple[int, int, str | None]]) -> None:
        """Bulk-apply ``(winner_id, loser_id, surface)`` tuples in order."""
        for winner_id, loser_id, surface in matches:
            self.update(winner_id, loser_id, surface=surface)


def walk_forward_brier(
    matches: list[tuple[int, int, int, str | None]],
    *,
    seeds: dict[int, float] | None = None,
    surface_aware: bool = True,
    surface_weight: float = 0.5,
) -> dict[str, float | int | None]:
    """Out-of-sample Brier for the Elo model over one ladder's match
    history. ``matches`` is chronological ``(p1_id, p2_id, winner_id,
    surface)``. For each match the model predicts P(p1 wins) from its
    PRE-match ratings, scores it against the actual winner, *then* applies
    the result — so every prediction is genuinely out-of-sample (the model
    never saw that match's outcome). This is the honest measure of whether
    the Elo has skill; lower is better (0.25 = always guess 0.5).

    >>> # one match, equal seeds → first prediction is 0.5; p1 wins
    >>> r = walk_forward_brier([(1, 2, 1, None)])
    >>> round(r["brier"], 3), r["n"]
    (0.25, 1)
    >>> walk_forward_brier([])["brier"] is None
    True
    """
    model = EloModel(surface_aware=surface_aware, surface_weight=surface_weight,
                     seeds=seeds or {})
    n = 0
    total = 0.0
    for p1, p2, winner, surface in matches:
        if winner not in (p1, p2):
            continue
        pred = model.prob(p1, p2, surface=surface)
        actual = 1.0 if winner == p1 else 0.0
        total += (pred - actual) ** 2
        n += 1
        loser = p2 if winner == p1 else p1
        model.update(winner, loser, surface=surface)
    return {"n": n, "brier": (total / n) if n else None}
