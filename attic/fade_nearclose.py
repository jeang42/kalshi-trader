"""Fade-nearclose — 2-HOUR-turnaround near-close longshot fade (paper OOS).

The strongest NEW lead from the 2026-07-09 mine of the near-close panel
(`near_close_capture.sqlite`, 21,975 RESOLVED snapshots at ~2h lead — 3 orders
of magnitude past the starved n=7 sample that once concluded "no near-close
edge"). Venue-wide, executable + de-correlated (buy NO at 1-yes_bid, net Kalshi
fee, ONE observation per event):

  band 0.10-0.20  →  532 events  →  +1.6¢/contract  →  90% by-event win.

Higher bands and the favorite side all net NEGATIVE (0.20-0.35 -1.8¢, 0.35-0.55
-2.1¢, favorites -2.2¢) — so this arm fades ONLY the 0.10-0.20 near-close
longshot.

**2026-07-14 OOS correction (n=670 live fills).** The forward arm ran net
-$— at 88% win — the fat tail of longshot-fade, concentrated in a structural
class the panel had under-sampled. Breakdown by market STRUCTURE, not category:

    weather    (KXTEMP*/KXLOWT*)    n=274  net -$—  (-3.4¢/ct)  EXCLUDED
    commodity  (WTI/gold/silver/…)  n= 45  net  -$— (-11.3¢/ct)  EXCLUDED
    crypto/index thresholds         n=312  net  +$—  (+1.1¢/ct)  kept
    discrete retail (mention/award) n= 23  net   +$—  (+3.6¢/ct)  kept

Weather + commodity settles are continuous-underlying THRESHOLD markets: a
0.10-0.20 longshot there is a real Gaussian tail priced off realized vol, not a
retail-overpriced discrete outcome — and near-settle the underlying drifts TOWARD
the threshold, so the longshot resolves YES more than priced (fading it loses).
The behavioral fade edge lives only in fan-priced markets. Excluding those two
series classes flips the arm to ~+$—.

Exclusions: **Sports** (in-game books — falsified pro corner), **Finance**
(measured dead), and the continuous-threshold **weather + commodity/energy/FX**
series (EXCLUDE_SERIES_PREFIXES). Everything else stays in.

Small per trade (~+1.9% / ~2h) but the turnaround is 2 HOURS vs weeks for the
retail/prop fades — the compounding substrate. This is the PAPER arm: buy NO
marketable at (1-yes_bid), hold <=2h to resolution, de-correlate per-event, log
NET P&L tagged by category. PAPER ONLY — no live-order path.

SQLite data/shared/fade_nearclose.sqlite (env FADE_NEARCLOSE_DB).
CLI: `python fade_nearclose.py` | `--loop --interval 900` | `--stats`.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime

import pandas as pd

from pm.core import logctx, provenance, shutdown, sqlite_schema
from pm.venue import kalshi_client as kc
from pm.venue import kalshi_fees

log = logging.getLogger(__name__)

def _db_path() -> str:
    """Resolved on every call, never frozen at import.

    A module-level ``DB = os.getenv(...)`` captures whatever the environment
    held at import time, so nothing downstream — tests especially — can
    redirect it. That is not hypothetical: it is how synthetic rows reached
    this project's production stores, twice, and back_nearclose.py carries the
    same note for the same reason.
    """
    return os.getenv("FADE_NEARCLOSE_DB", "data/shared/fade_nearclose.sqlite")

# ─── Live fill-quality canary (2026-07-30) ─────────────────────────────────
# DORMANT BY DEFAULT. Purpose is measurement, not profit: this arm's paper edge
# is ~+2¢/contract, thin enough that execution decides whether it exists at all.
# The retail fade already demonstrated the failure mode — backtest +12% ROI,
# live ~breakeven — so the ONE number this canary exists to produce is
# `live_fill_price - no_entry_cost`: what we really paid vs the paper-assumed
# (1 - yes_bid). If fills come in ~flat, the +2¢ survives and sizing up is
# justified; if they give up 1-2¢, the edge is gone and this arm stays paper.
# Triple-gated exactly like fade_retail_longshot's canary: this flag AND the
# profile in LIVE_TRADING_PROFILES AND execution's own live gates.
CANARY_PROFILE = "agentic_fade_nearclose"
CANARY_CONTRACTS = 1          # minimum size — measuring fills, not making money
CANARY_MAX_PER_CYCLE = 2      # the 2h window opens many positions; stay tiny
# Aggregate real-$ ceiling. NOTE this is CUMULATIVE LIFETIME SPEND, not
# concurrent exposure: _canary_deployed sums live_cost over ALL rows, settled
# ones included, so it never decreases even as positions pay out ~40 min later.
# It is a total-orders budget (~1 fill per $0.85), which is what we want for a
# measurement instrument — it bounds how much the experiment can ever cost.
# Raised 25 -> 75 on 2026-08-02 (user go-ahead) to carry the slippage sample
# from n=29 to n~80; the first canary dollars answered the headline question (median
# slippage 0.0c) and the extra budget is for confirming it at size.
# Env-overridable so the limit can be tuned without a code edit; read fresh at
# call time (never at import) per the load_dotenv-ordering trap.
CANARY_MAX_TOTAL_DEFAULT = 75.0

FADE_LO, FADE_HI = 0.10, 0.20   # the ONLY surviving band (+1.6¢, 90% by-event)
MIN_VOLUME = 50
CLOSE_WITHIN_HOURS = 2.0        # near-close entry window (2h before close)
STAKE_DOLLARS = 10.0
# FLATTENED 2026-07-30 — uniform stake. The 4x tight-spread tier (added
# 2026-07-14 on a panel showing spread<=2c +4.40c/ct vs >2c -0.36c/ct) was
# INVERTED by its own forward data. Two independent live reads (07-18, 07-24)
# and the 07-30 re-read all say the opposite of the panel:
#     tight (<=2c)  n=117  +$—  +1.24c/ct
#     wide  (> 2c)  n=255  +$ 77.55  +2.81c/ct
# Both slices are positive, so the tier was not losing money — but its stated
# rationale ("the tight book carries the edge") is simply wrong, and 4x stake
# on a thin edge quadruples variance: the tier produced every one of the ~-$40
# tail events in the by-event table while the $10 rows capped at ~-$10.
# We do NOT re-base the tier on the *wide* signal — that would be a second
# cherry-pick on the same sample with no mechanistic story. Size uniformly
# until a differential survives out-of-sample on its own.
# TIER_MAX_SPREAD is retained: `spread` is still recorded per position and the
# --stats SPREAD SPLIT keeps validating the tier hypothesis on flat stakes,
# where per-contract numbers are comparable across the split.
TIER_MAX_SPREAD = 0.02      # reporting-only boundary; no longer sizes anything
# Fade every non-sport, non-finance category. Sports near-close = in-game books
# (falsified pro corner); Finance measured dead (-4.9¢). Everything else in.
EXCLUDE_CATEGORIES = {"Sports", "Finance"}

# Series-level exclusions (2026-07-14 OOS mine, n=670 live fills). The near-close
# window is FLOODED with continuous-underlying THRESHOLD markets — weather ("temp
# above X°") and commodity/energy settles ("gold above $Y"). A 0.10-0.20 longshot
# there is a genuine Gaussian tail priced off realized vol, NOT a retail-overpriced
# discrete outcome — so the behavioral fade edge does not apply, and near-settle
# drift toward the threshold makes fading the longshot a net LOSER:
#     weather   n=274  net -$—  (-3.4¢/ct)
#     commodity n= 45  net  -$— (-11.3¢/ct)
# vs the buckets that DO carry the edge (kept):
#     crypto/index thresholds n=312  net +$—  (+1.1¢/ct)
#     discrete retail         n= 23  net  +$—  (+3.6¢/ct)
# Excluding weather+commodity flips the arm from -$— to ~+$—.
EXCLUDE_SERIES_PREFIXES = (
    # Weather — continuous-temperature threshold, not a fan-priced longshot.
    "KXTEMP", "KXLOWT", "KXHIGH",
    # Commodity / energy / FX settles — Gaussian price tail, efficiently priced.
    "KXWTI", "KXBRENT", "KXGOLD", "KXSILVER", "KXNATGAS", "KXCOPPER",
    "KXAAAGAS", "KXEURUSD", "KXUSDJPY",
)

# Structural-KIND classifier (2026-07-14 cherry-pick cross-validation): the
# ONLY realized-P&L cut that replicated across two independent samples was
# kind-level, not family-level (per-family P&L flips sign at n=5-30 — see
# docs/edge-research-log.md 2026-07-14). crypto/index-threshold tickers get
# a tiered stake below when their book is tight (the mechanistic slice that
# carries the edge: spread<=2c +4.40c/ct n=206 vs >2c -0.36c/ct n=198).
_CRYPTO_ASSETS = ("HYPE", "DOGE", "NEAR", "AVAX", "LINK", "SHIB",
                   "BTC", "ETH", "XRP", "SOL", "LTC", "ADA", "ZEC", "HYP")
_CADENCE_SUFFIXES = ("15M", "D", "E", "H", "U", "W", "")

# Commodity / FX / index roots, for `asset_of` only (kind_of stays crypto-vs-
# discrete). Added 2026-08-06: these classes also publish several series per
# underlying (KXWTI / KXWTIH / KXWTI15M), so the crypto-only table let one
# barrel of crude count as three independent assets. NASDAQ before NASDAQ100 is
# handled by longest-match, so listing the short root is enough.
_COMMODITY_INDEX_ROOTS = ("BRENT", "NATGAS", "COPPER", "SILVER", "AAAGAS",
                          "EURUSD", "USDJPY", "NASDAQ", "GOLD", "WTI",
                          "INX", "DJI")
_UNDERLYING_ROOTS = _CRYPTO_ASSETS + _COMMODITY_INDEX_ROOTS


def kind_of(ticker: str) -> str:
    """Structural kind: 'crypto' (spot/index threshold series) else
    'discrete_retail'. Callers only ever see already-kept candidates
    (EXCLUDE_SERIES_PREFIXES / EXCLUDE_CATEGORIES filter first) — this
    doesn't re-derive those exclusions.

    Strips the KX prefix and an optional cadence suffix (15M|D|E|H|U|W),
    matching the LONGEST known asset first so e.g. HYPE isn't misparsed
    as HYP+E.

    >>> kind_of("KXBTCD-26JUL1105")
    'crypto'
    >>> kind_of("KXHYPE15M-x")
    'crypto'
    >>> kind_of("KXSOLD-x")
    'crypto'
    >>> kind_of("KXETH15M-x")
    'crypto'
    >>> kind_of("KXCPI-x")
    'discrete_retail'
    >>> kind_of("KXNETFLIXRANKMOVIE-x")
    'discrete_retail'
    """
    base = str(ticker).upper().split("-", 1)[0]
    if base.startswith("KX"):
        base = base[2:]
    for asset in sorted(_CRYPTO_ASSETS, key=len, reverse=True):
        if base.startswith(asset) and base[len(asset):] in _CADENCE_SUFFIXES:
            return "crypto"
    return "discrete_retail"


# ─── Pure helpers (doctested) ──────────────────────────────────────────────

def in_band(mid: float, *, lo: float = FADE_LO, hi: float = FADE_HI) -> bool:
    """YES mid in the overpriced-longshot band we fade (buy NO)?

    >>> in_band(0.15)
    True
    >>> in_band(0.10)
    True
    >>> in_band(0.20)
    True
    >>> in_band(0.25)
    False
    >>> in_band(0.05)
    False
    >>> in_band(None)
    False
    """
    try:
        return lo <= float(mid) <= hi
    except (TypeError, ValueError):
        return False


def event_of(ticker: str, event_id: str | None = None) -> str:
    """Independent-event key for de-correlation: the parent event_id if we have
    one, else the ticker minus its final ``-member`` segment (the one-winner
    field). One position per event caps correlated bets.

    >>> event_of("KXBTCD-25JUL0812-T112000", "KXBTCD-25JUL0812")
    'KXBTCD-25JUL0812'
    >>> event_of("KXBTCD-25JUL0812-T112000")
    'KXBTCD-25JUL0812'
    >>> event_of("SOLO")
    'SOLO'
    """
    if event_id:
        return str(event_id)
    return str(ticker).rsplit("-", 1)[0]


def asset_of(ticker: str) -> str:
    """Underlying-asset key for correlation control.

    ``event_of`` de-correlates one-winner FIELDS, but it does not de-correlate
    two different series written on the SAME underlying — which is the real
    dependence structure here. Measured 2026-08-02 on 1,259 resolved rows:
    22% sat in a same-asset/same-close-hour cluster and **90% of those clusters
    resolved identically** (KXSOLD + KXSOLE at 01:00; KXXRPD + KXXRP at 05:00
    are literally one bet booked twice). Tickers therefore collapse to their
    underlying symbol; anything unrecognized falls back to its series prefix.

    Commodity and index roots were added 2026-08-06 after the same bug resurfaced
    on the non-crypto side: ``KXWTI`` / ``KXWTIH`` / ``KXWTI15M`` are three series
    on ONE barrel of crude and were being booked as three independent assets
    (six same-hour duplicate pairs observed in back_nearclose). The original
    crypto-only table simply didn't cover the classes that arm actually trades.

    >>> asset_of("KXSOLD-26AUG0121-T71.9999")
    'SOL'
    >>> asset_of("KXSOLE-26AUG0121-B72.125")
    'SOL'
    >>> asset_of("KXSOL15M-26AUG020000-00")
    'SOL'
    >>> asset_of("KXHYPE15M-x")           # longest-match: not HYP + E
    'HYPE'
    >>> asset_of("KXXRPMINMON-XRP-26JUL31-100")
    'XRP'
    >>> asset_of("KXWTIH-26AUG0314-T79.39")
    'WTI'
    >>> asset_of("KXWTI15M-26AUG041200-00")
    'WTI'
    >>> asset_of("KXNASDAQ100U-26AUG04H160")
    'NASDAQ'
    >>> asset_of("KXGOLDH-x"), asset_of("KXGOLDD-x")
    ('GOLD', 'GOLD')
    >>> asset_of("KXRAIN-26AUG01-SATX")   # unrecognized -> series prefix
    'KXRAIN'
    """
    base = str(ticker).upper().split("-", 1)[0]
    stem = base[2:] if base.startswith("KX") else base
    for a in sorted(_UNDERLYING_ROOTS, key=len, reverse=True):
        # exact cadence suffix (KXSOLD -> SOL) or any longer qualifier
        # (KXXRPMINMON -> XRP), which the plain suffix table doesn't cover.
        if stem.startswith(a) and (stem[len(a):] in _CADENCE_SUFFIXES
                                   or stem[len(a):].isalnum()):
            return a
    return base


def close_hour(close_time: str) -> str:
    """Hour bucket of a close timestamp — the grain at which two markets on the
    same asset are the same bet. Empty/short input returns itself.

    >>> close_hour("2026-08-02T01:00:00Z")
    '2026-08-02T01'
    >>> close_hour("")
    ''
    """
    s = str(close_time or "")
    return s[:13]


def strike_of(ticker: str) -> float | None:
    """Strike K from a T-strike crypto ticker (settle >= K), else None.

    B-strikes are BETWEEN-buckets — a single distance is ill-defined for a
    range, so they deliberately return None rather than a half-right number.

    >>> strike_of("KXBTCD-26AUG0117-T62749.99")
    62749.99
    >>> strike_of("KXXRPD-26AUG0201-T1.0799")
    1.0799
    >>> strike_of("KXSOLE-26AUG0121-B72.125") is None    # between-bucket
    True
    >>> strike_of("KXRAIN-26AUG01-SATX") is None
    True
    """
    m = re.match(r".+-T([\d.]+)$", str(ticker))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def compute_z(spot: float, strike: float, sigma_hourly: float,
              hours_to_close: float) -> float | None:
    """Spot-distance z: how many close-horizon sigmas is spot past the strike.

        z = (spot - K) / (sigma_hourly * sqrt(h) * spot)

    Positive = spot above strike (YES-favourable). The 2026-08-07 study
    (docs/edge-research-log.md) found the favourite-back edge monotone in z:
    z<1 carries ~none of it, z 1-2 +2.85c/ct, z 2-4 +4.02c/ct, both halves
    positive. Recorded at entry for FORWARD validation before any gating.

    >>> round(compute_z(105.0, 100.0, 0.01, 4.0), 2)    # 5% above, 2% band
    2.38
    >>> compute_z(100.0, 100.0, 0.01, 4.0)
    0.0
    >>> compute_z(100.0, 105.0, 0.0, 4.0) is None       # zero vol -> undefined
    True
    """
    try:
        spot, strike = float(spot), float(strike)
        sigma_hourly, hours_to_close = float(sigma_hourly), float(hours_to_close)
    except (TypeError, ValueError):
        return None
    if spot <= 0 or sigma_hourly <= 0 or hours_to_close <= 0:
        return None
    return (spot - strike) / (sigma_hourly * hours_to_close ** 0.5 * spot)


# Max quote age accepted for z. Crypto (Coinbase) is 24/7 so its ticker call
# is always fresh; commodity/index/FX markets CLOSE, and a Friday-close quote
# says nothing about a weekend-settling threshold — computing z from one would
# poison the sample with confident-looking garbage (measured at build time:
# on a Saturday every Yahoo symbol served ~20-hour-old quotes).
MAX_SPOT_AGE_S = 30 * 60.0


def z_for_entry(ticker: str, close_time: str) -> tuple[float | None, float | None, float | None]:
    """(spot, sigma_hourly, z) for a T-strike entry, or Nones.

    Routes by asset: crypto -> crypto_data (Coinbase, always fresh);
    commodity/index/FX/yields -> underlying_data (Yahoo, delayed + sessioned,
    quotes older than MAX_SPOT_AGE_S refused). Measurement instrumentation —
    must NEVER block or delay an entry, so every failure path (unsupported
    asset, B-strike, source down, stale quote, bad close_time) degrades to
    Nones. Callers may invoke it for every candidate; unsupported assets cost
    two dict lookups, no network.
    """
    try:
        from pm.feeds import crypto_data, underlying_data
        strike = strike_of(ticker)
        asset = asset_of(ticker)
        if strike is None:
            return None, None, None
        close = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
        hours = (close - datetime.now(UTC)).total_seconds() / 3600.0
        if hours <= 0:
            return None, None, None
        if asset in crypto_data.SYMBOLS:
            spot = crypto_data.get_spot_price(asset)
            sigma = crypto_data.hourly_sigma(asset)
        elif underlying_data.supported(asset):
            got = underlying_data.get_spot(asset)
            if got is None:
                return None, None, None
            spot, age = got
            if age > MAX_SPOT_AGE_S:
                log.info("z_for_entry(%s): quote %.0f min stale — refusing z",
                         ticker, age / 60)
                return None, None, None
            sigma = underlying_data.hourly_sigma(asset)
        else:
            return None, None, None
        if spot is None or sigma is None:
            return spot, sigma, None
        return spot, sigma, compute_z(spot, strike, sigma, hours)
    except Exception as e:  # noqa: BLE001 — instrumentation never blocks entry
        log.warning("z_for_entry(%s) failed: %s", ticker, e)
        return None, None, None


def series_excluded(ticker: str,
                    prefixes: tuple[str, ...] = EXCLUDE_SERIES_PREFIXES) -> bool:
    """Is this a continuous-underlying threshold series we do NOT fade?

    Weather + commodity/energy/FX settles: a 0.10-0.20 longshot there is a real
    distribution tail, not a retail-overpriced discrete outcome (measured net
    loser OOS). See EXCLUDE_SERIES_PREFIXES.

    >>> series_excluded("KXTEMPNYCH-26JUL1400")
    True
    >>> series_excluded("KXWTIH-26JUL1417")
    True
    >>> series_excluded("KXBTCD-26JUL1105")
    False
    >>> series_excluded("KXNETFLIXRANKMOVIE-26JUL13")
    False
    """
    t = str(ticker).upper()
    return any(t.startswith(p) for p in prefixes)


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape exactly as every file on disk had it
# (CREATE IF NOT EXISTS + the three generations of ADD COLUMN instrumentation:
# 2026-07-14 spread/kind/stake, 2026-07-30 live-canary fill quality, 2026-08-07
# spot/sigma/z). v2 rebuilds `positions` with the constraints SQLite cannot
# add in place — verified against every live row before shipping.

def _schema_v1(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        ticker TEXT PRIMARY KEY, event_id TEXT, category TEXT, question TEXT,
        yes_bid REAL, yes_mid REAL, no_entry_cost REAL, count INTEGER,
        opened_at INTEGER, close_time TEXT,
        resolved INTEGER DEFAULT 0, resolved_yes INTEGER, pnl REAL, closed_at INTEGER)""")
    for col, coltype in (("spread", "REAL"), ("kind", "TEXT"), ("stake", "REAL"),
                         ("live_order_id", "TEXT"), ("live_status", "TEXT"),
                         ("live_count", "INTEGER"), ("live_fill_price", "REAL"),
                         ("live_cost", "REAL"), ("live_quote_cost", "REAL"),
                         ("spot", "REAL"), ("sigma", "REAL"), ("z", "REAL")):
        sqlite_schema.add_column_if_missing(c, "positions", col, coltype)


