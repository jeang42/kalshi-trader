"""What is armed right now — the single authoritative answer.

Why this exists
---------------
Whether this system can place a real order depends on a chain of gates spread
across three modules, and the chain resolves differently depending on *which
process asks*: ``kalshi_auth.authorized_profiles()`` compares against the
**active profile**, and each daemon pins ``POLYMARKET_ACTIVE_PROFILE`` to its
own profile for the duration of its cycle. So the obvious sanity check —
running a snippet in a shell — reports every profile as paper while the daemons
are live. That is exactly how a live canary ran for a week while the operating
notes described it as dormant.

This module resolves the chain the way a daemon does, for every profile, and
prints it::

    python -m live_gates            # human table
    python -m live_gates --json     # machine-readable
    python -m live_gates --quiet    # exit code only

Exit code is 0 when the resolved state is self-consistent, 1 when it is not
(e.g. the owner profile names a profile that does not exist). Read-only: it
places nothing, and imports no module that can.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field

# Arm-specific "actually place orders" flags. Allow-list membership is
# necessary but never sufficient — each arm carries its own switch on top, and
# reading the allow-list alone overstates what is armed.
ARM_FLAGS: dict[str, tuple[str, str]] = {
    # agentic_fade_nearclose's canary went with the arm when fade_nearclose
    # (and back_nearclose) were retired to attic/ on 2026-08-24 — the
    # candle_panel retrospective showed the band mispricing gone (+0.4pp on
    # 14,400 markets), so there is no paper edge left to measure fills
    # against. The profile holds closed evidence and can place nothing.
    # agentic_fade_retail's canary (FADE_CANARY_ENABLED) went with the arm
    # when fade_retail_longshot was retired to attic/ on 2026-08-23. The
    # profile still exists (it holds the closed evidence), but nothing
    # reads that flag any more, so listing it here would overstate the
    # surface that can arm.
}


@dataclass
class ProfileGates:
    """Resolved gate state for one profile, as its own daemon would see it."""

    profile: str
    exists: bool
    in_allow_list: bool
    is_owner: bool
    auth_configured: bool
    trading_enabled: bool
    broker: str                     # "live" | "paper"
    arm_flag: str | None = None     # env var name, when the profile has one
    arm_flag_on: bool | None = None

    @property
    def can_place_orders(self) -> bool:
        """The honest bottom line: live broker AND (no arm flag, or it is on)."""
        return self.broker == "live" and (self.arm_flag is None or bool(self.arm_flag_on))


@dataclass
class GateReport:
    live_enabled: bool
    owner_profile: str
    owner_profile_exists: bool
    allow_list: list[str]
    emergency_stop_active: bool
    drawdown: dict = field(default_factory=dict)
    profiles: list[ProfileGates] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # exit code 1
    notes: list[str] = field(default_factory=list)      # informational

    @property
    def armed(self) -> list[str]:
        return [p.profile for p in self.profiles if p.can_place_orders]


def _allow_list() -> list[str]:
    raw = os.getenv("LIVE_TRADING_PROFILES", "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


@contextlib.contextmanager
def _as_profile(profile: str):
    """Pin the active profile exactly as a daemon's run_cycle does, so gate
    resolution here matches gate resolution there."""
    key = "POLYMARKET_ACTIVE_PROFILE"
    prev = os.environ.get(key)
    os.environ[key] = profile
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def resolve(profile: str, *, known_profiles: set[str] | None = None) -> ProfileGates:
    """Resolve every gate for ``profile`` under that profile's own identity."""
    from pm.trading import execution
    from pm.venue import kalshi_auth as ka
    from pm.venue import kalshi_orders as ko

    if known_profiles is None:
        known_profiles = set(_known_profiles())

    flag_name, _label = ARM_FLAGS.get(profile, (None, None))
    flag_on = None
    if flag_name is not None:
        flag_on = os.getenv(flag_name, "").strip().lower() in ("1", "true", "yes", "on")

    with _as_profile(profile):
        try:
            broker = execution.get_broker(profile).name
        except Exception:  # noqa: BLE001 - report, never raise, from a status tool
            broker = "unknown"
        return ProfileGates(
            profile=profile,
            exists=profile in known_profiles,
            in_allow_list=profile in _allow_list(),
            is_owner=profile == ka.owner_profile(),
            auth_configured=ka.is_configured(),
            trading_enabled=ko.is_trading_enabled(),
            broker=broker,
            arm_flag=flag_name,
            arm_flag_on=flag_on,
        )


def _known_profiles() -> list[str]:
    try:
        from pm.core import profiles
        return list(profiles.list_profiles())
    except Exception:  # noqa: BLE001
        return []


