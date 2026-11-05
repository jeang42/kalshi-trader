"""Circuit breakers on the outbound connectors (P2 / R-21).

Before this, a persistently failing dependency was retried every cycle,
forever: each ``_get`` did its own 3 retries with backoff, failed, the arm
logged "discovery failed", slept its interval, and did it all again — for
hours if the venue was down, and, in a 429 storm, making the storm worse
(seven daemons restarted at once on 2026-08-16 did exactly that). Nothing
told anyone; the log just filled.

One :class:`Breaker` per dependency (``"kalshi"``, ``"espn"``, ``"ollama"``,
…), wrapping that connector's single HTTP entry point:

* **closed** — calls pass; consecutive dependency failures are counted.
* **open** — after :data:`FAILURE_THRESHOLD` consecutive failures. Calls
  fail FAST with :class:`CircuitOpenError` for :data:`COOLDOWN_S` seconds.
  ``CircuitOpenError`` subclasses ``requests.RequestException`` on purpose:
  every existing ``except RequestException`` / ``except Exception`` in the
  arms and the UI already handles it, as "the dependency is unavailable".
* **half-open** — after the cooldown, ONE call is let through as a probe.
  Success closes the circuit; failure re-opens it for another cooldown.

What counts as a dependency failure: connection errors, timeouts, HTTP 5xx,
HTTP 429 (after the connector's own Retry-After handling gave up), and a
body that isn't the JSON it claims to be. A 4xx other than 429 is a healthy
dependency saying no to *this* request — it resets the count.

State transitions are logged ONCE each and written to the health log via
``notify.record`` (``circuit_open`` / ``circuit_closed``), which is how
``health_digest`` — a separate process — sees them: breaker state itself is
per-process, in memory. Money paths fail closed under an open circuit
(``kalshi_orders`` cannot place; ``LiveBroker`` records an error result),
which is the direction we want.

Env: ``CIRCUIT_FAILURE_THRESHOLD`` (default 5), ``CIRCUIT_COOLDOWN_S``
(default 300). Tests: :func:`reset_all` in the root ``conftest``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import requests

log = logging.getLogger(__name__)

FAILURE_THRESHOLD = 5
COOLDOWN_S = 300.0

T = TypeVar("T")


class CircuitOpenError(requests.exceptions.RequestException):
    """The dependency's circuit is open; the call was not attempted."""


def _threshold() -> int:
    try:
        return max(1, int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", FAILURE_THRESHOLD)))
    except ValueError:
        return FAILURE_THRESHOLD


def _cooldown() -> float:
    try:
        return max(1.0, float(os.getenv("CIRCUIT_COOLDOWN_S", COOLDOWN_S)))
    except ValueError:
        return COOLDOWN_S


def is_dependency_failure(exc: BaseException) -> bool:
    """Does this exception mean the DEPENDENCY failed (vs. this request)?

    >>> import requests
    >>> is_dependency_failure(requests.ConnectionError("x"))
    True
    >>> is_dependency_failure(requests.Timeout("x"))
    True
    >>> r = requests.Response(); r.status_code = 404
    >>> is_dependency_failure(requests.HTTPError(response=r))
    False
    >>> r.status_code = 503
    >>> is_dependency_failure(requests.HTTPError(response=r))
    True
    >>> r.status_code = 429
    >>> is_dependency_failure(requests.HTTPError(response=r))
    True
    >>> is_dependency_failure(ValueError("not json"))
    True
    >>> is_dependency_failure(CircuitOpenError("open"))
    False
    """
    if isinstance(exc, CircuitOpenError):
        return False
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
        if code is None:
            return True
        return code == 429 or code >= 500
    if isinstance(exc, requests.RequestException | ValueError | OSError):
        return True
    return False


class Breaker:
    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._opens = 0
        self._last_error = ""

    # ---- introspection -------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            remaining = 0.0
            if self._state == "open":
                remaining = max(0.0, self._opened_at + _cooldown() - time.monotonic())
            return {"name": self.name, "state": self._state,
                    "consecutive_failures": self._failures, "opens": self._opens,
                    "cooldown_remaining_s": round(remaining, 1),
                    "last_error": self._last_error}

    # ---- protocol ---------------------------------------------------------
    def before_call(self) -> None:
        """Raise CircuitOpenError if open; flip to half-open after cooldown."""
        with self._lock:
            if self._state != "open":
                return
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= _cooldown():
                self._state = "half_open"
                log.info("circuit %s: cooldown over — letting one probe through", self.name)
                return
            raise CircuitOpenError(
                f"{self.name}: circuit open after {self._failures} consecutive failures "
                f"(retry in {_cooldown() - elapsed:.0f}s; last: {self._last_error})")

    def record_success(self) -> None:
        with self._lock:
            was = self._state
            self._state, self._failures = "closed", 0
        if was != "closed":
            log.warning("circuit %s: CLOSED — dependency is back", self.name)
            _health("circuit_closed", f"{self.name} recovered", severity="info",
                    breaker=self.name)

    def record_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            trip = self._state == "half_open" or self._failures >= _threshold()
            newly = trip and self._state != "open"
            if trip:
                self._state, self._opened_at = "open", time.monotonic()
                if newly:
                    self._opens += 1
            n, last = self._failures, self._last_error
        if newly:
            log.warning("circuit %s: OPEN after %d consecutive failures — failing fast "
                        "for %.0fs (last: %s)", self.name, n, _cooldown(), last)
            _health("circuit_open", f"{self.name}: {n} consecutive failures; {last}",
                    severity="warning", breaker=self.name)

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` under the breaker. The one wrapper every connector uses."""
        self.before_call()
        try:
            out = fn(*args, **kwargs)
        except BaseException as e:
            if is_dependency_failure(e):
                self.record_failure(e)
            elif isinstance(e, requests.HTTPError):
                self.record_success()      # a 4xx: the dependency answered
            raise
        self.record_success()
        return out


def _health(event: str, detail: str, *, severity: str, breaker: str) -> None:
    try:
        from pm.core import notify
        notify.record(event, detail, severity=severity, extra={"breaker": breaker})
    except Exception:  # noqa: BLE001 — a breaker must never raise on bookkeeping
        pass


_registry: dict[str, Breaker] = {}
_registry_lock = threading.Lock()


def get(name: str) -> Breaker:
    with _registry_lock:
        b = _registry.get(name)
        if b is None:
            b = _registry[name] = Breaker(name)
        return b


def status() -> list[dict[str, Any]]:
    """Every breaker this process has touched, for the control plane / CLI."""
    with _registry_lock:
        return [b.snapshot() for b in _registry.values()]


def reset_all() -> None:
    """Tests only."""
    with _registry_lock:
        _registry.clear()