_POSITIONS_V2 = """CREATE TABLE positions__new (
    ticker         TEXT PRIMARY KEY NOT NULL CHECK (ticker <> ''),
    event_id       TEXT, category TEXT, question TEXT,
    yes_bid        REAL CHECK (yes_bid IS NULL OR (yes_bid >= 0 AND yes_bid <= 1)),
    yes_mid        REAL CHECK (yes_mid IS NULL OR (yes_mid >= 0 AND yes_mid <= 1)),
    no_entry_cost  REAL NOT NULL CHECK (no_entry_cost > 0 AND no_entry_cost <= 1),
    count          INTEGER NOT NULL CHECK (count > 0),
    opened_at      INTEGER NOT NULL,
    close_time     TEXT,
    resolved       INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_yes   INTEGER CHECK (resolved_yes IS NULL OR resolved_yes IN (0, 1)),
    pnl            REAL, closed_at INTEGER,
    spread REAL, kind TEXT,
    stake          REAL CHECK (stake IS NULL OR stake > 0),
    live_order_id TEXT, live_status TEXT, live_count INTEGER,
    live_fill_price REAL, live_cost REAL, live_quote_cost REAL,
    spot REAL, sigma REAL, z REAL,
    -- a resolved row carries its outcome and P&L; an open row carries neither
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
    os.makedirs(os.path.dirname(_db_path()) or ".", exist_ok=True)
    c = sqlite3.connect(_db_path())
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(c, store="fade_nearclose", migrations=SCHEMA,
                                db_path=_db_path())
    return c


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


# ─── Discovery / entry ─────────────────────────────────────────────────────

def _discover() -> pd.DataFrame:
    """One closing-soon fetch: markets within CLOSE_WITHIN_HOURS of close."""
    df = kc.get_markets_closing_within(CLOSE_WITHIN_HOURS / 24.0, limit=1000)
    if df is None or df.empty:
        return pd.DataFrame()
    for c in ("best_bid", "best_ask", "volume", "last_trade_price"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # yes mid from top-of-book (fallback to last if a side is missing)
    bid = df["best_bid"].where(df["best_bid"] > 0)
    ask = df["best_ask"].where(df["best_ask"] < 1)
    mid = (bid + ask) / 2
    if "last_trade_price" in df.columns:
        mid = mid.fillna(df["last_trade_price"].where(
            (df["last_trade_price"] > 0) & (df["last_trade_price"] < 1)))
    df["yes_mid"] = mid
    return df


def _open(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    held = {r[0] for r in con.execute("SELECT ticker FROM positions")}
    per_event: dict[str, int] = {}
    for ev, n in con.execute("SELECT event_id, COUNT(*) FROM positions GROUP BY event_id"):
        per_event[ev] = n

    # Asset-level correlation control (2026-08-02). `event_of` de-correlates
    # one-winner FIELDS but does NOT stop us booking the same underlying twice:
    # two series on one asset settling the same hour are one bet. Measured on
    # 1,259 resolved rows: 172 (22% of rows) were same-asset/same-close-hour
    # duplicates and 90% of those clusters resolved IDENTICALLY (KXSOLD+KXSOLE
    # @01:00 and KXXRPD+KXXRP @05:00 each lost as a pair in the live canary).
    # One position per (asset, close-hour) collapses them.
    #
    # DELIBERATELY NOT ALSO CAPPING SERIAL (consecutive-hour) exposure, though
    # the run of four SOL losses through 71.99/72.49/72.99 looks like it wants
    # one. Everything tested on the real 1,087-cluster record, and every
    # variant was rejected on its own evidence:
    #   * concurrency cap  — never binds. Median hold is 38 MINUTES (not the 2h
    #     lead), so consecutive hourly markets don't overlap; a cap of 1 blocked
    #     exactly 1 row in 1,259.
    #   * rolling window   — costs far more than it saves: max2/4h drops net
    #     $— -> $— (blocks 225 rows that were net +$—) and leaves the worst
    #     clusters unchanged (-42.8/-40.3/-40.2 -> -42.8/-40.3/-39.9).
    #   * cool-off after a loss — apparent gains are lookahead (the rule needs
    #     the prior market's OUTCOME at our open time) and non-monotonic in the
    #     window (2h $—, 4h $—, 8h $—) = overfitting, not signal.
    # The serial effect is real but weak: P(loss | same-asset loss within 4h)
    # = 16.5% vs 11.3% base, n=91, and multi-loss runs are rare (96 runs of 1,
    # 9 of 2, 3 of 3). Meanwhile the TAIL it was meant to control is already
    # gone: worst loss is -$— on legacy $40-tier rows vs -$— at the
    # current uniform $10 stake (flattened 2026-07-30). Revisit only if the
    # conditional lift holds up at n >~ 300.
    taken_asset_hour = {
        (asset_of(t), close_hour(ct))
        for t, ct in con.execute("SELECT ticker, close_time FROM positions")}

    cand = df[df["yes_mid"].map(in_band)].copy()
    if cand.empty:
        return 0
    cand = cand[~cand["category"].isin(EXCLUDE_CATEGORIES)]
    if cand.empty:
        return 0
    # Bail before .map() on an empty frame: pandas returns a str-dtype Series
    # there, and inverting it yields a mask that silently drops every COLUMN,
    # so the volume filter below then raises KeyError('volume'). run_cycle
    # catches that and prints "discovery failed", which is doubly wrong —
    # discovery succeeded, and the arm quietly booked nothing. It fires on any
    # cycle where every in-band candidate happens to be an excluded category.
    cand = cand[~cand["condition_id"].map(series_excluded)]   # drop continuous-threshold series
    if cand.empty:
        return 0
    cand = cand[pd.to_numeric(cand["volume"], errors="coerce").fillna(0) > MIN_VOLUME]
    if cand.empty:
        return 0

    opened = 0
    for _, r in cand.iterrows():
        if shutdown.requested():
            break
        tk = str(r["condition_id"])
        if tk in held:
            continue
        ev = event_of(tk, r.get("event_id"))
        if per_event.get(ev, 0) >= 1:          # one position per event (de-correlate)
            continue
        asset = asset_of(tk)
        if (asset, close_hour(r.get("close_time"))) in taken_asset_hour:
            continue                           # same underlying, same hour = same bet
        yb = r.get("best_bid")
        if pd.isna(yb) or float(yb) <= 0:      # require a real standing YES bid
            continue
        no_cost = round(1.0 - float(yb), 4)
        if not (0.0 < no_cost < 1.0):
            continue
        ya = r.get("best_ask")
        spread = float(ya) - float(yb) if pd.notna(ya) and pd.notna(yb) else None
        kind = kind_of(tk)
        # Uniform stake since 2026-07-30 (see STAKE_DOLLARS): the tight-spread
        # 4x tier was inverted by its own forward data. `spread` and `kind` are
        # still recorded so --stats can keep testing the hypothesis on flat
        # stakes, where per-contract P&L is comparable across the split.
        stake = STAKE_DOLLARS
        cnt = int(stake / no_cost)
        if cnt < 1:
            continue
        # z instrumentation (any supported underlying; Nones elsewhere) —
        # measure-first, never gates. One fresh spot + cached sigma per entry.
        # (The fade arm excludes commodity series at discovery, so in practice
        # this records crypto; the shared helper self-filters regardless.)
        spot, sigma, z = z_for_entry(tk, str(r.get("close_time", "")))
        cur = con.execute(
            "INSERT OR IGNORE INTO positions (ticker,event_id,category,question,"
            "yes_bid,yes_mid,no_entry_cost,count,opened_at,close_time,"
            "spread,kind,stake,spot,sigma,z,commit_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tk, ev, str(r.get("category", "")), str(r.get("question", ""))[:140],
             float(yb), float(r["yes_mid"]), no_cost, cnt, _now(),
             str(r.get("close_time", "")), spread, kind, stake, spot, sigma, z,
             provenance.label()))
        if cur.rowcount == 0:
            # OR IGNORE swallows CHECK/NOT NULL violations as silently as PK
            # duplicates (and `held` already excludes those) — say so loudly
            # and do NOT count it as opened.
            log.warning("fade_nearclose: insert IGNORED for %s (constraint?) "
                        "no_cost=%s count=%s", tk, no_cost, cnt)
            continue
        per_event[ev] = per_event.get(ev, 0) + 1
        taken_asset_hour.add((asset, close_hour(r.get("close_time"))))
        held.add(tk)
        opened += 1
    con.commit()
    return opened


# ─── Live fill-quality canary ──────────────────────────────────────────────

def _canary_enabled() -> bool:
    """Read FRESH at call time, never at import. ``main()`` calls load_dotenv()
    AFTER this module is imported, so a module-level constant would freeze to
    False and the gate would silently stay paper — the documented ordering trap
    that bit the reactive arm (see docs/live-execution-adapter.md)."""
    return os.getenv("NEARCLOSE_CANARY_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _canary_max_total() -> float:
    """Cumulative real-$ ceiling, read FRESH from env at call time (same
    ordering trap as _canary_enabled — a module constant would freeze before
    main()'s load_dotenv). Bad/missing value falls back to the default."""
    try:
        v = float(os.getenv("NEARCLOSE_CANARY_MAX_TOTAL", "") or
                  CANARY_MAX_TOTAL_DEFAULT)
    except ValueError:
        log.warning("bad NEARCLOSE_CANARY_MAX_TOTAL; using default %.2f",
                    CANARY_MAX_TOTAL_DEFAULT)
        return CANARY_MAX_TOTAL_DEFAULT
    return max(0.0, v)


def _canary_deployed(con: sqlite3.Connection) -> float:
    """Real dollars deployed by THIS canary — the local recorded live_cost sum.

    Deliberately NOT the account-wide exposure read fade_retail_longshot uses:
    the Kalshi account is SHARED (the retail-fade book + the user's manual
    trades), so an account-wide number is permanently >= the $25 ceiling and
    would silently veto every order — which is exactly what happened on the
    canary's first armed cycle (account ~$— vs ceiling $25 -> 0 orders, no log).
    Scoping to our own recorded fills is the correct per-arm cap and mirrors
    execution's scope_to_own_positions philosophy."""
    return float(con.execute(
        "SELECT COALESCE(SUM(live_cost),0) FROM positions").fetchone()[0])


def _fresh_no_cost(ticker: str) -> float | None:
    """CURRENT executable NO cost (1 - live yes_bid), or None if unquotable.

    The fill-quality benchmark MUST be sampled at order time, not read from the
    stored paper entry: the very first armed cycle proved why — KXRAIN repriced
    yes 0.17→0.75 in the 14 minutes between paper entry and order, so the
    stored benchmark produced a nonsense −58c "slip". Handles the
    yes_bid_dollars-vs-yes_bid field gotcha (see RESUME 07-24 note).
    """
    try:
        m = kc._get(f"markets/{ticker}").get("market") or {}
    except Exception:  # noqa: BLE001
        return None
    if m.get("status") not in ("active", "open"):
        return None
    yb = m.get("yes_bid_dollars")
    if yb is None and m.get("yes_bid") is not None:
        yb = float(m["yes_bid"]) / 100.0
    if yb is None:
        return None
    yb = float(yb)
    if not (0.0 < yb < 1.0):
        return None
    return round(1.0 - yb, 4)


def _live_canary_order(con: sqlite3.Connection, ticker: str, question: str,
                       no_cost: float, count: int = CANARY_CONTRACTS) -> bool:
    """Place one real buy-NO through the vetted, triple-gated LiveBroker and
    record what we ACTUALLY paid next to what paper assumes RIGHT NOW.

    ``no_cost`` here is the FRESH quote from ``_fresh_no_cost`` (the caller
    re-checks the band on it too) — recorded as ``live_quote_cost`` so
    slippage = fill − same-instant benchmark, uncontaminated by market drift
    since the paper entry.

    Never raises: a canary failure must not break paper accrual. A non-fill is
    recorded too — an order that rests unfilled is itself a fill-quality result.
    """
    try:
        from pm.trading import execution
        if not execution.is_profile_live(CANARY_PROFILE):
            return False
        # place → record is one critical section (R-20): a SIGTERM between
        # the venue accepting the order and the UPDATE+commit landing would
        # leave a real position this store does not know about.
        with shutdown.deferred(f"nearclose canary {ticker}"):
            res = execution.get_broker(CANARY_PROFILE).buy(
                question=str(question)[:100], ticker=ticker, outcome="No",
                count=count, price=no_cost, current_mid=no_cost)
            filled = float(getattr(res, "filled_count", 0) or 0)
            avg = getattr(res, "avg_fill_price", None)
            fill_price = float(avg) if (filled and avg) else None
            live_cost = round(filled * fill_price, 4) if fill_price is not None else 0.0
            con.execute(
                "UPDATE positions SET live_order_id=?, live_status=?, live_count=?, "
                "live_fill_price=?, live_cost=?, live_quote_cost=? WHERE ticker=?",
                (getattr(res, "order_id", None), getattr(res, "status", None),
                 int(filled), fill_price, live_cost, no_cost, ticker))
            con.commit()
        slip = (fill_price - no_cost) if fill_price is not None else None
        log.info("nearclose canary LIVE buy-NO %dx %s -> %s quote=%.4f fill=%s "
                 "slip=%s", count, ticker, getattr(res, "status", "?"), no_cost,
                 f"{fill_price:.4f}" if fill_price is not None else "none",
                 f"{slip:+.4f}" if slip is not None else "n/a")
        return True
    except Exception as e:  # noqa: BLE001 — must never break paper accrual
        log.warning("nearclose canary order failed for %s: %s", ticker, e)
        return False


def _canary_backfill(con: sqlite3.Connection) -> int:
    """Place tiny real buy-NOs on open paper fades that don't have one yet.

    No-op unless NEARCLOSE_CANARY_ENABLED and the profile passes every live
    gate. Bounded three ways: per-cycle count, aggregate real-$ ceiling, and
    the band check (only fade prices the measured edge actually covers).
    """
    if not _canary_enabled():
        return 0
    try:
        from pm.trading import execution
        if not execution.is_profile_live(CANARY_PROFILE):
            return 0
    except Exception:  # noqa: BLE001
        return 0
    deployed, ceiling = _canary_deployed(con), _canary_max_total()
    if deployed >= ceiling:
        log.info("canary ceiling reached: $%.2f of $%.2f deployed — paused",
                 deployed, ceiling)
        return 0
    rows = con.execute(
        "SELECT ticker, question, no_entry_cost FROM positions "
        "WHERE resolved=0 AND live_order_id IS NULL "
        "ORDER BY opened_at DESC LIMIT ?", (CANARY_MAX_PER_CYCLE,)).fetchall()
    placed = 0
    for tk, q, no_cost in rows:
        if shutdown.requested():
            break
        # Cheap pre-filter on the stored paper entry...
        if not (0.80 <= float(no_cost) <= 0.90):
            continue
        # ...but the ORDER decision and benchmark use a FRESH quote: the market
        # can reprice hugely between paper entry and this cycle (KXRAIN went
        # yes 0.17→0.75 in 14 min on the first armed cycle) — a stale quote
        # both mis-prices the order and poisons the slippage measurement.
        fresh = _fresh_no_cost(tk)
        if fresh is None or not (0.80 <= fresh <= 0.90):
            log.info("canary skip %s: fresh quote %s out of band/unquotable",
                     tk, fresh)
            continue
        if _live_canary_order(con, tk, str(q), fresh):
            placed += 1
        time.sleep(0.2)
    con.commit()
    return placed


# ─── Resolution ────────────────────────────────────────────────────────────

def _resolve(con: sqlite3.Connection) -> int:
    now_iso = datetime.now(UTC).isoformat()
    rows = con.execute(
        "SELECT ticker, no_entry_cost, count FROM positions "
        "WHERE resolved=0 AND (close_time='' OR close_time < ?)", (now_iso,)).fetchall()
    done = 0
    for tk, no_cost, cnt in rows:
        if shutdown.requested():
            break
        try:
            data = kc._get(f"markets/{tk}")
            m = data.get("market") or {}
        except Exception:  # noqa: BLE001
            continue
        # Per docs/file-map.md gotcha #12: settled markets have status="finalized"
        # and result in {"yes","no"} (response vocabulary, not query vocabulary).
        # result="" means still settling — skip.
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
        time.sleep(0.15)   # pace resolution lookups (429 friendliness)
    con.commit()
    return done


# ─── Cycle / reporting ─────────────────────────────────────────────────────

def _decision_params() -> dict:
    """The values that decide what this arm trades.

    Registered with oos_ledger every cycle so a change to any of them resets
    the out-of-sample clock. That is the whole point: this arm's headline P&L
    became positive only after weather and commodity series were excluded
    using its OWN forward data, so "since when do these rules have untainted
    evidence" must be answered by the system rather than from memory.

    Only genuine selection inputs belong here — not stake size, not cadence,
    not anything that changes execution without changing what gets traded.
    """
    return {
        "band_lo": FADE_LO,
        "band_hi": FADE_HI,
        "min_volume": MIN_VOLUME,
        "close_within_hours": CLOSE_WITHIN_HOURS,
        "excluded_categories": sorted(EXCLUDE_CATEGORIES),
        "excluded_series_prefixes": sorted(EXCLUDE_SERIES_PREFIXES),
    }


@logctx.scope(arm="fade_nearclose")
def run_cycle(*, verbose: bool = True) -> dict:
    # Register the current rules (writes only when they actually change).
    try:
        from pm.ledger import oos_ledger
        oos_ledger.record_params("fade_nearclose", _decision_params())
    except Exception as e:  # noqa: BLE001 - bookkeeping must never stop trading
        log.warning("oos_ledger registration failed: %s", e)
    # Pin the active profile so kalshi_auth.is_configured() resolves the canary
    # gate (authorized set = owner ∪ LIVE_TRADING_PROFILES; the ACTIVE profile
    # must be in it). Standalone daemon → process-local, no restore needed.
    # Mirrors fade_retail_longshot.run_cycle.
    os.environ["POLYMARKET_ACTIVE_PROFILE"] = CANARY_PROFILE
    con = _con()
    opened = 0
    try:
        df = _discover()
        if not df.empty:
            opened = _open(con, df)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"discovery failed: {e}")
    resolved = _resolve(con)
    canary = _canary_backfill(con)     # no-op unless every live gate passes
    if verbose:
        n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
        # log, not print (R-23): the cycle-summary headline is the one line
        # most worth carrying the cycle id.
        log.info("opened=%d resolved=%d canary_live_orders=%d open_now=%d",
                 opened, resolved, canary, n_open)
    con.close()
    return {"opened": opened, "resolved": resolved, "canary_live_orders": canary}


