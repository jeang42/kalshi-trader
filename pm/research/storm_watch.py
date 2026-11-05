"""Storm-detection trigger for the scheduled-advisory weather candidate.

Why this exists (2026-08-24): after the day's edge search closed every other
candidate, the sole survivor — NHC scheduled-advisory weather — is gated on a
pre-registered trigger: *specify the instrument when a named storm exists,
build nothing before*. A trigger nobody is watching never fires, so this
module watches, from both ends:

1. **NHC** (the primary source): a new NAMED storm in ``CurrentStorms.json``
   — this is "it is time to design the advisory instrument".
2. **Kalshi** (the tradeable side): a storm-flavored market series we have
   not seen before — this is "there is now a book to measure against".
   Detection is per SERIES ticker, keyword-matched on event titles, because
   per-storm series names are unknowable until Kalshi invents them.

On a NEW detection of either kind it alerts once through `pm.core.notify`
(health log + desktop + phone) and never again for the same storm id /
series — state in ``data/shared/storm_watch.json`` via `atomic_io` (env
``STORM_WATCH_STATE_FILE``; redirected in the root conftest like every other
store). Deliberately NOT a daemon: a cron single-shot every few hours is
faster than any human response to it, and the advisory instrument itself —
which will care about minutes — does not exist yet.

First-run behavior is intentional: if a named storm is active the day this
ships, it alerts immediately — that is the trigger firing, not a bug.

CLI: ``python -m pm.research.storm_watch`` (cron single-shot) | ``--status``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from pm.core import atomic_io, logctx, notify
from pm.feeds import nhc_data
from pm.venue import kalshi_client as kc

log = logging.getLogger(__name__)

DEFAULT_STATE = Path("data/shared/storm_watch.json")

#: Event-title keywords that mark a storm-flavored Kalshi listing.
STORM_TITLE_RE = re.compile(
    r"hurricane|tropical\s+storm|tropical\s+cyclone|landfall|storm\s+surge"
    r"|named\s+storm|typhoon", re.I)

#: Series that are ALWAYS listed (season aggregates) — never trigger-worthy.
#: Seeded into state on first run too; this constant is belt-and-braces.
KNOWN_AGGREGATE_SERIES = {"KXHURRICANE", "KXNAMEDSTORM"}


def _state_path() -> Path:
    return Path(os.getenv("STORM_WATCH_STATE_FILE", str(DEFAULT_STATE)))


def _series_of(event_ticker: str) -> str:
    return (event_ticker or "").split("-")[0].upper()


def scan_kalshi_storm_series() -> dict[str, str]:
    """Storm-flavored OPEN series → one example event title.

    Full open-events scan (cursor-paginated); the close-time feed is NOT used
    because props/one-offs never surface there (the documented discovery
    gotcha). Returns {} on venue failure — the watcher treats that as
    "nothing new", never as an error worth paging about.
    """
    found: dict[str, str] = {}
    try:
        events = kc._paginate("events", {"status": "open", "limit": 200},
                              items_key="events", max_items=6000)
    except Exception as e:  # noqa: BLE001 — cron single-shot; degrade quiet
        log.warning("storm_watch: kalshi scan failed: %s", e)
        return {}
    for ev in events:
        title = str(ev.get("title") or "")
        if not STORM_TITLE_RE.search(title):
            continue
        series = _series_of(str(ev.get("event_ticker") or ""))
        if not series or series in KNOWN_AGGREGATE_SERIES:
            continue
        found.setdefault(series, title)
    return found


def run_once(*, quiet: bool = False) -> dict:
    """One watch pass. Returns a summary dict (used by --status and tests)."""
    storms = nhc_data.active_storms()
    named = [s for s in storms if s.is_named]
    kalshi = scan_kalshi_storm_series()

    new_storms, new_series = [], []
    with atomic_io.mutate(_state_path(), {"alerted_storms": [],
                                          "alerted_series": [],
                                          "seeded": False}) as st:
        if not st.get("seeded"):
            # First run: seed with whatever aggregate-ish series exist NOW so
            # a pre-existing listing doesn't fire forever after; storms are
            # NOT seeded — an active named storm on day one SHOULD fire.
            st["seeded"] = True
        for s in named:
            if s.id not in st["alerted_storms"]:
                st["alerted_storms"].append(s.id)
                new_storms.append(s)
        for series, title in sorted(kalshi.items()):
            if series not in st["alerted_series"]:
                st["alerted_series"].append(series)
                new_series.append((series, title))
        st["last_run"] = datetime.now(UTC).isoformat()
        st["last_named_active"] = [f"{s.id}:{s.name}:{s.classification}"
                                   for s in named]

    if new_storms and not quiet:
        body = "; ".join(f"{s.name} ({s.classification}, {s.basin})"
                         for s in new_storms)
        notify.alert(
            "Named storm active — advisory-instrument trigger",
            f"NHC: {body}. Pre-registered next step: specify the "
            "scheduled-advisory instrument (edge log 2026-08-24). "
            "Check Kalshi for per-storm listings.",
            severity="warning", event="storm_trigger", tags="cyclone")
    if new_series and not quiet:
        body = "; ".join(f"{s}: {t[:60]}" for s, t in new_series)
        notify.alert(
            "New storm-flavored Kalshi series",
            f"{body}. A book now exists to measure against.",
            severity="warning", event="storm_series", tags="cyclone")

    return {"named_active": len(named), "new_storms": [s.id for s in new_storms],
            "new_series": [s for s, _ in new_series],
            "kalshi_series_seen": sorted(kalshi)}


def main(argv: list[str] | None = None) -> int:
    # gotcha #4: nothing loads .env unless it says so — and this entry point's
    # entire JOB is the ntfy page, whose topic lives in .env. The first live
    # run alerted to desktop+health_log only and the phone stayed silent.
    try:
        from dotenv import load_dotenv

        from pm import REPO_ROOT
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:  # pragma: no cover
        pass
    logctx.setup()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true",
                    help="Print state; no fetch, no alert.")
    args = ap.parse_args(argv)
    if args.status:
        st = atomic_io.read_json(_state_path(), {}) or {}
        print(f"last run       : {st.get('last_run', 'never')}")
        print(f"named active   : {st.get('last_named_active', [])}")
        print(f"alerted storms : {st.get('alerted_storms', [])}")
        print(f"alerted series : {st.get('alerted_series', [])}")
        return 0
    with logctx.bind(watch="storm", cycle=logctx.new_id("cyc")):
        summary = run_once()
        log.info("storm_watch: named_active=%d new_storms=%s new_series=%s",
                 summary["named_active"], summary["new_storms"],
                 summary["new_series"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
