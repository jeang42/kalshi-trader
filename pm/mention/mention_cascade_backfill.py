"""Mention-cascade SHAPE reconstruction from the historical trade tape.

Motivation (2026-07-23): the system will be OFF over a weekend, so LIVE
capture of the WHCD event (Fri 07-24, `KXTRUMPMENTION-26JUL24`) and the MLB
announcer-mention games (Jul 25-26, `KXMLBMENTION-*`) is impossible. But
Kalshi's public executed-trade tape (`kalshi_client.get_all_trades`) is
backfillable with exchange timestamps at full precision *after the fact* —
no live capture needed. This tool reconstructs the repricing-cascade SHAPE
of any past mention event purely from that tape, so Wednesday's weekend
tickers can be measured on Monday.

Background this tool validates against: `docs/edge-research-log.md`,
2026-07-18 (the "one-tick instant" gate read — later shown to be a
10s-capture-cadence artifact) and 2026-07-19 (the trade-tape revision: real
cascades are a 7-60s HUMAN sweep, not an instant tick, with a five-figure
per-event prize).

Read-only. Imports NO trading/order modules (`kalshi_orders`, `execution`
are forbidden here) — this module places nothing, ever.

Window logic (see module docstring on ``fetch_trade_tape``): mention
markets carry a `close_time` weeks out (Kalshi's settlement-window
backstop, not when the word was actually said), so we do NOT use it to
bound the trade-tape pull. Instead we pull the market's FULL trade history
(no since_ts/until_ts — capped by ``max_trades``) and run cascade detection
over the whole series. Mention markets are near-silent until the broadcast
moment, so total trade count stays modest (typically hundreds to a few
thousand rows) even pulling full lifetime history — the cascade shape falls
out of the same series `mention_rehearsal.cascade_start` already knows how
to find.

CLI:
    python mention_cascade_backfill.py --event KXTRUMPMENTION-26JUL15
    python mention_cascade_backfill.py --event KXTRUMPMENTION-26JUL24 \\
        --event KXMLBMENTION-26JUL25SEATEX --event KXMLBMENTION-26JUL25LADNYM \\
        --event KXMLBMENTION-26JUL26CLETB
"""
from __future__ import annotations

import argparse
import logging
import os
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pm.core import logctx, provenance
from pm.mention.mention_rehearsal import cascade_start
from pm.venue import kalshi_client as kc

log = logging.getLogger(__name__)

REPORT_DIR = Path(os.getenv("MENTION_CASCADE_BACKFILL_REPORT_DIR", "data/shared"))

DEFAULT_MAX_TRADES = 50_000
SWEEP_PRICE = 0.95
MEDIAN_LOOKBACK = 10
INSTANT_S = 2.0
FAST_S = 15.0
NO_SPIKE_THRESHOLD = 0.5
NO_SPIKE_FALLBACK_MARGIN = 0.15


# --------------------------------------------------------------------------
# Pure functions (unit-tested against synthetic tapes)
# --------------------------------------------------------------------------


@dataclass
class CascadeCharacterization:
    """Shape of one word's repricing cascade, reconstructed from its tape."""

    cascade_start_ts: float | None
    pre_jump_price: float | None
    seconds_to_95: float | None
    cascade_volume: float
    n_trades: int
    shape: str  # "instant" | "fast" | "slow_cascade" | "no_sweep"


