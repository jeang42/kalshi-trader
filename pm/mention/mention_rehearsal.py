"""Mention-cascade PAPER-FIRE + post-event scoring harness.

Thesis pointer: docs/edge-research-log.md, 2026-07-19 entry ("MAJOR REVISION:
the trade tape shows the 'instant' reprice is a 7-60s HUMAN CASCADE, and the
prize is 5 figures per event") — building on the 2026-07-18 gate read that
the 10s-cadence capture cadence made instant reprices look like a single
tick. The tape shows real cascades: first fills land ~4s post-utterance,
then a crowd of humans sweeps the book over 7-60s. This module answers the
next question before any real trader is built: if we had fired a real order
the instant our detector fired, where would we have landed in that cascade
and what would we have made, net of fees? It is a REHEARSAL, not a trader —
no orders are ever placed.

Two modes:

  --follow
      Tails `data/shared/mention_detections.jsonl` (produced by
      `mention_listen.py` — the live whisper-detection daemon) for NEW rows
      only. For each, immediately fetches the market's current quote
      (read-only REST) and appends an "intended order" record to
      `data/shared/mention_rehearsal.jsonl`. The quote round-trip latency
      (ts_quote - ts_detected) is itself a measurement of how much of our
      total latency budget quote-fetching alone burns.

  --report EVENT_TICKER
      For every rehearsal/detection row belonging to that event, pulls the
      executed-trade tape (`kalshi_client.get_all_trades`, detection ± 10
      min) and reconstructs: where the cascade started relative to our
      detection (did we beat it?), what price a real order landed at if
      placed `placement_latency` seconds after detection, and what that
      would have been worth net of the resolved outcome + Kalshi fee.
      Writes a text report + prints a summary table.

  --simulate-detection TICKER
      Appends one synthetic detection row (now-timestamps) so `--follow`
      can be smoke-tested end-to-end against a live quote without audio.

Deliberately does NOT import kalshi_orders or execution — this module never
places an order. All the "would we have..." arithmetic is retrospective,
computed from public read-only endpoints (current quote in --follow, the
public trade tape + market result in --report).

CLI:
    python mention_rehearsal.py --follow [--stake 50]
    python mention_rehearsal.py --report KXFTNMENTION-26JUL19 [--placement-latency 0.7]
    python mention_rehearsal.py --simulate-detection KXFTNMENTION-26JUL19-WORD
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pm.core import logctx, provenance
from pm.venue import kalshi_client as kc
from pm.venue.kalshi_fees import fee_for_trade

log = logging.getLogger(__name__)

DETECTIONS_PATH = Path(
    os.getenv("MENTION_DETECTIONS_PATH", "data/shared/mention_detections.jsonl"),
)
REHEARSAL_PATH = Path(
    os.getenv("MENTION_REHEARSAL_PATH", "data/shared/mention_rehearsal.jsonl"),
)
REPORT_DIR = Path(os.getenv("MENTION_REHEARSAL_REPORT_DIR", "data/shared"))

DEFAULT_STAKE_DOLLARS = 50.0
DEFAULT_PLACEMENT_LATENCY_S = 0.7
FEE_RATE = 0.07  # non-sport mention series -> standard rate, not the sports 0.035
FOLLOW_POLL_INTERVAL_S = 0.2
CASCADE_WINDOW_S = 10 * 60  # +/- around detection when pulling the trade tape


# --------------------------------------------------------------------------
# Pure functions (unit-tested against synthetic tapes)
# --------------------------------------------------------------------------


def cascade_start(
    trades: list[tuple[float, float, float]],
    *,
    sweep_price: float = 0.95,
    sweep_window_s: float = 60.0,
    jump_threshold: float = 0.05,
    median_lookback: int = 10,
) -> float | None:
    """First trade timestamp where a sustained sweep toward YES begins.

    ``trades`` is a list of ``(ts_unix, yes_price, count)`` tuples, any
    order (sorted internally). A trade T qualifies as the cascade start
    when:

    1. T's price is below ``sweep_price`` (0.95) — the market hasn't
       already resolved-in-price yet.
    2. Within the following ``sweep_window_s`` (60s) seconds, some trade
       prints at or above ``sweep_price`` — i.e. the sweep actually
       completes shortly after T.
    3. T's price is at least ``jump_threshold`` (0.05) above the median
       price of the prior ``median_lookback`` trades — T is itself a jump,
       not just noise sitting in an already-elevated band.

    Returns the ts_unix of the FIRST such qualifying trade, or ``None``
    when no sweep exists in the tape (already high with no jump, or no
    tape at all).

    Tolerant of missing/sparse data: with fewer than ``median_lookback``
    prior trades, whatever prior trades exist are used (median of 1 trade
    is still an improvement over comparing to nothing). With ZERO prior
    trades (the very first print in the tape) there's no baseline to
    judge a jump against, so that trade never qualifies as a cascade
    start on its own — a genuine cascade needs a quiet-then-jump shape,
    which requires at least one prior print to detect.

    >>> cascade_start([]) is None
    True
    >>> # clean sweep: quiet band then a jump that completes within 60s
    >>> tape = [(0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
    ...         (20, 0.35, 50), (25, 0.72, 80), (30, 0.97, 200)]
    >>> cascade_start(tape)
    20
    >>> # already high, no sweep -> None
    >>> tape2 = [(0, 0.96, 10), (10, 0.97, 10), (20, 0.98, 10)]
    >>> cascade_start(tape2) is None
    True
    """
    if not trades:
        return None
    ordered = sorted(trades, key=lambda t: t[0])
    n = len(ordered)
    for i in range(n):
        ts_i, price_i, _count_i = ordered[i]
        if price_i >= sweep_price:
            continue  # already resolved-in-price; can't be the *start*

        # Condition 3: jump vs the median of prior trades. No prior trades
        # means no baseline to judge a jump against -> not a candidate.
        prior = ordered[max(0, i - median_lookback):i]
        if not prior:
            continue
        prior_prices = sorted(p for _, p, _ in prior)
        mid = len(prior_prices) // 2
        if len(prior_prices) % 2 == 1:
            median_prior = prior_prices[mid]
        else:
            median_prior = (prior_prices[mid - 1] + prior_prices[mid]) / 2.0
        if price_i - median_prior < jump_threshold:
            continue

        # Condition 2: does the tape reach sweep_price within the window?
        cutoff = ts_i + sweep_window_s
        swept = any(
            price_j >= sweep_price
            for ts_j, price_j, _count_j in ordered[i:]
            if ts_i <= ts_j <= cutoff
        )
        if swept:
            return ts_i
    return None


def simulated_fill(
    trades: list[tuple[float, float, float]],
    *,
    t_land: float,
    fill_window_s: float = 2.0,
    fallback_window_s: float = 30.0,
) -> float | None:
    """VWAP fill price for an order landing at ``t_land``.

    Primary rule: volume-weighted average price (VWAP) of every trade in
    ``[t_land, t_land + fill_window_s]``.

    Fallback: when nothing prints in that primary window, use the price of
    the FIRST trade at or after ``t_land`` within ``fallback_window_s``
    (30s) — a thin-book approximation of "we'd have taken whatever was
    resting when the next print finally happened."

    Returns ``None`` when no trade prints within ``fallback_window_s`` of
    ``t_land`` at all (order would not plausibly have filled).

    ``trades`` is ``(ts_unix, yes_price, count)``, any order.

    >>> tape = [(100, 0.30, 10), (100.5, 0.50, 20), (101.5, 0.70, 5),
    ...         (105, 0.99, 50)]
    >>> round(simulated_fill(tape, t_land=100.0), 4)
    0.4714
    >>> simulated_fill(tape, t_land=200.0) is None
    True
    >>> # fallback: nothing in the 2s primary window, but a print at +12s
    >>> tape2 = [(90, 0.20, 10), (112, 0.90, 5)]
    >>> simulated_fill(tape2, t_land=100.0)
    0.9
    """
    if not trades:
        return None
    ordered = sorted(trades, key=lambda t: t[0])

    primary = [
        (ts, price, count)
        for ts, price, count in ordered
        if t_land <= ts <= t_land + fill_window_s
    ]
    if primary:
        total_count = sum(c for _, _, c in primary)
        if total_count > 0:
            vwap = sum(price * c for _, price, c in primary) / total_count
            return vwap
        # Degenerate (zero-size prints) -> simple average.
        return sum(p for _, p, _ in primary) / len(primary)

    fallback_cutoff = t_land + fallback_window_s
    later = [
        (ts, price)
        for ts, price, _count in ordered
        if t_land <= ts <= fallback_cutoff
    ]
    if not later:
        return None
    later.sort(key=lambda t: t[0])
    return later[0][1]


def would_be_pnl(
    fill_price: float,
    *,
    resolved_yes: bool | None,
    contracts: int = 1,
    fee_rate: float = FEE_RATE,
) -> float | None:
    """Net P&L per the fee model, for a YES buy at ``fill_price``.

    Returns ``None`` when the market hasn't resolved yet (``resolved_yes``
    is ``None``). Buying YES: pnl = (1.00 - fill - fee) if resolved YES,
    else -(fill + fee) — we lose the whole stake plus the fee we already
    paid on entry.

    >>> round(would_be_pnl(0.60, resolved_yes=True, contracts=100), 2)
    38.32
    >>> round(would_be_pnl(0.60, resolved_yes=False, contracts=100), 2)
    -61.68
    >>> would_be_pnl(0.5, resolved_yes=None) is None
    True
    """
    if resolved_yes is None:
        return None
    fee = fee_for_trade(fill_price, contracts, fee_rate=fee_rate)
    if resolved_yes:
        return (1.0 - fill_price) * contracts - fee
    return -fill_price * contracts - fee


# --------------------------------------------------------------------------
# JSONL helpers
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("Skipping malformed JSONL line in %s", path)
    return rows


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


@dataclass
class JsonlTailer:
    """Tails a JSONL file, yielding only rows appended since last read.

    Tracks progress by line count, plus (file size, inode) so a file
    that's truncated-and-rewritten or rotated (e.g. logrotate's
    copytruncate, or a fresh file replacing the old one) is detected and
    re-read from the start rather than silently stalling forever. Inode
    tracking catches same-size rotations that a size-only shrink check
    would miss.
    """

    path: Path
    _lines_seen: int = field(default=0, init=False)
    _last_size: int = field(default=0, init=False)
    _last_ino: int | None = field(default=None, init=False)

    def poll(self) -> list[dict]:
        if not self.path.exists():
            return []
        st = self.path.stat()
        rotated = st.st_size < self._last_size or (
            self._last_ino is not None and st.st_ino != self._last_ino
        )
        if rotated:
            self._lines_seen = 0
        self._last_size = st.st_size
        self._last_ino = st.st_ino

        rows = _read_jsonl(self.path)
        if len(rows) <= self._lines_seen:
            return []
        new_rows = rows[self._lines_seen:]
        self._lines_seen = len(rows)
        return new_rows


# --------------------------------------------------------------------------
# Timestamp normalization
# --------------------------------------------------------------------------


def _ts_seconds(ts: float | int | None) -> float | None:
    """Normalize a detection timestamp to epoch SECONDS.

    ``mention_listen`` writes ``ts_detected`` as epoch **milliseconds** (its
    documented schema), but every consumer here — the trade-tape window, the
    cascade lag, the simulated fill — works in seconds. Mixing the two silently
    poisoned the whole report: ``get_all_trades`` 400s on a ms ``min_ts``, so
    every row scored ``n/a`` and the false-positive count read 0.

    Values above 1e11 are milliseconds (1e11 s is year 5138); anything smaller
    is already seconds. Handles both old rows on disk and any future writer.

    >>> _ts_seconds(1784930815354)
    1784930815.354
    >>> _ts_seconds(1784930815.354)
    1784930815.354
    >>> _ts_seconds(None) is None
    True
    """
    if ts is None:
        return None
    ts = float(ts)
    return ts / 1000.0 if ts > 1e11 else ts


# --------------------------------------------------------------------------
# --follow mode
# --------------------------------------------------------------------------


def rehearse_detection(
    detection: dict,
    *,
    stake_dollars: float = DEFAULT_STAKE_DOLLARS,
    get_market_fn: Callable[[str], dict] = kc.get_market,
    now_fn: Callable[[], float] = time.time,
) -> dict:
    """Fetch a live quote for one detection row and build the rehearsal record.

    Pure aside from the injected ``get_market_fn``/``now_fn`` — easy to
    unit-test without hitting the network.
    """
    ticker = detection.get("ticker", "")
    ts_detected = detection.get("ts_detected")

    market = get_market_fn(ticker) or {}
    ts_quote = now_fn()

    yes_bid = float(market.get("yes_bid_dollars") or 0.0)
    yes_ask = float(market.get("yes_ask_dollars") or 0.0)

    price = min(yes_ask + 0.02, 0.99) if yes_ask > 0 else min(0.99, 0.99)
    count = int(math.floor(stake_dollars / price)) if price > 0 else 0

    return {
        "ts_detected": ts_detected,
        "ts_quote": ts_quote,
        "ticker": ticker,
        "event_ticker": detection.get("event_ticker", ""),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "intended": {
            "side": "yes",
            "price": round(price, 4),
            "count": count,
        },
        "stake_dollars": stake_dollars,
        "matched_text": detection.get("matched_text", ""),
        "quote_latency_s": (
            round(ts_quote - _ts_seconds(ts_detected), 3)  # type: ignore[operator]
            if ts_detected is not None else None
        ),
    }


def run_follow(
    *,
    stake_dollars: float = DEFAULT_STAKE_DOLLARS,
    poll_interval_s: float = FOLLOW_POLL_INTERVAL_S,
    max_cycles: int | None = None,
) -> None:
    """Tail mention_detections.jsonl forever, rehearsing every new row."""
    tailer = JsonlTailer(DETECTIONS_PATH)
    log.info(
        "mention_rehearsal --follow starting (watching %s, stake=$%.2f)",
        DETECTIONS_PATH, stake_dollars,
    )
    cycles = 0
    while True:
        for detection in tailer.poll():
            try:
                record = rehearse_detection(detection, stake_dollars=stake_dollars)
            except Exception:
                log.exception(
                    "Failed to rehearse detection %s", detection.get("ticker"),
                )
                continue
            _append_jsonl(REHEARSAL_PATH, record)
            log.info(
                "Rehearsed %s: quote_latency=%.3fs bid=%.2f ask=%.2f -> "
                "intended price=%.2f count=%d",
                record["ticker"],
                record["quote_latency_s"] or -1.0,
                record["yes_bid"], record["yes_ask"],
                record["intended"]["price"], record["intended"]["count"],
            )
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        time.sleep(poll_interval_s)


def simulate_detection(ticker: str, event_ticker: str = "") -> dict:
    """Append one synthetic detection row for end-to-end smoke testing."""
    now = time.time()
    row = {
        "ts_detected": now,
        "ts_word_audio": now,
        "event_ticker": event_ticker or ticker.rsplit("-", 1)[0],
        "ticker": ticker,
        "matched_text": "(simulated)",
        "pattern": "(simulated)",
        "model": "(simulated)",
        "source": "mention_rehearsal --simulate-detection",
    }
    _append_jsonl(DETECTIONS_PATH, row)
    return row


# --------------------------------------------------------------------------
# --report mode
# --------------------------------------------------------------------------


def _trades_to_tuples(df: Any) -> list[tuple[float, float, float]]:
    if df is None or len(df) == 0:
        return []
    return [
        (float(row.ts_unix), float(row.yes_price), float(row.count))
        for row in df.itertuples()
    ]


@dataclass
class RehearsalScore:
    ticker: str
    matched_text: str
    ts_detected: float | None
    cascade_start_ts: float | None
    our_lag_s: float | None
    fill: float | None
    resolved_yes: bool | None
    pnl_per_contract: float | None
    stake_dollars: float
    pnl_at_stake: float | None


def score_event(
    event_ticker: str,
    *,
    placement_latency_s: float = DEFAULT_PLACEMENT_LATENCY_S,
    fee_rate: float = FEE_RATE,
    get_all_trades_fn: Callable[..., Any] = kc.get_all_trades,
    get_market_fn: Callable[[str], dict] = kc.get_market,
) -> list[RehearsalScore]:
    """Score every rehearsal/detection row belonging to ``event_ticker``."""
    rehearsals = {
        r["ticker"]: r
        for r in _read_jsonl(REHEARSAL_PATH)
        if r.get("event_ticker") == event_ticker
    }
    detections = [
        d for d in _read_jsonl(DETECTIONS_PATH)
        if d.get("event_ticker") == event_ticker
    ]
    # Merge: prefer rehearsal rows (they carry stake/intended), fall back
    # to bare detections for tickers we never got a quote for.
    by_ticker: dict[str, dict] = {}
    for d in detections:
        by_ticker.setdefault(d["ticker"], {}).update(d)
    for ticker, r in rehearsals.items():
        by_ticker.setdefault(ticker, {}).update(r)

    scores: list[RehearsalScore] = []
    for ticker, row in by_ticker.items():
        # Detections carry epoch MILLISECONDS; the tape window, cascade lag and
        # fill simulation below are all in seconds. Normalize once, here.
        ts_detected = _ts_seconds(row.get("ts_detected"))
        stake_dollars = row.get("stake_dollars", DEFAULT_STAKE_DOLLARS)
        matched_text = row.get("matched_text", "")

        if ts_detected is None:
            scores.append(RehearsalScore(
                ticker=ticker, matched_text=matched_text, ts_detected=None,
                cascade_start_ts=None, our_lag_s=None, fill=None,
                resolved_yes=None, pnl_per_contract=None,
                stake_dollars=stake_dollars, pnl_at_stake=None,
            ))
            continue

        since_ts = int(ts_detected - CASCADE_WINDOW_S)
        until_ts = int(ts_detected + CASCADE_WINDOW_S)
        try:
            trades_df = get_all_trades_fn(ticker, since_ts=since_ts, until_ts=until_ts)
        except Exception:
            log.exception("Failed to fetch trade tape for %s", ticker)
            trades_df = None
        tape = _trades_to_tuples(trades_df)

        c_start = cascade_start(tape)
        our_lag = (ts_detected - c_start) if c_start is not None else None

        t_land = ts_detected + placement_latency_s
        fill = simulated_fill(tape, t_land=t_land)

        resolved_yes: bool | None = None
        try:
            market = get_market_fn(ticker) or {}
            result = str(market.get("result", "")).lower()
            if result == "yes":
                resolved_yes = True
            elif result == "no":
                resolved_yes = False
        except Exception:
            log.exception("Failed to fetch resolution for %s", ticker)

        pnl_per_contract = (
            would_be_pnl(fill, resolved_yes=resolved_yes, contracts=1, fee_rate=fee_rate)
            if fill is not None else None
        )
        contracts_at_stake = (
            int(math.floor(stake_dollars / fill)) if fill and fill > 0 else 0
        )
        pnl_at_stake = (
            would_be_pnl(
                fill, resolved_yes=resolved_yes,
                contracts=contracts_at_stake, fee_rate=fee_rate,
            )
            if fill is not None and contracts_at_stake > 0 else None
        )

        scores.append(RehearsalScore(
            ticker=ticker, matched_text=matched_text, ts_detected=ts_detected,
            cascade_start_ts=c_start, our_lag_s=our_lag, fill=fill,
            resolved_yes=resolved_yes, pnl_per_contract=pnl_per_contract,
            stake_dollars=stake_dollars, pnl_at_stake=pnl_at_stake,
        ))
    return scores


def format_report(event_ticker: str, scores: list[RehearsalScore]) -> str:
    lines = [f"Mention rehearsal report: {event_ticker}", "=" * 60, ""]
    header = (
        f"{'ticker':<28} {'matched':<12} {'lag_s':>8} {'fill':>6} "
        f"{'resolved':>9} {'pnl/ct':>8} {'pnl@stake':>10}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for s in scores:
        lag_str = f"{s.our_lag_s:.1f}" if s.our_lag_s is not None else "n/a"
        fill_str = f"{s.fill:.3f}" if s.fill is not None else "n/a"
        resolved_str = (
            "YES" if s.resolved_yes is True
            else "NO" if s.resolved_yes is False
            else "pending"
        )
        pnl_ct_str = f"{s.pnl_per_contract:.3f}" if s.pnl_per_contract is not None else "n/a"
        pnl_stake_str = f"{s.pnl_at_stake:.2f}" if s.pnl_at_stake is not None else "n/a"
        lines.append(
            f"{s.ticker:<28} {s.matched_text[:12]:<12} {lag_str:>8} {fill_str:>6} "
            f"{resolved_str:>9} {pnl_ct_str:>8} {pnl_stake_str:>10}",
        )

    n = len(scores)
    beat_cascade = sum(
        1 for s in scores if s.our_lag_s is not None and s.our_lag_s <= 0
    )
    lags = sorted(s.our_lag_s for s in scores if s.our_lag_s is not None)
    median_lag = (
        lags[len(lags) // 2] if lags and len(lags) % 2 == 1
        else (lags[len(lags) // 2 - 1] + lags[len(lags) // 2]) / 2.0
        if lags else None
    )
    total_pnl = sum(s.pnl_at_stake for s in scores if s.pnl_at_stake is not None)
    false_positives = [
        s for s in scores
        if s.resolved_yes is False and s.pnl_at_stake is not None
    ]
    fp_cost = sum(s.pnl_at_stake for s in false_positives)

    # Detector precision is reported INDEPENDENTLY of whether a fill was
    # simulated. `false_positives` above is the *cost* of bad detections and so
    # requires a fill; but a detection that resolved NO is a detector error
    # whether or not we could have traded it, and that ratio is the number the
    # thesis lives on. Reporting only the fill-gated count understates it.
    n_scored = sum(1 for s in scores if s.resolved_yes is not None)
    n_wrong = sum(1 for s in scores if s.resolved_yes is False)
    precision = (
        f"{100.0 * (n_scored - n_wrong) / n_scored:.0f}% "
        f"({n_scored - n_wrong}/{n_scored})" if n_scored else "n/a"
    )

    lines += [
        "",
        "Summary",
        "-------",
        f"n detections:        {n}",
        f"detector precision:  {precision}   [{n_wrong} resolved NO]",
        f"n beat cascade:      {beat_cascade}",
        f"median lag (s):      {median_lag if median_lag is not None else 'n/a'}",
        f"total would-be P&L:  ${total_pnl:.2f}",
        f"false positives:     {len(false_positives)} filled (cost ${fp_cost:.2f})",
    ]
    if false_positives:
        lines.append("")
        lines.append("!! FALSE POSITIVES (detected but resolved NO) !!")
        for s in false_positives:
            lines.append(f"  {s.ticker} matched={s.matched_text!r} pnl@stake=${s.pnl_at_stake:.2f}")

    return "\n".join(lines)


def run_report(event_ticker: str, *, placement_latency_s: float) -> str:
    scores = score_event(event_ticker, placement_latency_s=placement_latency_s)
    text = format_report(event_ticker, scores)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"mention_rehearsal_report_{event_ticker}.txt"
    out_path.write_text(provenance.stamp_line() + "\n" + text + "\n")
    return text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--follow", action="store_true", help="Tail detections and rehearse each in real time")
    parser.add_argument("--stake", type=float, default=DEFAULT_STAKE_DOLLARS, help="Dollar stake per rehearsed order")
    parser.add_argument("--report", metavar="EVENT_TICKER", help="Score a past event's cascade rehearsal")
    parser.add_argument("--placement-latency", type=float, default=DEFAULT_PLACEMENT_LATENCY_S, help="Assumed seconds from detection to order landing")
    parser.add_argument("--simulate-detection", metavar="TICKER", help="Append one synthetic detection row for smoke-testing --follow")
    args = parser.parse_args()

    logctx.setup()

    if args.simulate_detection:
        row = simulate_detection(args.simulate_detection)
        print(f"Appended synthetic detection: {row}")
        return

    if args.report:
        text = run_report(args.report, placement_latency_s=args.placement_latency)
        print(text)
        return

    if args.follow:
        run_follow(stake_dollars=args.stake)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
