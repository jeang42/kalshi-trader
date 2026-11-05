"""Per-position lifecycle metadata: entry price, opened-at, running peak.

The transaction log in ``portfolio.py`` is *what happened* — buys and sells
at point-in-time prices. That's enough to derive open positions and P&L,
but it's not enough for the agentic runner's exit logic, which needs:

- **entry_price** — the effective price we paid. Cheaper to read here than
  to re-derive from the transaction log every cycle.
- **opened_at** — UTC timestamp of the first buy. Used by ``--time-stop-days``.
- **peak_price** — the highest mark-to-market price we've seen since entry,
  on the side we're long. Used by ``--trail-stop-pct``. Has to persist
  across runner restarts or the trailing stop is reset to the current
  price every cycle.

Storage: ``data/<profile>/position_meta.json``::

    {
      "version": 1,
      "positions": {
        "<condition_id>|<outcome>": {
          "entry_price": 0.42,
          "opened_at": "2026-05-22T14:30:00+00:00",
          "peak_price": 0.71,
          "peak_at": "2026-05-22T18:15:00+00:00"
        }
      }
    }

Keyed on ``"<condition_id>|<outcome>"`` so YES and NO on the same market
can coexist. Entries are removed when the position fully closes (last
sell brings shares to 0) — handled by the runner, not this module.

Lazy file path resolution mirrors ``portfolio.py`` / ``calibration.py``:
the active profile's directory is the default; ``POLYMARKET_POSITION_META_FILE``
env var overrides for tests.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from pm.core import atomic_io

log = logging.getLogger(__name__)

VERSION = 1


def _meta_path() -> Path:
    env = os.getenv("POLYMARKET_POSITION_META_FILE")
    if env:
        return Path(env)
    from pm.core import profiles
    return profiles.profile_dir(profiles.active_profile()) / "position_meta.json"


def _empty_store() -> dict:
    return {"version": VERSION, "positions": {}}


def _normalize(data: dict | None) -> dict:
    if data is None:
        return _empty_store()
    data.setdefault("version", VERSION)
    data.setdefault("positions", {})
    return data


def _load() -> dict:
    """Read-only view; tolerates a damaged file instead of raising."""
    return _normalize(atomic_io.read_json(_meta_path(), None))


def _save(data: dict) -> None:
    """Publish atomically.

    Every mutating function in this module takes ``atomic_io.locked()`` around
    its own read-modify-write instead of calling this directly — each of them
    has a no-op path (entry already present, peak not exceeded, key absent)
    that must not cost a write, since they run for every open position every
    cycle. This store decides what a live arm believes it owns, so a lost
    update can make an arm stop managing a position it really holds.
    """
    atomic_io.write_json(_meta_path(), data)


def _key(condition_id: str, outcome: str) -> str:
    return f"{condition_id}|{outcome}"


def record_entry(
    condition_id: str,
    outcome: str,
    *,
    entry_price: float,
    opened_at: datetime | None = None,
    extra: dict | None = None,
) -> dict:
    """Record the first buy on a position. Idempotent on the same key —
    a second call updates ``entry_price`` to a cost-weighted average is
    deliberately NOT done here; the runner only calls this on initial
    open. Re-buys on top of an existing position keep the original entry
    so the trail/peak math stays interpretable.

    ``extra`` merges arbitrary strategy-specific flags into the entry
    (e.g. the longshot scalper's ``{"from_flip": True}`` so a position it
    reversed into doesn't flip a second time). Reserved keys
    (``entry_price`` / ``opened_at`` / ``peak_price`` / ``peak_at``) are
    not overridable via ``extra``.
    """
    opened_at = opened_at or datetime.now(UTC)
    k = _key(condition_id, outcome)
    path = _meta_path()
    with atomic_io.locked(path):
        data = _normalize(atomic_io.read_json(path, None))
        # Idempotent on (condition_id, outcome): a re-buy must NOT reset the
        # original entry price or peak, or trail/stop maths stops meaning
        # anything. Return without writing — this path is hit on every re-buy.
        if k in data["positions"]:
            return data["positions"][k]
        entry = {
            "entry_price": float(entry_price),
            "opened_at": opened_at.isoformat(),
            "peak_price": float(entry_price),
            "peak_at": opened_at.isoformat(),
            # Two-stage take-profit: once the TP threshold is hit, this
            # flips to True and subsequent cycles use a tighter trailing
            # stop instead of an instant sell. Lets winners run while
            # still capping the giveback. See ExitPolicy.tp_to_trail_stop_pct.
            "tp_armed": False,
        }
        if extra:
            reserved = {"entry_price", "opened_at", "peak_price", "peak_at"}
            for kk, vv in extra.items():
                if kk not in reserved:
                    entry[kk] = vv
        data["positions"][k] = entry
        atomic_io.write_json(path, data)
    return entry


def update_peak(
    condition_id: str,
    outcome: str,
    *,
    current_price: float,
    now: datetime | None = None,
) -> dict | None:
    """Update the running peak for a position. Returns the (possibly
    updated) entry dict, or None if no entry exists for the key.

    Peak is one-directional: it only ratchets up, never down. That's
    what makes a trailing stop a *trailing* stop — once the high-water
    mark is set, the exit triggers when price drops X% below it.
    """
    k = _key(condition_id, outcome)
    path = _meta_path()
    with atomic_io.locked(path):
        data = _normalize(atomic_io.read_json(path, None))
        entry = data["positions"].get(k)
        if entry is None:
            return None
        # Ratchet only — and only write when the high-water mark actually
        # moved, since this is called for every open position every cycle.
        if current_price > entry["peak_price"]:
            entry["peak_price"] = float(current_price)
            entry["peak_at"] = (now or datetime.now(UTC)).isoformat()
            atomic_io.write_json(path, data)
    return entry


def get_entry(condition_id: str, outcome: str) -> dict | None:
    """Return the meta entry for a position, or None."""
    return _load()["positions"].get(_key(condition_id, outcome))


def arm_tp(condition_id: str, outcome: str) -> dict | None:
    """Mark a position as having armed the two-stage take-profit. After
    this, the exit-management phase uses ``tp_to_trail_stop_pct`` for
    its trailing stop instead of firing an instant sell. Idempotent.
    Returns the updated entry, or None if no entry exists."""
    k = _key(condition_id, outcome)
    path = _meta_path()
    with atomic_io.locked(path):
        data = _normalize(atomic_io.read_json(path, None))
        entry = data["positions"].get(k)
        if entry is None:
            return None
        if not entry.get("tp_armed", False):
            entry["tp_armed"] = True
            atomic_io.write_json(path, data)
    return entry


def set_field(condition_id: str, outcome: str, key: str, value) -> dict | None:
    """Attach/update an arbitrary (non-reserved) field on an existing entry —
    e.g. the live cash-out limit's ``tp_order_id`` so the exit-coordination
    phase can cancel or reconcile it. Returns the updated entry, or None if
    the entry doesn't exist. Reserved lifecycle keys can't be clobbered."""
    reserved = {"entry_price", "opened_at", "peak_price", "peak_at"}
    if key in reserved:
        raise ValueError(f"{key!r} is a reserved lifecycle field")
    k = _key(condition_id, outcome)
    path = _meta_path()
    with atomic_io.locked(path):
        data = _normalize(atomic_io.read_json(path, None))
        entry = data["positions"].get(k)
        if entry is None:
            return None
        entry[key] = value
        atomic_io.write_json(path, data)
    return entry


def clear_entry(condition_id: str, outcome: str) -> bool:
    """Remove the meta entry on full close. Returns True if removed."""
    k = _key(condition_id, outcome)
    path = _meta_path()
    with atomic_io.locked(path):
        data = _normalize(atomic_io.read_json(path, None))
        if k not in data["positions"]:
            return False
        del data["positions"][k]
        atomic_io.write_json(path, data)
    return True


def all_entries() -> dict[str, dict]:
    """Return every tracked position's meta as ``{key: entry_dict}``.
    Caller-side dict; mutation does not write back."""
    return dict(_load()["positions"])