def characterize_cascade(
    trades: list[tuple[float, float, float]],
    *,
    sweep_price: float = SWEEP_PRICE,
    sweep_window_s: float = 60.0,
    jump_threshold: float = 0.05,
    median_lookback: int = MEDIAN_LOOKBACK,
    instant_s: float = INSTANT_S,
    fast_s: float = FAST_S,
) -> CascadeCharacterization:
    """Characterize a single word market's repricing cascade.

    ``trades`` is ``(ts_unix, yes_price, count)``, any order. Reuses
    ``mention_rehearsal.cascade_start`` for sweep-start detection (a
    trade that's a ``jump_threshold`` jump above the median of its prior
    ``median_lookback`` trades AND is followed by a ``sweep_price`` print
    within ``sweep_window_s``).

    Returns a ``CascadeCharacterization`` with:

    - ``cascade_start_ts``: the sweep-start timestamp, or ``None`` if no
      qualifying sweep exists in the tape at all (``shape="no_sweep"`` —
      either the word was never said, or the tape shows no clean jump).
    - ``pre_jump_price``: median of the (up to ``median_lookback``) trades
      strictly before the cascade-start trade — the "quiet band" price
      just before the word landed.
    - ``seconds_to_95``: seconds from ``cascade_start_ts`` to the first
      trade at-or-after it whose price >= ``sweep_price`` (0.95).
    - ``cascade_volume``: total contracts traded from ``cascade_start_ts``
      through the +0.95 point, inclusive both ends.
    - ``n_trades``: number of trades in that same window.
    - ``shape``: "instant" (``seconds_to_95`` <= 2), "fast" (<= 15),
      "slow_cascade" (> 15), or "no_sweep" (no cascade found).

    >>> tape = [(0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
    ...         (20, 0.35, 50), (25, 0.72, 80), (30, 0.97, 200)]
    >>> c = characterize_cascade(tape)
    >>> c.cascade_start_ts, c.seconds_to_95, c.shape
    (20, 10, 'fast')
    >>> round(c.pre_jump_price, 2)
    0.1
    >>> c.cascade_volume
    330
    >>> characterize_cascade([]).shape
    'no_sweep'
    """
    if not trades:
        return CascadeCharacterization(
            cascade_start_ts=None, pre_jump_price=None, seconds_to_95=None,
            cascade_volume=0.0, n_trades=0, shape="no_sweep",
        )
    ordered = sorted(trades, key=lambda t: t[0])

    start = cascade_start(
        ordered,
        sweep_price=sweep_price,
        sweep_window_s=sweep_window_s,
        jump_threshold=jump_threshold,
        median_lookback=median_lookback,
    )
    if start is None:
        return CascadeCharacterization(
            cascade_start_ts=None, pre_jump_price=None, seconds_to_95=None,
            cascade_volume=0.0, n_trades=0, shape="no_sweep",
        )

    # Position of the (first) trade at ts == start, to slice the "prior"
    # window the same way cascade_start's own jump-detection does.
    start_idx = next(i for i, t in enumerate(ordered) if t[0] == start)
    prior = ordered[max(0, start_idx - median_lookback):start_idx]
    pre_jump_price = statistics.median(p for _, p, _ in prior) if prior else None

    hit = next(
        (ts for ts, p, _c in ordered if ts >= start and p >= sweep_price),
        None,
    )
    if hit is None:
        # Defensive: cascade_start guarantees a sweep within
        # sweep_window_s, so this shouldn't happen, but never crash the
        # backfill on an unexpected tape shape.
        return CascadeCharacterization(
            cascade_start_ts=start, pre_jump_price=pre_jump_price,
            seconds_to_95=None, cascade_volume=0.0, n_trades=0,
            shape="no_sweep",
        )

    seconds_to_95 = hit - start
    window = [(ts, p, c) for ts, p, c in ordered if start <= ts <= hit]
    cascade_volume = sum(c for _, _, c in window)
    n_trades = len(window)

    if seconds_to_95 <= instant_s:
        shape = "instant"
    elif seconds_to_95 <= fast_s:
        shape = "fast"
    else:
        shape = "slow_cascade"

    return CascadeCharacterization(
        cascade_start_ts=start, pre_jump_price=pre_jump_price,
        seconds_to_95=seconds_to_95, cascade_volume=cascade_volume,
        n_trades=n_trades, shape=shape,
    )


@dataclass
class NoWordSpike:
    """A NO-resolved word whose tape spiked toward YES then fell back."""

    peak_price: float
    peak_ts: float
    final_price: float


def detect_no_spike(
    trades: list[tuple[float, float, float]],
    *,
    spike_threshold: float = NO_SPIKE_THRESHOLD,
    fallback_margin: float = NO_SPIKE_FALLBACK_MARGIN,
) -> NoWordSpike | None:
    """Flag a NO-resolved word that nonetheless spiked toward YES.

    Potential false-positive risk for the detection pipeline: if a word
    that ultimately resolved NO spiked >= ``spike_threshold`` (0.5) mid-
    event and then fell back by at least ``fallback_margin`` (0.15) from
    its peak, that's a market that "almost" looked like a hit — worth a
    human's attention when tuning detection patterns.

    Returns ``None`` when the tape never spiked, or spiked and STAYED
    high (didn't meaningfully fall back — e.g. thin last print noise).

    >>> detect_no_spike([(0, 0.05, 10), (10, 0.6, 50), (20, 0.15, 10)])
    NoWordSpike(peak_price=0.6, peak_ts=10, final_price=0.15)
    >>> detect_no_spike([(0, 0.05, 10), (10, 0.1, 10)]) is None
    True
    >>> detect_no_spike([(0, 0.05, 10), (10, 0.9, 50), (20, 0.85, 10)]) is None
    True
    >>> detect_no_spike([]) is None
    True
    """
    if not trades:
        return None
    ordered = sorted(trades, key=lambda t: t[0])
    peak_ts, peak_price, _peak_count = max(ordered, key=lambda t: t[1])
    if peak_price < spike_threshold:
        return None
    final_price = ordered[-1][1]
    if final_price <= peak_price - fallback_margin:
        return NoWordSpike(
            peak_price=peak_price, peak_ts=peak_ts, final_price=final_price,
        )
    return None