def build_report() -> GateReport:
    """Resolve the whole picture. Never raises — a status tool that dies when
    something is misconfigured is useless precisely when it is needed."""
    from pm.core import emergency_stop
    from pm.venue import kalshi_auth as ka
    from pm.venue import kalshi_orders as ko

    known = set(_known_profiles())
    owner = ka.owner_profile()
    allow = _allow_list()

    # Everything that could conceivably be live, plus the owner.
    candidates: list[str] = []
    for p in [owner, *allow, *sorted(ARM_FLAGS)]:
        if p not in candidates:
            candidates.append(p)

    report = GateReport(
        live_enabled=ko.is_live_enabled(),
        owner_profile=owner,
        owner_profile_exists=owner in known,
        allow_list=allow,
        emergency_stop_active=emergency_stop.is_active(),
        profiles=[resolve(p, known_profiles=known) for p in candidates],
    )

    # Drawdown breaker state, if it has ever been evaluated.
    try:
        from pm.core import atomic_io
        from pm.trading import execution
        state = atomic_io.read_json(execution._equity_state_path(), None)
        if state:
            report.drawdown = state
    except Exception:  # noqa: BLE001
        pass

    report.problems, report.notes = find_problems(report)
    return report


def find_problems(report: GateReport) -> tuple[list[str], list[str]]:
    """Consistency checks over a resolved report. Pure — takes the report,
    returns ``(problems, notes)``, touches nothing.

    ``problems`` mean the configuration is internally inconsistent and drive a
    non-zero exit code. ``notes`` are states worth seeing that may be entirely
    deliberate — a status tool that cries wolf about intentional configuration
    stops being read, which is how the thing it was built to catch gets missed.

    These are the mismatches that have actually bitten this project, not
    hypothetical ones.
    """
    problems: list[str] = []
    notes: list[str] = []

    if not report.owner_profile_exists:
        problems.append(
            f"KALSHI_OWNER_PROFILE={report.owner_profile!r} is not an existing "
            "profile. Auth will behave unpredictably; point it at a real one.")

    for p in report.profiles:
        if p.in_allow_list and not p.exists:
            problems.append(
                f"{p.profile!r} is in LIVE_TRADING_PROFILES but is not an "
                "existing profile.")
        # One boolean from live. Usually deliberate (an arm parked between
        # runs), so it is a note — but it must never be invisible, because an
        # allow-list entry outliving its purpose is exactly how a falsified
        # strategy stays armed.
        if p.broker == "live" and p.arm_flag and not p.arm_flag_on:
            notes.append(
                f"{p.profile!r} resolves to the LIVE broker with its own switch "
                f"({p.arm_flag}) off — one environment variable from placing "
                "real orders. Remove it from LIVE_TRADING_PROFILES if that is "
                "not intended.")

    if report.drawdown.get("paused"):
        problems.append(
            "The drawdown circuit breaker is TRIPPED — new orders are blocked "
            f"(equity ${report.drawdown.get('equity_dollars', 0):.2f} vs peak "
            f"${report.drawdown.get('peak_equity_dollars', 0):.2f}).")

    return problems, notes


def format_report(report: GateReport) -> str:
    """Human-readable rendering."""
    out: list[str] = []
    armed = report.armed
    banner = (f"ARMED — {len(armed)} profile(s) can place real orders: "
              f"{', '.join(armed)}") if armed else "NOT ARMED — no profile can place real orders"
    out.append("=" * 78)
    out.append(f"  LIVE GATES: {banner}")
    out.append("=" * 78)
    out.append(f"  KALSHI_LIVE_ENABLED   : {report.live_enabled}")
    out.append(f"  owner profile         : {report.owner_profile}"
               f"{'' if report.owner_profile_exists else '   <-- DOES NOT EXIST'}")
    out.append(f"  LIVE_TRADING_PROFILES : {', '.join(report.allow_list) or '(empty)'}")
    out.append(f"  emergency stop        : {'ACTIVE — all orders blocked' if report.emergency_stop_active else 'inactive'}")
    if report.drawdown:
        d = report.drawdown
        out.append(f"  drawdown breaker      : {'TRIPPED' if d.get('paused') else 'ok'}"
                   f"  equity ${d.get('equity_dollars', 0):.2f}"
                   f"  peak ${d.get('peak_equity_dollars', 0):.2f}"
                   f"  threshold {float(d.get('threshold_pct', 0)) * 100:.0f}%")
    else:
        out.append("  drawdown breaker      : not yet evaluated (no live order attempted)")
    out.append("")
    hdr = f"  {'profile':<28} {'broker':<8} {'auth':<6} {'trade':<6} {'arm switch':<30} {'ORDERS?':<8}"
    out.append(hdr)
    out.append("  " + "-" * (len(hdr) - 2))
    for p in sorted(report.profiles, key=lambda x: (not x.can_place_orders, x.profile)):
        switch = "(none)" if not p.arm_flag else f"{p.arm_flag}={'on' if p.arm_flag_on else 'off'}"
        mark = "YES" if p.can_place_orders else "no"
        name = p.profile + ("" if p.exists else " (missing)")
        out.append(f"  {name:<28} {p.broker:<8} {str(p.auth_configured):<6} "
                   f"{str(p.trading_enabled):<6} {switch:<30} {mark:<8}")
    for label, items in (("PROBLEMS", report.problems), ("NOTES", report.notes)):
        if items:
            out.append("")
            out.append(f"  {label}")
            for item in items:
                out.append(f"    - {item}")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="exit code only")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    report = build_report()
    if args.json:
        payload = asdict(report)
        payload["armed"] = report.armed
        print(json.dumps(payload, indent=2, default=str))
    elif not args.quiet:
        print(format_report(report))
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
