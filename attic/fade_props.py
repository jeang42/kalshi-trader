"""Fade-props — FAST-resolving sports-prop longshot fade (paper OOS validation).

The "quick turnaround like MLB" lead (2026-07-05): the favorite-longshot bias
(retail overpays for longshots; the most-documented sports-betting anomaly)
shows up in FAST-resolving sports PROPS — golf top-N / round-leader, World Cup
player-goal / first-goal props — not the (efficient) game outcomes we killed.
Calibration @18h lead: YES 0.10-0.20 longshots resolved YES far less than priced
(gap -9pp); 0.20-0.35 -28pp. Fade = BUY NO on the overpriced longshot, hold to
resolution (DAYS).

De-correlated backtest was PROMISING but thin (5-11 independent events, spread-
fragile). So this is a PAPER arm: props resolve in days with hundreds/10d, so it
accrues enough INDEPENDENT events (grouped by tournament) to confirm/kill in
weeks. No live money until the OOS event-count + net-of-spread edge hold up.

Each cycle: discover fast prop longshots in the fade band, buy NO (paper) at
(1-yes_bid), cap per EVENT (tournament fields are heavily correlated — one
winner), hold to resolution, log NET P&L tagged by event/series.

SQLite data/shared/fade_props.sqlite (env FADE_PROPS_DB).
CLI: `python fade_props.py` | `--loop` | `--stats`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime

import pandas as pd

import kalshi_client as kc
import kalshi_fees
import provenance
import shutdown
import sqlite_schema

log = logging.getLogger(__name__)


def _db_path() -> str:
    """Resolved on every call, never frozen at import.

    A module-level ``DB = os.getenv(...)`` captures whatever the environment
    held at import time, so nothing downstream — tests especially — can
    redirect it. That is not hypothetical: it is how synthetic rows reached
    this project's production stores, twice, and back_nearclose.py carries the
    same note for the same reason.
    """
    return os.getenv("FADE_PROPS_DB", "data/shared/fade_props.sqlite")

# Longshot-rich, fast-resolving prop series (from the resolved-flow scan).
# We query each series DIRECTLY (series_ticker filter) — the close-time feed is
# 100% flooded by one huge esports series, so props never surface there. These
# prefixes are STABLE; the event under each rotates per tournament/match.
PROP_SERIES = (
    # golf — finish top-N (each player a longshot; field of ~150)
    "KXPGATOP5", "KXPGATOP10", "KXPGATOP20", "KXPGATOP30",
    "KXDPWORLDTOURTOP5", "KXDPWORLDTOURTOP10", "KXDPWORLDTOURTOP20",
    "KXCHAMPTOURTOP5", "KXCHAMPTOURTOP10", "KXLPGATOP5", "KXLPGATOP10",
    # golf — round leader (1 of ~150 leads)
    "KXPGAR1LEAD", "KXPGAR2LEAD", "KXPGAR3LEAD",
    "KXDPWORLDTOURR1LEAD", "KXDPWORLDTOURR2LEAD", "KXDPWORLDTOURR3LEAD",
    "KXCHAMPTOURR1LEAD", "KXCHAMPTOURR2LEAD", "KXCHAMPTOURR3LEAD",
    # world cup — player/team props
    "KXWCPLAYERGOALS", "KXWCTEAM1STGOAL", "KXWCTEAMLEADGOAL", "KXWCTEAMTOTALGOALS",
    # cycling stage winner
    "KXCYCLINGSTAGE",
)
FADE_LO, FADE_HI = 0.10, 0.32   # overpriced longshot band (calibration-confirmed)
MIN_VOLUME = 100
MIN_DAYS, MAX_DAYS = 1.0, 30.0  # currently-open props resolve at tournament END
#   (~2-4wk), not same-day — the truly-fast round-leader props resolve in hours
#   (a separate real-time build). Sorted soonest-first; still cycles far faster
#   + with many more independent events (every tournament) than the retail fade.
PER_EVENT_CAP = 4               # tournament fields are ~150 correlated members
STAKE_DOLLARS = 10.0


def _discover_props() -> pd.DataFrame:
    """Fetch open markets for each prop series directly (series_ticker filter),
    bypassing the esports-flooded close-time feed."""
    rows: list[dict] = []
    for s in PROP_SERIES:
        try:
            items = kc._paginate("markets",
                                 params={"series_ticker": s, "status": "open", "limit": 1000},
                                 items_key="markets", max_items=1000)
            for m in items:
                rows.append(kc._market_to_row(m, event_category="Sports", event_title=""))
        except Exception:  # noqa: BLE001 — one dead series must not stop the rest
            continue
        time.sleep(0.4)
    if not rows:
        return pd.DataFrame()
    return kc._add_days_to_resolution(pd.DataFrame(rows))


def in_fade_band(yes: float, *, lo: float = FADE_LO, hi: float = FADE_HI) -> bool:
    """Overpriced-longshot band we fade (buy NO)?

    >>> in_fade_band(0.15)
    True
    >>> in_fade_band(0.50)
    False
    """
    try:
        return lo <= float(yes) <= hi
    except (TypeError, ValueError):
        return False


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────

def _schema_v1(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        ticker TEXT PRIMARY KEY, event_id TEXT, series TEXT, question TEXT,
        yes_bid REAL, no_entry_cost REAL, count INTEGER,
        opened_at INTEGER, close_time TEXT,
        resolved INTEGER DEFAULT 0, resolved_yes INTEGER, pnl REAL, closed_at INTEGER)""")


