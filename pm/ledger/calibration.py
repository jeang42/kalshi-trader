"""Forecast log + calibration scoring.

A "forecast" is a probability estimate you made for a market outcome at a
point in time. After the market resolves, you can record whether you were
right or wrong, and then calibration scoring tells you whether your
estimates are systematically biased.

Two scoring concepts:

- **Brier score**: mean squared error between your probability and the
  realized outcome (0/1). Lower is better; 0 is perfect. A uniformly
  random forecaster gets 0.25 on a 50/50 market.

- **Calibration plot**: bin forecasts by predicted probability (e.g. all
  forecasts where you said 60–70%). For a well-calibrated forecaster,
  the actual fraction of yeses in that bin should also be ~65%. Plotting
  predicted vs. actual gives a curve; a perfectly calibrated forecaster
  sits on the diagonal.

Storage: a single `forecasts.json` next to `portfolio.json`. Forecasts
are append-only; "delete forecast" is for typo correction only.
"""

import contextlib
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import atomic_io

log = logging.getLogger(__name__)

VERSION = 1


# ---------------------------------------------------------------------------
# Storage path — lazy lookup so profile switches take effect immediately.
# ---------------------------------------------------------------------------

def _forecasts_path() -> Path:
    env = os.getenv("POLYMARKET_FORECASTS_FILE")
    if env:
        return Path(env)
    from pm.core import profiles
    return profiles.file_path("forecasts")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _empty_store() -> dict:
    return {"version": VERSION, "forecasts": []}


def _load() -> dict:
    """Read-only view. Tolerates a damaged file (recovers from ``.bak``,
    quarantines the bad copy) rather than raising — the auto-resolve runner
    walks every profile and one corrupt store must not stop the rest."""
    data = atomic_io.read_json(_forecasts_path(), None)
    return _empty_store() if data is None else data


def _save(data: dict) -> None:
    """Publish atomically. Prefer :func:`_mutate` for read-then-write."""
    atomic_io.write_json(_forecasts_path(), data)


@contextlib.contextmanager
def _mutate():
    """Locked read-modify-write over the forecast log.

    The forecast store is the Brier/calibration record — the evidence base for
    every claim about whether a source is any good — so a silently dropped
    resolution corrupts a conclusion, not just a row.
    """
    path = _forecasts_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            data = _empty_store()
        yield data
        atomic_io.write_json(path, data)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_forecast(
    question: str,
    condition_id: str,
    outcome: str,
    market_price: float,
    your_probability: float,
    *,
    source: str = "user",
    notes: str = "",
) -> dict:
    """Record a forecast snapshot. Returns the recorded forecast dict.

    Both prices are stored so we can later compute the edge you saw at the
    time the forecast was made.

    ``source`` tags where the forecast came from — ``"user"`` for ones
    the user enters manually, ``"llm:<model>"`` for ones recorded from
    the LLM edge source. The Calibration page groups by this so each
    source has its own Brier score (LLM forecasts shouldn't pollute the
    user's calibration table, and vice versa).
    """
    if not (0.0 <= market_price <= 1.0):
        raise ValueError(f"market_price must be in [0, 1], got {market_price}")
    if not (0.0 <= your_probability <= 1.0):
        raise ValueError(f"your_probability must be in [0, 1], got {your_probability}")

    forecast = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "question": question,
        "condition_id": condition_id,
        "outcome": outcome,
        "market_price": float(market_price),
        "your_probability": float(your_probability),
        "resolution": None,        # None | True | False
        "resolved_at": None,
        "source": source,
        "notes": notes,
    }
    with _mutate() as data:
        data.setdefault("forecasts", []).append(forecast)
    log.info("recorded forecast: %s @ %.2f (market %.2f, source=%s)",
             outcome, your_probability, market_price, source)
    return forecast


def record_resolution(forecast_id: str, outcome_yes: bool) -> bool:
    """Mark a forecast resolved. Returns True if found and updated."""
    found = False
    with _mutate() as data:
        for f in data.get("forecasts", []):
            if f["id"] == forecast_id:
                f["resolution"] = bool(outcome_yes)
                f["resolved_at"] = datetime.now(UTC).isoformat()
                found = True
                break
    if found:
        log.info("resolved forecast %s: %s",
                 forecast_id[:8], "Yes" if outcome_yes else "No")
        return True
    log.warning("record_resolution: forecast id %s not found", forecast_id[:8])
    return False


