"""Reaction capacity + edge-quality analysis — deeper-vs-wider decision tool.

We have one validated edge (in-game react-and-hold, ~+2.5c/contract on MLB) but
two open questions decide how to scale it toward the leaderboard:

  1. CAPACITY — how many contracts can we actually buy near the ask at a reaction
     moment? A thin book caps $ per trade, which decides deeper (fatten MLB) vs
     wider (many sports in parallel).
  2. EDGE QUALITY — does the +EV concentrate in the highest-WPA plays and/or the
     thinnest books (where the lag should be biggest)? Better selection could
     fatten the edge enough to carry real size.

Method: for each real react_entry (`reactive_log.jsonl`, carries ticker + ts +
wpa + entry price), find the nearest `book_archive` snapshot, parse executable
depth on the buy side, and resolve the game (`baseball.resolve_pending_tickers`)
to compute hold-to-resolution P&L. Then bucket by |WPA| and by book depth.

Read-only; prints a report. No trades.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pandas as pd

import kalshi_fees

log = logging.getLogger(__name__)

REACTIVE_LOG = Path("data/shared/reactive_log.jsonl")
BOOK_DB = Path("data/shared/book_archive.sqlite")


# ─── Pure helpers (testable) ───────────────────────────────────────────────

def executable_depth(book_json: str, *, side: str = "ask",
                     within_cents: float = 0.03) -> tuple[float, float]:
    """(contracts, dollars) available to BUY within `within_cents` of the best
    ask (the realistic fill window for a marketable reaction buy). Returns
    (0,0) on unparseable/empty book."""
    try:
        levels = json.loads(book_json)
    except (TypeError, ValueError):
        return 0.0, 0.0
    asks = sorted((lv for lv in levels if lv.get("side") == side),
                  key=lambda lv: lv.get("price", 1.0))
    if not asks:
        return 0.0, 0.0
    best = asks[0]["price"]
    contracts = 0.0
    dollars = 0.0
    for lv in asks:
        if lv["price"] > best + within_cents:
            break
        contracts += float(lv.get("size") or 0)
        dollars += float(lv.get("size") or 0) * float(lv["price"])
    return contracts, dollars


def _hold_pnl(entry: float, won: bool, *, ticker: str = "") -> float:
    rate = kalshi_fees.fee_rate_for_series(ticker) if ticker else 0.035
    fee = kalshi_fees.fee_for_trade(entry, 1, fee_rate=rate)
    return (1.0 if won else 0.0) - entry - fee


def summarize_by_bucket(df: pd.DataFrame, col: str, edges: list[float],
                        labels: list[str]) -> pd.DataFrame:
    """Mean net P&L/contract, win rate, n per bucket of `col`."""
    out = []
    df = df.copy()
    df["_bucket"] = pd.cut(df[col], bins=edges, labels=labels, include_lowest=True)
    for lab in labels:
        g = df[df["_bucket"] == lab]
        if g.empty:
            continue
        out.append({"bucket": lab, "n": len(g),
                    "net_per_contract": round(g["pnl"].mean(), 4),
                    "win_rate": round(g["won"].mean(), 3),
                    "mean_depth_contracts": round(g["depth_contracts"].mean(), 0),
                    "mean_edge_below_fair": round(g["edge_below_fair"].mean(), 4)})
    return pd.DataFrame(out)


# ─── Join + analyze (network/db) ──────────────────────────────────────────

def _nearest_book(conn, market_id: str, ts_unix: int,
                  *, window_s: int = 120) -> str | None:
    """book_json of the snapshot nearest (within window) to ts for the YES
    token of market_id. The arm bought YES, so we want the YES-side book."""
    # Bound snapshot_ts to a range so SQLite uses idx_by_ts — without the range
    # a bare market_id filter can't use the PK index (PK starts with `venue`)
    # and full-scans all 16.7M rows per lookup.
    row = conn.execute(
        "SELECT book_json FROM book_snapshots "
        "WHERE snapshot_ts BETWEEN ? AND ? AND market_id = ? "
        "ORDER BY ABS(snapshot_ts - ?) ASC LIMIT 1",
        (ts_unix - window_s, ts_unix + window_s, market_id, ts_unix)).fetchone()
    return row[0] if row else None


def build_dataset(*, within_cents: float = 0.03) -> pd.DataFrame:
    import baseball
    rows = [json.loads(line) for line in REACTIVE_LOG.read_text().splitlines()
            if '"react_entry"' in line]
    entries = [r for r in rows if (r.get("fill_price") or r.get("ask"))]
    tickers = sorted({r["ticker"] for r in entries})
    resolved = baseball.resolve_pending_tickers(tickers)

    conn = sqlite3.connect(str(BOOK_DB))
    out = []
    for r in entries:
        tk = r["ticker"]
        if tk not in resolved:
            continue
        entry = float(r.get("fill_price") or r.get("ask"))
        ts = pd.Timestamp(r["ts"]).timestamp()
        book = _nearest_book(conn, tk, int(ts))
        depth_c, depth_d = executable_depth(book, within_cents=within_cents) \
            if book else (0.0, 0.0)
        won = bool(resolved[tk])
        out.append({
            "ticker": tk, "wpa": abs(float(r.get("wpa") or 0)),
            "entry": entry, "won": won,
            "pnl": _hold_pnl(entry, won, ticker=tk),
            "depth_contracts": depth_c, "depth_dollars": depth_d,
            "has_book": book is not None,
            # how far below fair (resolution) we bought — the raw edge
            "edge_below_fair": (1.0 if won else 0.0) - entry,
        })
    conn.close()
    return pd.DataFrame(out)


def format_report(df: pd.DataFrame, *, within_cents: float) -> str:
    L = ["=" * 70, "REACTION CAPACITY + EDGE-QUALITY ANALYSIS",
         f"  react entries resolved: {len(df)}  "
         f"(with book: {int(df['has_book'].sum())})", "=" * 70, ""]
    booked = df[df["has_book"]]
    if booked.empty:
        L.append("No reaction entries matched a book snapshot — can't measure "
                 "capacity. (book_archive may not cover these tickers/times.)")
        L.append("=" * 70)
        return "\n".join(L)

    # CAPACITY
    d = booked["depth_contracts"]
    dd = booked["depth_dollars"]
    L.append(f"[CAPACITY] executable depth within {within_cents*100:.0f}c of ask:")
    L.append(f"  contracts: median {d.median():.0f}  mean {d.mean():.0f}  "
             f"p25 {d.quantile(.25):.0f}  p75 {d.quantile(.75):.0f}")
    L.append(f"  dollars:   median ${dd.median():.0f}  mean ${dd.mean():.0f}  "
             f"p25 ${dd.quantile(.25):.0f}")
    L.append(f"  trades with < $50 available: {(dd < 50).mean()*100:.0f}%  "
             f"| < $200: {(dd < 200).mean()*100:.0f}%")
    L.append("")

    # OVERALL EDGE
    L.append(f"[EDGE] all resolved entries: n={len(df)} "
             f"net/contract={df['pnl'].mean():+.4f} win={df['won'].mean():.3f} "
             f"mean_edge_below_fair={df['edge_below_fair'].mean():+.4f}")
    L.append("")

    # BY WPA
    L.append("[BY |WPA|] does the edge concentrate in bigger swings?")
    wpa = summarize_by_bucket(df, "wpa", [0, 0.12, 0.18, 0.30, 1.0],
                              ["0.10-0.12", "0.12-0.18", "0.18-0.30", "0.30+"])
    L.append(wpa.to_string(index=False) if not wpa.empty else "  (insufficient)")
    L.append("")

    # BY BOOK DEPTH (thin books should lag more → bigger edge)
    L.append("[BY BOOK DEPTH] is the edge bigger in THIN books (more lag)?")
    if booked["depth_contracts"].nunique() > 3:
        q = booked["depth_contracts"].quantile([.33, .66]).tolist()
        depth = summarize_by_bucket(
            booked, "depth_contracts", [0, q[0], q[1], 1e9],
            ["thin", "mid", "deep"])
        L.append(depth.to_string(index=False) if not depth.empty else "  (insufficient)")
    else:
        L.append("  (insufficient depth variation)")
    L.append("")
    L.append("READ: capacity median $ = per-trade size ceiling → if tiny, breadth")
    L.append("(more sports) is the only scaling path; if decent, deepen MLB. Edge")
    L.append("concentrating in high-WPA / thin-book buckets = a sharper selection rule.")
    L.append("=" * 70)
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--within-cents", type=float, default=0.03)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    df = build_dataset(within_cents=args.within_cents)
    print(format_report(df, within_cents=args.within_cents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