_POSITIONS_V2 = """CREATE TABLE positions__new (
    ticker        TEXT PRIMARY KEY NOT NULL CHECK (ticker <> ''),
    event_id      TEXT, series TEXT, question TEXT,
    yes_bid       REAL CHECK (yes_bid IS NULL OR (yes_bid >= 0 AND yes_bid <= 1)),
    no_entry_cost REAL NOT NULL CHECK (no_entry_cost > 0 AND no_entry_cost <= 1),
    count         INTEGER NOT NULL CHECK (count > 0),
    opened_at     INTEGER NOT NULL,
    close_time    TEXT,
    resolved      INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_yes  INTEGER CHECK (resolved_yes IS NULL OR resolved_yes IN (0, 1)),
    pnl           REAL, closed_at INTEGER,
    CHECK (resolved = 0 OR (resolved_yes IS NOT NULL AND pnl IS NOT NULL
                            AND closed_at IS NOT NULL)),
    CHECK (resolved = 1 OR (pnl IS NULL AND closed_at IS NULL)))"""


def _schema_v2(c: sqlite3.Connection) -> None:
    sqlite_schema.rebuild_table(c, "positions", _POSITIONS_V2)


def _schema_v3(c: sqlite3.Connection) -> None:
    # R-18: which code opened this position. NULL on rows that predate it.
    sqlite_schema.add_column_if_missing(c, "positions", "commit_sha", "TEXT")


SCHEMA = [
    sqlite_schema.Migration(1, "adopt pre-versioning positions table", _schema_v1),
    sqlite_schema.Migration(2, "CHECK/NOT NULL constraints on positions",
                            _schema_v2, backup=True),
    sqlite_schema.Migration(3, "commit_sha column (which code opened the position)",
                            _schema_v3),
]