def auto_resolve_pending(closed_lookup: dict[str, bool]) -> list[str]:
    """Mark each *pending* forecast resolved using ``closed_lookup``.

    ``closed_lookup`` is ``{condition_id: resolved_yes}`` (boolean). The
    typical caller builds this by unioning ``get_closed_markets()`` from
    every registered provider — but this function takes the dict
    directly so it stays venue-agnostic and easy to unit-test.

    Mapping: a forecast's ``resolution`` is True when the forecast's
    chosen outcome happened — so an ``outcome="Yes"`` forecast on a
    market that resolved YES gets ``resolution=True``; an
    ``outcome="No"`` forecast on the same market gets ``resolution=False``.

    Skipped silently:
    - forecasts already resolved
    - forecasts whose ``condition_id`` isn't in ``closed_lookup``
    - forecasts with no/empty ``condition_id``

    Returns the IDs of forecasts that were updated. Writes the store
    once at the end.
    """
    if not closed_lookup:
        return []
    updated: list[str] = []
    path = _forecasts_path()
    # locked()+explicit write rather than _mutate(): this runs for EVERY
    # profile on a 15-minute cron and usually resolves nothing, so we take the
    # lock but only pay for a write when something actually changed.
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            data = _empty_store()
        for f in data.get("forecasts", []):
            if f.get("resolution") is not None:
                continue
            cid = f.get("condition_id")
            if not cid or cid not in closed_lookup:
                continue
            resolved_yes = closed_lookup[cid]
            outcome_yes = str(f.get("outcome", "")).strip().lower() == "yes"
            forecast_won = outcome_yes == resolved_yes
            f["resolution"] = bool(forecast_won)
            f["resolved_at"] = datetime.now(UTC).isoformat()
            updated.append(f["id"])
        if updated:
            atomic_io.write_json(path, data)
            log.info("auto-resolved %d forecast(s)", len(updated))
    return updated


def delete_forecast(forecast_id: str) -> bool:
    path = _forecasts_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            return False
        forecasts = data.get("forecasts", [])
        new = [f for f in forecasts if f["id"] != forecast_id]
        if len(new) == len(forecasts):
            return False            # nothing matched — leave the file alone
        data["forecasts"] = new
        atomic_io.write_json(path, data)
    return True


def get_forecasts() -> pd.DataFrame:
    """All forecasts as a DataFrame, sorted oldest-first by timestamp.

    Records written before the ``source`` field was added default to
    ``source="user"`` on read — no on-disk migration needed.
    """
    data = _load()
    forecasts = data.get("forecasts", [])
    cols = ["id", "timestamp", "question", "condition_id", "outcome",
            "market_price", "your_probability", "resolution", "resolved_at",
            "source", "notes"]
    if not forecasts:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(forecasts)
    if "source" not in df.columns:
        df["source"] = "user"
    else:
        df["source"] = df["source"].fillna("user")
    if "notes" not in df.columns:
        df["notes"] = ""
    else:
        df["notes"] = df["notes"].fillna("")
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df.sort_values("timestamp_dt").reset_index(drop=True)


def get_forecasts_all_profiles() -> pd.DataFrame:
    """Concatenate every profile's forecast log into one DataFrame.

    Track records — especially per-source Brier — only become useful
    after dozens of resolved forecasts. The agentic runners write to
    their own profiles (``agentic_*``); a user on ``jeff`` who has
    never personally logged a forecast would otherwise see "untested"
    badges on every source even though the runners have been
    accumulating evidence. This helper pulls every profile's log so
    the Recommendations page can show the true cross-profile track
    record.

    Adds a ``profile`` column (which profile each row came from) so
    callers can subset if they want a single-profile view.
    """
    from pm.core import profiles
    rows: list[pd.DataFrame] = []
    saved = profiles.active_profile()
    try:
        for name in profiles.list_profiles():
            profiles.set_active_profile(name)
            df = get_forecasts()
            if df.empty:
                continue
            df = df.copy()
            df["profile"] = name
            rows.append(df)
    finally:
        profiles.set_active_profile(saved)
    if not rows:
        return get_forecasts()  # empty, but with the right column shape
    out = pd.concat(rows, ignore_index=True)
    return out.sort_values("timestamp_dt").reset_index(drop=True)


