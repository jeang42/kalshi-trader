"""WNBA capture — step 2 of react-and-hold for WNBA: MEASURE the lag.

`wnba_live.py` detects in-game win-prob swings (ESPN's homeWinPercentage moving
>= threshold) with a beneficiary team. This instrument joins those swings to the
Kalshi market: on a swing, find the market where the beneficiary is the YES side
and record the price AT DETECTION, so we can later measure whether the book
moves toward that side afterward — does the WNBA book lag like MLB's? We do NOT
assume MLB's edge transfers; this is the gate before arming.

Reuses: `wnba_live.live_games`/`detect_events` (detector),
`wnba_runner._discover_wnba_markets` (open KXWNBAGAME markets via the events
path), and `wnba._parse_ticker` (ticker → yes-team ESPN abbr). Matching is
abbr→abbr (the beneficiary is already an ESPN abbr), so it's exact — no fuzzy
names like tennis. Ambiguous (same team twice) → skip. Read-only; writes only
its SQLite (env WNBA_CAPTURE_DB). Mirrors `tennis_capture.py`.

CLI:
  python wnba_capture.py                  # one cycle
  python wnba_capture.py --loop --interval 30
  python wnba_capture.py --stats
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import logctx, sqlite_schema
from pm.live import event_followup as ef
from pm.live import wnba_live

log = logging.getLogger(__name__)

DEFAULT_DB = Path("data/shared/wnba_capture.sqlite")


def _db_path() -> Path:
    return Path(os.getenv("WNBA_CAPTURE_DB", str(DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    sqlite_schema.set_wal(conn)   # busy-safe (R-27)
    conn.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(conn, store="wnba_capture", migrations=SCHEMA, db_path=p)
    return conn


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape verbatim. Constraints on the capture
# tables are deferred: adding CHECK/NOT NULL means a table rebuild, which is
# not something a capture daemon should do on the way to its next cycle.

def _schema_v1(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wnba_events (
            game_id      TEXT,
            event_seq    INTEGER,
            kind         TEXT,        -- "wp_swing" | "game"
            beneficiary  TEXT,        -- ESPN abbr
            wp_delta     REAL,
            detail       TEXT,
            captured_at  TEXT,
            ticker       TEXT,        -- matched Kalshi market (NULL if none)
            question     TEXT,
            yes_bid      REAL,
            yes_ask      REAL,
            yes_mid      REAL,
            PRIMARY KEY (game_id, event_seq, kind, beneficiary)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_wnba_captured "
                 "ON wnba_events(captured_at)")
    # Forward price trajectory after each matched event (the lag curve).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forward_samples (
            follow_key    TEXT,
            ticker        TEXT,
            base_ts       REAL,
            sample_ts     REAL,
            seconds_since REAL,
            beneficiary   TEXT,
            kind          TEXT,
            detection_mid REAL,
            yes_bid       REAL,
            yes_ask       REAL,
            yes_mid       REAL,
            PRIMARY KEY (follow_key, sample_ts)
        )
    """)


SCHEMA = [sqlite_schema.Migration(1, "adopt pre-versioning tables", _schema_v1)]


def migrate() -> None:
    """Bring the store to the current schema version. Idempotent, and also
    happens on every ``_connect()`` — kept as the explicit entry point the
    runners call at cycle start."""
    _connect().close()



# ─── Pure helpers (testable) ───────────────────────────────────────────────

def build_team_index(markets: pd.DataFrame) -> dict[str, dict]:
    """{yes-team ESPN abbr → market row}. Uses wnba._parse_ticker; drops a team
    that appears in >1 market (ambiguous, e.g. a doubleheader) so we never
    guess."""
    import wnba
    idx: dict[str, dict] = {}
    dupes: set[str] = set()
    for _, m in markets.iterrows():
        parsed = wnba._parse_ticker(str(m.get("condition_id", "")))
        if not parsed:
            continue
        _date, yes_team = parsed
        key = yes_team.upper()
        if key in idx:
            dupes.add(key)
        idx[key] = dict(m)
    for k in dupes:
        idx.pop(k, None)
    return idx


def match_market(beneficiary: str, team_index: dict[str, dict]) -> dict | None:
    """Market where the beneficiary team is the YES side. Exact abbr match."""
    return team_index.get(str(beneficiary).upper())


def _yes_mid(bid, ask, last):
    b = bid if bid and bid > 0 else None
    a = ask if ask and ask < 1 else None
    if b is not None and a is not None:
        return (b + a) / 2.0
    if last is not None and 0 < last < 1:
        return float(last)
    return b if b is not None else a


# ─── Store ───────────────────────────────────────────────────────────────