def _con() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    sqlite_schema.ensure_schema(c, store="fade_props", migrations=SCHEMA,
                                db_path=_db_path())
    return c


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _open(con, df: pd.DataFrame) -> int:
    held = {r[0] for r in con.execute("SELECT ticker FROM positions")}
    per_event = {}
    for ev, n in con.execute("SELECT event_id, COUNT(*) FROM positions GROUP BY event_id"):
        per_event[ev] = n
    cand = df[df["mid"].map(in_fade_band)].copy()
    if cand.empty:
        return 0
    # prefer soonest-resolving (fast turnover)
    cand["_dtr"] = pd.to_numeric(cand["days_to_resolution"], errors="coerce").fillna(9e9)
    cand = cand.sort_values("_dtr")
    opened = 0
    for _, r in cand.iterrows():
        if shutdown.requested():
            break
        tk = r["id"]
        if tk in held:
            continue
        ev = str(r["id"]).rsplit("-", 1)[0]     # tournament/event = ticker w/o member
        if per_event.get(ev, 0) >= PER_EVENT_CAP:
            continue
        yb = r.get("best_bid")
        if pd.isna(yb):
            continue
        no_cost = round(1.0 - float(yb), 4)
        if not (0.65 <= no_cost <= 0.92):        # executable band (yes 0.08-0.35)
            continue
        cnt = int(STAKE_DOLLARS / no_cost)
        if cnt < 1:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO positions (ticker,event_id,series,question,yes_bid,"
            "no_entry_cost,count,opened_at,close_time,commit_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tk, ev, r["series"], str(r.get("question", ""))[:140], float(yb),
             no_cost, cnt, _now(), str(r.get("close_time", "")), provenance.label()))
        if cur.rowcount == 0:
            log.warning("fade_props: insert IGNORED for %s (constraint?) "
                        "no_cost=%s count=%s", tk, no_cost, cnt)
            continue
        per_event[ev] = per_event.get(ev, 0) + 1
        opened += 1
    con.commit()
    return opened


def _resolve(con) -> int:
    rows = con.execute(
        "SELECT ticker, no_entry_cost, count FROM positions WHERE resolved=0").fetchall()
    done = 0
    for tk, no_cost, cnt in rows:
        if shutdown.requested():
            break
        # Read settlement from the RAW market endpoint. `lookup_markets_by_id`
        # normalizes away the outcome — it has no `resolved_yes` column — so the
        # old lookup-based resolver silently skipped every position forever.
        # Per docs/file-map.md gotcha #12: settled => status="finalized" and result in
        # {"yes","no"}; result="" is still settling and "scalar" is a
        # non-binary settlement we can't score. Same path as fade_nearclose.
        try:
            m = kc._get(f"markets/{tk}").get("market") or {}
        except Exception:  # noqa: BLE001
            continue
        if m.get("status") != "finalized":
            continue
        result = m.get("result", "")
        if result not in ("yes", "no"):
            continue
        resolved_yes = result == "yes"
        no_won = not resolved_yes
        fee = kalshi_fees.fee_for_trade(min(max(float(no_cost), 0.01), 0.99), int(cnt))
        pnl = round(((1.0 if no_won else 0.0) - float(no_cost)) * int(cnt) - fee, 4)
        con.execute("UPDATE positions SET resolved=1, resolved_yes=?, pnl=?, closed_at=? "
                    "WHERE ticker=?", (1 if resolved_yes else 0, pnl, _now(), tk))
        done += 1
        time.sleep(0.15)
    con.commit()
    return done


def run_cycle(*, verbose: bool = True) -> dict:
    con = _con()
    opened = 0
    try:
        df = _discover_props()
        if not df.empty:
            df["series"] = df["id"].astype(str).str.split("-").str[0]
            for c in ("best_bid", "best_ask", "volume", "days_to_resolution"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df["mid"] = (df["best_bid"] + df["best_ask"]) / 2
            props = df[(df["volume"] > MIN_VOLUME)
                       & df["days_to_resolution"].between(MIN_DAYS, MAX_DAYS)]
            opened = _open(con, props)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"discovery failed: {e}")
    resolved = _resolve(con)
    if verbose:
        n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
        print(f"opened={opened} resolved={resolved} open_now={n_open}")
    return {"opened": opened, "resolved": resolved}


def summary() -> dict:
    con = _con()
    n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
    n_done, net, wins = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
        "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
        "FROM positions WHERE resolved=1").fetchone()
    ev = con.execute("SELECT COUNT(DISTINCT event_id) FROM positions").fetchone()[0]
    return {"open": n_open, "resolved": n_done, "net": round(float(net), 2),
            "win": round(100 * wins / n_done) if n_done else None, "events": ev}


