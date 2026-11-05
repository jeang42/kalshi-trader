"""Fade-retail-longshot — the first +EV lead, as a clean forward paper arm.

The one signal that survived the whole search ([[project_retail_longshot_fade]]):
NON-sport/crypto RETAIL markets (primaries, awards, music — arbitrageur-light)
show a large favorite-longshot bias. At a 72h lead, YES priced 0.15-0.55
resolves YES far LESS than its price (gap -8 to -19pp); favorites calibrated.
Fade = BUY NO on the overpriced longshot, hold to resolution.

SHARPEST sub-edge (user idea): **prop / "which of N plausible things happens"
fields** — features, attendees, "will X say Y", award mentions. Retail
overprices EACH salient candidate, but only 1-2 of N happen, so the field
sums to WAY over its true count (13 Drake-feature props at 0.45 = 5.85!). The
model-free signal is that **field OVERROUND** — fade the whole field of
mid-priced members. This arm tags market-type + field-overround so the
out-of-sample track tells us which cut actually holds up across MANY
independent events (the backtest's +60% on features was really ~2 events).

Each cycle (cron-able; hold-to-resolution so no fast loop):
  DISCOVER live retail markets; group by EVENT; compute each field's overround.
  OPEN a paper "buy NO" on fade-band members at (1 - best_yes_bid); wider
  per-event cap on strongly-overround fields (fade the field), else capped to
  limit correlation. RESOLVE + log NET P&L (NO pays $1 if it didn't happen),
  tagged by market-type + strategy (overround vs broad).

SQLite data/shared/fade_retail.sqlite (env FADE_RETAIL_DB).
CLI: `python fade_retail_longshot.py` | `--loop` | `--stats`.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import re
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
    return os.getenv("FADE_RETAIL_DB", "data/shared/fade_retail.sqlite")

FADE_LO, FADE_HI = 0.12, 0.45      # overpriced-longshot band to fade
MIN_VOLUME = 500
MIN_DAYS = float(os.getenv("FADE_MIN_DAYS", "2"))    # >72h lead (bias present)
MAX_DAYS = float(os.getenv("FADE_MAX_DAYS", "30"))   # velocity horizon (was 45).
#   Candidates are filled SOONEST-resolving first, so genuinely-fast markets
#   (earnings "say Y", SNL, debates) get prioritized the moment they list.
#   Right now nothing in-band closes <21d, so tightening below ~30 just
#   concentrates in the one near field (Trump-mention) — dial FADE_MAX_DAYS
#   down for more velocity, up for more event diversity.
PER_EVENT_CAP = 2                  # broad fade — limit correlated stacking
OVERROUND_CAP = 6                  # strongly-overround field — fade more of it
OVERROUND_STRONG = 1.6             # field mid-sum (plausible members) => prop overpricing
OVERROUND_LO, OVERROUND_HI = 0.08, 0.65   # "plausible member" band for the field sum
STAKE_DOLLARS = 10.0
# Early-exit take-profit: sell the NO once it has captured this fraction of its
# move (entry -> $1). DISABLED by default (0.0): `fade_early_exit_backtest.py`
# showed it HURTS — corrections happen LATE (~4.7 of 6 hold-days), so a TP at
# 0.6 roughly HALVED ROI (17.3% -> 7.9%) to save ~1 day, and on the 77% that
# triggered, holding earned +30.8% (all resolved NO -> paid $1) vs +18.6%
# exiting early. Hold-to-resolution wins on return AND is fast enough (~6d);
# velocity comes from breadth (many parallel positions). Kept env-tunable in
# case a slower market class ever benefits.
EARLY_EXIT_TP_FRACTION = float(os.getenv("FADE_TP_FRACTION", "0.0"))

# Live execution CANARY — DORMANT by default. When enabled, each new fade also
# places ONE minimum-size (1-contract) REAL "buy NO" order through the vetted,
# triple-gated execution.LiveBroker (pre-flight caps, emergency-stop, owner
# lock) — to validate that NO orders actually FILL on these thin retail books
# (the one thing the paper arm can't test). Real money only when ALL hold:
#   FADE_CANARY_ENABLED=true  AND  the profile is in LIVE_TRADING_PROFILES  AND
#   kalshi_orders live gates pass. Any one missing => paper/no-op.
CANARY_PROFILE = os.getenv("FADE_CANARY_PROFILE", "agentic_fade_retail")
CANARY_MAX_PER_CYCLE = int(os.getenv("FADE_CANARY_MAX_PER_CYCLE", "5"))
# Live sizing: at least $BASE per fade, $HIGH for high-certainty (strongly-
# overround field or very-likely-NO). Contracts sized to clear the minimum.
CANARY_STAKE_BASE = float(os.getenv("FADE_CANARY_STAKE_BASE", "2"))
CANARY_STAKE_HIGH = float(os.getenv("FADE_CANARY_STAKE_HIGH", "5"))
# Aggregate real-money cap: stop opening NEW live orders once total deployed
# (sum of actual filled cost) reaches this. Bounds the canary; env-tunable.
CANARY_MAX_TOTAL = float(os.getenv("FADE_CANARY_MAX_TOTAL", "75"))
# Mentions ("will X say Y") are the least-proven cut AND member selection is
# semantically hard (can't tell overpriced-topical from underpriced-common by
# price). Keep them PAPER-only for live until the OOS track proves them.
CANARY_SKIP_MENTIONS = os.getenv("FADE_CANARY_SKIP_MENTIONS", "true").strip().lower() in (
    "1", "true", "yes", "on")
_ACTIVE_PROFILE_ENV = "POLYMARKET_ACTIVE_PROFILE"


def _canary_size(no_cost: float, overround: float) -> tuple[int, bool]:
    """Return (contracts, worth_it). Bigger stake for high-certainty fades
    (strong overround, or very-likely-NO). worth_it=False when the estimated
    calibration edge (gap) doesn't beat the per-contract fee — so we only bet
    when the fade actually pays after fees.

    >>> c, w = _canary_size(0.70, 7.7); (c >= 3, w)   # $5 high-certainty band
    (True, True)
    >>> _canary_size(0.60, 0.2)[1]                     # normal, edge >> fee
    True
    >>> _canary_size(0.92, 7.7)[1]                      # deep-NO (yes 0.08) — off band
    False
    """
    high = (overround or 0) >= OVERROUND_STRONG or no_cost >= 0.80
    stake = CANARY_STAKE_HIGH if high else CANARY_STAKE_BASE
    contracts = max(1, math.ceil(stake / no_cost))    # >= the $ minimum
    exp_gap = 0.12 if high else 0.08                  # conservative band-level edge
    fee_pc = kalshi_fees.fee_for_trade(no_cost, 1)    # per-contract fee
    # only bet where the measured edge lives: executable NO cost 0.55-0.88
    # (yes bid 0.12-0.45). Deeper (yes<0.12) has thin edge; shallower (yes>0.45)
    # is out of the fade band. AND the gap must beat the per-contract fee.
    in_band = 0.55 <= no_cost <= 0.88
    worth_it = in_band and exp_gap > fee_pc * 1.3
    return contracts, worth_it


def _canary_enabled() -> bool:
    """Read FRESH at call-time (not import) — the daemon load_dotenv()s in
    main() AFTER import, so a module constant would freeze to False and the
    live gate would silently stay paper (the documented ordering trap)."""
    return os.getenv("FADE_CANARY_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")

SPORT_KW = ("MLB", "NBA", "NFL", "NHL", "WNBA", "SOCCER", "UFC", "TENNIS",
            "GOLF", "NASCAR", "MVE", "LOL", "VALORANT", "CRICKET", "F1", "BOX",
            "MLS", "EPL", "UCL", "NCAA", "CFB", "PGA", "ATP", "WTA", "FIFA", "OLY",
            "KXWC", "WORLDCUP", "SUPERBOWL", "OLYMP", "FORMULA", "RUGBY", "DARTS")
CRYPTO_KW = ("BTC", "ETH", "XRP", "DOGE", "SOL", "CRYPTO", "ADA", "LINK")
# Scheduled macro releases — efficient/HFT-watched, NOT the retail behavioral
# corner (the fade edge is primaries/awards/mentions/music). Exclude.
ECON_KW = ("CPI", "GDP", "FEDDEC", "FEDEND", "PPI", "PAYROLL", "NFP", "JOBLESS",
           "CLAIMS", "RATEHIKE", "RATECUT", "RATEDEC", "INFLATION", "UNEMP",
           "CBDECISION", "ECONSTAT", "KXU3", "PMI", "ISM", "RETAILSALES",
           "CONSUMER", "MORTGAGE", "TREASURY", "YIELD")

# Market-type from the question language (for OOS breakdown; salience-bias props first).
_MTYPE = [
    ("say_mention", r"\bsay\b|\bmention|utter|\bword|\bthank|\btweet|\bpost\b"),
    ("feature_attend", r"\bfeatur|\bappear|\battend|\bwear|guest|\bjoin\b|\bcameo"),
    ("win_award", r"\bwin\b|winner|\baward|nominee|\bmvp\b|\bbest\b"),
    ("election", r"primary|nominee for|\belect|senate|governor|caucus|president"),
    ("music", r"\bstream|billboard|chart|album|\bsong|number one|#1|debut"),
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def is_retail_series(series: str) -> bool:
    """Retail/behavioral corner = NOT sport, crypto, or scheduled macro.

    >>> is_retail_series("KXCAPRIMARY")
    True
    >>> is_retail_series("KXTRUMPMENTION")
    True
    >>> is_retail_series("KXMLBGAME")
    False
    >>> is_retail_series("KXWCROUND")
    False
    >>> is_retail_series("KXCPIYOY")
    False
    """
    s = str(series).upper()
    return not (any(k in s for k in SPORT_KW)
                or any(k in s for k in CRYPTO_KW)
                or any(k in s for k in ECON_KW))


def in_fade_band(yes_price: float, *, lo: float = FADE_LO, hi: float = FADE_HI) -> bool:
    """Is this YES price an overpriced-longshot we'd fade (buy NO)?

    >>> in_fade_band(0.25)
    True
    >>> in_fade_band(0.80)
    False
    """
    try:
        return lo <= float(yes_price) <= hi
    except (TypeError, ValueError):
        return False


def classify_mtype(question: str) -> str:
    """Tag a market by question language — salience-bias props (say/feature)
    are the sharpest fade targets.

    >>> classify_mtype("Will Drake feature SZA on his next album?")
    'feature_attend'
    >>> classify_mtype("Will the President say 'inflation' in the address?")
    'say_mention'
    >>> classify_mtype("Who will win the mayoral race?")
    'win_award'
    """
    q = str(question).lower()
    for tag, pat in _MTYPE:
        if re.search(pat, q):
            return tag
    return "other"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape (CREATE IF NOT EXISTS + the mtype/strategy/
# overround, exit, and live-canary ADD COLUMN generations). v2 rebuilds with
# constraints. NOTE the invariant differs from the other arms: a take-profit
# exit sets resolved=1 with resolved_yes NULL (the market hasn't settled), so
# only pnl/closed_at are required on a resolved row here.

def _schema_v1(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        ticker TEXT PRIMARY KEY, event_id TEXT, series TEXT, question TEXT,
        yes_bid REAL, yes_ask REAL, no_entry_cost REAL, count INTEGER,
        mtype TEXT, strategy TEXT, field_overround REAL, field_size INTEGER,
        opened_at INTEGER, close_time TEXT,
        resolved INTEGER DEFAULT 0, resolved_yes INTEGER, pnl REAL, closed_at INTEGER,
        exit_reason TEXT, exit_price REAL, live_order_id TEXT, live_status TEXT,
        live_cost REAL)""")
    for col, decl in (("mtype", "TEXT"), ("strategy", "TEXT"),
                      ("field_overround", "REAL"), ("field_size", "INTEGER"),
                      ("exit_reason", "TEXT"), ("exit_price", "REAL"),
                      ("live_order_id", "TEXT"), ("live_status", "TEXT"),
                      ("live_cost", "REAL")):
        sqlite_schema.add_column_if_missing(c, "positions", col, decl)


