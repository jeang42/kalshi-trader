"""Sidecar protocol — the shared shape every sidecar engine implements.

A *sidecar* is a domain-isolated sub-package that runs on its own schedule,
maintains its own state, and exposes a narrow public surface to the market
app. Examples (planned or in progress):

* ``baseball/`` — MLB team-strength ratings + matchup model
* ``news/`` — RSS poller + relevance store
* ``nba/``, ``nfl/``, ``nhl/`` — same shape as baseball
* ``politics/`` — polling aggregator
* ``macro/`` — economic-indicator forecaster (could fold the existing
  macro_data.py into this shape eventually)

Why a protocol
--------------
Sidecars share an *operational* surface (am I healthy? what have I done
recently? how much data do I hold?) even though their *domain* surfaces
diverge wildly. The market app's control-plane UI and any future
"tie-them-together" features (chat interface, cross-sidecar queries)
need a uniform way to introspect them without knowing what each one does.

This file is the canonical place for that shared shape. The control-plane
page (`pages/sidecars.py`, not yet built) iterates the registry, calls
the three operational methods, and renders generically.

Domain surface is *not* in the protocol
---------------------------------------
``predict_game(home, away, date)`` lives on the baseball sidecar.
``recent_since(timestamp, tags)`` lives on the news sidecar. They are
unique to each sidecar and the market app calls them directly via the
sub-package's public API, not through this protocol. The protocol is
exclusively for the operational layer that's the same across all engines.

Registration
------------
Sidecars register at import time via :func:`register_sidecar`. The
market app imports the sub-package once (typically in ``app.py`` startup)
and the side-effect populates the registry. Mirrors the pattern in
``backtest.register_strategy`` + ``provider.register_provider``.

Adding a new sidecar
--------------------
1. Create the sub-package directory: ``mysport/``.
2. Implement the operational methods in ``mysport/__init__.py``:

   .. code-block:: python

       from pm.core.sidecars import register_sidecar, SidecarHealth

       NAME = "mysport"
       DISPLAY_NAME = "My Sport"

       def health() -> SidecarHealth: ...
       def stats() -> dict: ...
       def recent_activity(limit: int = 20) -> list[dict]: ...

       register_sidecar(__name__)  # picks up NAME / DISPLAY_NAME / fns from module

3. Add the import to ``app.py`` (or wherever startup registration lives).
4. Domain surface (``predict_game`` / etc.) is up to you — not constrained
   by this protocol.

A sidecar that hasn't been wired up yet still works as a Python module;
it just doesn't appear in the control plane until ``register_sidecar``
is called.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# ─── Operational data shapes ──────────────────────────────────────────────


@dataclass
class SidecarHealth:
    """Health snapshot returned by ``health()``.

    The control plane derives a green/yellow/red badge from ``status``;
    detailed fields drive the per-sidecar drawer.

    Fields
    ------
    status:
        ``"ok"`` / ``"degraded"`` / ``"down"``. ``"degraded"`` means the
        sidecar is still answering queries but has problems (e.g. upstream
        API failing, stale data). ``"down"`` means callers should not
        trust its outputs.
    last_update_ts:
        When the sidecar's nightly/periodic runner last completed a
        successful update. Stale-data badges compare this to expected
        cadence. ``None`` if never run.
    next_update_ts:
        Best-effort estimate of when the next refresh will land. Used
        only for display; control plane will not crash if absent.
    errors_24h:
        Count of errors in the last 24 hours. Sidecars are free to define
        what counts (failed fetches, model exceptions, schema mismatches).
        Used to surface trouble before it cascades.
    detail:
        Free-form one-line description for humans. Shown in the badge
        tooltip. Examples: "MLB API responsive, 30 teams rated",
        "FRED unreachable for 4h — using cached values".
    """

    status: str  # "ok" | "degraded" | "down"
    last_update_ts: datetime | None = None
    next_update_ts: datetime | None = None
    errors_24h: int = 0
    detail: str = ""


@dataclass
class SidecarActivityEvent:
    """One line in the sidecar's recent-activity feed.

    Sidecars are responsible for their own activity log (typically a
    bounded ring in SQLite or a tail of a log file). ``recent_activity``
    just deserializes the most-recent N entries.

    Fields
    ------
    ts: when the event happened.
    kind: short tag — ``"fetch"`` / ``"compute"`` / ``"error"`` / etc.
        Free-form per sidecar. Control plane uses it only for icon choice.
    message: human-readable one-liner.
    detail: optional dict for drill-down. Schema is sidecar-specific.
    """

    ts: datetime
    kind: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


# ─── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class Sidecar(Protocol):
    """Shape every sidecar implements.

    Used as a structural type — implement these by exposing module-level
    attributes (the ``register_sidecar`` helper wires the module itself
    in as the Sidecar instance via duck-typing). No base class to inherit;
    a sub-package's ``__init__.py`` IS the Sidecar.

    Stability promises
    ------------------
    * ``name`` and ``display_name`` are stable identifiers. ``name`` is
      used as a registry key and in storage paths; don't rename without
      a migration.
    * ``health()`` must be cheap to call — control plane polls it on every
      page render. No HTTP, no DB scans larger than O(1). If you need to
      compute health from expensive data, cache it.
    * ``stats()`` and ``recent_activity()`` are allowed to do small reads
      from the sidecar's own state store. They should still return in
      well under 1 second for a smooth UI.
    * None of these methods may raise. Catch and convert to status=down.
    """

    name: str
    display_name: str

    def health(self) -> SidecarHealth: ...
    def stats(self) -> dict[str, Any]: ...
    def recent_activity(self, limit: int = 20) -> list[SidecarActivityEvent]: ...


def resolve_pending_tickers(tickers: list[str]) -> dict[str, bool]:
    """**Optional** Sidecar method — sidecars MAY implement this to
    short-circuit slow venue settlement.

    Many venues (Kalshi MLB markets, e.g.) keep a market `open` for days
    after the underlying event has resolved, as a settlement buffer.
    Forecasts on those markets sit in `pending` state during that
    window. A sidecar that already knows the answer — the baseball
    sidecar knows the final score the moment MLB does — can resolve
    forecasts much earlier than the venue's `get_closed_markets` feed.

    Signature
    ---------
    Given a list of venue tickers, return a ``{ticker: resolved_yes}``
    dict containing only the tickers this sidecar can confidently
    resolve. Sidecars MUST NOT guess — when in doubt, omit the ticker
    from the response. The auto_resolve_runner will use this as an
    overlay on top of the public ``get_closed_markets`` feed and
    venue-side settlements.

    Stability + safety
    ------------------
    * Sidecars MUST treat this as read-only. No state mutation.
    * Sidecars MUST return ``bool`` for ``resolved_yes``, never None
      or anything ambiguous.
    * If the sidecar can't resolve a ticker (game not final, ticker
      doesn't match the domain, state not loaded), omit it.
    * The method MUST NOT raise. Catch + return ``{}`` on any failure.
    """
    # This module-level function is a STUB documenting the protocol
    # shape. The actual implementation lives per-sidecar — see
    # ``baseball.resolve_pending_tickers`` for the canonical example.
    # auto_resolve_runner calls ``getattr(sidecar, "resolve_pending_tickers", None)``
    # at runtime rather than relying on the static Protocol typing,
    # so sidecars that don't implement this stay forward-compatible.
    return {}


# ─── Registry ─────────────────────────────────────────────────────────────


_REGISTRY: dict[str, Sidecar] = {}


def register_sidecar(module_or_name: Any) -> None:
    """Register a sidecar with the control plane.

    Accepts either a module (typical — pass ``__name__`` from inside the
    sub-package's ``__init__.py`` and the module is looked up via
    :mod:`importlib`) or any object that satisfies the :class:`Sidecar`
    protocol.

    Re-registering an existing ``name`` overwrites it — that's intentional
    so re-importing a sub-package during development just refreshes the
    entry rather than dying on a duplicate-key error.
    """
    sidecar: Any
    if isinstance(module_or_name, str):
        sidecar = importlib.import_module(module_or_name)
    else:
        sidecar = module_or_name

    name = getattr(sidecar, "name", None) or getattr(sidecar, "NAME", None)
    display_name = (
        getattr(sidecar, "display_name", None)
        or getattr(sidecar, "DISPLAY_NAME", None)
        or name
    )
    if not name:
        raise ValueError(
            f"register_sidecar({sidecar!r}): module must define `name` or `NAME`"
        )
    for required in ("health", "stats", "recent_activity"):
        if not callable(getattr(sidecar, required, None)):
            raise ValueError(
                f"register_sidecar({name!r}): missing required callable "
                f"`{required}`"
            )
    # Coerce module to a duck-typed Sidecar by attaching the conventional
    # lowercase attrs if only the SHOUTY_CASE ones were defined.
    if not getattr(sidecar, "name", None):
        sidecar.name = name
    if not getattr(sidecar, "display_name", None):
        sidecar.display_name = display_name

    _REGISTRY[name] = sidecar
    log.info("registered sidecar %r (%s)", name, display_name)


def list_sidecars() -> list[Sidecar]:
    """All registered sidecars, sorted by display_name for stable UI ordering."""
    return sorted(_REGISTRY.values(), key=lambda s: s.display_name)


def get_sidecar(name: str) -> Sidecar | None:
    """Look up a sidecar by ``name``. Returns ``None`` if not registered."""
    return _REGISTRY.get(name)


def _now_utc() -> datetime:
    """Current UTC timestamp — exported for sidecars to use in their own
    activity events so timestamps stay comparable across the system."""
    return datetime.now(tz=UTC)


# ─── Helpers for sidecar authors ──────────────────────────────────────────


def safe_health(fn: Callable[[], SidecarHealth]) -> SidecarHealth:
    """Wrap a health-computing function so exceptions become status=down
    rather than propagating. Sidecars are expected to use this in their
    ``health()`` implementations:

        def health() -> SidecarHealth:
            return safe_health(_compute_real_health)
    """
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — explicit catch-all is the point
        log.exception("sidecar health probe raised")
        return SidecarHealth(
            status="down",
            detail=f"health probe failed: {type(e).__name__}: {e}",
        )
