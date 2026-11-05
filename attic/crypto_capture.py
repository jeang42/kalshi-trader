"""Crypto latency capture — does the Kalshi book lag Coinbase spot?

The one validated edge (react-and-hold) is a LATENCY edge: the book lags a live
event. Crypto is the highest-frequency place to test the same mechanism — Kalshi
runs hourly BTC/ETH range markets (KXBTC/KXETH) + daily (KXBTCD/KXETHD), all a
direct function of spot, and it trades 24/7 so data accrues immediately (no game
schedule). Honest risk: crypto is the most quant-saturated corner, so the book
may track spot with ~zero lag (edge DOA, like news-RSS). This instrument settles
it empirically.

Model-light by design: capture a frequent time series of (Coinbase spot, Kalshi
market mid) for near-money short-dated crypto markets. The later analysis asks
whether market-mid changes LAG spot changes (cross-correlation at lag δ) — if
the book follows spot with a reachable delay, there's an edge; if it moves in
lockstep (lag 0), there isn't. Focus on near-money buckets (mid 0.05-0.95) since
deep-OTM buckets don't move.

Reuses `crypto_data.get_spot_price` (Coinbase) + `get_markets_closing_within`
(crypto hourly markets surface there). Read-only; writes only its SQLite (env
CRYPTO_CAPTURE_DB). Does NOT trade.

CLI:
  python crypto_capture.py                  # one cycle
  python crypto_capture.py --loop --interval 20
  python crypto_capture.py --stats
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

import crypto_data
import kalshi_client  # noqa: F401 — registers Kalshi provider
from provider import get_provider

log = logging.getLogger(__name__)

DEFAULT_DB = Path("data/shared/crypto_capture.sqlite")

# Kalshi crypto series → Coinbase asset symbol.
SERIES_ASSET = {
    "KXBTCD": "BTC", "KXBTC": "BTC", "KXBTC15M": "BTC",
    "KXETHD": "ETH", "KXETH": "ETH", "KXETH15M": "ETH",
    "KXXRP15M": "XRP", "KXDOGE15M": "DOGE",
}


def _db_path() -> Path:
    return Path(os.getenv("CRYPTO_CAPTURE_DB", str(DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crypto_snapshots (
                ticker        TEXT,
                ts            TEXT,
                asset         TEXT,
                close_time    TEXT,
                mins_to_close REAL,
                spot          REAL,
                yes_bid       REAL,
                yes_ask       REAL,
                yes_mid       REAL,
                volume        REAL,
                PRIMARY KEY (ticker, ts)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_crypto_ticker_ts "
                     "ON crypto_snapshots(ticker, ts)")


# ─── Pure helpers (testable) ───────────────────────────────────────────────

def asset_from_ticker(ticker: str) -> str | None:
    """Map a Kalshi crypto ticker to its Coinbase asset via the series prefix.
    Longest-prefix-first so KXBTC15M resolves before KXBTC."""
    t = str(ticker or "")
    for series in sorted(SERIES_ASSET, key=len, reverse=True):
        if t.startswith(series):
            return SERIES_ASSET[series]
    return None


def _mins_to_close(close_time: str, now: datetime) -> float | None:
    try:
        ct = pd.Timestamp(close_time)
        ct = ct.tz_localize("UTC") if ct.tzinfo is None else ct.tz_convert("UTC")
    except Exception:  # noqa: BLE001
        return None
    return (ct.to_pydatetime() - now).total_seconds() / 60.0


def _yes_mid(bid, ask, last):
    b = bid if bid and bid > 0 else None
    a = ask if ask and ask < 1 else None
    if b is not None and a is not None:
        return (b + a) / 2.0
    if last is not None and 0 < last < 1:
        return float(last)
    return b if b is not None else a


def select_markets(df: pd.DataFrame, *, mid_lo: float = 0.05, mid_hi: float = 0.95,
                   max_markets: int = 60) -> pd.DataFrame:
    """Near-money short-dated crypto markets worth capturing: a recognized
    crypto asset + a two-sided mid in (mid_lo, mid_hi) (deep-OTM buckets don't
    move, so they teach us nothing about lag). Highest-volume first."""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["asset"] = df["condition_id"].map(asset_from_ticker)
    df = df[df["asset"].notna()]
    df["_mid"] = [_yes_mid(b, a, lt) for b, a, lt in
                  zip(df.get("best_bid"), df.get("best_ask"),
                      df.get("last_trade_price"), strict=False)]
    df = df[df["_mid"].notna() & (df["_mid"] > mid_lo) & (df["_mid"] < mid_hi)]
    if "volume" in df:
        df = df.sort_values("volume", ascending=False)
    return df.head(max_markets)


# ─── Store ───────────────────────────────────────────────────────────────

def store_snapshots(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany("""
            INSERT OR IGNORE INTO crypto_snapshots
              (ticker, ts, asset, close_time, mins_to_close, spot,
               yes_bid, yes_ask, yes_mid, volume)
            VALUES
              (:ticker, :ts, :asset, :close_time, :mins_to_close, :spot,
               :yes_bid, :yes_ask, :yes_mid, :volume)
        """, rows)
        return conn.total_changes - before


def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM crypto_snapshots").fetchone()[0]
        mkts = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM crypto_snapshots").fetchone()[0]
        assets = conn.execute(
            "SELECT asset, COUNT(*) c FROM crypto_snapshots GROUP BY asset "
            "ORDER BY c DESC").fetchall()
        span = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM crypto_snapshots").fetchone()
    return {"snapshots": total, "markets": mkts,
            "by_asset": {r[0]: r[1] for r in assets},
            "span": (span[0], span[1])}


def load_series() -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query("SELECT * FROM crypto_snapshots", conn)


# ─── Cycle ─────────────────────────────────────────────────────────────────

def run_cycle(*, hours: float = 2.0, max_markets: int = 60) -> dict:
    migrate()
    prov = get_provider("kalshi")
    try:
        df = prov.get_markets_closing_within(hours / 24.0, limit=1000)
    except Exception as e:  # noqa: BLE001
        log.warning("closing-within fetch failed: %s", e)
        return {"captured": 0}
    sel = select_markets(df, max_markets=max_markets)
    if sel.empty:
        return {"captured": 0}
    now = datetime.now(UTC)
    iso = now.isoformat()
    spot_cache: dict[str, float | None] = {}
    rows: list[dict] = []
    for _, m in sel.iterrows():
        asset = m["asset"]
        if asset not in spot_cache:
            try:
                spot_cache[asset] = crypto_data.get_spot_price(asset)
            except Exception:  # noqa: BLE001
                spot_cache[asset] = None
        spot = spot_cache[asset]
        if spot is None:
            continue
        rows.append({
            "ticker": str(m["condition_id"]), "ts": iso, "asset": asset,
            "close_time": str(m.get("close_time", "")),
            "mins_to_close": _mins_to_close(str(m.get("close_time", "")), now),
            "spot": float(spot),
            "yes_bid": float(m.get("best_bid") or 0) or None,
            "yes_ask": float(m.get("best_ask") or 0) or None,
            "yes_mid": float(m["_mid"]),
            "volume": float(m.get("volume", 0) or 0),
        })
    n = store_snapshots(rows)
    return {"captured": n, "markets": len(rows)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=2.0,
                   help="Capture crypto markets closing within this many hours.")
    p.add_argument("--max-markets", type=int, default=60)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=20)
    p.add_argument("--stats", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if args.stats:
        migrate()
        print(stats())
        return 0
    if not args.loop:
        log.info("cycle: %s", run_cycle(hours=args.hours, max_markets=args.max_markets))
        return 0
    log.info("crypto_capture starting (interval=%ds, hours=%.1f)",
             args.interval, args.hours)
    last_log = 0.0
    while True:
        try:
            s = run_cycle(hours=args.hours, max_markets=args.max_markets)
            now = time.time()
            if now - last_log > 300:  # heartbeat every ~5 min
                log.info("cycle: %s", s)
                last_log = now
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    sys.exit(main())