_POSITIONS_V2 = """CREATE TABLE positions__new (
    ticker          TEXT PRIMARY KEY NOT NULL CHECK (ticker <> ''),
    event_id        TEXT, series TEXT, question TEXT,
    yes_bid         REAL CHECK (yes_bid IS NULL OR (yes_bid >= 0 AND yes_bid <= 1)),
    yes_ask         REAL CHECK (yes_ask IS NULL OR (yes_ask >= 0 AND yes_ask <= 1)),
    no_entry_cost   REAL NOT NULL CHECK (no_entry_cost > 0 AND no_entry_cost <= 1),
    count           INTEGER NOT NULL CHECK (count > 0),
    mtype TEXT, strategy TEXT, field_overround REAL, field_size INTEGER,
    opened_at       INTEGER NOT NULL,
    close_time      TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_yes    INTEGER CHECK (resolved_yes IS NULL OR resolved_yes IN (0, 1)),
    pnl             REAL, closed_at INTEGER,
    exit_reason     TEXT CHECK (exit_reason IS NULL
                                OR exit_reason IN ('resolution', 'take_profit')),
    exit_price      REAL CHECK (exit_price IS NULL OR (exit_price >= 0 AND exit_price <= 1)),
    live_order_id TEXT, live_status TEXT, live_cost REAL,
    CHECK (resolved = 0 OR (pnl IS NOT NULL AND closed_at IS NOT NULL)),
    CHECK (resolved = 1 OR (pnl IS NULL AND closed_at IS NULL AND exit_reason IS NULL)))"""


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
    sqlite_schema.ensure_schema(c, store="fade_retail", migrations=SCHEMA,
                                db_path=_db_path())
    return c


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------

