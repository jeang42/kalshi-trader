"""Auto-resolve pending forecasts from market resolutions.

The Calibration page has an "🔄 Auto-resolve" button that scans the
public closed-markets feed (plus your Kalshi settlements when authed)
and marks matching pending forecasts as resolved. That button only
fires when you visit the page and click it. This runner does the same
thing on a schedule, so Track Record badges actually populate as
markets resolve in the background.

Profile semantics
-----------------
Forecasts are per-profile. Five+ profiles have forecast logs
(``jeff``, ``agentic``, ``agentic_favorites``, ``agentic_llm``, etc.).
This runner walks every profile and runs auto-resolve in each one.

Kalshi settlements are only fetched when the active profile is the
configured ``KALSHI_OWNER_PROFILE`` (default ``jeff``). Other profiles
fall back to the public closed-markets lookup only — they can't see
the real account state.

The runner saves and restores the user's UI-selected profile around
its work, same pattern as ``strategy_runner``. Cron firings can't
silently steal the sidebar selection.

Usage
-----
::

    python auto_resolve_runner.py              # one cycle
    python auto_resolve_runner.py --loop --interval 900   # 15-min loop

Cron::

    */15 * * * *  cd /path/to/repo && .venv/bin/python auto_resolve_runner.py \\
                  >> data/shared/auto_resolve.log 2>&1
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from pm import REPO_ROOT

# Load .env so KALSHI_API_KEY_ID etc. are visible when run from cron.
# The Streamlit app does this in app.py at startup, but standalone
# CLI runners need to load it themselves.
try:
    from dotenv import load_dotenv
    # .env lives at the REPO root — Path(__file__).parent stopped meaning that
    # when R-24 moved this module under pm/ (gotcha 1b: a __file__ default
    # captures the module's LOCATION). Loading a nonexistent .env is silent,
    # which is gotcha #4's failure mode: config believed loaded, actually not.
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from datetime import UTC

import pandas as pd

from pm.core import logctx, profiles
from pm.ledger import calibration as cal
from pm.venue import provider as mp

# Import each sidecar so it self-registers; the resolve overlay discovers
# them via sidecars.list_sidecars() and calls their resolve_pending_tickers
# hooks. Same try/except discipline as elsewhere — one broken sidecar
# shouldn't break the resolver. Add new sidecars to this tuple.
for _sc_mod in ("baseball", "wnba", "tennis"):
    try:
        __import__(_sc_mod)  # side-effect: register_sidecar
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "%s sidecar not importable: %s", _sc_mod, _e)

log = logging.getLogger(__name__)


_LOOKBACK_DAYS = 14
_TARGETED_BATCH_SIZE = 100


def build_closed_lookup() -> dict[str, bool]:
    """Union of recently-closed markets across every registered
    provider. Used as the base lookup; Kalshi settlements layer on top
    when the active profile is the auth owner.

    Uses ``get_closed_markets_in_range`` with a 14-day lookback so we
    catch markets that settled days ago — the old ``get_closed_markets
    (limit=500)`` call only returned whatever the API's default sort
    surfaced, missing low-volume markets entirely.

    Returns ``{condition_id: resolved_yes}`` (bool). Providers that
    error out are skipped — partial coverage is better than aborting.
    """
    lookup: dict[str, bool] = {}
    for p in mp.list_providers():
        try:
            df = p.get_closed_markets_in_range(
                since_days=_LOOKBACK_DAYS, max_items=5000,
            )
        except Exception as e:
            log.warning("provider %s.get_closed_markets_in_range failed: %s",
                        p.name, e)
            continue
        if df.empty:
            continue
        for _, row in df.iterrows():
            cid = row.get("condition_id")
            ry = row.get("resolved_yes")
            if not cid or ry is None or pd.isna(ry):
                continue
            lookup[str(cid)] = bool(ry)
    return lookup


def _pending_forecast_tickers() -> list[str]:
    """Every pending forecast's condition_id across every profile.
    Used to seed the sidecar resolve_pending_tickers() calls so each
    sidecar only has to look up tickers we actually care about, not
    every ticker the venue knows."""
    out: set[str] = set()
    saved = profiles.active_profile()
    try:
        for name in profiles.list_profiles():
            try:
                profiles.set_active_profile(name)
                df = cal.get_forecasts()
                if df.empty:
                    continue
                # Pending = no resolution set. Schema has had a few
                # shapes over time; accept whatever's there.
                if "resolution" in df.columns:
                    pending = df[df["resolution"].isna()]
                else:
                    pending = df
                out.update(pending["condition_id"].astype(str).tolist())
            except Exception as e:  # noqa: BLE001
                log.debug("couldn't list forecasts for %s: %s", name, e)
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
    return list(out)


def _pending_forecast_tickers_older_than(*, hours: float = 24) -> list[str]:
    """Pending forecast tickers whose forecast was placed >hours ago.
    Fresh forecasts probably haven't settled yet — no point checking."""
    from datetime import datetime, timedelta
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    out: set[str] = set()
    saved = profiles.active_profile()
    try:
        for name in profiles.list_profiles():
            try:
                profiles.set_active_profile(name)
                df = cal.get_forecasts()
                if df.empty:
                    continue
                if "resolution" not in df.columns:
                    continue
                pending = df[df["resolution"].isna()]
                if "timestamp_dt" in pending.columns:
                    old = pending[pending["timestamp_dt"] < cutoff]
                    out.update(old["condition_id"].astype(str).tolist())
                else:
                    out.update(pending["condition_id"].astype(str).tolist())
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't collect pending tickers for %s: %s", name, e)
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
    return list(out)


