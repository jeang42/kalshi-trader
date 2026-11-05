"""WNBA agentic paper-trade runner — favorite-hold strategy.

The generic ``strategy_runner`` can't see WNBA markets: they're low-volume
and never crack the volume-ordered ``get_markets_closing_within`` feed
(verified — 0 WNBA tickers in a 5000-market fetch while MLB shows ~50). So
we discover WNBA game markets the same way the MLB tooling does — via the
``/events?series_ticker=KXWNBAGAME`` path + ``lookup_markets_by_id`` — and
inject that DataFrame into ``strategy_runner.run_cycle(markets_df=...)``.

Strategy: ``wnba_favorite_hold`` — buy the model's moderate-favorite side
(P(YES) ∈ [0.55, 0.70]) and HOLD to resolution. The favorite-longshot-bias
bet (see docs/strategies.md §17). Paper-only into ``agentic_wnba_favorite``.

The WNBA sidecar must have today's predictions in state first — the
``wnba.runner`` cron populates them. This runner reads them via the edges
adapter's ticker bridge.

CLI: single cycle (default) or ``--loop --interval N``. Cron-able like the
other agentic runners.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import pandas as pd

# Import the sidecar at module load so it registers with `sidecars` before
# strategy_runner's resolve phase runs — that phase calls every registered
# sidecar's resolve_pending_tickers to close finished games fast (instead of
# waiting on Kalshi settlement). Without this import the sidecar wouldn't be
# registered in this process and WNBA paper positions would only resolve on
# the venue's ~1-4 day lag.
import wnba  # noqa: F401 — side-effect: register_sidecar
from pm.core import logctx

log = logging.getLogger("wnba_runner")

PROFILE = "agentic_wnba_favorite"
STRATEGY = "wnba_favorite_hold"


def _discover_wnba_markets(*, max_days: float = 5.0) -> pd.DataFrame:
    """Today's + near-future WNBA game markets (both sides) as a normalized
    DataFrame, via the events path. Mirrors ``mlb_deep_primer._todays_mlb_
    markets`` — the events endpoint is the only way these surface."""
    from pm.venue import kalshi_client as kc
    from pm.venue.kalshi_client import _get

    try:
        payload = _get("/events", params={
            "series_ticker": "KXWNBAGAME", "status": "open", "limit": 200,
        })
    except Exception as e:  # noqa: BLE001
        log.warning("WNBA events fetch failed: %s", e)
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for ev in payload.get("events", []):
        et = ev.get("event_ticker")
        if not et:
            continue
        try:
            sub = kc.lookup_markets_by_id(et)
        except Exception as e:  # noqa: BLE001
            log.debug("lookup_markets_by_id(%s) failed: %s", et, e)
            continue
        if sub is not None and not sub.empty:
            frames.append(sub)

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["condition_id"].astype(str).str.startswith("KXWNBAGAME")]
    df = df.drop_duplicates("condition_id")
    if "days_to_resolution" in df.columns:
        df = df[df["days_to_resolution"].fillna(99.0) <= max_days]
    return df.reset_index(drop=True)


def run_once(*, bankroll: float, max_position_pct: float, max_days: float):
    from pm.arms import strategy_runner as sr

    df = _discover_wnba_markets(max_days=max_days)
    if df.empty:
        log.info("no WNBA markets discovered this cycle")
        return None
    log.info("discovered %d WNBA market(s)", len(df))
    stats = sr.run_cycle(
        strategy_key=STRATEGY, bankroll=bankroll, kelly_multiplier=0.5,
        half_spread=0.005, max_position_pct=max_position_pct, min_gap=0.0,
        max_markets=len(df), profile_name=PROFILE,
        markets_df=df,
        # hold to resolution — no exit policy (favorite-hold thesis)
    )
    log.info("cycle: scanned=%d signals=%d opened=%d resolved=%d errors=%d",
             stats.markets_scanned, stats.signals_emitted,
             stats.positions_opened, stats.resolved_closed, stats.errors)
    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WNBA favorite-hold paper runner")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--max-position-pct", type=float, default=0.05)
    p.add_argument("--max-days", type=float, default=5.0,
                   help="only trade games resolving within this many days")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=1800.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup()
    if not args.loop:
        run_once(bankroll=args.bankroll,
                 max_position_pct=args.max_position_pct, max_days=args.max_days)
        return 0
    log.info("wnba_runner starting (interval=%.0fs, profile=%s)",
             args.interval, PROFILE)
    while True:
        try:
            run_once(bankroll=args.bankroll,
                     max_position_pct=args.max_position_pct,
                     max_days=args.max_days)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("cycle failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