def _field_overround(df: pd.DataFrame) -> dict:
    """event_id -> (overround_sum, n_members) over plausible members. A field
    of many mid-priced 'will X happen' props sums well over its true count."""
    out = {}
    plausible = df[df["mid"].between(OVERROUND_LO, OVERROUND_HI)]
    for ev, g in plausible.groupby("event_id"):
        out[str(ev)] = (float(g["mid"].sum()), int(len(g)))
    return out


def _live_canary_order(con, ticker: str, question: str, no_cost: float,
                       count: int = 1) -> bool:
    """Place a real buy-NO (``count`` contracts) via the vetted, triple-gated
    LiveBroker — a no-op unless FADE_CANARY_ENABLED and the profile passes every
    live gate. Records the order id/status; never raises (must not break paper)."""
    try:
        import execution
        if not execution.is_profile_live(CANARY_PROFILE):
            return False
        # place → record is one critical section (R-20): a SIGTERM between
        # the venue accepting the order and the UPDATE+commit landing would
        # leave a real position this store does not know about.
        with shutdown.deferred(f"fade canary {ticker}"):
            res = execution.get_broker(CANARY_PROFILE).buy(
                question=str(question)[:100], ticker=ticker, outcome="No",
                count=count, price=no_cost, current_mid=no_cost)
            filled = float(getattr(res, "filled_count", 0) or 0)
            avg = getattr(res, "avg_fill_price", None)
            live_cost = round(filled * float(avg), 4) if (filled and avg) else 0.0
            con.execute(
                "UPDATE positions SET live_order_id=?, live_status=?, live_cost=? WHERE ticker=?",
                (getattr(res, "order_id", None), getattr(res, "status", None), live_cost, ticker))
            con.commit()
        log.info("fade canary LIVE buy-NO %dx %s -> %s ($%.2f)",
                 count, ticker, getattr(res, "status", "?"), live_cost)
        return True
    except Exception as e:  # noqa: BLE001 — canary must never break paper accrual
        log.warning("fade canary order failed for %s: %s", ticker, e)
        return False


