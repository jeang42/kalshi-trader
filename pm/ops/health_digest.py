"""Daily fleet health digest — the pull-to-push half of operator alerting.

``notify.py`` handles the event case: something failed, tell me now. This
handles the quieter and more dangerous case: **nothing crashed, but an arm
stopped doing its job.** A daemon that is `active (running)` while silently
capturing nothing, or an arm that has not opened a position in three days,
looks perfectly healthy to systemd and to a glance at the control plane.

Deliberately quiet by design
----------------------------
It pushes **only when something needs attention**. A digest that arrives every
day regardless of state gets muted within a week, and then you are worse off
than with no alerting at all — you have the illusion of coverage. ``--force``
sends anyway, for testing and for a weekly "yes it is still working" check.

Checks
------
* Any unit in the curated fleet that is enabled but not running.
* Any unit that has restarted repeatedly since boot (crash-looping while
  still reporting `active`).
* Any capture store whose newest row is older than its expected cadence.
* Anything the health log recorded in the last 24h.
* The live gate and drawdown breaker state, so an armed system cannot drift
  out of your attention the way the near-close canary did.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass, field

from pm.core import logctx

log = logging.getLogger(__name__)

#: Units expected to be running. Kept explicit rather than globbed: the point
#: is to notice when one of THESE stops, and a glob would silently shrink as
#: units are disabled.
EXPECTED_UNITS = (
    # advisory-capture: deliberately STOPPED 2026-09-09 when candidate A died
    # on its pre-registered kill rule. State retained; if it is ever restarted
    # for a new thesis, put it back here or the digest will not watch it.
    # near-close-capture + mention-capture: deliberately DISABLED 2026-11-04 —
    # every thesis they fed is closed, so they were accruing on spec. State
    # retained. If revived, restore here and in FRESHNESS_CHECKS.
    "candle-panel.service",
    "control-plane.service",
    "baseball-web.service",
    "wnba-web.service",
    "tennis-web.service",
)

#: (store, table, timestamp column, max age in hours). Age thresholds are set
#: well above the normal cadence so ordinary jitter is not an alert.
FRESHNESS_CHECKS: tuple[tuple[str, str, str, float], ...] = (
    # advisory_capture (2026-09-09), mention_capture + near_close_capture
    # (2026-11-04) are deliberately stopped, so their stores are FROZEN by
    # design — watching them here would page daily about services we chose to
    # switch off. Restore alongside EXPECTED_UNITS if an instrument is revived.
)

#: Written by scripts/backup.sh at the end of every run. A backup that fails
#: or stops running is invisible to the unit/freshness checks (the snapshot
#: DIRECTORY still appears nightly even when the sqlite stage fails inside
#: it) — which is exactly how four consecutive failed runs went unnoticed
#: 2026-08-25..28 after R-24 broke the module path.
BACKUP_STATUS_FILE = "data/shared/backup_status.json"
BACKUP_MAX_AGE_H = 26.0


@dataclass
class Digest:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.problems)


def _systemctl_show(unit: str, props: str) -> dict[str, str]:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", props, unit],
            capture_output=True, text=True, timeout=15).stdout
        return dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line)
    except Exception as e:  # noqa: BLE001
        log.warning("health_digest: systemctl show failed for %s: %s", unit, e)
        return {}


def check_units(units=EXPECTED_UNITS) -> tuple[list[str], list[str]]:
    """Report units that are not running, or that keep restarting."""
    problems, notes = [], []
    for unit in units:
        info = _systemctl_show(unit, "ActiveState,UnitFileState,NRestarts")
        if not info:
            continue
        state = info.get("ActiveState", "?")
        enabled = info.get("UnitFileState", "?")
        restarts = info.get("NRestarts", "0")
        if enabled == "enabled" and state != "active":
            problems.append(f"{unit} is {state} (enabled but not running)")
        try:
            # Restarts accumulate since boot; a handful is normal after a
            # deploy, a steady climb is a crash-loop wearing a healthy mask.
            if int(restarts) >= 5:
                problems.append(f"{unit} has restarted {restarts} times — likely crash-looping")
            elif int(restarts) > 0:
                notes.append(f"{unit} has restarted {restarts} time(s)")
        except ValueError:
            pass
    return problems, notes


def check_freshness(checks=FRESHNESS_CHECKS) -> tuple[list[str], list[str]]:
    """Report capture stores that have gone quiet.

    This is the failure systemd cannot see: the process is alive and the
    service is green, but nothing is landing in the database.
    """
    import os
    import sqlite3
    import time

    problems, notes = [], []
    for path, table, col, max_age_h in checks:
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                newest = con.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{os.path.basename(path)} is unreadable: {e}")
            continue
        if newest is None:
            problems.append(f"{os.path.basename(path)}.{table} is empty")
            continue
        try:
            age_h = (time.time() - float(newest)) / 3600.0
        except (TypeError, ValueError):
            continue                      # non-epoch column; skip rather than guess
        if age_h > max_age_h:
            problems.append(
                f"{os.path.basename(path)}: newest row is {age_h:.1f}h old "
                f"(expected < {max_age_h:.0f}h) — the service may be running "
                "but capturing nothing")
        else:
            notes.append(f"{os.path.basename(path)}: fresh ({age_h:.1f}h)")
    return problems, notes


def check_backup(path=None, max_age_h=BACKUP_MAX_AGE_H) -> tuple[list[str], list[str]]:
    """Report a nightly backup that failed, went stale, or never recorded.

    Reads the status file scripts/backup.sh writes after every run. Missing
    file IS a problem: either the backup has never completed since this check
    shipped, or something is eating the status write — both need eyes.
    """
    import json
    import os
    from datetime import UTC, datetime

    problems, notes = [], []
    path = path or os.getenv("BACKUP_STATUS_FILE", BACKUP_STATUS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        finished = datetime.fromisoformat(
            str(st["finished_at"]).replace("Z", "+00:00"))
        status = str(st.get("status", "?"))
    except FileNotFoundError:
        return ([f"{os.path.basename(path)} missing — no backup outcome "
                 "recorded (backup never ran, or its status write is broken)"],
                [])
    except Exception as e:  # noqa: BLE001
        return [f"backup status file is unreadable: {e}"], []
    age_h = (datetime.now(UTC) - finished).total_seconds() / 3600.0
    if status != "ok":
        problems.append(
            f"last backup run FAILED ({st.get('stamp', '?')}) — see "
            "data/shared/backup.log; the newest snapshot may be unusable")
    elif age_h > max_age_h:
        problems.append(
            f"last successful backup is {age_h:.0f}h old (expected < "
            f"{max_age_h:.0f}h) — the nightly cron may have stopped")
    else:
        notes.append(f"backup: ok ({st.get('stamp', '?')}, {age_h:.1f}h ago)")
    # A size-capped database is absent from the snapshot even on an "ok" run
    # — worse, its last staged copy keeps appearing, so the snapshot LOOKS
    # complete. The candle panel sat at an Oct-1 copy for 12 green nights.
    skipped = st.get("skipped") or []
    if skipped:
        problems.append(
            f"backup skipped {len(skipped)} database(s) over the size cap: "
            f"{', '.join(map(str, skipped))} — the snapshot holds a STALE copy "
            "at best; raise BACKUP_MAX_SQLITE_MB or set BACKUP_FORCE_INCLUDE")
    expected = st.get("skipped_expected") or []
    if expected:
        notes.append(f"backup: deliberately excluded {', '.join(map(str, expected))}")
    return problems, notes


def check_recent_events() -> tuple[list[str], list[str]]:
    problems, notes = [], []
    try:
        from pm.core import notify
        events = notify.recent_events(limit=20, since_hours=24)
    except Exception as e:  # noqa: BLE001
        return [f"could not read the health log: {e}"], []
    for ev in events:
        line = f"{ev.get('ts', '?')[:19]} {ev.get('event')}: {ev.get('detail', '')}"
        if ev.get("severity") in ("error", "critical"):
            problems.append(line)
        else:
            notes.append(line)
    # Circuit breakers (R-21) report open/close through this log — it is the
    # only cross-process view of per-process breaker state. An open with no
    # later close means a dependency has been down since then. `events` is
    # newest-first; replay oldest→newest so the LAST transition wins.
    still_open: dict[str, str] = {}
    for ev in reversed(events):
        b = ev.get("breaker")
        if b and ev.get("event") == "circuit_open":
            still_open[b] = str(ev.get("ts", "?"))[:19]
        elif b and ev.get("event") == "circuit_closed":
            still_open.pop(b, None)
    for b, ts in sorted(still_open.items()):
        problems.append(f"circuit '{b}' has been OPEN since {ts} with no recovery — "
                        "a dependency is down and its callers are failing fast")
    return problems, notes


def check_live_gates() -> tuple[list[str], list[str]]:
    """Surface armed state and the drawdown breaker in the daily digest.

    An armed system drifting out of attention is the specific failure that let
    a live canary run for a week while the operating notes called it dormant.
    """
    problems, notes = [], []
    try:
        from pm.trading import live_gates
        report = live_gates.build_report()
        armed = report.armed
        notes.append(f"armed profiles: {', '.join(armed) if armed else 'none'}")
        problems.extend(report.problems)
        notes.extend(report.notes)
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not resolve the live gates: {e}")
    return problems, notes


def build_digest() -> Digest:
    d = Digest()
    for fn in (check_units, check_freshness, check_backup,
               check_recent_events, check_live_gates):
        try:
            probs, notes = fn()
        except Exception as e:  # noqa: BLE001 - one bad check must not kill the digest
            # getattr, not fn.__name__: this runs on the error path, and a
            # callable without __name__ (a partial, a mock) would otherwise
            # raise here — turning "one check failed" into "monitoring died".
            probs, notes = [f"{getattr(fn, '__name__', fn)!s} raised: {e}"], []
        d.problems.extend(probs)
        d.notes.extend(notes)
    return d


def format_digest(d: Digest) -> str:
    lines = []
    if d.problems:
        lines.append(f"{len(d.problems)} problem(s):")
        lines.extend(f"  ! {p}" for p in d.problems)
    else:
        lines.append("No problems detected.")
    if d.notes:
        lines.append("")
        lines.extend(f"  · {n}" for n in d.notes)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true",
                    help="push even when nothing needs attention")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the digest without sending anything")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logctx.setup(logging.WARNING)

    d = build_digest()
    body = format_digest(d)
    print(body)

    if args.print_only:
        return 0
    if d.needs_attention or args.force:
        from pm.core import notify
        title = (f"Fleet: {len(d.problems)} problem(s)" if d.problems
                 else "Fleet: all clear")
        # Body is deliberately free of balances and position sizes — the push
        # topic is protected only by being unguessable.
        notify.alert(title=title, body=body[:1500],
                     severity="error" if d.problems else "info",
                     event="health_digest",
                     tags="rotating_light" if d.problems else "white_check_mark")
    return 0


if __name__ == "__main__":
    sys.exit(main())