def brier_by_source(df: pd.DataFrame) -> dict[str, dict]:
    """Per-source Brier score + sample size, for ``brier_score``-eligible
    forecasts (i.e. ones with a resolution).

    Returns ``{source: {"brier": float | None, "n": int}}``. None Brier
    means the source has no resolved forecasts yet.
    """
    if df is None or df.empty or "source" not in df.columns:
        return {}
    out: dict[str, dict] = {}
    for src, sub in df.groupby("source"):
        out[str(src)] = {"brier": brier_score(sub), "n": int(len(sub))}
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def brier_score(df: pd.DataFrame) -> float | None:
    """Brier score over resolved forecasts. Lower is better; 0 is perfect.

    Computed as mean( (your_probability - actual)^2 ) where actual is 1 if
    the resolution matched what you forecast (you said 'Yes' on 'Yes', or
    'No' on 'No'), 0 otherwise. Returns None if no resolved forecasts.

    >>> import pandas as pd
    >>> df = pd.DataFrame([
    ...     {"your_probability": 1.0, "resolution": True,  "outcome": "Yes"},
    ...     {"your_probability": 0.0, "resolution": False, "outcome": "Yes"},
    ... ])
    >>> brier_score(df)
    0.0
    >>> df2 = pd.DataFrame([
    ...     {"your_probability": 0.5, "resolution": True,  "outcome": "Yes"},
    ...     {"your_probability": 0.5, "resolution": False, "outcome": "Yes"},
    ... ])
    >>> brier_score(df2)
    0.25
    """
    if df.empty:
        return None
    resolved = df[df["resolution"].notna()].copy()
    if resolved.empty:
        return None
    # actual = 1 if the resolution matches the outcome the forecaster bet on,
    # else 0. For a YES forecast: actual = 1 iff resolution=True. For a NO
    # forecast: actual = 1 iff resolution=False.
    yes_forecast = resolved["outcome"].astype(str).str.lower().eq("yes")
    resolution_bool = resolved["resolution"].astype(bool)
    # actual = 1 iff the resolution matches the side we forecasted
    actual = yes_forecast.eq(resolution_bool).astype(float)
    sq_err = (resolved["your_probability"] - actual) ** 2
    return float(sq_err.mean())


_MIN_RESOLVED_PER_CATEGORY = 5
"""Minimum sample size before a per-category Brier score is reported.
Below this, statistical noise dominates and a number would mislead more
than inform — callers receive None for that category."""


def brier_score_by_category(
    df_forecasts: pd.DataFrame,
    df_markets: pd.DataFrame,
) -> dict[str, float | None]:
    """Per-category Brier score. Joins forecasts to markets by condition_id.

    Categories with fewer than `_MIN_RESOLVED_PER_CATEGORY` resolved forecasts
    return None — a number from a tiny sample would be more misleading than
    useful.

    Returns: {category: brier_or_None}
    """
    if df_forecasts is None or df_forecasts.empty:
        return {}
    if df_markets is None or df_markets.empty:
        return {}

    resolved = df_forecasts[df_forecasts["resolution"].notna()].copy()
    if resolved.empty:
        return {}

    cat_map = df_markets.set_index("condition_id").get("category")
    if cat_map is None:
        return {}
    resolved["category"] = resolved["condition_id"].map(cat_map).fillna("Unknown")

    out: dict[str, float | None] = {}
    for category, group in resolved.groupby("category"):
        if len(group) < _MIN_RESOLVED_PER_CATEGORY:
            out[str(category)] = None
            continue
        out[str(category)] = brier_score(group)
    return out