def _canary_deployed(con) -> float:
    """Total real $ deployed in live positions — from the ACCOUNT
    (market_exposure_cents), so it counts the existing book, not just rows with
    a recorded live_cost. Conservative (sums all account positions). Falls back
    to the SQLite live_cost sum if the authed read fails."""
    try:
        p = kc.get_authed_positions()
        if p is not None and len(p) and "market_exposure_cents" in p.columns:
            cents = pd.to_numeric(p["market_exposure_cents"], errors="coerce").fillna(0).sum()
            return round(float(cents) / 100.0, 2)
    except Exception:  # noqa: BLE001
        pass
    return float(con.execute(
        "SELECT COALESCE(SUM(live_cost),0) FROM positions").fetchone()[0])


def _canary_backfill(con) -> int:
    """When the canary is live, place real buy-NO on the strongest open fades
    that lack a live order. Caps per-cycle AND at the aggregate $ ceiling;
    skips mentions (paper-only); no-op unless FADE_CANARY_ENABLED + profile live."""
    if not _canary_enabled():
        return 0
    try:
        import execution
        if not execution.is_profile_live(CANARY_PROFILE):
            return 0
    except Exception:  # noqa: BLE001
        return 0
    if _canary_deployed(con) >= CANARY_MAX_TOTAL:      # aggregate exposure cap
        return 0
    rows = con.execute(
        "SELECT ticker, question, no_entry_cost, field_overround FROM positions "
        "WHERE resolved=0 AND live_order_id IS NULL "
        "ORDER BY field_overround DESC LIMIT ?", (CANARY_MAX_PER_CYCLE,)).fetchall()
    placed = 0
    for tk, q, no_cost, oround in rows:
        if shutdown.requested():
            break
        if not (0.5 <= no_cost <= 0.97):
            continue
        # mentions are PAPER-only for live (least-proven + hard member selection)
        if CANARY_SKIP_MENTIONS and classify_mtype(q) == "say_mention":
            continue
        count, worth = _canary_size(float(no_cost), oround or 0)
        if not worth:
            log.info("canary skip %s: est edge <= fee", tk)
            continue
        # stored entry NO cost (recent); LiveBroker does its own pre-flight +
        # $-cap, and a stale price simply 400s/doesn't fill (logged).
        if _live_canary_order(con, tk, q, float(no_cost), count):
            placed += 1
        time.sleep(0.2)
    con.commit()
    return placed


