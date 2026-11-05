"""Structured logging + correlation IDs (P3/R-23).

The problem this solves: one logical operation — an arm cycle, a live order —
produces log lines across several modules (`pm.arms.fade_nearclose` →
`pm.trading.execution` → `pm.venue.kalshi_orders`) plus an `orders.log` audit
row, and nothing ties them together. When a cycle misbehaves, "which lines
belong to THIS cycle" is reconstructed by timestamp squinting.

Design, deliberately stdlib-only (no structlog — one more dependency is not
worth it for what a `ContextVar` + a `logging.Filter` already do):

- ``bind(**fields)`` — context manager that adds correlation fields for the
  duration of a block. Nests additively; always restores on exit. Backed by a
  ``ContextVar``, so it is safe across threads and async tasks — each context
  sees its own bindings, never a sibling's.
- ``new_id(prefix)`` — short unique id, e.g. ``cyc-3f2a8b1c``.
- ``current()`` — snapshot of the active fields, for embedding in places that
  are already structured (the ``orders.log`` audit rows).
- ``setup()`` — the one logging config call every entry point should use
  instead of hand-rolling ``logging.basicConfig`` (25 slightly-different
  copies existed when this landed). Text mode appends ``[k=v ...]`` to lines
  only when context is bound; ``POLYMARKET_LOG_FORMAT=json`` switches the
  same handler to one-JSON-object-per-line for machine consumption.

What is deliberately NOT here: cross-process propagation (the daemons are
single-process; the control plane reads stores, not logs) and any change to
``health_events.jsonl`` / ``orders.log`` framing (both were already
structured; audit rows now *embed* ``ctx`` — see ``kalshi_orders._audit``).

Usage in a daemon cycle::

    from pm.core import logctx
    with logctx.bind(arm="fade_nearclose", cycle=logctx.new_id("cyc")):
        run_cycle()          # every log line in here carries [arm=… cycle=…]

>>> with bind(a="1"):
...     with bind(b="2"):
...         sorted(current().items())
[('a', '1'), ('b', '2')]
>>> current()
{}
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# default=None, not {}: a mutable ContextVar default is shared across every
# context that never called set() (ruff B039). Accessors treat None as {}.
_ctx: ContextVar[dict[str, str] | None] = ContextVar("polymarket_log_ctx", default=None)

TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s%(ctx)s"
DATE_FORMAT = "%H:%M:%S"


def new_id(prefix: str) -> str:
    """Short unique correlation id.

    >>> cid = new_id("cyc")
    >>> cid.startswith("cyc-") and len(cid) == 4 + 8
    True
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def current() -> dict[str, str]:
    """Snapshot of the currently-bound correlation fields ({} outside bind)."""
    return dict(_ctx.get() or {})


@contextlib.contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Add correlation fields for the duration of the block. Values are
    stringified once here so a non-str (an int count, a Path) can't break a
    formatter later. Restores the previous context on exit, exceptions
    included."""
    merged = {**(_ctx.get() or {}), **{k: str(v) for k, v in fields.items()}}
    token = _ctx.set(merged)
    try:
        yield
    finally:
        _ctx.reset(token)


def scope(_id_field: str = "cycle", _id_prefix: str = "cyc", **fields: Any):
    """Decorator: run the function under ``bind(**fields, <id_field>=fresh id)``.

    Made for the arms' ``run_cycle`` — every invocation gets a new cycle id
    without re-indenting a 200-line body::

        @logctx.scope(arm="fade_nearclose")
        def run_cycle(...): ...

    >>> @scope(arm="x")
    ... def f():
    ...     return sorted(current())
    >>> f()
    ['arm', 'cycle']
    """
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*args: Any, **kwargs: Any):
            with bind(**fields, **{_id_field: new_id(_id_prefix)}):
                return fn(*args, **kwargs)
        return wrapped
    return deco


class ContextFilter(logging.Filter):
    """Injects the bound context into every record passing the handler.

    ``record.ctx`` is the TEXT suffix ('' when nothing is bound — so lines
    outside any bind() look exactly as they did before R-23), and
    ``record.ctx_dict`` is the raw dict for the JSON formatter. Attached to
    the HANDLER, not a logger, so third-party records (urllib3, streamlit)
    get the attributes too and the format string never KeyErrors.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _ctx.get() or {}
        record.ctx_dict = ctx
        record.ctx = (
            " [" + " ".join(f"{k}={v}" for k, v in ctx.items()) + "]"
        ) if ctx else ""
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Correlation fields land under ``ctx`` so a
    field named ``msg`` or ``level`` can never collide with the envelope."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx_dict", None)
        if ctx:
            out["ctx"] = ctx
        if record.exc_info and record.exc_info[0] is not None:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(level: str | int | None = None, *, stream: Any = None) -> None:
    """Configure root logging the project way. Replaces per-entry-point
    ``logging.basicConfig`` calls.

    - level: explicit, else ``POLYMARKET_LOG_LEVEL`` env, else INFO.
    - ``POLYMARKET_LOG_FORMAT=json`` → JSONL to the same stream; anything
      else → the standard text format with the ``[k=v]`` context suffix.
    - Idempotent in the way basicConfig is: if the root logger already has
      handlers it only adjusts the level (so a daemon calling setup() after
      app-style init doesn't double-log), matching basicConfig's contract.
    """
    if level is None:
        level = os.getenv("POLYMARKET_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(ContextFilter())
    if os.getenv("POLYMARKET_LOG_FORMAT", "").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