# --------------------------------------------------------------------------
# Orchestration (per event: fetch markets, fetch tapes, characterize)
# --------------------------------------------------------------------------


def _word_label(m: dict) -> str:
    """Best-effort human label for a mention word market.

    Prefers ``yes_sub_title`` (Kalshi's per-market display label), falls
    back to ``custom_strike.Word`` (same value, structured), falls back to
    the bare ticker.
    """
    label = m.get("yes_sub_title")
    if label:
        return str(label)
    custom_strike = m.get("custom_strike") or {}
    word = custom_strike.get("Word") if isinstance(custom_strike, dict) else None
    if word:
        return str(word)
    return str(m.get("ticker", ""))


def _trades_to_tuples(df: Any) -> list[tuple[float, float, float]]:
    if df is None or len(df) == 0:
        return []
    return [
        (float(row.ts_unix), float(row.yes_price), float(row.count))
        for row in df.itertuples()
    ]


def fetch_event_markets(
    event_ticker: str,
    *,
    get_markets_fn: Callable[[str], list[dict]] | None = None,
) -> list[dict]:
    """Every member market of ``event_ticker``, raw Kalshi dicts."""
    fn = get_markets_fn or (
        lambda ev: kc._paginate(  # noqa: SLF001 - project convention, see kalshi_client
            "markets", {"event_ticker": ev, "limit": 100}, "markets", 1000,
        )
    )
    return fn(event_ticker)


def fetch_trade_tape(
    ticker: str,
    *,
    max_trades: int = DEFAULT_MAX_TRADES,
    get_all_trades_fn: Callable[..., Any] = kc.get_all_trades,
) -> list[tuple[float, float, float]]:
    """Full lifetime trade tape for ``ticker`` as ``(ts, price, count)``.

    Deliberately does NOT bound by ``close_time`` (see module docstring —
    that field is a weeks-out settlement backstop, not when the word was
    said). Bounded only by ``max_trades`` as a runaway-market safety cap.
    """
    df = get_all_trades_fn(ticker, max_trades=max_trades)
    return _trades_to_tuples(df)


@dataclass
class YesWordRow:
    ticker: str
    word: str
    characterization: CascadeCharacterization


@dataclass
class NoSpikeRow:
    ticker: str
    word: str
    spike: NoWordSpike


@dataclass
class EventCascadeReport:
    event_ticker: str
    yes_rows: list[YesWordRow] = field(default_factory=list)
    no_spike_rows: list[NoSpikeRow] = field(default_factory=list)
    n_markets: int = 0
    n_no_words: int = 0
    n_unresolved: int = 0
    n_fetch_failed: int = 0


def process_event(
    event_ticker: str,
    *,
    max_trades: int = DEFAULT_MAX_TRADES,
    get_markets_fn: Callable[[str], list[dict]] | None = None,
    get_all_trades_fn: Callable[..., Any] = kc.get_all_trades,
) -> EventCascadeReport:
    """Reconstruct every word's cascade shape for one mention event."""
    markets = fetch_event_markets(event_ticker, get_markets_fn=get_markets_fn)
    report = EventCascadeReport(event_ticker=event_ticker, n_markets=len(markets))

    for m in markets:
        ticker = str(m.get("ticker", ""))
        result = str(m.get("result", "")).lower()
        word = _word_label(m)

        if result not in ("yes", "no"):
            report.n_unresolved += 1
            continue

        try:
            tape = fetch_trade_tape(
                ticker, max_trades=max_trades, get_all_trades_fn=get_all_trades_fn,
            )
        except Exception:
            log.exception("Failed to fetch trade tape for %s", ticker)
            report.n_fetch_failed += 1
            continue

        if result == "yes":
            char = characterize_cascade(tape)
            report.yes_rows.append(YesWordRow(ticker=ticker, word=word, characterization=char))
        else:
            report.n_no_words += 1
            spike = detect_no_spike(tape)
            if spike is not None:
                report.no_spike_rows.append(NoSpikeRow(ticker=ticker, word=word, spike=spike))

    return report