def _open_positions(con, df: pd.DataFrame, overround: dict) -> int:
    held = {r[0] for r in con.execute("SELECT ticker FROM positions")}
    per_event = {}
    for (ev, n) in con.execute("SELECT event_id, COUNT(*) FROM positions GROUP BY event_id"):
        per_event[ev] = n
    cand = df[df["mid"].map(in_fade_band)].copy()
    # attach field overround; prefer SOONEST-resolving (velocity), ties by
    # overround (signal strength) — strong fields also get the wider cap below.
    cand["_or"] = cand["event_id"].astype(str).map(lambda e: overround.get(e, (0, 0))[0])
    if "days_to_resolution" in cand.columns:
        cand["_dtr"] = pd.to_numeric(cand["days_to_resolution"], errors="coerce").fillna(9e9)
    else:
        cand["_dtr"] = 9e9
    cand = cand.sort_values(["_dtr", "_or"], ascending=[True, False])
    opened = n_canary = 0
    for _, r in cand.iterrows():
        if shutdown.requested():
            break
        tk = r["id"]
        if tk in held:
            continue
        ev = str(r.get("event_id", "") or "")
        oround, fsize = overround.get(ev, (0.0, 0))
        strong = oround >= OVERROUND_STRONG
        cap = OVERROUND_CAP if strong else PER_EVENT_CAP
        if per_event.get(ev, 0) >= cap:
            continue
        yb, ya = r.get("best_bid"), r.get("best_ask")
        if pd.isna(yb):
            continue
        no_cost = round(1.0 - float(yb), 4)
        if not (0.5 <= no_cost <= 0.97):
            continue
        cnt = int(STAKE_DOLLARS / no_cost)
        if cnt < 1:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO positions (ticker,event_id,series,question,"
            "yes_bid,yes_ask,no_entry_cost,count,mtype,strategy,field_overround,"
            "field_size,opened_at,close_time,commit_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tk, ev, r["series"], str(r.get("question", ""))[:140],
             float(yb), float(ya) if pd.notna(ya) else None, no_cost, cnt,
             classify_mtype(r.get("question", "")),
             "overround" if strong else "broad", round(oround, 3), fsize,
             _now(), str(r.get("close_time", "")), provenance.label()))
        if cur.rowcount == 0:
            # OR IGNORE swallows CHECK/NOT NULL violations silently. Skip the
            # whole block — including the live canary below, which must never
            # place a real order for a position that was not recorded.
            log.warning("fade_retail: insert IGNORED for %s (constraint?) "
                        "no_cost=%s count=%s", tk, no_cost, cnt)
            continue
        per_event[ev] = per_event.get(ev, 0) + 1
        opened += 1
        if (_canary_enabled() and n_canary < CANARY_MAX_PER_CYCLE
                and not (CANARY_SKIP_MENTIONS
                         and classify_mtype(r.get("question", "")) == "say_mention")
                and _canary_deployed(con) < CANARY_MAX_TOTAL):
            _cnt, _worth = _canary_size(no_cost, oround)
            if _worth and _live_canary_order(con, tk, r.get("question", ""),
                                             no_cost, _cnt):
                n_canary += 1
    con.commit()
    return opened


