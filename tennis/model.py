"""Tennis matchup model — thin wrapper over the Elo ladder.

Unlike the team sports (Pythagorean), tennis win-probability comes straight
from the two players' Elo ratings (``tennis.elo``). This module just exposes
a ``predict_match`` that reads cached Elo from state and returns P(p1 wins)
plus an auditable inputs dict. The Elo *engine* is in ``elo.py``; the
*ratings* are computed by the runner/backfill and cached in
``state.players.elo``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tennis import elo

MODEL_VERSION = "0.2.0"  # v0.2: rank-seeded Elo (walk-forward Brier 0.2405→0.2198)


@dataclass
class Prediction:
    p1_win_prob: float
    p1_name: str
    p2_name: str
    p1_elo: float
    p2_elo: float
    surface: str
    model_version: str = MODEL_VERSION

    def to_inputs(self) -> dict[str, Any]:
        return {
            "p1_elo": round(self.p1_elo, 1),
            "p2_elo": round(self.p2_elo, 1),
            "surface": self.surface,
            "model_version": self.model_version,
        }


def _surface_elo(player_row: dict[str, Any] | None, surface: str) -> float | None:
    if not player_row:
        return None
    raw = player_row.get("surface_elo")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return d.get(surface)


def predict_match(p1_row: dict[str, Any] | None, p2_row: dict[str, Any] | None,
                  *, surface: str = "unknown",
                  surface_weight: float = 0.5) -> Prediction | None:
    """P(p1 beats p2) from cached Elo. Either player missing → None.

    Blends global + surface Elo (when both available) the same way the
    engine does, so live predictions match backfilled ones."""
    if not p1_row or not p2_row:
        return None
    e1 = float(p1_row.get("elo", elo.DEFAULT_RATING))
    e2 = float(p2_row.get("elo", elo.DEFAULT_RATING))

    if surface and surface != "unknown":
        s1 = _surface_elo(p1_row, surface)
        s2 = _surface_elo(p2_row, surface)
        if s1 is not None:
            e1 = (1 - surface_weight) * e1 + surface_weight * s1
        if s2 is not None:
            e2 = (1 - surface_weight) * e2 + surface_weight * s2

    return Prediction(
        p1_win_prob=elo.expected_score(e1, e2),
        p1_name=str(p1_row.get("name", "")),
        p2_name=str(p2_row.get("name", "")),
        p1_elo=e1, p2_elo=e2, surface=surface,
    )