def overlay_sidecar_resolutions(lookup: dict[str, bool],
                                  pending_tickers: list[str]) -> dict[str, bool]:
    """Ask every registered sidecar that implements the optional
    ``resolve_pending_tickers`` hook to short-circuit pending resolutions
    using its own domain data — typically MLB game finals before Kalshi
    settles the market 4 days later.

    Sidecar resolutions OVERLAY the public + settlements feed (later
    overlays win). Sidecars know game outcomes faster than venues do,
    and the venue lookup is the authoritative tiebreaker if any
    disagreement exists later — but since both should agree, this is
    purely a latency win.
    """
    if not pending_tickers:
        return lookup
    try:
        from pm.core import sidecars as _sidecars
    except ImportError:
        return lookup
    for sc in _sidecars.list_sidecars():
        fn = getattr(sc, "resolve_pending_tickers", None)
        if not callable(fn):
            continue
        try:
            extras = fn(pending_tickers)
        except Exception as e:  # noqa: BLE001 — sidecar should swallow already
            log.warning("sidecar %s.resolve_pending_tickers raised: %s",
                        sc.name, e)
            continue
        if not extras:
            continue
        before = len(lookup)
        for ticker, yes in extras.items():
            lookup[str(ticker)] = bool(yes)
        added = len(lookup) - before
        log.info("sidecar %s resolved %d ticker(s), %d new in lookup",
                 sc.name, len(extras), added)
    return lookup


def overlay_kalshi_settlements(lookup: dict[str, bool]) -> dict[str, bool]:
    """Add Kalshi authed-settlement entries when configured.

    Mutates and returns ``lookup``. Best-effort: auth errors and
    network blips fall through to the existing public-feed lookup.
    Settlements take precedence over the public feed when both have
    the same ticker — the account-side record is authoritative for
    the user's resolved positions.
    """
    try:
        from pm.venue import kalshi_auth as ka
        if not ka.is_configured():
            return lookup
        from pm.venue import kalshi_client as kc
        df_settle = kc.get_settlements(limit=500)
        for _, row in df_settle.iterrows():
            ticker = row.get("ticker")
            result = str(row.get("market_result", "") or "").strip().lower()
            if not ticker or result not in ("yes", "no"):
                continue
            lookup[str(ticker)] = (result == "yes")
    except Exception as e:
        log.warning("settlements overlay failed: %s — using public feed only", e)
    return lookup