def _manage_positions(con) -> tuple[int, int]:
    """Resolve settled positions AND take early profits on open ones that have
    appreciated enough. Returns (n_resolved, n_early_exited)."""
    rows = con.execute(
        "SELECT ticker, no_entry_cost, count FROM positions WHERE resolved=0").fetchall()
    resolved = exited = 0
    for tk, no_cost, cnt in rows:
        if shutdown.requested():
            break
        # Settlement comes from the RAW market endpoint. `lookup_markets_by_id`
        # normalizes the outcome away — it has no `resolved_yes` column — so the
        # old lookup-based check silently skipped every position forever.
        # Per docs/file-map.md gotcha #12: settled => status="finalized" + result in
        # {"yes","no"} ("" = still settling, "scalar" = non-binary, unscoreable).
        try:
            m = kc._get(f"markets/{tk}").get("market") or {}
        except Exception:  # noqa: BLE001
            continue
        if not m:
            continue
        result = m.get("result", "")
        settled = m.get("status") == "finalized" and result in ("yes", "no")
        if settled:
            resolved_yes = result == "yes"
            no_won = not resolved_yes
            fee = kalshi_fees.fee_for_trade(min(max(no_cost, 0.01), 0.99), cnt)
            pnl = round(((1.0 if no_won else 0.0) - no_cost) * cnt - fee, 4)
            con.execute(
                "UPDATE positions SET resolved=1, resolved_yes=?, pnl=?, "
                "exit_reason='resolution', exit_price=?, closed_at=? WHERE ticker=?",
                (1 if resolved_yes else 0, pnl, 1.0 if no_won else 0.0, _now(), tk))
            resolved += 1
        elif EARLY_EXIT_TP_FRACTION > 0:
            # mark to market: sell NO at the NO bid (= 1 - current yes_ask).
            # Newer markets expose yes_ask_dollars; older ones yes_ask (cents).
            _ask = m.get("yes_ask_dollars")
            if _ask is None and m.get("yes_ask") is not None:
                _ask = float(m["yes_ask"]) / 100.0
            ya = pd.to_numeric(pd.Series([_ask]), errors="coerce").iloc[0]
            if pd.notna(ya):
                no_sell = 1.0 - float(ya)
                mark = no_sell - no_cost
                max_profit = 1.0 - no_cost
                if max_profit > 0 and mark > 0 and mark >= EARLY_EXIT_TP_FRACTION * max_profit:
                    fee = kalshi_fees.round_trip_fees(no_cost, no_sell, cnt)  # entry+exit
                    pnl = round(mark * cnt - fee, 4)
                    con.execute(
                        "UPDATE positions SET resolved=1, pnl=?, exit_reason='take_profit', "
                        "exit_price=?, closed_at=? WHERE ticker=?",
                        (pnl, round(no_sell, 4), _now(), tk))
                    exited += 1
        time.sleep(0.15)
    con.commit()
    return resolved, exited


