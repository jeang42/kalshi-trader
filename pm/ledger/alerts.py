"""Price alerts.

Each alert specifies a threshold for a single (market, outcome): trigger when
the price crosses *above* or *below* the threshold. Alerts are checked
against the live market data on every page load that calls `check_alerts`.

Once triggered, an alert stays in the triggered state until you `reset_alert`
(un-trigger so it can fire again) or `delete_alert` (remove entirely). This
means each crossing fires exactly once — no spamming on every page rerun.

Storage: alerts.json next to portfolio.json. Gitignored.

Schema:
    {
      "version": 1,
      "alerts": [
        {
          "id": "uuid4",
          "created_at": "ISO",
          "question": "...",
          "condition_id": "0x...",
          "outcome": "Yes",
          "direction": "above" | "below",
          "threshold": 0.80,
          "triggered": false,
          "triggered_at": null,
          "triggered_price": null
        }
      ]
    }
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


def _alerts_path() -> Path:
    """Lazy lookup so profile switches take effect immediately."""
    env = os.getenv("POLYMARKET_ALERTS_FILE")
    if env:
        return Path(env)
    from pm.core import profiles
    return profiles.file_path("alerts")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _empty_store() -> dict:
    return {"version": VERSION, "alerts": []}


def _load() -> dict:
    """Read-only view; tolerates a damaged file instead of raising."""
    data = atomic_io.read_json(_alerts_path(), None)
    return _empty_store() if data is None else data


def _save(data: dict) -> None:
    """Publish atomically. Prefer :func:`_mutate` for read-then-write."""
    atomic_io.write_json(_alerts_path(), data)


@contextlib.contextmanager
def _mutate():
    """Locked read-modify-write over the alert store. check_alerts() runs on
    every page render, so the UI is a genuine concurrent writer here."""
    path = _alerts_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            data = _empty_store()
        yield data
        atomic_io.write_json(path, data)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_alert(
    question: str,
    condition_id: str,
    outcome: str,
    direction: str,
    threshold: float,
) -> dict:
    """Create an alert. direction must be 'above' or 'below'.

    >>> # threshold must be in [0, 1]
    >>> add_alert.__doc__ is not None
    True
    """
    if direction not in ("above", "below"):
        raise ValueError(f"direction must be 'above' or 'below', got {direction!r}")
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0, 1], got {threshold}")

    alert = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "question": question,
        "condition_id": condition_id,
        "outcome": outcome,
        "direction": direction,
        "threshold": float(threshold),
        "triggered": False,
        "triggered_at": None,
        "triggered_price": None,
    }
    with _mutate() as data:
        data.setdefault("alerts", []).append(alert)
    log.info("created alert: %s %s $%.4f on %s",
             outcome, direction, threshold, condition_id[:18])
    return alert


def delete_alert(alert_id: str) -> bool:
    path = _alerts_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            return False
        alerts = data.get("alerts", [])
        new = [a for a in alerts if a["id"] != alert_id]
        if len(new) == len(alerts):
            return False
        data["alerts"] = new
        atomic_io.write_json(path, data)
    return True


def reset_alert(alert_id: str) -> bool:
    """Un-trigger an alert so it can fire again on the next crossing."""
    path = _alerts_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            return False
        for a in data.get("alerts", []):
            if a["id"] == alert_id:
                a["triggered"] = False
                a["triggered_at"] = None
                a["triggered_price"] = None
                atomic_io.write_json(path, data)
                return True
    return False


def get_alerts() -> pd.DataFrame:
    """All alerts as a DataFrame, newest-first by creation."""
    data = _load()
    alerts = data.get("alerts", [])
    cols = ["id", "created_at", "question", "condition_id", "outcome",
            "direction", "threshold", "triggered", "triggered_at", "triggered_price"]
    if not alerts:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(alerts)
    df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
    return df.sort_values("created_at_dt", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Threshold checking
# ---------------------------------------------------------------------------

def _build_price_map(df_markets: pd.DataFrame) -> dict[tuple[str, str], float]:
    """(condition_id, outcome) -> current mid price."""
    price_map: dict[tuple[str, str], float] = {}
    if df_markets is None or df_markets.empty:
        return price_map
    for _, m in df_markets.iterrows():
        outcomes = m.get("outcomes") or []
        prices = m.get("outcome_prices") or []
        cid = m.get("condition_id", "")
        if not isinstance(outcomes, list) or not isinstance(prices, list):
            continue
        for o, p in zip(outcomes, prices, strict=False):
            if p is None:
                continue
            try:
                price_map[(cid, str(o))] = float(p)
            except (TypeError, ValueError):
                pass
    return price_map


def _alert_should_trigger(direction: str, current_price: float, threshold: float) -> bool:
    if direction == "above":
        return current_price >= threshold
    if direction == "below":
        return current_price <= threshold
    return False


def check_alerts(df_markets: pd.DataFrame) -> list[dict]:
    """Check all non-triggered alerts against current market prices.

    Mutates state: any alert whose threshold has been crossed is marked
    triggered (with timestamp + price) and saved. Returns the list of
    newly-triggered alerts from THIS call only — alerts that were already
    triggered before this call are not re-included.
    """
    price_map = _build_price_map(df_markets)
    newly_triggered: list[dict] = []
    now_iso = datetime.now(UTC).isoformat()
    changed = False

    path = _alerts_path()
    # The lock is held across the whole scan, not just the write: two renders
    # racing here would otherwise both observe the same un-triggered alert and
    # both report it as newly fired.
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        if data is None:
            data = _empty_store()
        for a in data.get("alerts", []):
            if a.get("triggered"):
                continue
            key = (a["condition_id"], a["outcome"])
            current = price_map.get(key)
            if current is None:
                continue
            if _alert_should_trigger(a["direction"], current, a["threshold"]):
                a["triggered"] = True
                a["triggered_at"] = now_iso
                a["triggered_price"] = current
                newly_triggered.append(dict(a))
                changed = True

        if changed:
            atomic_io.write_json(path, data)
            log.info("alerts fired: %d", len(newly_triggered))
    return newly_triggered


def summary_stats() -> dict:
    """Counts for the sidebar / page header."""
    data = _load()
    alerts = data.get("alerts", [])
    triggered = sum(1 for a in alerts if a.get("triggered"))
    return {
        "total": len(alerts),
        "active": len(alerts) - triggered,
        "triggered": triggered,
    }