def market_brier_score(df: pd.DataFrame) -> float | None:
    """Brier score over a market-calibration dataset (Tier 3a).

    `df` must have columns `predicted_prob` (float in [0,1]) and `actual`
    (0.0 or 1.0, where 1.0 means the YES side won). Returns None for empty
    input.

    >>> import pandas as pd
    >>> df = pd.DataFrame([
    ...     {"predicted_prob": 0.7, "actual": 1.0},
    ...     {"predicted_prob": 0.3, "actual": 0.0},
    ... ])
    >>> round(market_brier_score(df), 4)
    0.09
    """
    if df is None or df.empty:
        return None
    sq_err = (df["predicted_prob"] - df["actual"]) ** 2
    return float(sq_err.mean())


def market_brier_by_category(df: pd.DataFrame) -> dict[str, float | None]:
    """Per-category Brier over a market-calibration dataset.

    Same `_MIN_RESOLVED_PER_CATEGORY` threshold as the user-side scoring —
    categories with too few resolved markets return None so we don't report
    noise.
    """
    if df is None or df.empty:
        return {}
    out: dict[str, float | None] = {}
    for cat, group in df.groupby("category"):
        if len(group) < _MIN_RESOLVED_PER_CATEGORY:
            out[str(cat)] = None
            continue
        out[str(cat)] = market_brier_score(group)
    return out


def market_calibration_bins(df: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    """Calibration bins for the market-baseline dataset, parallel in shape
    to the user-side `calibration_bins`.

    Returns one row per non-empty bin with `bin_center`, `n`,
    `mean_predicted`, `observed_rate`. Used for the side-by-side calibration
    plot on the Calibration page.
    """
    cols = ["bin_center", "n", "mean_predicted", "observed_rate"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    edges = [i / n_bins for i in range(n_bins + 1)]
    df2 = df.copy()
    df2["bin"] = pd.cut(df2["predicted_prob"], bins=edges, include_lowest=True)

    rows = []
    for bin_interval, group in df2.groupby("bin", observed=True):
        if group.empty:
            continue
        rows.append({
            "bin_center": float(bin_interval.mid),
            "n": int(len(group)),
            "mean_predicted": float(group["predicted_prob"].mean()),
            "observed_rate": float(group["actual"].mean()),
        })
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("bin_center").reset_index(drop=True)


def calibration_bins(df: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    """Bin resolved forecasts by predicted probability and report observed rate.

    Returns one row per non-empty bin with columns:
        bin_center        midpoint of the probability bin (e.g. 0.55)
        n                 number of forecasts in this bin
        mean_predicted    mean of your_probability in this bin
        observed_rate     fraction of forecasts in this bin that resolved
                          in favor of the forecaster's chosen outcome

    A well-calibrated forecaster has observed_rate ≈ mean_predicted for
    every bin (points sit on the diagonal).
    """
    cols = ["bin_center", "n", "mean_predicted", "observed_rate"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    resolved = df[df["resolution"].notna()].copy()
    if resolved.empty:
        return pd.DataFrame(columns=cols)

    yes_forecast = resolved["outcome"].astype(str).str.lower().eq("yes")
    resolution_bool = resolved["resolution"].astype(bool)
    resolved["actual"] = yes_forecast.eq(resolution_bool).astype(float)

    edges = [i / n_bins for i in range(n_bins + 1)]
    resolved["bin"] = pd.cut(
        resolved["your_probability"], bins=edges, include_lowest=True
    )

    rows = []
    for bin_interval, group in resolved.groupby("bin", observed=True):
        if group.empty:
            continue
        rows.append({
            "bin_center": float(bin_interval.mid),
            "n": int(len(group)),
            "mean_predicted": float(group["your_probability"].mean()),
            "observed_rate": float(group["actual"].mean()),
        })
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("bin_center").reset_index(drop=True)


def summary_stats(df: pd.DataFrame) -> dict:
    """Quick scoreboard for the calibration page."""
    total = len(df)
    resolved = int(df["resolution"].notna().sum()) if not df.empty else 0
    pending = total - resolved
    return {
        "total": total,
        "resolved": resolved,
        "pending": pending,
        "brier": brier_score(df),
    }
