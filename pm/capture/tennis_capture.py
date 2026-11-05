"""Tennis capture — step 2 of react-and-hold for tennis: MEASURE the lag.

Step 1 (`tennis_live.py`) detects high-leverage in-match events (a set/match
won) with a beneficiary. This instrument joins those events to the Kalshi
market: when a player wins a set, find the market where they're the YES side,
record the price AT DETECTION, and (later) measure whether the book moves toward
that side afterward — i.e. does the book lag tennis events the way it lags MLB
plays? We do NOT assume MLB's +2.5¢ transfers; this is the gate before arming.

Reuses: `tennis_live.live_matches`/`detect_events` (the detector),
`tennis_runner._discover_tennis_markets` (open ATP/WTA match markets via the
events path — they don't surface in the volume feed), and the tennis sidecar's
`_name_from_question` (the full-name bridge — Kalshi's 3-letter codes are
unreliable). On no unique name match we SKIP and record it as unmatched rather
than guess. Read-only; writes only its own SQLite (env TENNIS_CAPTURE_DB).

CLI:
  python tennis_capture.py                  # one cycle
  python tennis_capture.py --loop --interval 45
  python tennis_capture.py --stats
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import logctx, sqlite_schema
from pm.live import event_followup as ef
from pm.live import tennis_live

log = logging.getLogger(__name__)

DEFAULT_DB = Path("data/shared/tennis_capture.sqlite")


def _db_path() -> Path:
    return Path(os.getenv("TENNIS_CAPTURE_DB", str(DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    sqlite_schema.set_wal(conn)   # busy-safe (R-27)
    conn.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(conn, store="tennis_capture", migrations=SCHEMA, db_path=p)
    return conn


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape verbatim. Constraints on the capture
# tables are deferred: adding CHECK/NOT NULL means a table rebuild, which is
# not something a capture daemon should do on the way to its next cycle.

def _schema_v1(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tennis_events (
            match_id     TEXT,
            event_seq    INTEGER,     -- per-match event counter (set 1, 2, ...)
            kind         TEXT,        -- "set" | "match"
            beneficiary  TEXT,
            tournament   TEXT,
            detail       TEXT,
            captured_at  TEXT,
            ticker       TEXT,        -- matched Kalshi market (NULL if unmatched)
            question     TEXT,
            yes_is_beneficiary INTEGER, -- 1 if YES side == beneficiary
            yes_bid      REAL,
            yes_ask      REAL,
            yes_mid      REAL,
            PRIMARY KEY (match_id, event_seq, kind, beneficiary)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tennis_captured "
                 "ON tennis_events(captured_at)")
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

def _norm(name: str) -> str:
    """Lowercase, strip accents/punctuation — for robust full-name matching
    (ESPN 'Félix Auger-Aliassime' vs question 'Felix Auger-Aliassime')."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace("-", " ").replace(".", " ").split())


def build_player_index(markets: pd.DataFrame) -> dict[str, dict]:
    """{normalized YES-player name → market row dict} from open tennis markets.
    Uses the sidecar's question parser; drops ambiguous duplicate names."""
    import tennis
    idx: dict[str, dict] = {}
    dupes: set[str] = set()
    for _, m in markets.iterrows():
        yp = tennis._name_from_question(str(m.get("question", "")))
        if not yp:
            continue
        key = _norm(yp)
        if key in idx:
            dupes.add(key)
        idx[key] = dict(m)
    for k in dupes:           # ambiguous → don't guess
        idx.pop(k, None)
    return idx