def store_events(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany("""
            INSERT OR IGNORE INTO wnba_events
              (game_id, event_seq, kind, beneficiary, wp_delta, detail,
               captured_at, ticker, question, yes_bid, yes_ask, yes_mid)
            VALUES
              (:game_id, :event_seq, :kind, :beneficiary, :wp_delta, :detail,
               :captured_at, :ticker, :question, :yes_bid, :yes_ask, :yes_mid)
        """, rows)
        return conn.total_changes - before


def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM wnba_events").fetchone()[0]
        matched = conn.execute(
            "SELECT COUNT(*) FROM wnba_events WHERE ticker IS NOT NULL"
        ).fetchone()[0]
        kinds = conn.execute(
            "SELECT kind, COUNT(*) c FROM wnba_events GROUP BY kind").fetchall()
        newest = conn.execute(
            "SELECT MAX(captured_at) FROM wnba_events").fetchone()[0]
    return {"total": total, "matched_to_market": matched,
            "by_kind": {r[0]: r[1] for r in kinds}, "newest": newest}


def store_samples(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany("""
            INSERT OR IGNORE INTO forward_samples
              (follow_key, ticker, base_ts, sample_ts, seconds_since,
               beneficiary, kind, detection_mid, yes_bid, yes_ask, yes_mid)
            VALUES
              (:follow_key, :ticker, :base_ts, :sample_ts, :seconds_since,
               :beneficiary, :kind, :detection_mid, :yes_bid, :yes_ask, :yes_mid)
        """, rows)
        return conn.total_changes - before


def load_events() -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query("SELECT * FROM wnba_events", conn)


def load_samples() -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query("SELECT * FROM forward_samples", conn)


def _price_lookup(markets: pd.DataFrame):
    """ticker → (bid, ask, mid) from the current discovery snapshot."""
    by_tk = {str(m["condition_id"]): m for _, m in markets.iterrows()} \
        if markets is not None and not markets.empty else {}

    def price_of(ticker: str):
        m = by_tk.get(ticker)
        if m is None:
            return None
        bid = float(m.get("best_bid") or 0) or None
        ask = float(m.get("best_ask") or 0) or None
        last = float(m.get("last_trade_price") or 0) or None
        return bid, ask, _yes_mid(bid, ask, last)
    return price_of


# ─── Cycle ─────────────────────────────────────────────────────────────────

def run_cycle(state: dict[str, wnba_live.LiveGame],
              follows: dict[str, dict] | None = None,
              *, wp_threshold: float = 0.10) -> dict:
    """One poll. `state` (game_id → last LiveGame) and `follows` (forward-sample
    tracker) are mutated in place so the caller's loop carries history across
    cycles."""
    migrate()
    if follows is None:
        follows = {}
    try:
        from pm.arms.wnba_runner import _discover_wnba_markets
        markets = _discover_wnba_markets()
    except Exception as e:  # noqa: BLE001
        log.warning("WNBA market discovery failed: %s", e)
        markets = pd.DataFrame()
    team_index = build_team_index(markets) if not markets.empty else {}

    games = wnba_live.live_games(include_recent_final=True)
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    now_ts = now_dt.timestamp()
    seq: dict[str, int] = {}
    rows: list[dict] = []
    events = 0
    for g in games:
        prev = state.get(g.game_id)
        if prev is not None:
            for ev in wnba_live.detect_events(prev, g, wp_threshold=wp_threshold):
                events += 1
                seq[g.game_id] = seq.get(g.game_id, 0) + 1
                row = match_market(ev.beneficiary, team_index)
                bid = float(row.get("best_bid") or 0) or None if row else None
                ask = float(row.get("best_ask") or 0) or None if row else None
                last = float(row.get("last_trade_price") or 0) or None if row else None
                mid = _yes_mid(bid, ask, last)
                rows.append({
                    "game_id": g.game_id, "event_seq": seq[g.game_id],
                    "kind": ev.kind, "beneficiary": ev.beneficiary,
                    "wp_delta": ev.wp_delta, "detail": ev.detail,
                    "captured_at": now,
                    "ticker": str(row["condition_id"]) if row else None,
                    "question": str(row.get("question", "")) if row else None,
                    "yes_bid": bid, "yes_ask": ask, "yes_mid": mid,
                })
                if row is not None:
                    key = f"{g.game_id}:{seq[g.game_id]}:{ev.kind}"
                    follows[key] = ef.make_follow(
                        str(row["condition_id"]), now_ts,
                        beneficiary=ev.beneficiary, kind=ev.kind,
                        detection_mid=mid)
        state[g.game_id] = g
    n = store_events(rows)
    samples = ef.collect_samples(follows, now_ts=now_ts,
                                 price_of=_price_lookup(markets))
    ns = store_samples(samples)
    if n or ns:
        log.info("WNBA: %d events, %d stored (%d matched); %d fwd-samples "
                 "(%d active follows)", events, n,
                 sum(1 for r in rows if r["ticker"]), ns, len(follows))
    return {"live_games": sum(1 for g in games if g.state == "in"),
            "events": events, "stored": n, "fwd_samples": ns}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wp-threshold", type=float, default=0.10)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=30)
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
    state: dict[str, wnba_live.LiveGame] = {}
    follows: dict[str, dict] = {}
    if not args.loop:
        log.info("cycle: %s", run_cycle(state, follows, wp_threshold=args.wp_threshold))
        return 0
    log.info("wnba_capture starting (interval=%ds, wp_threshold=%.2f)",
             args.interval, args.wp_threshold)
    while True:
        try:
            s = run_cycle(state, follows, wp_threshold=args.wp_threshold)
            if s["events"] or s.get("fwd_samples"):
                log.info("cycle: %s", s)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    sys.exit(main())