# --------------------------------------------------------------------------
# Report formatting
# --------------------------------------------------------------------------


def _utc_hms(ts: float | None) -> str:
    if ts is None:
        return "n/a"
    import datetime

    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).strftime("%H:%M:%S")


def format_report(report: EventCascadeReport) -> str:
    lines = [f"Mention cascade backfill: {report.event_ticker}", "=" * 70, ""]

    # Sort YES words by cascade duration: fastest first, no_sweep last.
    def sort_key(row: YesWordRow) -> tuple[int, float]:
        s = row.characterization.seconds_to_95
        return (1, 0.0) if s is None else (0, s)

    ordered_rows = sorted(report.yes_rows, key=sort_key)

    header = (
        f"{'word':<24} {'pre_jump':>9} {'cascade_start':>14} "
        f"{'secs_to_95':>11} {'volume':>10} {'shape':>13}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in ordered_rows:
        c = row.characterization
        pre_str = f"{c.pre_jump_price:.3f}" if c.pre_jump_price is not None else "n/a"
        start_str = _utc_hms(c.cascade_start_ts)
        secs_str = f"{c.seconds_to_95:.1f}" if c.seconds_to_95 is not None else "n/a"
        vol_str = f"{c.cascade_volume:,.0f}" if c.cascade_volume else "0"
        lines.append(
            f"{row.word[:24]:<24} {pre_str:>9} {start_str:>14} "
            f"{secs_str:>11} {vol_str:>10} {c.shape:>13}",
        )

    n_yes = len(report.yes_rows)
    swept = [r for r in report.yes_rows if r.characterization.seconds_to_95 is not None]
    secs = sorted(r.characterization.seconds_to_95 for r in swept)
    median_secs: float | None
    if secs:
        mid = len(secs) // 2
        median_secs = (
            secs[mid] if len(secs) % 2 == 1
            else (secs[mid - 1] + secs[mid]) / 2.0
        )
    else:
        median_secs = None
    shape_counts: dict[str, int] = {}
    for r in report.yes_rows:
        shape_counts[r.characterization.shape] = shape_counts.get(r.characterization.shape, 0) + 1
    total_volume = sum(r.characterization.cascade_volume for r in report.yes_rows)

    lines += [
        "",
        "Summary",
        "-------",
        f"n markets:            {report.n_markets}",
        f"n YES words:          {n_yes}",
        f"n NO words:           {report.n_no_words}",
        f"n unresolved/skipped: {report.n_unresolved}",
        f"n tape-fetch failed:  {report.n_fetch_failed}",
        f"median seconds_to_95: {median_secs if median_secs is not None else 'n/a'}",
        f"shape counts:         {shape_counts}",
        f"total cascade volume: {total_volume:,.0f} contracts",
    ]

    if report.no_spike_rows:
        lines.append("")
        lines.append("!! NO-resolved words that spiked toward YES then fell back !!")
        lines.append("(false-positive risk for a live detector)")
        for row in report.no_spike_rows:
            s = row.spike
            lines.append(
                f"  {row.word!r} ({row.ticker}): peaked {s.peak_price:.2f} "
                f"@ {_utc_hms(s.peak_ts)}, settled {s.final_price:.2f}",
            )
    else:
        lines.append("")
        lines.append("No NO-resolved words spiked toward YES intra-event.")

    return "\n".join(lines)


def run_event(event_ticker: str, *, max_trades: int = DEFAULT_MAX_TRADES) -> str:
    report = process_event(event_ticker, max_trades=max_trades)
    text = format_report(report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"mention_cascade_backfill_{event_ticker}.txt"
    out_path.write_text(provenance.stamp_line() + "\n" + text + "\n")
    return text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event", action="append", required=True, metavar="EVENT_TICKER",
        help="Event ticker to reconstruct (repeatable, e.g. --event A --event B)",
    )
    parser.add_argument(
        "--max-trades", type=int, default=DEFAULT_MAX_TRADES,
        help="Per-market trade-tape fetch cap (default %(default)s)",
    )
    args = parser.parse_args()

    logctx.setup()

    for event_ticker in args.event:
        text = run_event(event_ticker, max_trades=args.max_trades)
        print(text)
        print()


if __name__ == "__main__":
    main()
