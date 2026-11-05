"""Pure-function WNBA matchup model.

Deliberately minimal, per ``docs/sports-model-playbook.md`` §3: start with
the factors that earn their place and add nothing until a Brier test
justifies it. Baseball's expensive lesson was that "obvious" secondary
factors (pitcher/bullpen ERA) were noise or double-counted team quality.

Stages
------
1. **Team strength** from Pythagorean expectancy with the **basketball
   exponent (13.91)** — points-for/against, not runs. (Bill James'
   exponent is sport-specific: ~2 for baseball, ~13.91 for basketball,
   the Daryl Morey / Football Outsiders value used league-wide for the
   NBA and applied to the WNBA here.)
2. **Bayesian regression to .500** with a virtual-games prior, so an
   early-season hot/cold start doesn't read as true talent. WNBA plays a
   ~44-game season; PRIOR_WEIGHT=10 is ~quarter-season.
3. **Home-field advantage** as a log-odds boost. Seeded at a conservative
   NBA-ish ~0.55 home win rate and **to be re-measured** against our own
   settled predictions (baseball taught us not to trust the textbook
   number — re-evaluate once we have n≥50 settled games).

Everything is pure: inputs → P(home win) + an ``inputs`` dict the caller
persists alongside the prediction for auditability and Brier analysis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ─── Model constants ─────────────────────────────────────────────────────

MODEL_VERSION = "0.1.0"  # v0.1: Pythagorean + Bayesian prior + seeded HFA

# Basketball Pythagorean exponent. Baseball uses 2.0; basketball's
# empirically-fit value is ~13.91 (Morey/Football-Outsiders, standard for
# NBA and applied to WNBA). High exponent reflects that small per-game
# point-differentials map to large win-probability swings in basketball.
PYTHAGOREAN_EXPONENT = 13.91

# Bayesian prior on team strength: PRIOR_WEIGHT virtual games at a
# league-average points level are added before the ratio, regressing
# early-season records toward .500. WNBA season ≈ 44 games; 10 ≈ ¼ season.
PRIOR_WEIGHT = 10

# League-average points per game — the prior anchor. ~81 PPG is a recent
# WNBA-typical scoring level; updated-annually is a future refinement.
LEAGUE_AVG_PPG = 81.0

# Home-court advantage as a log-odds boost. Seeded at ~0.55 home win rate
# (ln(0.55/0.45) ≈ 0.201), a conservative basketball default. MUST be
# re-measured against our own settled predictions — see module docstring.
HOME_COURT_LOGODDS = math.log(0.55 / 0.45)


# ─── Result type ─────────────────────────────────────────────────────────


@dataclass
class Prediction:
    """Model output. ``home_win_prob`` is the headline; ``inputs`` is the
    auditable breakdown persisted alongside for Brier analysis."""

    home_win_prob: float
    home_team_name: str
    away_team_name: str
    home_strength: float           # regressed Pythagorean
    away_strength: float
    home_field: bool
    model_version: str = MODEL_VERSION
    notes: str = ""

    def to_inputs(self) -> dict[str, Any]:
        return {
            "home_strength": round(self.home_strength, 4),
            "away_strength": round(self.away_strength, 4),
            "home_field": self.home_field,
            "model_version": self.model_version,
            "notes": self.notes,
        }


# ─── Pure functions ──────────────────────────────────────────────────────


def regressed_pythagorean(points_for: int, points_against: int,
                          games_played: int) -> float:
    """Bayesian-regressed Pythagorean expectancy (basketball exponent).

    Adds ``PRIOR_WEIGHT`` virtual games of league-average scoring to both
    sides before the ratio, so a small-sample hot start regresses toward
    .500. With ``games_played`` only used to scale the prior implicitly
    (the prior is in points, added directly).

    >>> round(regressed_pythagorean(587, 558, 7), 3)
    0.572
    >>> regressed_pythagorean(0, 0, 0)
    0.5
    """
    pf = points_for + (PRIOR_WEIGHT * LEAGUE_AVG_PPG)
    pa = points_against + (PRIOR_WEIGHT * LEAGUE_AVG_PPG)
    pfe = pf ** PYTHAGOREAN_EXPONENT
    pae = pa ** PYTHAGOREAN_EXPONENT
    if pfe + pae == 0:
        return 0.5
    return pfe / (pfe + pae)


def _prob_to_logodds(p: float) -> float:
    p = max(min(p, 0.999), 0.001)
    return math.log(p / (1 - p))


def _logodds_to_prob(lo: float) -> float:
    return 1.0 / (1.0 + math.exp(-lo))


def predict_matchup(
    *,
    home_strength: float,
    away_strength: float,
    home_team_name: str = "Home",
    away_team_name: str = "Away",
    home_field: bool = True,
) -> Prediction:
    """Compute P(home wins).

    1. Convert each team's regressed Pythagorean to log-odds; the
       difference is the base strength delta.
    2. Add home-court advantage (if applicable).
    3. Convert back to probability.

    Log-odds composition is the right shape — rate probabilities don't
    add (50% + 5% home edge ≠ 55%) but log-odds do.
    """
    notes: list[str] = []
    delta = _prob_to_logodds(home_strength) - _prob_to_logodds(away_strength)
    if home_field:
        delta += HOME_COURT_LOGODDS
    else:
        notes.append("neutral site")
    return Prediction(
        home_win_prob=_logodds_to_prob(delta),
        home_team_name=home_team_name,
        away_team_name=away_team_name,
        home_strength=home_strength,
        away_strength=away_strength,
        home_field=home_field,
        notes="; ".join(notes),
    )
