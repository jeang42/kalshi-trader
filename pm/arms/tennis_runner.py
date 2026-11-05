"""Tennis agentic paper-trade runner — Elo value strategy.

Like ``wnba_runner.py``: tennis match markets are low-volume and won't crack
the volume-ordered ``get_markets_closing_within`` feed, so we discover them
via ``/events?series_ticker=KXATPMATCH|KXWTAMATCH`` + ``lookup_markets_by_id``
and inject the DataFrame into ``strategy_runner.run_cycle(markets_df=...)``.

Strategy: ``tennis_value`` — bet the side the Elo model thinks is
underpriced by ≥ min_gap, with the K-decay guardrail (see edges.py), and
HOLD to resolution. Paper-only into ``agentic_tennis_value``.

Requires the tennis sidecar to have Elo populated (``tennis.runner
--backfill`` once, then nightly). CLI: single cycle or ``--loop``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import pandas as pd

# Import the sidecar at module load so it registers with `sidecars` before
# strategy_runner's resolve phase runs (which calls every registered
# sidecar's resolve_pending_tickers to close finished matches fast instead
# of waiting on Kalshi settlement).
import tennis  # noqa: F401 — side-effect: register_sidecar
from pm.core import logctx

log = logging.getLogger("tennis_runner")

PROFILE = "agentic_tennis_value"
STRATEGY = "tennis_value"


def _discover_tennis_markets() -> pd.DataFrame:
    """Open ATP+WTA match markets as a normalized DataFrame, via the events
    path (these don't surface in the volume feed)."""
    from pm.venue import kalshi_client as kc
    from pm.venue.kalshi_client import _get

    frames: list[pd.DataFrame] = []
    for series in ("KXATPMATCH", "KXWTAMATCH"):
        try:
            payload = _get("/events", params={
                "series_ticker": series, "status": "open", "limit": 200,
            })
        except Exception as e:  # noqa: BLE001
            log.warning("%s events fetch failed: %s", series, e)
            continue
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
    df = pd.concat(frames, ignore_index=True).drop_duplicates("condition_id")
    return _one_ticker_per_match(df)


def _one_ticker_per_match(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse a match's two mutually-exclusive tickers (…-P1 and …-P2) to a
    SINGLE row, so the value arm takes one position per match instead of two.

    Why this matters: a tennis match has one market per player. Run against
    BOTH, ``strategy_tennis_value`` backs the same player twice — YES on that
    player's ticker AND NO on the opponent's (the identical economic bet) —
    doubling exposure and making the two legs win/lose in lockstep (this is
    what inflated the paper P&L 2x). The decision is symmetric: |gap| and the
    guardrails are identical on both sides, so keeping either ticker reaches
    the same backed player. We keep one per match (match key = ticker minus
    the trailing ``-<player code>``)."""
    if df.empty or "condition_id" not in df.columns:
        return df.reset_index(drop=True)
    match_key = df["condition_id"].astype(str).str.rsplit("-", n=1).str[0]
    return (df.loc[~match_key.duplicated()].reset_index(drop=True))


def run_once(*, bankroll: float, max_position_pct: float):
    from pm.arms import strategy_runner as sr

    df = _discover_tennis_markets()
    if df.empty:
        log.info("no tennis markets discovered this cycle")
        return None
    log.info("discovered %d tennis market(s)", len(df))
    stats = sr.run_cycle(
        strategy_key=STRATEGY, bankroll=bankroll, kelly_multiplier=0.5,
        half_spread=0.005, max_position_pct=max_position_pct, min_gap=0.0,
        max_markets=len(df), profile_name=PROFILE, markets_df=df,
        # hold to resolution — no exit policy
    )
    log.info("cycle: scanned=%d signals=%d opened=%d resolved=%d errors=%d",
             stats.markets_scanned, stats.signals_emitted,
             stats.positions_opened, stats.resolved_closed, stats.errors)
    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tennis Elo value paper runner")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--max-position-pct", type=float, default=0.04)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=1800.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup()
    if not args.loop:
        run_once(bankroll=args.bankroll, max_position_pct=args.max_position_pct)
        return 0
    log.info("tennis_runner starting (interval=%.0fs, profile=%s)",
             args.interval, PROFILE)
    while True:
        try:
            run_once(bankroll=args.bankroll, max_position_pct=args.max_position_pct)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("cycle failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