def stats() -> None:
    con = _con()
    s = summary()
    print(f"fade_props: open={s['open']} resolved={s['resolved']} events={s['events']}")
    if s["resolved"]:
        print(f"  net ${s['net']} win {s['win']}%")
        print("  by EVENT (independent, resolved):")
        for ev, n, p in con.execute(
            "SELECT event_id, COUNT(*), COALESCE(SUM(pnl),0) FROM positions "
            "WHERE resolved=1 GROUP BY event_id ORDER BY 3"):
            print(f"    {str(ev)[:34]:34s} n={n:>2} net=${p:>6.2f}")
    print("\n  open fades (soonest first):")
    for r in con.execute("SELECT no_entry_cost, count, close_time, question FROM positions "
                         "WHERE resolved=0 ORDER BY close_time LIMIT 20"):
        print(f"    NO@{r[0]:.2f} x{r[1]:>2} close={str(r[2])[:10]} {str(r[3])[:44]}")


def calibrate(*, since_days: int = 12, lead_hours: float = 18.0,
              max_markets: int = 400) -> None:
    """Backtest the prop-fade thesis: pull recently-RESOLVED prop markets and
    measure favorite-longshot calibration (price at ``lead_hours`` before close
    vs outcome), bucketed by price band. Negative gap in low bands = longshots
    overpriced = the fade edge. Reproduces the 2026-07-05 discovery."""
    df = kc.get_closed_markets_in_range(since_days=since_days, max_items=5000)
    df["series"] = df["id"].astype(str).str.split("-").str[0]
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    props = df[df["series"].isin(PROP_SERIES) & (df["volume"] > MIN_VOLUME)]
    props = props.sort_values("volume", ascending=False).head(max_markets)
    print(f"resolved prop markets (vol>{MIN_VOLUME}): {len(props)}")
    cal = kc.get_market_calibration_data(props, lead_hours=lead_hours, max_markets=max_markets)
    cal["predicted_prob"] = pd.to_numeric(cal["predicted_prob"], errors="coerce")
    cal["actual"] = pd.to_numeric(cal["actual"], errors="coerce")
    cal = cal.dropna(subset=["predicted_prob", "actual"])
    print(f"calibrated (had {lead_hours:.0f}h history): {len(cal)}\n")
    print(f"=== PROP CALIBRATION (price ~{lead_hours:.0f}h before close vs outcome) ===")
    for lo, hi in [(0.03, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.55), (0.55, 0.97)]:
        s = cal[(cal["predicted_prob"] >= lo) & (cal["predicted_prob"] < hi)]
        if len(s) < 8:
            print(f"  {lo:.2f}-{hi:.2f} n={len(s)} (thin)")
            continue
        gap = (s["actual"].mean() - s["predicted_prob"].mean()) * 100
        print(f"  {lo:.2f}-{hi:.2f} n={len(s):>4} price={s['predicted_prob'].mean():.3f} "
              f"won={s['actual'].mean():.3f} gap={gap:+.1f}pp")
    print("\nnegative gap in low bands => longshots overpriced => fade edge")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=21600)  # 6h (props resolve in wks)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--calibrate", action="store_true", help="run the fade backtest")
    args = ap.parse_args()
    # The other arms configure logging; this one printed. Its warnings (an
    # ignored insert, a shutdown) went to stderr's last-resort handler with no
    # timestamp and INFO lines vanished. Same format as fade_nearclose.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.calibrate:
        calibrate()
    elif args.stats:
        stats()
    elif args.loop:
        shutdown.install()          # SIGTERM finishes the current step, then exits (R-20)
        while not shutdown.requested():
            run_cycle()
            if shutdown.sleep(args.interval):
                break
        log.info("clean shutdown")
    else:
        run_cycle()


if __name__ == "__main__":
    main()
