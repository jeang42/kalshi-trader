"""Near-close capture — an idler that builds a clean near-close price dataset.

The favorite-longshot mining stalled on sample size: historical near-close
prices aren't reachable (Kalshi's settled-events endpoint is newest-first). The
fix is to stop looking backward and accrue forward — exactly the idle-game
model. This job, run on a schedule:

  * CAPTURE — once per market, when it first falls within `target_lead_hours`
    of close, snapshot top-of-book (best_bid/ask), last price, volume, category,
    and event group. Zero extra API calls beyond one closing-soon fetch.
  * RESOLVE — fill in the outcome once captured markets settle.

Over weeks this becomes the unpolluted near-close panel the mining studies need
(price you could actually trade at + true outcome), growing on its own with no
rate-limit fight and no lookback limit. Read-only against the venue; writes only
its own SQLite at data/shared/near_close_capture.sqlite (env NEAR_CLOSE_DB).

CLI:
  python near_close_capture.py                      # one capture+resolve cycle
  python near_close_capture.py --loop --interval 1800
  python near_close_capture.py --stats
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import logctx, shutdown, sqlite_schema
from pm.venue import kalshi_client  # noqa: F401 — registers Kalshi provider
from pm.venue.provider import get_provider

log = logging.getLogger(__name__)

DEFAULT_DB = Path("data/shared/near_close_capture.sqlite")


def _db_path() -> Path:
    return Path(os.getenv("NEAR_CLOSE_DB", str(DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    sqlite_schema.set_wal(conn)   # busy-safe (R-27)
    conn.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(conn, store="near_close_capture", migrations=SCHEMA, db_path=p)
    return conn


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape verbatim. Constraints on the capture
# tables are deferred: adding CHECK/NOT NULL means a table rebuild, which is
# not something a capture daemon should do on the way to its next cycle.

def _schema_v1(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ticker        TEXT PRIMARY KEY,
            question      TEXT,
            category      TEXT,
            event_id      TEXT,
            close_time    TEXT,
            snapshot_ts   TEXT,
            lead_hours    REAL,
            yes_bid       REAL,
            yes_ask       REAL,
            yes_mid       REAL,
            last_price    REAL,
            spread        REAL,
            volume        REAL,
            resolved_yes  INTEGER,   -- NULL until settled, then 0/1
            resolved_at   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_snap_unresolved "
                 "ON snapshots(resolved_yes, close_time)")


SCHEMA = [sqlite_schema.Migration(1, "adopt pre-versioning tables", _schema_v1)]


def migrate() -> None:
    """Bring the store to the current schema version. Idempotent, and also
    happens on every ``_connect()`` — kept as the explicit entry point the
    runners call at cycle start."""
    _connect().close()



# ─── Pure helpers (testable) ───────────────────────────────────────────────

def _lead_hours(close_time: str, now: datetime) -> float | None:
    """Hours from `now` until close (negative if already past)."""
    try:
        ct = pd.Timestamp(close_time)
        ct = ct.tz_localize("UTC") if ct.tzinfo is None else ct.tz_convert("UTC")
    except Exception:  # noqa: BLE001
        return None
    return (ct.to_pydatetime() - now).total_seconds() / 3600.0


def _yes_mid(bid: float | None, ask: float | None,
             last: float | None) -> float | None:
    """Top-of-book mid; fall back to last trade if a side is missing."""
    b = bid if bid and bid > 0 else None
    a = ask if ask and ask < 1 else None
    if b is not None and a is not None:
        return (b + a) / 2.0
    if last is not None and 0 < last < 1:
        return float(last)
    return b if b is not None else a


def _resolution_updates(unresolved: list[str],
                        closed_lookup: dict[str, bool]) -> dict[str, bool]:
    """Which captured tickers can now be resolved, from a {ticker: yes} map."""
    return {t: closed_lookup[t] for t in unresolved if t in closed_lookup}


# ─── Store ops ─────────────────────────────────────────────────────────────

def _existing_tickers() -> set[str]:
    with _connect() as conn:
        return {r[0] for r in conn.execute("SELECT ticker FROM snapshots")}


def store_snapshots(rows: list[dict]) -> int:
    """Insert new snapshots; INSERT OR IGNORE keeps the first (closest to the
    target lead) and makes re-runs idempotent."""
    if not rows:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany("""
            INSERT OR IGNORE INTO snapshots
              (ticker, question, category, event_id, close_time, snapshot_ts,
               lead_hours, yes_bid, yes_ask, yes_mid, last_price, spread,
               volume, resolved_yes, resolved_at)
            VALUES
              (:ticker, :question, :category, :event_id, :close_time,
               :snapshot_ts, :lead_hours, :yes_bid, :yes_ask, :yes_mid,
               :last_price, :spread, :volume, NULL, NULL)
        """, rows)
        return conn.total_changes - before


def unresolved_past_close(now: datetime) -> list[str]:
    iso = now.isoformat()
    with _connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT ticker FROM snapshots WHERE resolved_yes IS NULL "
            "AND close_time < ?", (iso,))]


def apply_resolutions(updates: dict[str, bool], now: datetime) -> int:
    if not updates:
        return 0
    iso = now.isoformat()
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany(
            "UPDATE snapshots SET resolved_yes=?, resolved_at=? WHERE ticker=?",
            [(1 if yes else 0, iso, t) for t, yes in updates.items()])
        return conn.total_changes - before


def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE resolved_yes IS NOT NULL"
        ).fetchone()[0]
        cats = conn.execute(
            "SELECT category, COUNT(*) c FROM snapshots GROUP BY category "
            "ORDER BY c DESC LIMIT 8").fetchall()
        oldest = conn.execute(
            "SELECT MIN(snapshot_ts) FROM snapshots").fetchone()[0]
    return {"total": total, "resolved": resolved, "pending": total - resolved,
            "by_category": {r[0]: r[1] for r in cats}, "oldest": oldest}


def load_panel(*, resolved_only: bool = True) -> pd.DataFrame:
    """The accrued near-close panel as a DataFrame (for the mining studies)."""
    q = "SELECT * FROM snapshots"
    if resolved_only:
        q += " WHERE resolved_yes IS NOT NULL"
    with _connect() as conn:
        return pd.read_sql_query(q, conn)


# ─── Cycle ─────────────────────────────────────────────────────────────────

def capture_cycle(provider, *, target_lead_hours: float, min_volume: float,
                  now: datetime | None = None) -> int:
    """Snapshot markets newly within `target_lead_hours` of close."""
    now = now or datetime.now(UTC)
    df = provider.get_markets_closing_within(
        target_lead_hours / 24.0, limit=1000)
    if df is None or df.empty:
        return 0
    have = _existing_tickers()
    rows: list[dict] = []
    for _, m in df.iterrows():
        if shutdown.requested():
            break
        ticker = str(m["condition_id"])
        if ticker in have:
            continue
        if min_volume > 0 and float(m.get("volume", 0) or 0) < min_volume:
            continue
        lead = _lead_hours(str(m.get("close_time", "")), now)
        if lead is None or lead < 0:
            continue
        bid = float(m.get("best_bid") or 0) or None
        ask = float(m.get("best_ask") or 0) or None
        last = float(m.get("last_trade_price") or 0) or None
        rows.append({
            "ticker": ticker, "question": str(m.get("question", "")),
            "category": str(m.get("category", "")),
            "event_id": str(m.get("event_id", "")),
            "close_time": str(m.get("close_time", "")),
            "snapshot_ts": now.isoformat(), "lead_hours": round(lead, 3),
            "yes_bid": bid, "yes_ask": ask, "yes_mid": _yes_mid(bid, ask, last),
            "last_price": last, "spread": float(m.get("spread") or 0) or None,
            "volume": float(m.get("volume", 0) or 0),
        })
    n = store_snapshots(rows)
    if n:
        log.info("captured %d new near-close snapshots", n)
    return n


def resolve_cycle(provider, *, lookback_days: float = 4.0,
                  now: datetime | None = None) -> int:
    """Fill outcomes for captured markets that have since settled. Only scans
    the venue when something is actually waiting to resolve."""
    now = now or datetime.now(UTC)
    pending = unresolved_past_close(now)
    if not pending:
        return 0
    df_closed = provider.get_closed_markets_in_range(
        since_days=lookback_days, until_days=0.0, max_items=5000, min_volume=0.0)
    if df_closed is None or df_closed.empty:
        return 0
    lookup = {str(r["condition_id"]): bool(r["resolved_yes"])
              for _, r in df_closed.iterrows()
              if pd.notna(r.get("resolved_yes"))}
    updates = _resolution_updates(pending, lookup)
    n = apply_resolutions(updates, now)
    if n:
        log.info("resolved %d captured markets", n)
    return n


def run_cycle(*, target_lead_hours: float = 2.0, min_volume: float = 50.0,
              lookback_days: float = 4.0) -> dict:
    migrate()
    prov = get_provider("kalshi")
    captured = capture_cycle(prov, target_lead_hours=target_lead_hours,
                             min_volume=min_volume)
    resolved = resolve_cycle(prov, lookback_days=lookback_days)
    return {"captured": captured, "resolved": resolved}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target-lead-hours", type=float, default=2.0,
                   help="Capture a market when it first falls within this many "
                        "hours of close (the near-close entry point).")
    p.add_argument("--min-volume", type=float, default=50.0,
                   help="Skip thin/synthetic markets at capture.")
    p.add_argument("--lookback-days", type=float, default=4.0,
                   help="Recent settled window scanned to resolve outcomes.")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=1800)
    p.add_argument("--stats", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())
    if args.stats:
        migrate()
        print(stats())
        return 0
    if not args.loop:
        log.info("cycle: %s", run_cycle(
            target_lead_hours=args.target_lead_hours,
            min_volume=args.min_volume, lookback_days=args.lookback_days))
        return 0
    log.info("near_close_capture starting (lead=%.1fh, interval=%ds)",
             args.target_lead_hours, args.interval)
    shutdown.install()          # SIGTERM finishes the current step, then exits (R-20)
    while not shutdown.requested():
        try:
            s = run_cycle(target_lead_hours=args.target_lead_hours,
                          min_volume=args.min_volume,
                          lookback_days=args.lookback_days)
            if s["captured"] or s["resolved"]:
                log.info("cycle: captured=%d resolved=%d",
                         s["captured"], s["resolved"])
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        if shutdown.sleep(max(60, args.interval)):
            break
    log.info("clean shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
