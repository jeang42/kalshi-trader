"""back_nearclose — near-close FAVORITE-BACK paper arm (buy YES on 0.90-0.98).

The mirror of ``fade_nearclose``, and the other half of one bias.

THE FINDING (docs/edge-research-log.md 2026-07-30-late, re-verified 2026-08-02).
Re-mining the near-close panel across band x class x SIDE — the side sweep the
original 2026-07-09 mine never ran, it tested buy-NO only — showed that BUYING
YES on near-certain favourites is systematically +EV in continuous-underlying
classes, and NEGATIVE in exactly the class where fading longshots works:

    crypto      0.90-0.98 YES   n=670  +1.58c/ct  98% win
    commodity   0.90-0.98 YES   n= 37  +2.62c/ct 100% win
    finance     0.90-0.98 YES   n= 54  +0.98c/ct  98% win
    discrete    0.90-0.98 YES   n= 50  -3.37c/ct  <-- do NOT trade

MECHANISM. Same favourite-longshot bias, harvested from the expensive side.
Retail lottery demand bids up the cheap side, so the other side gets cheap. In
continuous-underlying markets the near-settle Gaussian drift toward the
already-likely outcome — the very thing that made our weather/commodity
longshot FADES lose money — is what pays the favourite buyer. Discrete markets
have no such drift, which is why the sign flips there. Two classes, two sides,
one bias; the two arms hold different tickers (a 0.94 market and a 0.15 market)
so they compose rather than overlap.

TWO CORRECTIONS applied at build time (2026-08-02), both against my own spec:

 1. The headline "+2.31c/ct (n=400)" does NOT hold at more data. Re-running the
    cell on the grown panel and de-duplicating per (asset, close-hour) rather
    than per event — the dependence structure the live fade canary exposed,
    where two series on one underlying settling the same hour are ONE bet —
    gives **+1.15c/ct, 97% win, n=757, thirds +1.19/+0.95/+1.30**. Still robust
    (all three time-thirds positive, and de-duplication slightly IMPROVED it:
    +1.00 -> +1.15), but roughly half the advertised edge. This module is built
    to the +1.15c number.

 2. The weather cell (0.75-0.90) is DROPPED, not merely tagged. It decays hard
    across time-thirds — **+6.51 / +0.37 / -1.49c** — so the most recent third
    is negative. It fails the replication demand the rest of the finding passes,
    and weather already cost the fade arm $—. Excluded until it re-earns a
    place.

RISK SHAPE — read before sizing. This is a high-win-rate, small-edge,
large-downside arm: ~97% of trades win ~+1c/contract and ~3% lose the whole
~94c entry. At the uniform $10 stake that is roughly +11c per win against
-$9.40 per loss. It will show long serene stretches punctuated by full-stake
losses; judge it on mean P&L per cluster, never on win rate.

PAPER ONLY. Deliberately imports neither ``kalshi_orders`` nor ``execution``
and places no orders — the fade arm earned its live canary over five weeks of
paper, and this one has none.

SQLite data/shared/back_nearclose.sqlite (env BACK_NEARCLOSE_DB).
CLI: `python back_nearclose.py` | `--loop --interval 900` | `--stats`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime

import pandas as pd

# asset_of/close_hour/event_of are imported rather than re-copied so there is
# ONE definition of this correctness-critical de-correlation logic (unit-tested
# when the fade canary's duplicate-booking bug was fixed on 2026-08-02).
# z_for_entry: spot-distance instrumentation shared with the fade arm
# (2026-08-07 study; measure-first, gates nothing yet).
from pm.arms.fade_nearclose import asset_of, close_hour, event_of, z_for_entry
from pm.core import logctx, provenance, shutdown, sqlite_schema
from pm.venue import kalshi_client as kc
from pm.venue import kalshi_fees

log = logging.getLogger(__name__)

# Band: yes MID in [0.90, 0.98) and an executable ASK at or below 0.97.
# The ask cap is EV arithmetic, not an outcome fit: at the measured ~3% loss
# rate, paying 0.985 needs a >99.4% win rate to break even, so the top of the
# book is structurally -EV no matter how the sample happened to land.
BACK_LO, BACK_HI = 0.90, 0.98
MAX_ENTRY_ASK = 0.97
MIN_VOLUME = 50
CLOSE_WITHIN_HOURS = 2.0
# Split point for a TWO-PASS fetch. Not an edge filter — a truncation fix.
#
# A bare "closing within 2h" fetch hits the 1000-row API cap, and since results
# are closing-soonest-first the truncation silently keeps only the most
# imminent markets: measured at build time, 75% of returned rows sat at EIGHT
# MINUTES of lead and the furthest was 68 min. The arm would never have seen
# most of the window. So we fetch the far band (min_days set) and the near band
# (unfiltered) separately and union them.
#
# We deliberately do NOT filter to the long-lead bucket even though it looks
# far better on the mean (lead>=1.0h: +2.70c/ct vs +1.15c all-leads). It fails
# the same replication test that killed the spread tier — its middle time-third
# is NEGATIVE (+4.19/-0.20/+4.14, n=131) while all-leads is positive in every
# third (+1.19/+0.95/+1.30, n=757). Higher mean, worse evidence; we trade the
# replicated cell. Revisit if the long-lead advantage survives a full third.
#
# NOTE: fade_nearclose has the same unguarded fetch and the same skew (its
# median hold is 38 min, not 2h). Left alone deliberately — its edge was
# validated on its own realized fills at whatever lead it truly used, so
# "fixing" discovery there would change the strategy out from under the track
# record that endorsed it.
FAR_BAND_LEAD_HOURS = 0.75
STAKE_DOLLARS = 10.0          # uniform; see fade_nearclose on why tiers lost

# Classes we back. Continuous-underlying only — these are the ones whose
# near-settle drift pays the favourite. Weather is a continuous underlying too
# but is excluded on its own decaying record (see module docstring).
BACK_CLASSES = frozenset({"crypto", "commodity", "finance"})

# Per-underlying exclusions (2026-08-06, n=263 live paper). Class-level gating
# wasn't fine enough: the arm's loss was ONE name.
#
#     WTI              n=78  -$—  -6.43c/ct  88% win  (9 full-stake losses)
#     NASDAQ           n= 5  -$ 7.50 -15.00c/ct  80% win
#     everything else  n=180 +$—  +2.30c/ct  97% win
#
# Excluding the worst name after seeing results is exactly the move that
# produced the spread tier we deleted, so this was held to the same test the
# lead-bucket cherry-pick failed: WTI is negative in two of three time-thirds
# (-6.34 / -13.90 / +0.95c) while the rest is positive in all three
# (+3.57 / +1.77 / +1.56c), and WTI's 88% win rate sits far outside the
# others' 97% — a different population, not variance. It is also mechanistically
# distinct: crude is the most professionally-traded underlying on the board and
# the only one publishing three overlapping series.
#
# CAVEAT, deliberately recorded: WTI's most recent third is POSITIVE (+0.95c)
# and its last 24h ran only -1.11c/ct, so the deficit is real but not currently
# widening. This is a hypothesis under test, not a settled fact — revisit if
# the excluded names would have turned positive over a full further third.
EXCLUDE_UNDERLYINGS = frozenset({"WTI", "NASDAQ"})

_WEATHER_PREFIXES = ("KXTEMP", "KXLOWT", "KXHIGH", "KXRAIN", "KXSNOW")
_COMMODITY_PREFIXES = ("KXWTI", "KXBRENT", "KXGOLD", "KXSILVER", "KXNATGAS",
                       "KXCOPPER", "KXAAAGAS", "KXEURUSD", "KXUSDJPY")
_CRYPTO_ASSETS = ("HYPE", "DOGE", "NEAR", "AVAX", "LINK", "SHIB",
                  "BTC", "ETH", "XRP", "SOL", "LTC", "ADA", "ZEC", "HYP")


# ─── Pure helpers (doctested) ──────────────────────────────────────────────

def classify(ticker: str, category: str = "") -> str:
    """Structural class driving the trade/skip decision.

    Prefix rules come first (they are exact), then the crypto asset table
    (longest match first so HYPE isn't read as HYP), then the venue category.

    >>> classify("KXBTCD-26AUG0117-T62749.99")
    'crypto'
    >>> classify("KXHYPE15M-x")
    'crypto'
    >>> classify("KXGOLDD-26AUG01", "Commodities")
    'commodity'
    >>> classify("KXTEMPNYC-26AUG01", "Climate")
    'weather'
    >>> classify("KXRAIN-26AUG01-SATX", "Climate")
    'weather'
    >>> classify("KXINXU-26AUG0113-T7414", "Financials")
    'finance'
    >>> classify("KXMLBGAME-26AUG01-NYY", "Sports")
    'sports'
    >>> classify("KXNETFLIXRANKMOVIE-x", "Entertainment")
    'discrete'
    """
    u = str(ticker).upper()
    if any(u.startswith(p) for p in _WEATHER_PREFIXES):
        return "weather"
    if any(u.startswith(p) for p in _COMMODITY_PREFIXES):
        return "commodity"
    stem = u.split("-", 1)[0]
    stem = stem[2:] if stem.startswith("KX") else stem
    for a in sorted(_CRYPTO_ASSETS, key=len, reverse=True):
        if stem.startswith(a):
            return "crypto"
    cat = str(category).strip().lower()
    if cat.startswith("sport"):
        return "sports"
    if cat.startswith("financ") or cat.startswith("econ"):
        return "finance"
    return "discrete"


def in_band(mid: float, ask: float) -> bool:
    """Backable favourite? Mid in [0.90, 0.98) AND an executable ask <= 0.97.

    Both legs matter: the mid defines the cell that was measured, the ask is
    what we actually pay.

    >>> in_band(0.94, 0.95)
    True
    >>> in_band(0.94, 0.98)          # ask above the EV cap
    False
    >>> in_band(0.99, 0.97)          # mid above the band
    False
    >>> in_band(0.85, 0.86)          # mid below the band
    False
    """
    try:
        m, a = float(mid), float(ask)
    except (TypeError, ValueError):
        return False
    return BACK_LO <= m < BACK_HI and 0.0 < a <= MAX_ENTRY_ASK


def backable(ticker: str, category: str = "") -> bool:
    """Is this a class whose favourites we back, and not an excluded underlying?

    >>> backable("KXBTCD-x")
    True
    >>> backable("KXGOLDH-x", "Commodities")
    True
    >>> backable("KXNETFLIXRANK-x", "Entertainment")     # discrete: measured -3.37c
    False
    >>> backable("KXTEMPNYC-x", "Climate")               # weather: decaying cell
    False
    >>> backable("KXWTIH-26AUG0314-T79.39", "Commodities")   # -6.43c/ct on n=78
    False
    >>> backable("KXWTI15M-x", "Commodities")            # all WTI series, one name
    False
    >>> backable("KXNASDAQ100U-x", "Financials")
    False
    """
    if asset_of(ticker) in EXCLUDE_UNDERLYINGS:
        return False
    return classify(ticker, category) in BACK_CLASSES


# ─── Store ─────────────────────────────────────────────────────────────────

def _db_path() -> str:
    """Resolved on every call, never frozen at import — a module-level constant
    makes tests silently open the LIVE database (that is exactly how a synthetic
    row got written into fade_nearclose's production DB on 2026-08-02)."""
    return os.getenv("BACK_NEARCLOSE_DB", "data/shared/back_nearclose.sqlite")


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape (CREATE IF NOT EXISTS + the 2026-08-07
# spot/sigma/z ADD COLUMNs); v2 rebuilds with constraints (verified against
# every live row before shipping).

def _schema_v1(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        ticker TEXT PRIMARY KEY, event_id TEXT, category TEXT, kind TEXT,
        question TEXT, yes_ask REAL, yes_mid REAL, entry_cost REAL,
        spread REAL, count INTEGER, stake REAL,
        opened_at INTEGER, close_time TEXT,
        resolved INTEGER DEFAULT 0, resolved_yes INTEGER,
        pnl REAL, closed_at INTEGER)""")
    for col in ("spot", "sigma", "z"):
        sqlite_schema.add_column_if_missing(c, "positions", col, "REAL")


_POSITIONS_V2 = """CREATE TABLE positions__new (
    ticker        TEXT PRIMARY KEY NOT NULL CHECK (ticker <> ''),
    event_id      TEXT, category TEXT, kind TEXT, question TEXT,
    yes_ask       REAL CHECK (yes_ask IS NULL OR (yes_ask >= 0 AND yes_ask <= 1)),
    yes_mid       REAL CHECK (yes_mid IS NULL OR (yes_mid >= 0 AND yes_mid <= 1)),
    entry_cost    REAL NOT NULL CHECK (entry_cost > 0 AND entry_cost <= 1),
    spread        REAL,
    count         INTEGER NOT NULL CHECK (count > 0),
    stake         REAL CHECK (stake IS NULL OR stake > 0),
    opened_at     INTEGER NOT NULL,
    close_time    TEXT,
    resolved      INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_yes  INTEGER CHECK (resolved_yes IS NULL OR resolved_yes IN (0, 1)),
    pnl           REAL, closed_at INTEGER,
    spot REAL, sigma REAL, z REAL,
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
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(c, store="back_nearclose", migrations=SCHEMA,
                                db_path=path)
    return c


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


# ─── Discovery / entry ─────────────────────────────────────────────────────

def _discover() -> pd.DataFrame:
    """Union of a FAR-band and a NEAR-band fetch — see FAR_BAND_LEAD_HOURS for
    why one fetch isn't enough (closing-soonest-first truncation at the cap)."""
    frames = []
    for label, kw in (("far", {"min_days": FAR_BAND_LEAD_HOURS / 24.0}),
                      ("near", {})):
        try:
            part = kc.get_markets_closing_within(
                CLOSE_WITHIN_HOURS / 24.0, limit=1000, **kw)
        except Exception as e:  # noqa: BLE001
            log.warning("%s-band discovery failed: %s", label, e)
            continue
        if part is None or part.empty:
            log.info("%s-band discovery: 0 markets", label)
            continue
        log.info("%s-band discovery: %d markets", label, len(part))
        frames.append(part)
    if not frames:
        log.warning("discovery returned NO markets within %.1fh — expected "
                    "hundreds; check the feed", CLOSE_WITHIN_HOURS)
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("condition_id")
    for c in ("best_bid", "best_ask", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["yes_mid"] = (df["best_bid"] + df["best_ask"]) / 2.0
    log.info("discovery: %d distinct markets within %.1fh",
             len(df), CLOSE_WITHIN_HOURS)
    return df


def _open(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    held = {r[0] for r in con.execute("SELECT ticker FROM positions")}
    per_event = {ev for (ev,) in con.execute("SELECT DISTINCT event_id FROM positions")}
    # One position per (asset, close-hour): two series on one underlying
    # settling the same hour are the same bet. This arm is crypto-heavy, which
    # is precisely where the fade canary proved such pairs resolve together.
    taken_asset_hour = {
        (asset_of(t), close_hour(ct))
        for t, ct in con.execute("SELECT ticker, close_time FROM positions")}

    # `category` may be absent from the feed; reindex to a real Series first —
    # df.get("category", "") would hand zip a bare STRING and silently iterate
    # its characters, classifying every row against "C", "r", "y", ...
    if df is None or df.empty:
        return 0
    cats = (df["category"] if "category" in df.columns
            else pd.Series([""] * len(df), index=df.index)).fillna("")
    # Build the mask as a boolean SERIES, not a bare list. `df[[]]` — an empty
    # list — is column selection in pandas, not row selection, so a frame with
    # no qualifying rows came back with zero COLUMNS and the volume filter
    # below then raised KeyError('volume'). Same failure the sibling arm had
    # via a different route: an ordinary "nothing qualified this cycle" became
    # an exception the cycle reported as a discovery failure.
    mask = pd.Series(
        [in_band(m, a) and backable(t, c)
         for m, a, t, c in zip(df["yes_mid"], df["best_ask"],
                               df["condition_id"], cats, strict=True)],
        index=df.index, dtype=bool)
    cand = df[mask].copy()
    if cand.empty:
        return 0
    cand = cand[pd.to_numeric(cand["volume"], errors="coerce").fillna(0) > MIN_VOLUME]
    if cand.empty:
        log.info("no backable candidates this cycle (of %d in window)", len(df))
        return 0

    opened = 0
    for _, r in cand.iterrows():
        if shutdown.requested():
            break
        tk = str(r["condition_id"])
        if tk in held:
            continue
        ev = event_of(tk, r.get("event_id"))
        if ev in per_event:
            continue
        asset, hour = asset_of(tk), close_hour(r.get("close_time"))
        if (asset, hour) in taken_asset_hour:
            continue
        ask = float(r["best_ask"])
        cnt = int(STAKE_DOLLARS / ask)
        if cnt < 1:
            continue
        bid = r.get("best_bid")
        spread = (ask - float(bid)) if pd.notna(bid) else None
        kind = classify(tk, r.get("category", ""))
        # z instrumentation for EVERY supported underlying (crypto via
        # Coinbase; commodity/index/FX/yields via underlying_data since
        # 2026-08-08) — measure-first, no gating. The 08-07 study predicts
        # this arm's edge lives at z >= 1; the commodity slice is the
        # largest flow and the one that hasn't been z-tested at all.
        spot, sigma, z = z_for_entry(tk, str(r.get("close_time", "")))
        cur = con.execute(
            "INSERT OR IGNORE INTO positions (ticker,event_id,category,kind,"
            "question,yes_ask,yes_mid,entry_cost,spread,count,stake,opened_at,"
            "close_time,spot,sigma,z,commit_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tk, ev, str(r.get("category", "")), kind,
             str(r.get("question", ""))[:140], ask, float(r["yes_mid"]), ask,
             spread, cnt, STAKE_DOLLARS, _now(), str(r.get("close_time", "")),
             spot, sigma, z, provenance.label()))
        if cur.rowcount == 0:
            # OR IGNORE swallows CHECK/NOT NULL violations as silently as PK
            # duplicates (`held` already excludes those) — be loud, don't count.
            log.warning("back_nearclose: insert IGNORED for %s (constraint?) "
                        "ask=%s count=%s", tk, ask, cnt)
            continue
        held.add(tk)
        per_event.add(ev)
        taken_asset_hour.add((asset, hour))
        opened += 1
        log.info("BACK yes %s @%.2f x%d (%s, z=%s, closes %s)", tk, ask, cnt,
                 kind, f"{z:.2f}" if z is not None else "n/a",
                 str(r.get("close_time", ""))[:16])
    con.commit()
    return opened


def _resolve(con: sqlite3.Connection) -> int:
    """Settle past-close positions from the RAW market endpoint.

    Per docs/file-map.md gotcha #12 settled => status="finalized" and result in
    {"yes","no"}; "" is still settling and "scalar" is unscoreable. NOT
    ``lookup_markets_by_id`` — it drops the outcome entirely, which silently
    froze fade_props and fade_retail_longshot at zero resolutions for weeks.
    """
    now_iso = datetime.now(UTC).isoformat()
    rows = con.execute(
        "SELECT ticker, entry_cost, count FROM positions "
        "WHERE resolved=0 AND (close_time='' OR close_time < ?)", (now_iso,)
    ).fetchall()
    done = 0
    for tk, cost, cnt in rows:
        if shutdown.requested():
            break
        try:
            m = kc._get(f"markets/{tk}").get("market") or {}
        except Exception:  # noqa: BLE001
            continue
        if m.get("status") != "finalized":
            continue
        result = m.get("result", "")
        if result not in ("yes", "no"):
            continue
        won = result == "yes"          # we hold YES
        fee = kalshi_fees.fee_for_trade(min(max(float(cost), 0.01), 0.99), int(cnt))
        pnl = round(((1.0 if won else 0.0) - float(cost)) * int(cnt) - fee, 4)
        con.execute("UPDATE positions SET resolved=1, resolved_yes=?, pnl=?, "
                    "closed_at=? WHERE ticker=?",
                    (1 if won else 0, pnl, _now(), tk))
        done += 1
        if not won:
            log.info("LOSS %s: paid %.2f x%d -> $%.2f", tk, cost, cnt, pnl)
        time.sleep(0.15)
    con.commit()
    return done


# ─── Cycle / reporting ─────────────────────────────────────────────────────

def _decision_params() -> dict:
    """The values that decide what this arm trades.

    See fade_nearclose's equivalent. This arm's WTI/NASDAQ exclusion was
    likewise derived from its own live record, and the source comment flags it
    as a hypothesis still under test — registering it here is what makes that
    testable rather than remembered.
    """
    return {
        "band_lo": BACK_LO,
        "band_hi": BACK_HI,
        "max_entry_ask": MAX_ENTRY_ASK,
        "min_volume": MIN_VOLUME,
        "close_within_hours": CLOSE_WITHIN_HOURS,
        "back_classes": sorted(BACK_CLASSES),
        "excluded_underlyings": sorted(EXCLUDE_UNDERLYINGS),
    }


@logctx.scope(arm="back_nearclose")
def run_cycle(*, verbose: bool = True) -> dict:
    # Register the current rules (writes only when they actually change).
    try:
        from pm.ledger import oos_ledger
        oos_ledger.record_params("back_nearclose", _decision_params())
    except Exception as e:  # noqa: BLE001 - bookkeeping must never stop trading
        log.warning("oos_ledger registration failed: %s", e)
    con = _con()
    opened = 0
    try:
        df = _discover()
        if not df.empty:
            opened = _open(con, df)
    except Exception as e:  # noqa: BLE001
        log.warning("discovery failed: %s", e)
    resolved = _resolve(con)
    n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
    if verbose:
        # log, not print (R-23): the headline carries the cycle id.
        log.info("opened=%d resolved=%d open_now=%d", opened, resolved, n_open)
    con.close()
    return {"opened": opened, "resolved": resolved, "open": n_open}


def summary() -> dict:
    con = _con()
    n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
    n_done, net, wins = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
        "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
        "FROM positions WHERE resolved=1").fetchone()
    con.close()
    return {"open": n_open, "resolved": n_done, "net": round(float(net), 2),
            "win": round(100 * wins / n_done) if n_done else None}


def stats() -> None:
    con = _con()
    s = summary()
    print(f"back_nearclose: open={s['open']} resolved={s['resolved']} "
          f"net=${s['net']} win={s['win']}%")
    if s["resolved"]:
        cts = con.execute(
            "SELECT COALESCE(SUM(count),0) FROM positions WHERE resolved=1").fetchone()[0]
        if cts:
            print(f"  per-contract: {100 * s['net'] / cts:+.2f}c/ct "
                  f"(panel says +1.15c/ct)")
        print("  by KIND:")
        for kind, n, net, w in con.execute(
            "SELECT kind, COUNT(*), COALESCE(SUM(pnl),0), "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
            "FROM positions WHERE resolved=1 GROUP BY kind ORDER BY 3"):
            print(f"    {str(kind):<12} n={n:>4} net=${net:>8.2f} "
                  f"win={round(100 * w / n) if n else 0:>3}%")
        # Z-gate forward validation (this arm's edge predicted at z >= 1).
        # Split by kind: crypto validates the 08-07 study; commodity (via the
        # 08-08 underlying_data feed) asks whether z separates the arm's
        # largest — and so far weakest — flow class the same way.
        zr = list(con.execute(
            "SELECT kind, z, pnl, count FROM positions "
            "WHERE resolved=1 AND z IS NOT NULL"))
        if zr:
            print("  Z AT ENTRY (spot-distance; back edge predicted at z >= 1):")
            kinds = sorted({k for k, _, _, _ in zr})
            for kd in kinds:
                for lbl, lo, hi in (("z < 0.5 (predicted none)", -1e9, 0.5),
                                    ("0.5 <= z < 1", 0.5, 1.0),
                                    ("z >= 1 (predicted edge)", 1.0, 1e9)):
                    s = [(p, ct) for k, z, p, ct in zr
                         if k == kd and lo <= z < hi]
                    if s:
                        net = sum(p for p, _ in s)
                        cts = sum(ct for _, ct in s)
                        print(f"    {kd:<10} {lbl:<25} n={len(s):>4} "
                              f"net=${net:>7.2f} per-ct={100 * net / cts:+6.2f}c")
        # Cluster level: the honest independent-observation count.
        rows = list(con.execute(
            "SELECT ticker, close_time, pnl FROM positions WHERE resolved=1"))
        cl: dict[tuple[str, str], float] = {}
        for tk, ct, p in rows:
            k = (asset_of(tk), close_hour(ct))
            cl[k] = cl.get(k, 0.0) + float(p or 0.0)
        cw = sum(1 for v in cl.values() if v > 0)
        print("  CLUSTER-LEVEL (asset × close-hour — the independent unit):")
        print(f"    rows={len(rows)}  clusters={len(cl)}  "
              f"(+{len(rows) - len(cl)} correlated dupes)")
        print(f"    cluster win={100 * cw / len(cl):.0f}%  net=${sum(cl.values()):.2f}")
        worst = sorted(cl.items(), key=lambda kv: kv[1])[:3]
        if worst and worst[0][1] < 0:
            print("    worst: " + ", ".join(f"{a} ${v:.2f}" for (a, _), v in worst))
    print("\n  open backs (soonest close first):")
    for r in con.execute(
        "SELECT yes_ask, count, close_time, kind, question FROM positions "
        "WHERE resolved=0 ORDER BY close_time LIMIT 20"):
        print(f"    YES@{r[0]:.2f} x{r[1]:>3} close={str(r[2])[:16]} "
              f"[{str(r[3])[:9]}] {str(r[4])[:44]}")
    con.close()


def main() -> None:
    import dotenv
    dotenv.load_dotenv()
    logctx.setup()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.stats:
        stats()
    elif args.loop:
        shutdown.install()          # SIGTERM finishes the current step, then exits (R-20)
        while not shutdown.requested():
            run_cycle()
            if shutdown.sleep(max(60, args.interval)):
                break
        log.info("clean shutdown")
    else:
        run_cycle()


if __name__ == "__main__":
    main()
