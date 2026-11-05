"""Risk management — Tier 1 of the recommendations roadmap.

See docs/recommendations-design.md for context. This module deals with
*anti-recommendations*: caps, warnings, and Kelly multipliers that prevent
the user from sizing into bets they shouldn't take.

Pure functions (no I/O) for the math; a small JSON-backed Settings store
for user-configurable thresholds.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from pm.core import atomic_io

log = logging.getLogger(__name__)


def _settings_path() -> Path:
    """Lazy lookup so profile switches take effect immediately."""
    env = os.getenv("POLYMARKET_SETTINGS_FILE")
    if env:
        return Path(env)
    from pm.core import profiles
    return profiles.file_path("settings")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """User-configurable risk thresholds.

    Defaults are conservative; the user can override in settings.json or via
    the Settings UI.
    """
    bankroll: float = 1000.0
    """Total trading capital. Used as the denominator for position-size limits."""

    max_kelly_fraction: float = 0.25
    """Absolute cap on the Kelly fraction regardless of calibration. Even if
    the math says bet 80% of bankroll, this trims it to 25%. Set lower to be
    more conservative."""

    max_market_position_pct: float = 0.05
    """Per-market position size limit as a fraction of bankroll (0.05 = 5%).
    ADVISORY: the Kelly page and pre_flight_check WARN when a stake exceeds
    it, but nothing blocks the order on it. The hard, enforced per-order
    protection is the stake cap (LIVE_MAX_STAKE_DOLLARS /
    kalshi_orders.mandatory_caps)."""

    max_category_exposure_pct: float = 0.30
    """Per-category cost-basis cap. Warn (don't block) when one category
    exceeds this fraction of total open cost basis."""

    drawdown_pause_pct: float = 0.15
    """Threshold for the drawdown circuit breaker. When current drawdown
    from peak exceeds this fraction, the Kelly page surfaces a warning
    banner recommending the user pause and review."""


def load_settings() -> Settings:
    """Read settings from disk. Returns defaults if the file is missing or
    invalid — never raises."""
    path = _settings_path()
    if not path.exists():
        return Settings()
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return Settings()
        # Only keep recognised fields so the dataclass init doesn't choke on
        # future-version keys.
        valid = {k: data[k] for k in Settings.__dataclass_fields__ if k in data}
        return Settings(**valid)
    except (json.JSONDecodeError, OSError, TypeError) as e:
        log.warning("Could not parse %s: %s. Using defaults.", path, e)
        return Settings()


def save_settings(s: Settings) -> None:
    """Publish settings atomically.

    No lock is taken: unlike the ledgers, this is a whole-object write with no
    read-modify-write step, so the only failure mode was the torn file — which
    the atomic replace closes. Settings are also written by a human on the
    Settings page, never by two daemons at once.
    """
    path = _settings_path()
    atomic_io.write_json(path, asdict(s))
    log.info("Saved risk settings to %s", path)


# ---------------------------------------------------------------------------
# Calibration-adjusted Kelly multiplier
# ---------------------------------------------------------------------------

# Thresholds in Brier-score space. Lower Brier = better forecaster.
# 0.0 is perfect, 0.25 is uniformly-random on 50/50 markets.
_BRIER_FULL_KELLY = 0.10       # ≤ this → full Kelly multiplier (1.0)
_BRIER_HALF_KELLY = 0.20       # ≤ this → half Kelly (0.5)
_BRIER_QUARTER_KELLY = 0.25    # ≤ this → quarter Kelly (0.25)
                                # > this  → 0 (don't bet your own number; match the market)

_NO_DATA_MULTIPLIER = 0.5      # half Kelly when we have no calibration data yet


def calibration_adjusted_kelly_multiplier(brier: float | None) -> tuple[float, str]:
    """Map a Brier score to a Kelly multiplier in [0, 1] plus an explanation.

    The multiplier scales whatever the raw Kelly fraction is. Returning 0
    means "don't bet on your own probability estimate; take the market price
    as authoritative" — i.e., a hard recommendation to abstain.

    >>> calibration_adjusted_kelly_multiplier(0.05)[0]
    1.0
    >>> calibration_adjusted_kelly_multiplier(0.15)[0]
    0.5
    >>> calibration_adjusted_kelly_multiplier(0.22)[0]
    0.25
    >>> calibration_adjusted_kelly_multiplier(0.30)[0]
    0.0
    >>> calibration_adjusted_kelly_multiplier(None)[0]
    0.5
    """
    if brier is None:
        return _NO_DATA_MULTIPLIER, (
            "½ Kelly applied — no calibration data yet for this category. "
            "Save and resolve forecasts to refine this."
        )
    if brier <= _BRIER_FULL_KELLY:
        return 1.0, (
            f"Full Kelly multiplier — your Brier of {brier:.3f} indicates "
            "sharp calibration in this category."
        )
    if brier <= _BRIER_HALF_KELLY:
        return 0.5, (
            f"½ Kelly applied — your Brier of {brier:.3f} is solid but not exceptional. "
            "Half-Kelly cuts variance with little loss of growth rate."
        )
    if brier <= _BRIER_QUARTER_KELLY:
        return 0.25, (
            f"¼ Kelly applied — your Brier of {brier:.3f} is only marginally better "
            "than random. Quarter-Kelly is the conservative choice here."
        )
    return 0.0, (
        f"Recommendation: don't bet your own probability estimate. Your Brier of "
        f"{brier:.3f} in this category is worse than a random forecaster (0.25). "
        "Take the market price as authoritative until your calibration improves."
    )


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def current_drawdown(df_snaps: pd.DataFrame) -> dict:
    """Compute peak-to-current drawdown from the snapshot log.

    Returns a dict with:
        peak                float | None    highest net_value seen
        current             float | None    latest net_value
        amount              float | None    current - peak (negative or zero)
        pct                 float | None    amount / peak (negative or zero)
        in_drawdown         bool            True when pct < 0

    Returns all-None / False when there's no snapshot data.

    >>> import pandas as pd
    >>> df = pd.DataFrame([
    ...     {"net_value": 1000.0},
    ...     {"net_value": 1200.0},
    ...     {"net_value": 1100.0},
    ... ])
    >>> r = current_drawdown(df)
    >>> r['peak'], r['current']
    (1200.0, 1100.0)
    >>> round(r['pct'], 4)
    -0.0833
    >>> r['in_drawdown']
    True
    """
    if df_snaps is None or df_snaps.empty or "net_value" not in df_snaps.columns:
        return {"peak": None, "current": None, "amount": None,
                "pct": None, "in_drawdown": False}
    peak = float(df_snaps["net_value"].max())
    current = float(df_snaps["net_value"].iloc[-1])
    amount = current - peak
    pct = (amount / peak) if peak > 0 else 0.0
    return {
        "peak": peak,
        "current": current,
        "amount": amount,
        "pct": pct,
        "in_drawdown": pct < 0,
    }


def should_pause_for_drawdown(drawdown: dict, threshold_pct: float) -> bool:
    """True when the user is currently in a drawdown deeper than threshold_pct.

    threshold_pct is positive (e.g. 0.15 means "pause at 15% drawdown").

    >>> should_pause_for_drawdown({"pct": -0.20, "in_drawdown": True}, 0.15)
    True
    >>> should_pause_for_drawdown({"pct": -0.05, "in_drawdown": True}, 0.15)
    False
    >>> should_pause_for_drawdown({"pct": None, "in_drawdown": False}, 0.15)
    False
    """
    pct = drawdown.get("pct")
    if pct is None:
        return False
    return pct <= -abs(threshold_pct)


# ---------------------------------------------------------------------------
# Sector / category exposure
# ---------------------------------------------------------------------------

def category_exposure_pct(df_pos: pd.DataFrame) -> dict[str, float]:
    """Return {category: share_of_total_cost_basis} for open positions.

    Empty df returns {}. NaN/empty categories are bucketed as "Unknown".

    >>> import pandas as pd
    >>> df = pd.DataFrame([
    ...     {"category": "Crypto",   "cost_basis": 200.0},
    ...     {"category": "Sports",   "cost_basis": 100.0},
    ...     {"category": "Crypto",   "cost_basis": 100.0},
    ... ])
    >>> {k: round(v, 2) for k, v in category_exposure_pct(df).items()}
    {'Crypto': 0.75, 'Sports': 0.25}
    """
    if df_pos is None or df_pos.empty or "cost_basis" not in df_pos.columns:
        return {}
    df = df_pos.copy()
    df["category"] = df.get("category", "Unknown").fillna("Unknown").replace("", "Unknown")
    total = df["cost_basis"].sum()
    if total <= 0:
        return {}
    grouped = df.groupby("category")["cost_basis"].sum() / total
    return {str(k): float(v) for k, v in grouped.items()}


def concentrated_categories(
    exposure: dict[str, float], threshold_pct: float
) -> list[tuple[str, float]]:
    """Return [(category, pct)] entries that exceed threshold_pct, sorted desc.

    >>> concentrated_categories({"Crypto": 0.75, "Sports": 0.25}, 0.30)
    [('Crypto', 0.75)]
    >>> concentrated_categories({"Crypto": 0.25, "Sports": 0.25}, 0.30)
    []
    """
    items = [(cat, pct) for cat, pct in exposure.items() if pct > threshold_pct]
    items.sort(key=lambda x: x[1], reverse=True)
    return items


# ---------------------------------------------------------------------------
# Position-size check
# ---------------------------------------------------------------------------

def position_exceeds_limit(
    cost_dollars: float, bankroll: float, max_pct: float
) -> bool:
    """True when the proposed cost would exceed max_pct of bankroll.

    >>> position_exceeds_limit(60, 1000, 0.05)
    True
    >>> position_exceeds_limit(40, 1000, 0.05)
    False
    >>> position_exceeds_limit(100, 0, 0.05)   # zero bankroll → always over
    True
    """
    if bankroll <= 0:
        return cost_dollars > 0
    return cost_dollars > max_pct * bankroll