def match_market(beneficiary: str, player_index: dict[str, dict]) -> dict | None:
    """Find the market where the beneficiary is the YES side. Exact normalized
    full-name match; surname fallback only if unique. None if no/ambiguous."""
    key = _norm(beneficiary)
    if key in player_index:
        return player_index[key]
    surname = key.split()[-1] if key else ""
    hits = [row for k, row in player_index.items() if k.split()[-1:] == [surname]]
    return hits[0] if len(hits) == 1 else None


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
            INSERT OR IGNORE INTO tennis_events
              (match_id, event_seq, kind, beneficiary, tournament, detail,
               captured_at, ticker, question, yes_is_beneficiary,
               yes_bid, yes_ask, yes_mid)
            VALUES
              (:match_id, :event_seq, :kind, :beneficiary, :tournament, :detail,
               :captured_at, :ticker, :question, :yes_is_beneficiary,
               :yes_bid, :yes_ask, :yes_mid)
        """, rows)
        return conn.total_changes - before


def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tennis_events").fetchone()[0]
        matched = conn.execute(
            "SELECT COUNT(*) FROM tennis_events WHERE ticker IS NOT NULL"
        ).fetchone()[0]
        kinds = conn.execute(
            "SELECT kind, COUNT(*) c FROM tennis_events GROUP BY kind").fetchall()
        newest = conn.execute(
            "SELECT MAX(captured_at) FROM tennis_events").fetchone()[0]
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
        return pd.read_sql_query("SELECT * FROM tennis_events", conn)


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

def run_cycle(state: dict[str, tennis_live.LiveMatch],
              follows: dict[str, dict] | None = None) -> dict:
    """One poll. `state` (match_id → last LiveMatch) and `follows` (forward-
    sample tracker) are mutated in place so the caller's loop carries history
    across cycles. Returns counts."""
    migrate()
    if follows is None:
        follows = {}
    try:
        from pm.arms.tennis_runner import _discover_tennis_markets
        markets = _discover_tennis_markets()
    except Exception as e:  # noqa: BLE001
        log.warning("tennis market discovery failed: %s", e)
        markets = pd.DataFrame()
    player_index = build_player_index(markets) if not markets.empty else {}

    matches = tennis_live.live_matches(include_recent_final=True)
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    now_ts = now_dt.timestamp()
    seq: dict[str, int] = {}
    rows: list[dict] = []
    events = 0
    for m in matches:
        prev = state.get(m.match_id)
        if prev is not None:
            for ev in tennis_live.detect_events(prev, m):
                events += 1
                seq[m.match_id] = seq.get(m.match_id, 0) + 1
                row = match_market(ev.beneficiary, player_index)
                bid = float(row.get("best_bid") or 0) or None if row else None
                ask = float(row.get("best_ask") or 0) or None if row else None
                last = float(row.get("last_trade_price") or 0) or None if row else None
                mid = _yes_mid(bid, ask, last)
                rows.append({
                    "match_id": m.match_id, "event_seq": seq[m.match_id],
                    "kind": ev.kind, "beneficiary": ev.beneficiary,
                    "tournament": ev.tournament, "detail": ev.detail,
                    "captured_at": now,
                    "ticker": str(row["condition_id"]) if row else None,
                    "question": str(row.get("question", "")) if row else None,
                    "yes_is_beneficiary": 1 if row else 0,
                    "yes_bid": bid, "yes_ask": ask, "yes_mid": mid,
                })
                # Register a forward-sample follow for matched events.
                if row is not None:
                    key = f"{m.match_id}:{seq[m.match_id]}:{ev.kind}"
                    follows[key] = ef.make_follow(
                        str(row["condition_id"]), now_ts,
                        beneficiary=ev.beneficiary, kind=ev.kind,
                        detection_mid=mid)
        state[m.match_id] = m
    n = store_events(rows)
    # Forward samples for active follows (the lag curve), reusing discovery.
    samples = ef.collect_samples(follows, now_ts=now_ts,
                                 price_of=_price_lookup(markets))
    ns = store_samples(samples)
    if n or ns:
        log.info("tennis: %d events, %d stored (%d matched); %d fwd-samples "
                 "(%d active follows)", events, n,
                 sum(1 for r in rows if r["ticker"]), ns, len(follows))
    return {"live_matches": sum(1 for m in matches if m.state == "in"),
            "events": events, "stored": n, "fwd_samples": ns}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=45)
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
    state: dict[str, tennis_live.LiveMatch] = {}
    follows: dict[str, dict] = {}
    if not args.loop:
        log.info("cycle: %s", run_cycle(state, follows))
        return 0
    log.info("tennis_capture starting (interval=%ds)", args.interval)
    while True:
        try:
            s = run_cycle(state, follows)
            if s["events"] or s.get("fwd_samples"):
                log.info("cycle: %s", s)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    sys.exit(main())