def summary() -> dict:
    con = _con()
    n_open = con.execute("SELECT COUNT(*) FROM positions WHERE resolved=0").fetchone()[0]
    n_done, net, wins = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
        "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
        "FROM positions WHERE resolved=1").fetchone()
    ev = con.execute("SELECT COUNT(DISTINCT event_id) FROM positions").fetchone()[0]
    con.close()
    return {"open": n_open, "resolved": n_done, "net": round(float(net), 2),
            "win": round(100 * wins / n_done) if n_done else None, "events": ev}


def stats() -> None:
    con = _con()
    s = summary()
    print(f"fade_nearclose: open={s['open']} resolved={s['resolved']} events={s['events']}")
    if s["resolved"]:
        print(f"  net ${s['net']} win {s['win']}%")
        print("  by CATEGORY (resolved):")
        for cat, n, p, w in con.execute(
            "SELECT category, COUNT(*), COALESCE(SUM(pnl),0), "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
            "FROM positions WHERE resolved=1 GROUP BY category ORDER BY 3 DESC"):
            wp = round(100 * w / n) if n else 0
            print(f"    {str(cat)[:16]:16s} n={n:>3} net=${p:>7.2f} win={wp:>3}%")
        print("  by KIND (resolved):")
        for kind, n, p, w, cpc in con.execute(
            "SELECT COALESCE(kind,'legacy'), COUNT(*), COALESCE(SUM(pnl),0), "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0), "
            "COALESCE(100.0*SUM(pnl)/NULLIF(SUM(count),0),0) "
            "FROM positions WHERE resolved=1 GROUP BY 1 ORDER BY 3 DESC"):
            wp = round(100 * w / n) if n else 0
            print(f"    {str(kind)[:16]:16s} n={n:>3} net=${p:>7.2f} win={wp:>3}% "
                  f"{cpc:>+5.2f}c/ct")
        print("  SPREAD SPLIT — crypto (OOS validation of the tier):")
        for lbl, cond in ((f"<=  {TIER_MAX_SPREAD:.2f}", f"<= {TIER_MAX_SPREAD}"),
                          (f"> {TIER_MAX_SPREAD:.2f}", f"> {TIER_MAX_SPREAD}")):
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
                "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0), "
                "COALESCE(100.0*SUM(pnl)/NULLIF(SUM(count),0),0) FROM positions "
                f"WHERE resolved=1 AND kind='crypto' AND spread IS NOT NULL "
                f"AND spread {cond}").fetchone()
            n, p, w, cpc = row
            wp = round(100 * w / n) if n else 0
            print(f"    spread {lbl:>6s}   n={n:>3} net=${p:>7.2f} win={wp:>3}% "
                  f"{cpc:>+5.2f}c/ct")
        print("  by STAKE TIER:")
        for stk, n_open, n_res, p in con.execute(
            "SELECT COALESCE(stake, -1), "
            "SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END), "
            "COALESCE(SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END),0) "
            "FROM positions GROUP BY 1 ORDER BY 1"):
            lbl = "legacy" if stk == -1 else f"${stk:.0f}"
            print(f"    {lbl:>8s}  open={n_open:>3} resolved={n_res:>3} net=${p:>7.2f}")
        print("  by EVENT (independent, resolved — the OOS read):")
        for ev, n, p in con.execute(
            "SELECT event_id, COUNT(*), COALESCE(SUM(pnl),0) FROM positions "
            "WHERE resolved=1 GROUP BY event_id ORDER BY 3 LIMIT 20"):
            print(f"    {str(ev)[:34]:34s} n={n:>2} net=${p:>6.2f}")
    # ── CLUSTER-LEVEL (the honest independent-observation count) ───────────
    # Nominal row count overstates independence: rows on one asset in one
    # close-hour are one bet. Collapse to (asset, close-hour) so win% and n
    # mean what they claim. Divergence between the two = correlation carried.
    rows = list(con.execute(
        "SELECT ticker, close_time, pnl FROM positions WHERE resolved=1"))
    if rows:
        clusters: dict[tuple[str, str], float] = {}
        for tk, ct, p in rows:
            k = (asset_of(tk), close_hour(ct))
            clusters[k] = clusters.get(k, 0.0) + float(p or 0.0)
        cw = sum(1 for v in clusters.values() if v > 0)
        dup = len(rows) - len(clusters)
        print("\n  CLUSTER-LEVEL (asset × close-hour — the independent unit):")
        print(f"    rows={len(rows)}  independent clusters={len(clusters)}  "
              f"(+{dup} correlated dupes)")
        print(f"    cluster win={100 * cw / len(clusters):.0f}%  "
              f"net=${sum(clusters.values()):.2f}")
        worst = sorted(clusters.items(), key=lambda kv: kv[1])[:3]
        if worst and worst[0][1] < 0:
            print("    worst clusters: " + ", ".join(
                f"{a}@{h[-2:]}h ${v:.2f}" for (a, h), v in worst))

    # ── Z-GATE forward validation (fade wants z << 0: spot well below K) ───
    zr = list(con.execute(
        "SELECT z, pnl, count FROM positions WHERE resolved=1 AND z IS NOT NULL"))
    if zr:
        print("\n  Z AT ENTRY (spot-distance; fade edge predicted at z <= -1):")
        for lbl, lo, hi in (("z <= -1 (predicted edge)", -1e9, -1.0),
                            ("-1 < z < -0.5", -1.0, -0.5),
                            ("z >= -0.5 (predicted none)", -0.5, 1e9)):
            s = [(p, ct) for z, p, ct in zr if lo <= z < hi]
            if s:
                net = sum(p for p, _ in s)
                cts = sum(ct for _, ct in s)
                print(f"    {lbl:<27} n={len(s):>4} net=${net:>7.2f} "
                      f"per-ct={100 * net / cts:+6.2f}c")

    # ── LIVE FILL QUALITY — the make-or-break number for this arm ──────────
    # Paper assumes it buys NO at exactly (1 - yes_bid). The whole canary
    # exists to test that assumption: at a ~+2c/contract edge, giving up 1-2c
    # to slippage erases it. Positive slip = we paid MORE than paper assumed.
    n_live, n_filled = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN live_fill_price IS NOT NULL "
        "THEN 1 ELSE 0 END),0) FROM positions WHERE live_order_id IS NOT NULL"
    ).fetchone()
    if n_live:
        deployed, ceiling = _canary_deployed(con), _canary_max_total()
        print("\n  LIVE FILL QUALITY (canary — real fills vs paper assumption):")
        print(f"    orders placed={n_live}  filled={n_filled}  "
              f"unfilled={n_live - n_filled}")
        print(f"    budget: ${deployed:.2f} of ${ceiling:.2f} cumulative"
              f"{'  — CEILING REACHED, paused' if deployed >= ceiling else ''}")
        # Benchmark = live_quote_cost (same-instant quote at order time).
        # Rows without it (the first cycle's two, pre-fix) are excluded — their
        # stored paper entry can be a whole cycle stale and produces nonsense
        # slippage (KXRAIN: −58c that was market drift, not fill quality).
        slips = [row[0] for row in con.execute(
            "SELECT (live_fill_price - live_quote_cost) FROM positions "
            "WHERE live_fill_price IS NOT NULL AND live_quote_cost IS NOT NULL "
            "ORDER BY 1")]
        if slips:
            mean_slip = sum(slips) / len(slips)
            mid = slips[len(slips) // 2] if len(slips) % 2 else (
                slips[len(slips) // 2 - 1] + slips[len(slips) // 2]) / 2.0
            print(f"    slippage vs paper:  mean {mean_slip * 100:+.2f}c/ct   "
                  f"median {mid * 100:+.2f}c/ct   worst {max(slips) * 100:+.2f}c/ct")
            # The paper edge this arm is built on, for direct comparison.
            print(f"    paper edge is ~+1.6c/ct  ->  net of measured slippage: "
                  f"{(0.016 - mean_slip) * 100:+.2f}c/ct")
            if mean_slip > 0.010:
                print("    ⚠️  slippage eats most/all of the edge — keep this arm PAPER")
        else:
            print("    no fills yet — nothing to conclude")

    print("\n  open fades (soonest close first):")
    for r in con.execute(
        "SELECT no_entry_cost, count, close_time, category, question FROM positions "
        "WHERE resolved=0 ORDER BY close_time LIMIT 20"):
        print(f"    NO@{r[0]:.2f} x{r[1]:>3} close={str(r[2])[:16]} [{str(r[3])[:8]}] "
              f"{str(r[4])[:40]}")
    con.close()


def main() -> None:
    # BEFORE run_cycle so NEARCLOSE_CANARY_ENABLED + the live gates load from
    # .env — the systemd unit has no EnvironmentFile, so without this the
    # canary silently no-ops forever (the documented ordering trap; see
    # docs/live-execution-adapter.md and _canary_enabled's docstring).
    import dotenv
    dotenv.load_dotenv()
    # A real handler, or every canary log.warning/info is silently dropped —
    # the canary's first armed cycle failed invisibly for exactly this reason.
    logctx.setup()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900,  # 15 min (window is 2h wide)
                    help="Seconds between cycles when looping.")
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
