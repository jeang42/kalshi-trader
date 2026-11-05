"""Pure-function baseball matchup model.

Stages
------
1. **Team strength** from Pythagorean expectancy (Bill James, exponent 2.0).
   Regressed toward .500 with a Bayesian prior (PRIOR_WEIGHT=40 virtual
   games) so early-season records aren't over-weighted.
2. **Home-field advantage** — halved in v0.6 from 54% to 52% after
   empirical analysis showed 50% actual home-win rate on 42 games.
3. **Lineup OPS delta** — the only factor with positive signal (r=0.126).
   Weight reduced to 0.75x of original in v0.6.
4. **SP ERA / Bullpen ERA** — DISABLED in v0.6. Zero correlation with
   single-game outcomes; bullpen ERA double-counted team quality
   (r=0.636 with Pythagorean strength). Code retained at zero weight
   for re-evaluation at n=150+.

Everything is pure: takes inputs, returns a probability + an "inputs"
dict the caller can persist alongside the prediction for auditability.

Model evolution is driven by empirical Brier analysis against settled
predictions. See docs/failed-experiments.md for the v0.5 pitching-weight
post-mortem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ─── Model constants ─────────────────────────────────────────────────────


MODEL_VERSION = "0.6.0"  # v0.6: evidence-based weight recalibration

# Bayesian prior on team strength: each team gets PRIOR_WEIGHT "virtual"
# games at .500 added to their actual record. Heavier prior = more
# regression to the mean (cold teams aren't punished as much early).
PRIOR_WEIGHT = 40  # ~quarter of a season

# Home-field advantage as a log-odds boost.
# v0.5 used ln(0.54/0.46) ≈ 0.161 (historical MLB average). Analysis of
# 42 settled predictions showed actual home win rate of 50.0% and optimal
# HFA multiplier ~0.5x. Halved to ln(0.52/0.48) ≈ 0.080 until sample
# size justifies reverting. Re-evaluate at n=150+.
HOME_FIELD_LOGODDS = math.log(0.52 / 0.48)

# League-average ERA — anchor for pitcher adjustments. Roughly the
# 2020s NL/AL combined average; updated annually is a future feature.
LEAGUE_AVG_ERA = 4.30

# SP ERA adjustment DISABLED (v0.6). 42-game analysis: r=0.001 between
# SP delta and outcome. Every pitching layer added made Brier worse
# (0.2544 without pitching → 0.2632 with). The weight is zeroed rather
# than the code removed — re-enable when n=150+ if signal emerges.
ERA_LOGODDS_PER_RUN = 0.0

# Bayesian prior for pitcher ERA regression. Retained for when SP
# adjustment is re-enabled.
ERA_PRIOR_IP = 40.0

# Bullpen ERA adjustment DISABLED (v0.6). r(bp_delta, strength_delta)
# = 0.636 — bullpen ERA is a proxy for team quality already captured by
# Pythagorean. The negative outcome correlation (97.3% bootstrap) is
# confounding, not real signal. Zeroed alongside SP.
BULLPEN_LOGODDS_PER_RUN = 0.0
LEAGUE_AVG_BULLPEN_ERA = 4.10

# Lineup OPS adjustment. The one factor with correct-direction signal
# (r=0.126 with outcome in 42-game analysis). Grid search optimal at
# 0.75x of original weight. Reduced from 3.0 to 2.25.
LINEUP_LOGODDS_PER_OPS = 2.25
LEAGUE_AVG_OPS = 0.720


# ─── Data shapes ─────────────────────────────────────────────────────────


@dataclass
class Prediction:
    """One model output. `home_win_prob` is the headline number; the
    rest are along for the ride so we can audit why the model said what
    it said."""

    home_win_prob: float
    home_team: str
    away_team: str
    base_home_strength: float
    base_away_strength: float
    home_field_pct: float
    home_pitcher_adj: float
    away_pitcher_adj: float
    home_bullpen_adj: float = 0.0
    away_bullpen_adj: float = 0.0
    home_lineup_adj: float = 0.0
    away_lineup_adj: float = 0.0
    model_version: str = MODEL_VERSION
    notes: list[str] = field(default_factory=list)

    def to_inputs(self) -> dict[str, Any]:
        """Serializable snapshot for storage alongside the prediction."""
        return {
            "base_home_strength": self.base_home_strength,
            "base_away_strength": self.base_away_strength,
            "home_field_pct": self.home_field_pct,
            "home_pitcher_adj": self.home_pitcher_adj,
            "away_pitcher_adj": self.away_pitcher_adj,
            "home_bullpen_adj": self.home_bullpen_adj,
            "away_bullpen_adj": self.away_bullpen_adj,
            "home_lineup_adj": self.home_lineup_adj,
            "away_lineup_adj": self.away_lineup_adj,
            "model_version": self.model_version,
            "notes": self.notes,
        }


# ─── Pure functions ──────────────────────────────────────────────────────


def regressed_pythagorean(runs_scored: int, runs_allowed: int,
                            games_played: int) -> float:
    """Bayesian-regressed Pythagorean expectancy.

    A virtual ``PRIOR_WEIGHT`` games of league-average production are
    added before computing the ratio, so a 5-1 hot start doesn't read as
    .833 true talent.
    """
    rs = runs_scored + (PRIOR_WEIGHT * 4.5)   # 4.5 R/G league avg
    ra = runs_allowed + (PRIOR_WEIGHT * 4.5)
    rs2, ra2 = rs ** 2, ra ** 2
    if rs2 + ra2 == 0:
        return 0.5
    return rs2 / (rs2 + ra2)


def _prob_to_logodds(p: float) -> float:
    p = max(min(p, 0.999), 0.001)
    return math.log(p / (1 - p))


def _logodds_to_prob(lo: float) -> float:
    return 1.0 / (1.0 + math.exp(-lo))


def regressed_era(era: float, innings_pitched: float) -> float:
    """Bayesian-regressed ERA toward league average.

    With few innings the observed ERA is noisy (a 17.18 ERA on 3.2 IP
    is one bad outing, not true talent). We blend observed and prior
    weighted by innings pitched::

        regressed = (IP * observed + PRIOR_IP * league_avg) / (IP + PRIOR_IP)

    At ERA_PRIOR_IP (40) innings the observed ERA carries 50% weight.
    A full-season starter (~180 IP) carries ~82% weight.

    >>> round(regressed_era(17.18, 3.2), 2)  # one bad outing
    5.25
    >>> round(regressed_era(2.50, 180.0), 2)  # full-season ace
    2.83
    >>> regressed_era(4.30, 0.0)  # zero IP → league average
    4.3
    """
    if innings_pitched <= 0:
        return LEAGUE_AVG_ERA
    return (innings_pitched * era + ERA_PRIOR_IP * LEAGUE_AVG_ERA) / (
        innings_pitched + ERA_PRIOR_IP
    )


def pitcher_logodds_adj(era: float | None,
                        innings_pitched: float | None = None) -> float:
    """Log-odds adjustment for a starting pitcher's quality vs league
    average. Better ERA = positive boost for the pitcher's team.

    When ``innings_pitched`` is provided, the ERA is Bayesian-regressed
    toward league average before computing the adjustment. This prevents
    extreme predictions from tiny-sample ERAs (e.g. 17.18 on 3.2 IP).

    ``None`` ERA (bullpen game / pitcher not stat-tracked) returns 0,
    treating the start as league-average. That's the right default —
    no information should mean no movement off the team baseline.
    """
    if era is None:
        return 0.0
    if innings_pitched is not None:
        era = regressed_era(era, innings_pitched)
    return ERA_LOGODDS_PER_RUN * (LEAGUE_AVG_ERA - era)


def lineup_logodds_adj(lineup_avg_ops: float | None) -> float:
    """Log-odds adjustment for the posted lineup's offensive output
    vs league average. None means lineup hasn't been posted or no
    batter stats — returns 0 (no adjustment)."""
    if lineup_avg_ops is None:
        return 0.0
    return LINEUP_LOGODDS_PER_OPS * (lineup_avg_ops - LEAGUE_AVG_OPS)


def bullpen_logodds_adj(era: float | None) -> float:
    """Log-odds adjustment for a team's bullpen quality vs league
    average. A team with a 3.0 bullpen ERA gets ~0.07 log-odds (=
    ~1.7pp at 50/50) vs a team with a 4.10 league-avg bullpen.

    Halved weight vs ``pitcher_logodds_adj`` because the bullpen
    pitches the back ~40% of the game, not the whole thing. Real
    impact concentrates in late innings; this is an aggregate
    season-long approximation.

    ``None`` returns 0 — league-average treatment.
    """
    if era is None:
        return 0.0
    return BULLPEN_LOGODDS_PER_RUN * (LEAGUE_AVG_BULLPEN_ERA - era)


def predict_matchup(
    *,
    home_strength: float,
    away_strength: float,
    home_pitcher_era: float | None = None,
    away_pitcher_era: float | None = None,
    home_pitcher_ip: float | None = None,
    away_pitcher_ip: float | None = None,
    home_bullpen_era: float | None = None,
    away_bullpen_era: float | None = None,
    home_lineup_avg_ops: float | None = None,
    away_lineup_avg_ops: float | None = None,
    home_team_name: str = "Home",
    away_team_name: str = "Away",
    home_field: bool = True,
) -> Prediction:
    """Compute P(home wins).

    Composition:

    1. Convert each team's Pythagorean to log-odds; difference = base
       strength delta.
    2. Add home-field advantage (if applicable).
    3. Add SP quality delta — home_adj - away_adj.
    4. Add bullpen quality delta — same shape as (3) but halved
       weight since the bullpen pitches the back ~40% of the game.
    5. Convert back to probability.

    The log-odds composition is the right shape because rate
    probabilities don't compose additively (50% + 5% home-field +
    5% pitcher edge ≠ 60%) but log-odds do.
    """
    notes: list[str] = []

    base_home_lo = _prob_to_logodds(home_strength)
    base_away_lo = _prob_to_logodds(away_strength)

    delta = base_home_lo - base_away_lo

    if home_field:
        delta += HOME_FIELD_LOGODDS
    else:
        notes.append("neutral site")

    h_pit = pitcher_logodds_adj(home_pitcher_era, home_pitcher_ip)
    a_pit = pitcher_logodds_adj(away_pitcher_era, away_pitcher_ip)
    delta += (h_pit - a_pit)
    if home_pitcher_era is None:
        notes.append(f"{home_team_name}: no SP info — league-average assumed")
    if away_pitcher_era is None:
        notes.append(f"{away_team_name}: no SP info — league-average assumed")

    h_pen = bullpen_logodds_adj(home_bullpen_era)
    a_pen = bullpen_logodds_adj(away_bullpen_era)
    delta += (h_pen - a_pen)
    if home_bullpen_era is None:
        notes.append(f"{home_team_name}: no bullpen info — league-average assumed")
    if away_bullpen_era is None:
        notes.append(f"{away_team_name}: no bullpen info — league-average assumed")

    h_lineup = lineup_logodds_adj(home_lineup_avg_ops)
    a_lineup = lineup_logodds_adj(away_lineup_avg_ops)
    delta += (h_lineup - a_lineup)
    if home_lineup_avg_ops is None:
        notes.append(f"{home_team_name}: lineup not posted — league-average assumed")
    if away_lineup_avg_ops is None:
        notes.append(f"{away_team_name}: lineup not posted — league-average assumed")

    # The "matchup log-odds" lands at the home side's perspective.
    # Use it directly as P(home wins).
    home_win_prob = _logodds_to_prob(delta)

    return Prediction(
        home_win_prob=home_win_prob,
        home_team=home_team_name,
        away_team=away_team_name,
        base_home_strength=home_strength,
        base_away_strength=away_strength,
        home_field_pct=0.52 if home_field else 0.50,
        home_pitcher_adj=h_pit,
        away_pitcher_adj=a_pit,
        home_bullpen_adj=h_pen,
        away_bullpen_adj=a_pen,
        home_lineup_adj=h_lineup,
        away_lineup_adj=a_lineup,
        notes=notes,
    )