def run_cycle() -> dict[str, int]:
    """Run one auto-resolve pass across every profile. Returns
    ``{profile_name: n_resolved}``. Save/restores the active profile.

    Two-phase: build the merged lookup ONCE on the owner profile so
    Kalshi settlements are visible to every profile's auto-resolution.
    Settlements are just "did this ticker resolve YES/NO" data — no
    per-profile state, safe to share. Without this, profiles other
    than the auth owner can never resolve forecasts on markets that
    aren't in the public closed-markets feed (which has limited
    coverage / lag) but which the owner has settlement records for.
    """
    profiles.initialize_profiles()
    saved = profiles.active_profile()
    results: dict[str, int] = {}

    # Phase 1: switch to owner profile, build merged lookup.
    try:
        from pm.venue import kalshi_auth as ka
        owner = ka.owner_profile()
    except Exception:
        owner = "jeff"

    public_lookup = build_closed_lookup()
    log.info("public closed-markets lookup: %d entries", len(public_lookup))

    merged_lookup: dict[str, bool] = dict(public_lookup)
    try:
        if owner in profiles.list_profiles():
            profiles.set_active_profile(owner)
            before = len(merged_lookup)
            merged_lookup = overlay_kalshi_settlements(merged_lookup)
            added = len(merged_lookup) - before
            if added > 0:
                log.info("kalshi settlements overlay added %d entries", added)
    except Exception as e:
        log.warning("couldn't build settlements overlay: %s", e)

    # Targeted per-ticker lookup for pending forecasts not found in the
    # bulk closed-markets feed. Low-volume markets (ITF tennis, eSports,
    # etc.) are buried past the events-endpoint pagination limit.
    # Checking them individually via /markets/{ticker} is the only
    # reliable way. Capped at _TARGETED_BATCH_SIZE per cycle to avoid
    # hammering the API; pending tickers rotate through over multiple
    # cycles. Only checks forecasts >24h old (fresh ones probably
    # haven't settled yet).
    try:
        stale_tickers = _pending_forecast_tickers_older_than(hours=24)
        missing = [t for t in stale_tickers if t not in merged_lookup]
        if missing:
            from pm.venue import kalshi_client as kc
            batch = missing[:_TARGETED_BATCH_SIZE]
            resolved_count = 0
            for ticker in batch:
                try:
                    resp = kc._get(f"markets/{ticker}")
                    m = resp.get("market", {})
                    status = m.get("status", "")
                    result = (m.get("result") or "").lower()
                    if status == "finalized" and result in ("yes", "no"):
                        merged_lookup[ticker] = (result == "yes")
                        resolved_count += 1
                except Exception as e:  # noqa: BLE001
                    # Skipping quietly means this ticker simply never resolves
                    # and the forecast stays "untested" forever.
                    log.debug("targeted lookup failed for %s: %s", ticker, e)
            if resolved_count:
                log.info("targeted ticker lookup resolved %d / %d checked "
                         "(%d remain)", resolved_count, len(batch),
                         len(missing) - len(batch))
    except Exception as e:  # noqa: BLE001
        log.warning("targeted ticker lookup failed: %s", e)

    # Sidecar overlay — short-circuits venue settlement lag. e.g. the
    # baseball sidecar knows MLB final scores immediately; Kalshi
    # holds those markets open ~4 more days. Without this, the Brier
    # signal lags by days. The owner-profile-only auth means
    # agentic_baseball wouldn't see settlements via Kalshi at all.
    try:
        pending_tickers = _pending_forecast_tickers()
        before = len(merged_lookup)
        merged_lookup = overlay_sidecar_resolutions(merged_lookup, pending_tickers)
        added = len(merged_lookup) - before
        if added > 0:
            log.info("sidecar overlay added %d entries (was %d, now %d)",
                     added, before, len(merged_lookup))
    except Exception as e:  # noqa: BLE001
        log.warning("sidecar overlay failed: %s", e)

    # Phase 2: walk every profile, run auto-resolve with the shared lookup.
    try:
        for name in profiles.list_profiles():
            profiles.set_active_profile(name)
            try:
                resolved_ids = cal.auto_resolve_pending(merged_lookup)
            except Exception as e:
                log.warning("auto_resolve failed for %s: %s", name, e)
                resolved_ids = []
            results[name] = len(resolved_ids)
            if resolved_ids:
                log.info("profile=%s resolved=%d", name, len(resolved_ids))
    finally:
        if saved and saved != profiles.active_profile():
            try:
                profiles.set_active_profile(saved)
            except Exception as e:
                log.warning("couldn't restore active profile: %s", e)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loop", action="store_true",
                     help="Run forever, sleeping --interval seconds between cycles.")
    ap.add_argument("--interval", type=int, default=900,
                     help="Seconds between cycles when --loop (default: 900 = 15 min).")
    ap.add_argument("--log-level", default="INFO",
                     help="DEBUG / INFO / WARNING")
    args = ap.parse_args()

    logctx.setup(args.log_level.upper())

    log.info("auto_resolve_runner starting (loop=%s interval=%ds)",
             args.loop, args.interval)

    while True:
        t0 = time.time()
        try:
            results = run_cycle()
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:
            log.error("cycle failed: %s", e, exc_info=True)
            if not args.loop:
                return 1
            results = {}

        total = sum(results.values())
        if total > 0:
            details = ", ".join(f"{p}={n}" for p, n in results.items() if n)
            log.info("cycle resolved %d forecast(s): %s", total, details)
        else:
            log.info("cycle resolved 0 forecasts")

        if not args.loop:
            return 0
        elapsed = time.time() - t0
        sleep_for = max(1.0, args.interval - elapsed)
        log.debug("sleeping %.0fs", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
