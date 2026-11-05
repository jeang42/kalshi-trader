"""Cooperative shutdown for the daemons (P2 / R-20).

Before this, no daemon handled signals. ``systemctl stop`` sent SIGTERM and
Python died wherever it was — and "wherever it was" could be between
``kalshi_orders.place_limit_order`` returning and the arm recording the fill
(the arm's ``UPDATE positions``, ``position_meta.record_entry``, the exits
log). The venue then holds a real position that nothing on this side knows
about. ``LiveBroker._reconcile_after_error`` covers the case where the
*submission* raises; it cannot cover the process vanishing after the
submission succeeded.

Three primitives, all cheap and safe to call when :func:`install` was never
run (single-cycle CLI use, tests):

``install()``
    Register handlers for SIGTERM and SIGINT in the daemon's main().
    First signal → set the stop flag, log, return (the loop notices).
    Second signal → escalate: log and ``raise SystemExit`` — the operator
    hit Ctrl-C twice, or systemd escalated; honour it.

``requested()`` / ``sleep(seconds)``
    What every loop checks between cycles AND between per-market steps
    (a capture cycle can run far longer than systemd's 90 s stop timeout).
    ``sleep`` returns early, True, when a stop was requested.

``deferred(label)``
    Context manager for the place-then-record window. A signal arriving
    inside it is *recorded, not acted on*; on exit the stop flag is set and
    the loop winds down normally. Re-entrant (an arm's canary wrapper
    contains ``LiveBroker._place``'s own). This is a mask, not a guarantee:
    SIGKILL still kills. It closes the window we control.

Signal handlers run on the main thread only; every daemon here is
single-threaded per process. ``kalshi_ws`` (asyncio) is not wired.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)

_stop = threading.Event()
_installed = False
_deferred_depth = 0
_pending: int | None = None      # signal that arrived while deferred
_signals_seen = 0


def _name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover
        return str(signum)


def _handler(signum, _frame) -> None:
    global _pending, _signals_seen
    _signals_seen += 1
    if _signals_seen >= 2:
        log.warning("shutdown: second signal (%s) — exiting NOW", _name(signum))
        raise SystemExit(128 + int(signum))
    if _deferred_depth > 0:
        _pending = signum
        log.warning("shutdown: %s received inside a critical section — "
                    "deferred until it completes", _name(signum))
        return
    _stop.set()
    log.warning("shutdown: %s received — finishing the current step, then exiting",
                _name(signum))


def install(signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)) -> None:
    """Register the handlers. Idempotent. Call once from a daemon's main()."""
    global _installed
    for s in signals:
        signal.signal(s, _handler)
    _installed = True


def requested() -> bool:
    """True once a stop has been requested (and not deferred)."""
    return _stop.is_set()


def sleep(seconds: float) -> bool:
    """Sleep up to ``seconds``; return True (early) if a stop was requested."""
    return _stop.wait(max(0.0, float(seconds)))


@contextmanager
def deferred(label: str = "critical section") -> Iterator[None]:
    """Mask stop-signals across a place-then-record window (re-entrant)."""
    global _deferred_depth, _pending
    _deferred_depth += 1
    try:
        yield
    finally:
        _deferred_depth -= 1
        if _deferred_depth == 0 and _pending is not None:
            sig, _pending = _pending, None
            _stop.set()
            log.warning("shutdown: %s completed; honouring deferred %s — "
                        "exiting after this step", label, _name(sig))


def reset() -> None:
    """Tests only: forget any requested stop / pending signal."""
    global _pending, _signals_seen, _deferred_depth
    _stop.clear()
    _pending = None
    _signals_seen = 0
    _deferred_depth = 0
