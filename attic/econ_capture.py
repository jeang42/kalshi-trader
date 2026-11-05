"""Econ / scheduled-release book capturer — the non-MLB timing-gap instrument.

Context: MLB is the most efficient corner (all our prior data); the venue scan
found non-MLB *loose* corners (wide spreads, real volume, no HFT) and the edge
target is SURPRISE scheduled releases (CPI/jobs/Fed) — not anticipated events
(the $6.6M Fed-chair market was priced 8 days early). Minute candlesticks can't
resolve the sub-minute "does the book lag the release" question, so we capture
FORWARD at high frequency around the next uncertain release.

Each cycle: discover open econ-release markets that are UNCERTAIN (mid in band,
so a release actually moves them) and close within --max-days (release near),
then poll each market's top-of-book every --interval s into SQLite. After the
next release we measure repricing speed (sub-minute) — is there a reachable lag.

Read-only vs venue. SQLite at data/shared/econ_capture.sqlite (env ECON_CAPTURE_DB).
CLI: `python econ_capture.py` (one cycle) | `--loop` | `--stats`.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import UTC, datetime

import pandas as pd

import kalshi_client as kc


def _db_path() -> str:
    """Resolved on every call, never frozen at import.

    A module-level ``DB = os.getenv(...)`` captures whatever the environment
    held at import time, so nothing downstream — tests especially — can
    redirect it. That is not hypothetical: it is how synthetic rows reached
    this project's production stores, twice, and back_nearclose.py carries the
    same note for the same reason.
    """
    return os.getenv("ECON_CAPTURE_DB", "data/shared/econ_capture.sqlite")
# Series prefixes / keywords for scheduled macro releases.
ECON_KW = ("CPI", "FED", "JOBS", "PAYROLL", "NFP", "PPI", "CLAIMS", "JOBLESS",
           "UNEMP", "INFLATION", "GDP", "RATEHIKE", "RATECUT", "RATEDEC")
LO_MID, HI_MID = 0.10, 0.90      # uncertain band — a release actually moves these
DEFAULT_MAX_DAYS = 14            # release "imminent" window
DEFAULT_INTERVAL = 8             # poll cadence (s) — sub-minute resolution


# ---------------------------------------------------------------------------
# Pure selectors
# ---------------------------------------------------------------------------

def is_econ_series(series: str) -> bool:
    """Does a series prefix look like a scheduled macro release?

    >>> is_econ_series("KXCPIYOY")
    True
    >>> is_econ_series("KXFEDDECISION")
    True
    >>> is_econ_series("KXMLBGAME")
    False
    """
    s = str(series).upper()
    return any(k in s for k in ECON_KW)


def select_targets(df: pd.DataFrame, *, lo: float = LO_MID, hi: float = HI_MID,
                   max_days: float = DEFAULT_MAX_DAYS) -> pd.DataFrame:
    """Pick uncertain, soon-resolving econ markets to watch. Pure over a
    get_markets() frame (cols: id, best_bid, best_ask, days_to_resolution)."""
    if df.empty:
        return df
    d = df.copy()
    d["series"] = d["id"].astype(str).str.split("-").str[0]
    for c in ("best_bid", "best_ask", "days_to_resolution"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d["mid"] = (d["best_bid"] + d["best_ask"]) / 2
    mask = (
        d["series"].map(is_econ_series)
        & d["mid"].between(lo, hi)
        & (d["days_to_resolution"].fillna(1e9) <= max_days)
    )
    return d[mask].copy()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _con():
    c = sqlite3.connect(_db_path())
    c.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        ticker TEXT, ts INTEGER, yes_bid REAL, yes_ask REAL, mid REAL,
        spread REAL, bid_sz REAL, ask_sz REAL, last REAL,
        PRIMARY KEY (ticker, ts))""")
    c.execute("""CREATE TABLE IF NOT EXISTS targets (
        ticker TEXT PRIMARY KEY, series TEXT, question TEXT,
        close_time TEXT, first_seen INTEGER)""")
    return c


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _poll_book(ticker: str) -> dict | None:
    """Top-of-book + total depth for one market via /book."""
    try:
        raw = kc.get_order_book(ticker)
        book = kc.parse_order_book(raw)
        stats = kc.order_book_stats(book)
    except Exception:
        return None
    bb, ba = stats.get("best_bid"), stats.get("best_ask")
    if bb is None and ba is None:
        return None
    mid = (bb + ba) / 2 if (bb is not None and ba is not None) else None
    return {
        "yes_bid": bb, "yes_ask": ba, "mid": mid,
        "spread": (ba - bb) if (bb is not None and ba is not None) else None,
        "bid_sz": stats.get("total_bid_size"), "ask_sz": stats.get("total_ask_size"),
    }


def run_cycle(*, max_days: float = DEFAULT_MAX_DAYS, discover: bool = True,
              verbose: bool = True) -> dict:
    con = _con()
    tgt = pd.DataFrame()
    if discover:  # heavy get_markets — only on the rediscover cadence
        try:
            df = kc.get_markets(limit=1500)
            tgt = select_targets(df, max_days=max_days)
        except Exception as e:
            if verbose:
                print(f"discovery failed ({e}); using cached targets")
    now = _now()
    if not tgt.empty:
        for _, r in tgt.iterrows():
            con.execute(
                "INSERT OR IGNORE INTO targets (ticker,series,question,close_time,first_seen)"
                " VALUES (?,?,?,?,?)",
                (r["id"], r["series"], str(r.get("question", ""))[:140],
                 str(r.get("close_time", "")), now))
        con.commit()
    tickers = [r[0] for r in con.execute("SELECT ticker FROM targets")]
    # poll
    n = 0
    for tk in tickers:
        b = _poll_book(tk)
        if not b:
            continue
        con.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(ticker,ts,yes_bid,yes_ask,mid,spread,bid_sz,ask_sz,last) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (tk, _now(), b["yes_bid"], b["yes_ask"], b["mid"], b["spread"],
             b["bid_sz"], b["ask_sz"], None))
        n += 1
        time.sleep(0.15)  # gentle on the rate limiter
    con.commit()
    if verbose:
        print(f"targets={len(tickers)} snapshots_this_cycle={n}")
    return {"targets": len(tickers), "snapshots": n}


def stats() -> None:
    con = _con()
    nt = con.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    ns = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"econ_capture: {nt} targets, {ns} snapshots")
    print("\nnearest releases (targets by close_time):")
    rows = con.execute(
        "SELECT ticker, close_time, question FROM targets ORDER BY close_time").fetchall()
    for tk, ct, q in rows[:20]:
        print(f"  {ct[:16]:16s} {tk[:34]:34s} {q[:46]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--max-days", type=float, default=DEFAULT_MAX_DAYS)
    ap.add_argument("--rediscover", type=int, default=1800)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.stats:
        stats()
        return
    if not args.loop:
        run_cycle(max_days=args.max_days)
        return
    last_disc = 0.0
    while True:
        # only re-run the heavy discovery occasionally; poll every interval
        do_disc = (time.time() - last_disc) >= args.rediscover
        run_cycle(max_days=args.max_days, discover=do_disc, verbose=do_disc)
        if do_disc:
            last_disc = time.time()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
