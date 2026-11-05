"""Forward-price follow-up — shared helper for the sport capture instruments.

The capture modules record ONE price at event detection, but measuring the
reaction lag needs the price trajectory AFTER the event (does the book move
toward the beneficiary, and how fast?). book_archive is MLB-only and Kalshi's
1-min candles are empty for low-volume tennis/WNBA markets, so we accrue the
forward series ourselves: after a matched event, keep sampling that market's
mid each poll cycle for a few minutes, tagged with seconds-since-event.

This module is the pure mechanics (register follows, emit due samples, expire
old ones). It reuses the price the caller already fetched in discovery — no
extra API calls. Used by `tennis_capture` and `wnba_capture`.
"""

from __future__ import annotations

from collections.abc import Callable


def make_follow(ticker: str, base_ts: float, *, beneficiary: str = "",
                kind: str = "", detection_mid: float | None = None) -> dict:
    """A follow record: the market + when/what the event was, so each later
    sample can be tagged with seconds-since-event and the detection baseline."""
    return {"ticker": str(ticker), "base_ts": float(base_ts),
            "beneficiary": beneficiary, "kind": kind,
            "detection_mid": detection_mid}


def collect_samples(
    follows: dict[str, dict], *, now_ts: float,
    price_of: Callable[[str], tuple[float | None, float | None, float | None] | None],
    max_follow_s: float = 300.0,
) -> list[dict]:
    """Emit one forward sample per active follow and prune expired ones
    (mutates `follows`).

    `price_of(ticker)` returns ``(bid, ask, mid)`` for the current cycle's
    discovery snapshot, or None if the market isn't currently quoted. A follow
    older than `max_follow_s` is dropped (the reaction window has passed).
    Returns sample rows ready for the store.
    """
    rows: list[dict] = []
    expired: list[str] = []
    for key, f in follows.items():
        age = now_ts - f["base_ts"]
        if age > max_follow_s:
            expired.append(key)
            continue
        q = price_of(f["ticker"])
        if q is None:
            continue
        bid, ask, mid = q
        if mid is None:
            continue
        rows.append({
            "follow_key": key, "ticker": f["ticker"],
            "base_ts": f["base_ts"], "sample_ts": now_ts,
            "seconds_since": round(age, 1),
            "beneficiary": f["beneficiary"], "kind": f["kind"],
            "detection_mid": f["detection_mid"],
            "yes_bid": bid, "yes_ask": ask, "yes_mid": mid,
        })
    for k in expired:
        follows.pop(k, None)
    return rows