def run_cycle(*, discover: bool = True, verbose: bool = True) -> dict:
    # Pin the active profile so kalshi_auth.is_configured() resolves the canary
    # gate (needs active ∈ authorized_profiles). Standalone daemon → no restore.
    os.environ[_ACTIVE_PROFILE_ENV] = CANARY_PROFILE
    con = _con()
    opened = 0
    if discover:
        try:
            # Discover by CLOSE TIME, not volume — the retail primaries/awards/
            # mention fields don't surface in the volume-ordered get_markets feed
            # (same gotcha as MLB/econ); get_markets_closing_within catches them.
            df = kc.get_markets_closing_within(MAX_DAYS, limit=4000)
            df["series"] = df["id"].astype(str).str.split("-").str[0]
            df["event_id"] = df["id"].astype(str).str.rsplit("-", n=1).str[0]
            for c in ("best_bid", "best_ask", "volume", "days_to_resolution"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df["mid"] = (df["best_bid"] + df["best_ask"]) / 2
            retail = df[
                df["series"].map(is_retail_series)
                & (df["volume"] > MIN_VOLUME)
                & df["days_to_resolution"].between(MIN_DAYS, MAX_DAYS)
            ].copy()
            overround = _field_overround(retail)
            opened = _open_positions(con, retail, overround)
        except Exception as e:
            if verbose:
                print(f"discovery failed: {e}")
    resolved, exited = _manage_positions(con)
    canary = _canary_backfill(con)
    if verbose:
        n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
        print(f"opened={opened} resolved={resolved} early_exit={exited} "
              f"canary_live_orders={canary} open_now={n_open}")
    return {"opened": opened, "resolved": resolved, "early_exit": exited,
            "canary": canary}


def summary() -> dict:
    """Machine-readable state for the control plane (open/resolved/net/win/events)."""
    con = _con()
    n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
    n_done, net, wins = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
        "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
        "FROM positions WHERE resolved=1").fetchone()
    n_events = con.execute("SELECT COUNT(DISTINCT event_id) FROM positions").fetchone()[0]
    n_live = con.execute(
        "SELECT COUNT(*) FROM positions WHERE live_order_id IS NOT NULL").fetchone()[0]
    return {"open": n_open, "resolved": n_done, "net": round(float(net), 2),
            "win": round(100 * wins / n_done) if n_done else None,
            "events": n_events, "live_orders": n_live}


def stats() -> None:
    con = _con()
    n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
    n_done, net, wins = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
        "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
        "FROM positions WHERE resolved=1").fetchone()
    print(f"fade_retail: open={n_open} resolved={n_done}")
    if n_done:
        print(f"  net P&L=${net:.2f}  win={wins}/{n_done}={100*wins/n_done:.0f}%")
        print("  by market-type (resolved):")
        for mt, n, p, w in con.execute(
            "SELECT mtype, COUNT(*), COALESCE(SUM(pnl),0), "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
            "FROM positions WHERE resolved=1 GROUP BY mtype ORDER BY 3 DESC"):
            print(f"    {str(mt):16s} n={n:>3} net=${p:>7.2f} win={100*w/n:.0f}%")
        print("  by strategy (resolved):")
        for st, n, p in con.execute(
            "SELECT strategy, COUNT(*), COALESCE(SUM(pnl),0) "
            "FROM positions WHERE resolved=1 GROUP BY strategy"):
            print(f"    {str(st):12s} n={n:>3} net=${p:>7.2f}")
        print("  by exit (resolution vs early take-profit), w/ avg hold:")
        for er, n, p, hd in con.execute(
            "SELECT exit_reason, COUNT(*), COALESCE(SUM(pnl),0), "
            "COALESCE(AVG((closed_at-opened_at)/86400.0),0) "
            "FROM positions WHERE resolved=1 GROUP BY exit_reason"):
            print(f"    {str(er):12s} n={n:>3} net=${p:>7.2f} avg_hold={hd:.1f}d")
    print("\nopen fades (buy-NO):")
    for r in con.execute(
        "SELECT no_entry_cost, count, mtype, field_overround, close_time, question "
        "FROM positions WHERE resolved=0 ORDER BY field_overround DESC LIMIT 25"):
        print(f"  NO@{r[0]:.2f} x{r[1]:>3} [{str(r[2])[:12]:12s} OR={r[3]:.1f}] "
              f"close={str(r[4])[:10]} {str(r[5])[:38]}")


def main() -> None:
    import dotenv
    dotenv.load_dotenv()  # BEFORE run_cycle so FADE_CANARY_ENABLED + live gates load
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=21600)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.stats:
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
