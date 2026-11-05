"""News capture — idle-accrual instrument for the news-reaction (speed) thesis.

We've shown two things: the market is calibrated at executable prices (no static
edge), and our LLM does NOT beat the market as a forecaster (loses head-to-head
on 776 resolved forecasts). So the only remaining information edge is SPEED — a
fresh catalyst the book hasn't repriced yet — NOT analysis.

This instrument tests whether that edge is reachable, before we ever trade it.
Each cycle: for news-sensitive open markets, search recent news; when a FRESH
item appears (published within `--freshness-hours`, not seen before for this
market), record it with the market price AT DETECTION (best_bid/ask, from the
markets fetch — zero extra calls). Later a measurement joins each event to the
subsequent price (candlesticks) and asks: did the price move after we detected
the news, and is there a window we could actually have traded? The gap between
the news's `published` time and our `captured_at` is our latency disadvantage —
if the move happens in that gap, RSS is too slow and the edge is unreachable.

Read-only against the venue + Google News RSS; writes only its own SQLite at
data/shared/news_capture.sqlite (env NEWS_CAPTURE_DB). Does NOT trade.

CLI:
  python news_capture.py                       # one cycle
  python news_capture.py --loop --interval 600
  python news_capture.py --stats
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import logctx, sqlite_schema
from pm.venue import kalshi_client  # noqa: F401 — registers Kalshi provider
from pm.venue.provider import get_provider

log = logging.getLogger(__name__)

DEFAULT_DB = Path("data/shared/news_capture.sqlite")

# Markets whose resolution actually moves on news. These are Kalshi's RAW event
# categories (get_markets returns them unmapped). Sports has its own (in-game)
# pipeline; Entertainment is mostly slow/non-newsy.
NEWS_CATEGORIES = ("Politics", "Elections", "Economics", "Financials",
                   "World", "Companies", "Science and Technology", "Social",
                   "Climate and Weather", "Health")

# Cumulative/scalar markets aren't clean single-catalyst bets — skip (same
# class excluded in the arb scan / inefficiency sweep).
_SCALAR_RX = re.compile(
    r"between \d|at least \d|at most \d|\babove\b|\bbelow\b|or more|or fewer"
    r"|% of the|of the (?:popular )?vote|\bbefore\b|\bafter\b", re.I)


def _db_path() -> Path:
    return Path(os.getenv("NEWS_CAPTURE_DB", str(DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    sqlite_schema.set_wal(conn)   # busy-safe (R-27)
    conn.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(conn, store="news_capture", migrations=SCHEMA, db_path=p)
    return conn


# ─── store schema (versioned via sqlite_schema, P2/R-19) ───────────────────
# v1 adopts the pre-versioning shape verbatim. Constraints on the capture
# tables are deferred: adding CHECK/NOT NULL means a table rebuild, which is
# not something a capture daemon should do on the way to its next cycle.

def _schema_v1(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_events (
            ticker         TEXT,
            news_url       TEXT,
            question       TEXT,
            category       TEXT,
            news_title     TEXT,
            news_source    TEXT,
            news_published TEXT,
            captured_at    TEXT,
            lag_minutes    REAL,   -- captured_at - news_published (our latency)
            yes_bid        REAL,
            yes_ask        REAL,
            yes_mid        REAL,
            last_price     REAL,
            volume         REAL,
            PRIMARY KEY (ticker, news_url)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_news_captured "
                 "ON news_events(captured_at)")


SCHEMA = [sqlite_schema.Migration(1, "adopt pre-versioning tables", _schema_v1)]


def migrate() -> None:
    """Bring the store to the current schema version. Idempotent, and also
    happens on every ``_connect()`` — kept as the explicit entry point the
    runners call at cycle start."""
    _connect().close()



# ─── Pure helpers (testable) ───────────────────────────────────────────────

_QUERY_STRIP = re.compile(r"^will\s+|\?|\s+by\s+\w+\s+\d+.*$", re.I)


def news_query_for(question: str) -> str:
    """Derive a news search query from a market question. Crude but Google News
    handles natural language: drop the 'Will ... ?' scaffolding and trailing
    date clauses, keep the subject."""
    q = str(question or "").strip()
    q = re.sub(r"^will\s+", "", q, flags=re.I)
    q = q.rstrip("?").strip()
    # Drop a trailing "by <date>" / "in <year>" deadline clause.
    q = re.sub(r"\s+(by|before|in|on)\s+[A-Z][a-z]+\.?\s*\d.*$", "", q)
    q = re.sub(r"\s+in\s+\d{4}.*$", "", q)
    return q.strip()


def parse_published(s: str | None) -> datetime | None:
    """Parse an RSS pubDate (RFC822) to a UTC datetime."""
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        try:
            return pd.Timestamp(s).tz_convert("UTC").to_pydatetime()
        except Exception:  # noqa: BLE001
            return None


def is_fresh(published: datetime | None, now: datetime,
             freshness_hours: float) -> bool:
    """True if the item was published within the freshness window (a real
    catalyst, not an old article resurfacing)."""
    if published is None:
        return False
    age_h = (now - published).total_seconds() / 3600.0
    return 0 <= age_h <= freshness_hours


def _yes_mid(bid, ask, last):
    b = bid if bid and bid > 0 else None
    a = ask if ask and ask < 1 else None
    if b is not None and a is not None:
        return (b + a) / 2.0
    if last is not None and 0 < last < 1:
        return float(last)
    return b if b is not None else a


# ─── Store ops ─────────────────────────────────────────────────────────────

def _seen_urls(ticker: str) -> set[str]:
    with _connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT news_url FROM news_events WHERE ticker=?", (ticker,))}


def store_events(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany("""
            INSERT OR IGNORE INTO news_events
              (ticker, news_url, question, category, news_title, news_source,
               news_published, captured_at, lag_minutes, yes_bid, yes_ask,
               yes_mid, last_price, volume)
            VALUES
              (:ticker, :news_url, :question, :category, :news_title,
               :news_source, :news_published, :captured_at, :lag_minutes,
               :yes_bid, :yes_ask, :yes_mid, :last_price, :volume)
        """, rows)
        return conn.total_changes - before


def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
        mkts = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM news_events").fetchone()[0]
        cats = conn.execute(
            "SELECT category, COUNT(*) c FROM news_events GROUP BY category "
            "ORDER BY c DESC LIMIT 8").fetchall()
        lag = conn.execute(
            "SELECT AVG(lag_minutes) FROM news_events").fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(captured_at) FROM news_events").fetchone()[0]
    return {"total": total, "markets": mkts,
            "by_category": {r[0]: r[1] for r in cats},
            "avg_lag_minutes": round(lag, 1) if lag else None,
            "newest": newest}


def load_events() -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query("SELECT * FROM news_events", conn)


# ─── Cycle ─────────────────────────────────────────────────────────────────

@dataclass
class NewsCaptureConfig:
    categories: tuple[str, ...] = NEWS_CATEGORIES
    freshness_hours: float = 6.0
    max_markets: int = 80        # cap news queries per cycle (politeness)
    min_volume: float = 0.0      # capture broadly; liquidity is a trade-time
                                 # filter, not a capture filter (news-reaction
                                 # may live in lower-volume markets)
    max_items_per_market: int = 4


def _news_universe(provider, cfg: NewsCaptureConfig) -> pd.DataFrame:
    df = provider.get_markets(limit=1500)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[df["category"].isin(cfg.categories)]
    if "question" in df:
        df = df[~df["question"].astype(str).str.contains(_SCALAR_RX, regex=True)]
    if "volume" in df:
        df = df[df["volume"].fillna(0) >= cfg.min_volume]
    if "volume" in df:
        df = df.sort_values("volume", ascending=False)
    return df.head(cfg.max_markets)


def capture_cycle(provider, cfg: NewsCaptureConfig,
                  now: datetime | None = None) -> int:
    from pm.feeds import web_context as wc
    now = now or datetime.now(UTC)
    universe = _news_universe(provider, cfg)
    if universe.empty:
        return 0
    rows: list[dict] = []
    for _, m in universe.iterrows():
        ticker = str(m["condition_id"])
        query = news_query_for(str(m.get("question", "")))
        if len(query) < 4:
            continue
        try:
            items = wc.search_news(query, max_items=cfg.max_items_per_market)
        except Exception as e:  # noqa: BLE001
            log.debug("news search failed for %r: %s", query, e)
            continue
        if not items:
            continue
        seen = _seen_urls(ticker)
        bid = float(m.get("best_bid") or 0) or None
        ask = float(m.get("best_ask") or 0) or None
        last = float(m.get("last_trade_price") or 0) or None
        for it in items:
            url = getattr(it, "url", "") or ""
            if not url or url in seen:
                continue
            pub = parse_published(getattr(it, "published", None))
            if not is_fresh(pub, now, cfg.freshness_hours):
                continue
            lag = (now - pub).total_seconds() / 60.0 if pub else None
            rows.append({
                "ticker": ticker, "news_url": url,
                "question": str(m.get("question", "")),
                "category": str(m.get("category", "")),
                "news_title": getattr(it, "title", "") or "",
                "news_source": getattr(it, "source", "") or "",
                "news_published": pub.isoformat() if pub else None,
                "captured_at": now.isoformat(),
                "lag_minutes": round(lag, 1) if lag is not None else None,
                "yes_bid": bid, "yes_ask": ask,
                "yes_mid": _yes_mid(bid, ask, last),
                "last_price": last, "volume": float(m.get("volume", 0) or 0),
            })
    n = store_events(rows)
    if n:
        log.info("captured %d fresh news events across %d markets",
                 n, universe["condition_id"].nunique())
    return n


def run_cycle(cfg: NewsCaptureConfig | None = None) -> dict:
    migrate()
    cfg = cfg or NewsCaptureConfig()
    prov = get_provider("kalshi")
    return {"captured": capture_cycle(prov, cfg)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--freshness-hours", type=float, default=6.0)
    p.add_argument("--max-markets", type=int, default=80)
    p.add_argument("--min-volume", type=float, default=0.0)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=600)
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
    cfg = NewsCaptureConfig(freshness_hours=args.freshness_hours,
                            max_markets=args.max_markets,
                            min_volume=args.min_volume)
    if not args.loop:
        log.info("cycle: %s", run_cycle(cfg))
        return 0
    log.info("news_capture starting (freshness=%.1fh, interval=%ds)",
             args.freshness_hours, args.interval)
    while True:
        try:
            s = run_cycle(cfg)
            if s["captured"]:
                log.info("cycle: captured=%d", s["captured"])
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    sys.exit(main())
