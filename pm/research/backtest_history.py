"""Persistent backtest-run history.

Auto-saves every ``BacktestResult`` from the 🧪 Backtest page so a 50-
market run isn't lost when Streamlit reruns the script or the session
ends. The 📜 Backtest history page renders them as a sortable table
with delete buttons. Settings page surfaces storage stats.

Storage: one JSON file per run in ``data/shared/backtest_runs/`` named
``{epoch_ms}_{strategy_key}.json``. Path override via
``BACKTEST_HISTORY_DIR`` env var.

Why not SQLite: BacktestResult is a flat record per run with mostly
metadata + two DataFrames (trades, equity_curve). No relational
queries needed — just "list recent" and "load one." One JSON file per
run lets the user `ls` / `rm` individual files without tooling.

Why JSON not pickle: human-readable, no version-of-Python concerns, no
unpickling-arbitrary-code surface area if a file is ever copied
between machines.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import MISSING, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pm.core import provenance
from pm.research import backtest as bt

log = logging.getLogger(__name__)

DEFAULT_HISTORY_DIR = Path("data/shared/backtest_runs")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _dir() -> Path:
    """Resolved history directory. ``BACKTEST_HISTORY_DIR`` env overrides."""
    return Path(os.getenv("BACKTEST_HISTORY_DIR", str(DEFAULT_HISTORY_DIR)))


def _ensure_dir() -> Path:
    p = _dir()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.mkdir(exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# DataFrame ↔ JSON helpers
# ---------------------------------------------------------------------------
# trades / equity_curve carry datetime columns that don't round-trip
# through json.dumps directly. We ISO-stringify on save and parse back
# on load, keying off column-name conventions used elsewhere in the
# codebase.

_DATETIME_COLS = {
    "as_of", "closed_time", "entry_time", "exit_time",
}


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """JSON-friendly list-of-dicts. ISO-stringifies datetime columns."""
    if df is None or df.empty:
        return []
    df2 = df.copy()
    for col in df2.columns:
        if pd.api.types.is_datetime64_any_dtype(df2[col]):
            df2[col] = df2[col].apply(
                lambda x: x.isoformat() if pd.notna(x) else None
            )
    return df2.to_dict(orient="records")


def _records_to_df(records: list[dict]) -> pd.DataFrame:
    """Reverse of _df_to_records. Parses ISO datetimes back."""
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in df.columns:
        if col in _DATETIME_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    return df


# ---------------------------------------------------------------------------
# BacktestResult ↔ JSON
# ---------------------------------------------------------------------------

def _result_to_dict(r: bt.BacktestResult) -> dict[str, Any]:
    """Serialize a BacktestResult to a JSON-friendly dict. DataFrames go
    to records; everything else passes through `dataclasses.asdict`."""
    out: dict[str, Any] = {}
    for f in fields(r):
        v = getattr(r, f.name)
        if isinstance(v, pd.DataFrame):
            out[f.name] = _df_to_records(v)
        else:
            out[f.name] = v
    # asdict would crash on the DataFrame; we override the two DF
    # fields above and the rest survive json.dumps because they're
    # already primitives / list / dict / tuple / None.
    return out


def _dict_to_result(payload: dict[str, Any]) -> bt.BacktestResult:
    """Reconstruct a BacktestResult from a saved JSON payload. Missing
    fields fall back to sensible defaults so older saves still load
    when the BacktestResult schema gains new fields."""
    kwargs: dict[str, Any] = {}
    for f in fields(bt.BacktestResult):
        present = f.name in payload
        v = payload.get(f.name)
        if f.name in ("trades", "equity_curve"):
            kwargs[f.name] = _records_to_df(v or [])
            continue
        if f.name == "hit_rate_ci" and isinstance(v, list):
            # JSON serializes tuples as lists; coerce back.
            kwargs[f.name] = tuple(v)
            continue
        # Field missing from payload (older save format) — fall back to
        # the dataclass's own default if it has one.
        if not present:
            if f.default_factory is not MISSING:  # type: ignore[misc]
                kwargs[f.name] = f.default_factory()
            elif f.default is not MISSING:
                kwargs[f.name] = f.default
            else:
                # No default — pass None and let the dataclass complain
                # if the type really requires a value.
                kwargs[f.name] = None
            continue
        kwargs[f.name] = v
    return bt.BacktestResult(**kwargs)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def save_run(
    result: bt.BacktestResult,
    *,
    params: dict[str, Any] | None = None,
    venue: str | None = None,
    eval_mode: str = "single_shot",
) -> str:
    """Persist a BacktestResult + the params used to generate it. Returns
    the run_id (also the filename stem). One JSON file per call.

    ``params`` is the human-facing knobs the Backtest page exposed
    (bankroll, kelly_multiplier, half_spread, fee_pct, lead_hours,
    history_days, n_markets, eval_step_hours if walk-forward, etc.) so
    a saved run can be reproduced or compared without ambiguity.
    """
    run_id = f"{int(time.time() * 1000)}_{result.strategy_name}"
    p = _ensure_dir() / f"{run_id}.json"
    payload = {
        "run_id": run_id,
        "saved_at": datetime.now(UTC).isoformat(),
        "venue": venue or "unknown",
        "eval_mode": eval_mode,
        "params": params or {},
        "provenance": provenance.stamp(),   # R-18: which code produced it
        "result": _result_to_dict(result),
    }
    try:
        with open(p, "w") as f:
            json.dump(payload, f, default=str)
    except (OSError, TypeError, ValueError) as e:
        log.warning("backtest_history.save_run failed: %s", e)
        return ""
    return run_id


def list_runs() -> pd.DataFrame:
    """Index DataFrame of saved runs, sorted newest-first. Cheap — only
    reads metadata (top-level keys), not the full result blob.

    Columns: ``run_id``, ``saved_at``, ``venue``, ``eval_mode``,
    ``strategy_name``, ``n_trades``, ``hit_rate``, ``roi``, ``brier_score``,
    ``market_baseline_brier``, ``beats_market_baseline``, ``total_pnl``,
    ``max_drawdown``, ``path``. The two baseline columns are None for runs
    saved before the gate was carried on BacktestResult.
    """
    d = _dir()
    if not d.exists():
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")):
        try:
            with open(path) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("list_runs: skipping malformed %s: %s", path.name, e)
            continue
        result = payload.get("result") or {}
        rows.append({
            "run_id": payload.get("run_id", path.stem),
            "saved_at": payload.get("saved_at", ""),
            "venue": payload.get("venue", "unknown"),
            "eval_mode": payload.get("eval_mode", "single_shot"),
            "strategy_name": result.get("strategy_name", ""),
            "n_trades": int(result.get("n_trades", 0) or 0),
            "hit_rate": result.get("hit_rate"),
            "roi": result.get("roi"),
            "brier_score": result.get("brier_score"),
            "market_baseline_brier": result.get("market_baseline_brier"),
            "beats_market_baseline": result.get("beats_market_baseline"),
            "total_pnl": float(result.get("total_pnl", 0.0) or 0.0),
            "max_drawdown": float(result.get("max_drawdown", 0.0) or 0.0),
            "path": str(path),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["saved_at_dt"] = pd.to_datetime(df["saved_at"], errors="coerce", utc=True)
    return df.sort_values("saved_at_dt", ascending=False).reset_index(drop=True)


def load_run(run_id: str) -> tuple[bt.BacktestResult, dict[str, Any]] | None:
    """Load a saved run by its ``run_id``. Returns ``(result, meta)``
    where ``meta`` carries ``saved_at`` / ``venue`` / ``eval_mode`` /
    ``params``. Returns None if the file isn't found or fails to parse.
    """
    p = _dir() / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        with open(p) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("load_run %s failed: %s", run_id, e)
        return None
    try:
        result = _dict_to_result(payload.get("result") or {})
    except Exception as e:
        log.warning("load_run %s: dict_to_result failed: %s", run_id, e)
        return None
    meta = {
        "saved_at": payload.get("saved_at", ""),
        "venue": payload.get("venue", "unknown"),
        "eval_mode": payload.get("eval_mode", "single_shot"),
        "params": payload.get("params", {}),
    }
    return result, meta


def delete_run(run_id: str) -> bool:
    """Delete one saved run. Returns True if a file was removed."""
    p = _dir() / f"{run_id}.json"
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError as e:
        log.warning("delete_run %s: %s", run_id, e)
        return False


def clear_history() -> int:
    """Delete every saved run. Returns the count removed."""
    d = _dir()
    if not d.exists():
        return 0
    n = 0
    for path in d.glob("*.json"):
        try:
            path.unlink()
            n += 1
        except OSError as e:
            log.warning("clear_history: failed to remove %s: %s", path.name, e)
    return n


def stats() -> dict[str, Any]:
    """Counts + age window for the Settings panel."""
    d = _dir()
    out: dict[str, Any] = {
        "runs": 0,
        "oldest_age_s": None,
        "newest_age_s": None,
        "bytes": 0,
    }
    if not d.exists():
        return out
    now = int(time.time())
    oldest: int | None = None
    newest: int | None = None
    total_bytes = 0
    for path in d.glob("*.json"):
        try:
            mtime = int(path.stat().st_mtime)
            total_bytes += path.stat().st_size
        except OSError:
            continue
        out["runs"] += 1
        if oldest is None or mtime < oldest:
            oldest = mtime
        if newest is None or mtime > newest:
            newest = mtime
    if oldest is not None:
        out["oldest_age_s"] = now - oldest
    if newest is not None:
        out["newest_age_s"] = now - newest
    out["bytes"] = total_bytes
    return out


__all__ = [
    "DEFAULT_HISTORY_DIR",
    "save_run",
    "list_runs",
    "load_run",
    "delete_run",
    "clear_history",
    "stats",
]
