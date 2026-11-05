"""Emergency stop — single-file flag that halts new actions across
every runner / order-placement path in the system.

Spec: drop `data/.emergency_stop` to halt; delete it to resume. One file
to create / delete to control the whole system in a panic — no env
edits, no service restarts, no per-process state.

Scope (what "halts" means):

    HALTED (refuses to take new actions):
      - kalshi_orders.place_limit_order        — refuses with OrderRejected
      - strategy_runner.run_cycle              — short-circuits, no opens
      - Streamlit Buy/Sell buttons             — disabled with banner
      - Future: trigger-rule firing            — disarmed

    KEEPS RUNNING (read-only bookkeeping):
      - book_poller, mlb_book_poller           — pure data collection
      - baseball.live                          — pure data collection
      - auto_resolve_runner                    — records resolutions for
                                                  ALREADY-finalized markets
                                                  (no new positions opened)
      - bulk_deep_analysis                     — produces forecasts only
                                                  (no trades fire from this)

The distinction: read-only data ingestion and bookkeeping continue so
we don't lose history; anything that opens NEW positions or sends NEW
orders refuses.

Storage: ``data/.emergency_stop``. Two-line text file:

    <ISO timestamp of activation>
    <optional reason string>

Env override: ``POLYMARKET_EMERGENCY_STOP_FILE``.

Public surface::

    is_active() -> bool                                  # cheap; safe to call frequently
    activate(reason: str | None = None) -> Path          # creates the file
    deactivate() -> bool                                 # removes the file
    get_status() -> {"active", "since", "reason"} | None # full state for UI

CLI: ``python -m emergency_stop {status|activate|deactivate} [--reason ...]``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pm import REPO_ROOT
from pm.core import logctx

log = logging.getLogger(__name__)

# Anchored on the REPO root, not this file: `Path(__file__).parent` silently
# became pm/core/ when R-24 moved the module, which would have split the kill
# switch — operators writing the flag at one path, old-code daemons reading
# another. On a money path, a wrong flag location means "stop" doesn't stop.
DEFAULT_PATH = REPO_ROOT / "data" / ".emergency_stop"


def _flag_path() -> Path:
    env = os.getenv("POLYMARKET_EMERGENCY_STOP_FILE")
    if env:
        return Path(env)
    return DEFAULT_PATH


def is_active() -> bool:
    """True iff the flag file exists. Designed to be cheap (single
    filesystem stat) so callers can check this at the top of every
    cycle without measurable overhead."""
    try:
        return _flag_path().exists()
    except OSError:
        # Filesystem error — fail SAFE (treat as active) so a broken
        # disk doesn't let us keep sending orders.
        log.warning("emergency_stop: could not stat flag file; "
                    "treating as ACTIVE for safety")
        return True


def activate(reason: str | None = None) -> Path:
    """Create the flag file. Writes the activation timestamp and an
    optional reason. Idempotent — re-activating updates the timestamp
    and reason rather than failing.

    Returns the flag file path. Callers should log the path so the
    audit trail makes the source obvious."""
    path = _flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    body = ts + "\n" + (reason or "").strip() + "\n"
    path.write_text(body)
    log.warning(
        "EMERGENCY STOP ACTIVATED at %s — %s (flag: %s)",
        ts, reason or "(no reason given)", path,
    )
    return path


def deactivate() -> bool:
    """Remove the flag file. Returns True if a file was actually
    removed, False if no flag was present. Idempotent."""
    path = _flag_path()
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as e:
        log.error("emergency_stop: could not remove flag file %s: %s",
                  path, e)
        return False
    log.warning("EMERGENCY STOP DEACTIVATED (flag removed: %s)", path)
    return True


def get_status() -> dict | None:
    """Full state for UI consumption. Returns None when not active;
    otherwise ``{"active": True, "since": "...", "reason": "..."}``.

    Parsing is lenient — a flag file with malformed contents is still
    considered active, just with empty timestamp/reason."""
    path = _flag_path()
    if not path.exists():
        return None
    since = ""
    reason = ""
    try:
        text = path.read_text()
        lines = text.splitlines()
        if lines:
            since = lines[0].strip()
        if len(lines) > 1:
            reason = "\n".join(lines[1:]).strip()
    except OSError:
        pass  # fall through with empty fields
    return {"active": True, "since": since, "reason": reason}


# ---------------------------------------------------------------------------
# Integration helpers — small wrappers callers can use without bloating
# their own modules with the "if active: raise / return" boilerplate.
# ---------------------------------------------------------------------------


class EmergencyStopActive(RuntimeError):
    """Raised by ``check_or_raise`` when the emergency stop is active.

    Catch this specifically when an order-placement path needs to
    differentiate "the system is locked down" from "the venue rejected
    the order." Inherits ``RuntimeError`` so a generic ``except
    Exception`` still catches it."""


def check_or_raise(*, context: str = "action") -> None:
    """Raise ``EmergencyStopActive`` if the flag is set. Use this at the
    top of any function that opens a new position, sends an order, or
    fires a trigger.

    ``context`` is included in the exception message so the audit log
    shows which subsystem was blocked."""
    if is_active():
        st = get_status() or {}
        raise EmergencyStopActive(
            f"emergency stop is active — refusing {context}. "
            f"since={st.get('since', '?')} reason={st.get('reason') or '(none)'}",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_status() -> int:
    st = get_status()
    if st is None:
        print(f"emergency stop: NOT ACTIVE (flag would be at {_flag_path()})")
        return 0
    print("⚠️  EMERGENCY STOP ACTIVE")
    print(f"   since:  {st['since']}")
    print(f"   reason: {st['reason'] or '(none given)'}")
    print(f"   flag:   {_flag_path()}")
    return 0


def _cli_activate(reason: str | None) -> int:
    path = activate(reason)
    print("⚠️  EMERGENCY STOP ACTIVATED")
    print(f"   flag: {path}")
    print(f"   reason: {reason or '(none given)'}")
    print()
    print("Effects:")
    print("  • strategy_runner cycles will short-circuit (no new opens)")
    print("  • kalshi_orders.place_limit_order will refuse")
    print("  • Streamlit Buy/Sell paths will refuse")
    print("  • Data-collection daemons keep running (read-only)")
    print("  • auto_resolve_runner keeps running (resolution bookkeeping)")
    print()
    print("To resume: python -m emergency_stop deactivate")
    return 0


def _cli_deactivate() -> int:
    if deactivate():
        print("✅  emergency stop deactivated")
        return 0
    print("emergency stop was not active; nothing to do")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Manage the emergency-stop flag. Halts new orders + "
                    "new strategy opens across the whole system; leaves "
                    "data collection running.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status",
                   help="Print current state.")
    p_act = sub.add_parser("activate",
                            help="Halt new actions system-wide.")
    p_act.add_argument("--reason", default=None,
                        help="Free-text reason recorded in the flag file "
                              "and shown in the UI banner.")
    sub.add_parser("deactivate",
                   help="Remove the flag; resume normal operation.")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())

    if args.cmd == "status":
        return _cli_status()
    if args.cmd == "activate":
        return _cli_activate(args.reason)
    if args.cmd == "deactivate":
        return _cli_deactivate()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
