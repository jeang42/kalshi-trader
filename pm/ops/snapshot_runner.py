"""Periodic portfolio snapshotter — records portfolio value without visiting the UI.

The Portfolio page auto-snapshots on every render, but that only works
when someone visits the page.  This runner captures snapshots for every
profile that has open positions or realized P&L, building a continuous
value chart even when the UI is idle.

Usage::

    python snapshot_runner.py                # one cycle, all profiles
    python snapshot_runner.py --profile jeff  # one profile only
"""

from __future__ import annotations

import argparse
import logging
import sys

from pm import REPO_ROOT

try:
    from dotenv import load_dotenv
    # .env lives at the REPO root — Path(__file__).parent stopped meaning that
    # when R-24 moved this module under pm/ (gotcha 1b: a __file__ default
    # captures the module's LOCATION). Loading a nonexistent .env is silent,
    # which is gotcha #4's failure mode: config believed loaded, actually not.
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from pm.core import logctx, profiles
from pm.ledger import portfolio as pf
from pm.venue import provider as mp

log = logging.getLogger(__name__)


def snapshot_profile(profile_name: str, min_interval_hours: float = 4.0) -> bool:
    """Compute and maybe-record a snapshot for one profile.

    Returns True if a snapshot was actually written.
    """
    profiles.set_active_profile(profile_name)

    df_pos = pf.get_positions()
    df_realized = pf.get_realized_pnl()

    total_realized = (
        df_realized["realized_pnl"].sum() if not df_realized.empty else 0.0
    )
    total_cost = 0.0
    total_value = 0.0
    total_unrealized = 0.0
    open_count = 0

    if not df_pos.empty:
        try:
            df_markets = mp.get_provider().get_markets()
        except Exception as e:
            log.warning("profile=%s market fetch failed: %s", profile_name, e)
            return False

        df_pos = pf.enrich_with_current_prices(df_pos, df_markets)
        if "current_value" in df_pos.columns:
            total_cost = df_pos["cost_basis"].sum()
            total_value = df_pos["current_value"].sum()
            total_unrealized = df_pos["unrealized_pnl"].sum()
            open_count = len(df_pos)

    recorded = pf.maybe_snapshot(
        cost_basis=total_cost,
        current_value=total_value,
        unrealized_pnl=total_unrealized,
        realized_pnl=total_realized,
        open_positions=open_count,
        min_interval_hours=min_interval_hours,
    )
    if recorded:
        log.info(
            "profile=%s snapshot: cost=$%.2f value=$%.2f unrealized=$%.2f "
            "realized=$%.2f positions=%d",
            profile_name, total_cost, total_value, total_unrealized,
            total_realized, open_count,
        )
    return recorded


def run_cycle(
    *,
    only_profile: str | None = None,
    min_interval_hours: float = 4.0,
) -> dict[str, bool]:
    """Snapshot every profile (or just one). Returns {profile: was_recorded}."""
    profiles.initialize_profiles()
    saved = profiles.active_profile()
    results: dict[str, bool] = {}

    target_profiles = (
        [only_profile] if only_profile else profiles.list_profiles()
    )

    try:
        for name in target_profiles:
            try:
                results[name] = snapshot_profile(name, min_interval_hours)
            except Exception as e:
                log.warning("snapshot failed for %s: %s", name, e)
                results[name] = False
    finally:
        try:
            profiles.set_active_profile(saved)
        except Exception as e:  # noqa: BLE001
            # Not cosmetic: this process is now pinned to whatever profile the
            # loop ended on, so anything that runs afterwards reads and writes
            # the WRONG profile's ledger. Loud on purpose.
            log.error("could not restore the active profile to %r (%s) — this "
                      "process is left pinned to another profile; later work "
                      "may touch the wrong data", saved, e)

    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default=None,
                    help="Snapshot only this profile (default: all).")
    ap.add_argument("--min-interval-hours", type=float, default=4.0,
                    help="Skip if last snapshot is younger than this (default: 4h).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logctx.setup(args.log_level.upper())

    results = run_cycle(
        only_profile=args.profile,
        min_interval_hours=args.min_interval_hours,
    )
    recorded = sum(1 for v in results.values() if v)
    skipped = len(results) - recorded
    log.info("done: %d recorded, %d skipped (recent or empty)", recorded, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
