"""Kalshi WebSocket client for real-time order book streaming.

Replaces REST polling with sub-100ms push updates from Kalshi's
``orderbook_delta`` channel. Maintains an in-memory order book per
subscribed ticker and periodically persists snapshots to book_archive.

Architecture
------------
One long-lived asyncio task per connection. The connection authenticates
at the WebSocket handshake (RSA-PSS signed headers, same as kalshi_auth).
On connect, subscribes to ``orderbook_delta`` for all MLB game tickers.
Receives an initial snapshot per ticker, then incremental deltas.

The in-memory books are the *live* state — always current. A background
task flushes them to book_archive.sqlite at a configurable interval
(default 15s) so the lead-lag analysis pipeline sees the same schema.

Usage::

    python kalshi_ws.py                           # run until Ctrl-C
    python kalshi_ws.py --archive-interval 5      # flush every 5s
    python kalshi_ws.py --tickers KXMLBGAME-26MAY261810WSHCLE-WSH,KXMLBGAME-26MAY261810WSHCLE-CLE

As a library::

    from pm.capture.kalshi_ws import OrderBookStream
    stream = OrderBookStream()
    stream.get_book("KXMLBGAME-26MAY261810WSHCLE-WSH")  # -> {price: size} per side
    stream.get_mid("KXMLBGAME-26MAY261810WSHCLE-WSH")   # -> float | None
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from pm import REPO_ROOT
from pm.core import logctx, sqlite_schema

try:
    from dotenv import load_dotenv
    # .env lives at the REPO root — Path(__file__).parent stopped meaning that
    # when R-24 moved this module under pm/ (gotcha 1b: a __file__ default
    # captures the module's LOCATION). Loading a nonexistent .env is silent,
    # which is gotcha #4's failure mode: config believed loaded, actually not.
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import websockets

log = logging.getLogger("kalshi_ws")

WS_URL = os.getenv(
    "KALSHI_WS_URL",
    "wss://api.elections.kalshi.com/trade-api/ws/v2",
)
WS_PATH = "/trade-api/ws/v2"


# ─── Auth headers ────────────────────────────────────────────────────────

def _ws_auth_headers() -> dict[str, str]:
    """Build the three auth headers for the WebSocket handshake."""
    from pm.venue import kalshi_auth
    return kalshi_auth._sign("GET", WS_PATH)


# ─── In-memory order book ────────────────────────────────────────────────

@dataclass
class LiveBook:
    """One ticker's order book, maintained from snapshots + deltas."""
    ticker: str
    yes: dict[str, float] = field(default_factory=dict)  # price_str -> size
    no: dict[str, float] = field(default_factory=dict)
    last_seq: int = 0
    last_update_ts: float = 0.0
    snapshot_received: bool = False

    def apply_snapshot(self, msg: dict) -> None:
        self.yes = {}
        self.no = {}
        for price, size in msg.get("yes_dollars_fp", []):
            self.yes[price] = float(size)
        for price, size in msg.get("no_dollars_fp", []):
            self.no[price] = float(size)
        self.snapshot_received = True
        self.last_update_ts = time.time()

    def apply_delta(self, msg: dict) -> None:
        side = self.yes if msg["side"] == "yes" else self.no
        price = msg["price_dollars"]
        delta = float(msg["delta_fp"])
        current = side.get(price, 0.0)
        new_size = current + delta
        if new_size <= 0.0001:
            side.pop(price, None)
        else:
            side[price] = new_size
        self.last_update_ts = time.time()

    @property
    def best_bid(self) -> float | None:
        """Highest YES price (= best bid for YES buyers)."""
        return max((float(p) for p in self.yes), default=None)

    @property
    def best_ask(self) -> float | None:
        """Lowest effective ask = 1 - highest NO price."""
        if not self.no:
            return None
        return 1.0 - max(float(p) for p in self.no)

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        if b is not None and a is not None:
            return (b + a) / 2.0
        return b or a

    def to_archive_rows(self) -> list[dict]:
        """Convert to the format book_archive expects (list of
        {side, price, size} dicts serialized as JSON)."""
        rows = []
        for price, size in sorted(self.yes.items(), key=lambda x: -float(x[0])):
            rows.append({"side": "bid", "price": float(price), "size": size})
        for price, size in sorted(self.no.items(), key=lambda x: float(x[0])):
            rows.append({"side": "ask", "price": 1.0 - float(price), "size": size})
        return rows


# ─── Stream manager ──────────────────────────────────────────────────────

class OrderBookStream:
    """Manages the WebSocket connection and in-memory book state."""

    def __init__(self) -> None:
        self.books: dict[str, LiveBook] = {}
        self._sid: int | None = None
        self._sids: list[int] = []
        self._cmd_id = 0
        self._ws: Any = None
        self._connected = False
        # Executed-trade prints from the ``trade`` channel, buffered
        # in-memory and flushed to trade_archive alongside the book flush.
        self._trade_buffer: list[dict] = []
        self._stats = {"snapshots": 0, "deltas": 0, "trades": 0, "errors": 0,
                       "reconnects": 0, "archives": 0, "trades_archived": 0}

    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    def get_book(self, ticker: str) -> LiveBook | None:
        return self.books.get(ticker)

    def get_mid(self, ticker: str) -> float | None:
        book = self.books.get(ticker)
        return book.mid if book else None

    def get_all_mids(self) -> dict[str, float | None]:
        return {t: b.mid for t, b in self.books.items()}

    async def connect_and_stream(
        self,
        tickers: list[str],
        *,
        archive_interval: float = 15.0,
        on_delta: Any = None,
    ) -> None:
        """Connect, subscribe, and stream forever with auto-reconnect."""
        while True:
            try:
                await self._run_connection(
                    tickers,
                    archive_interval=archive_interval,
                    on_delta=on_delta,
                )
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                self._connected = False
                self._stats["reconnects"] += 1
                log.warning("connection lost (%s), reconnecting in 5s...", e)
                await asyncio.sleep(5)
            except Exception as e:
                self._connected = False
                self._stats["errors"] += 1
                log.exception("unexpected error: %s, reconnecting in 10s", e)
                await asyncio.sleep(10)

    async def _run_connection(
        self,
        tickers: list[str],
        *,
        archive_interval: float,
        on_delta: Any,
    ) -> None:
        headers = _ws_auth_headers()
        log.info("connecting to %s (%d tickers)", WS_URL, len(tickers))

        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            log.info("connected, subscribing to orderbook_delta")

            await ws.send(json.dumps({
                "id": self._next_id(),
                "cmd": "subscribe",
                "params": {
                    # orderbook_delta = resting liquidity; trade = executed
                    # prints (the "trade tape" with aggressor side). Both
                    # are public, one connection.
                    "channels": ["orderbook_delta", "trade"],
                    "market_tickers": tickers,
                },
            }))

            archive_task = asyncio.create_task(
                self._archive_loop(archive_interval)
            )

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    self._handle_message(msg, on_delta=on_delta)
            finally:
                archive_task.cancel()
                try:
                    await archive_task
                except asyncio.CancelledError:
                    pass

    def _handle_message(self, msg: dict, *, on_delta: Any = None) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "orderbook_snapshot":
            data = msg.get("msg", {})
            ticker = data.get("market_ticker", "")
            if ticker not in self.books:
                self.books[ticker] = LiveBook(ticker=ticker)
            self.books[ticker].apply_snapshot(data)
            self.books[ticker].last_seq = msg.get("seq", 0)
            self._stats["snapshots"] += 1
            log.debug("snapshot: %s (%d yes, %d no levels)",
                      ticker, len(self.books[ticker].yes),
                      len(self.books[ticker].no))

        elif msg_type == "orderbook_delta":
            data = msg.get("msg", {})
            ticker = data.get("market_ticker", "")

            if ticker not in self.books:
                self.books[ticker] = LiveBook(ticker=ticker)

            book = self.books[ticker]
            book.apply_delta(data)
            self._stats["deltas"] += 1

            if on_delta:
                try:
                    on_delta(ticker, book, data)
                except Exception:
                    pass

        elif msg_type == "trade":
            row = _normalize_ws_trade(msg.get("msg", {}))
            if row is not None:
                self._trade_buffer.append(row)
                self._stats["trades"] += 1

        elif msg_type == "subscribed":
            sid = msg.get("msg", {}).get("sid")
            if sid is not None and sid not in self._sids:
                self._sids.append(sid)
            self._sid = sid  # most-recent, kept for back-compat
            log.info("subscribed (sid=%s, channel=%s)",
                     sid, msg.get("msg", {}).get("channel"))

        elif msg_type == "error":
            err = msg.get("msg", {})
            log.error("server error: code=%s msg=%s", err.get("code"), err.get("msg"))
            self._stats["errors"] += 1

    async def _archive_loop(self, interval: float) -> None:
        """Periodically flush in-memory books to book_archive.sqlite."""
        while True:
            await asyncio.sleep(interval)
            try:
                snapshots = []
                now = int(time.time())
                for ticker, book in self.books.items():
                    if not book.snapshot_received:
                        continue
                    rows = book.to_archive_rows()
                    snapshots.append({
                        "venue": "kalshi",
                        "market_id": ticker,
                        "token_id": ticker,
                        "book_json": json.dumps(rows),
                        "ts": now,
                    })
                if snapshots:
                    n = _bulk_archive(snapshots)
                    self._stats["archives"] += n
                    log.debug("archived %d/%d books", n, len(snapshots))
                # Flush buffered trade prints. Swap the buffer out first so
                # messages arriving mid-flush aren't lost or double-counted.
                if self._trade_buffer:
                    pending, self._trade_buffer = self._trade_buffer, []
                    try:
                        from pm.archive import trade_archive
                        stored = trade_archive.store_trades(pending)
                        self._stats["trades_archived"] += stored
                        log.debug("archived %d/%d trades", stored, len(pending))
                    except Exception as e:  # noqa: BLE001
                        log.warning("trade flush failed: %s", e)
            except Exception as e:
                log.warning("archive flush failed: %s", e)

    async def update_tickers(self, tickers: list[str]) -> None:
        """Add tickers to every active subscription (both the orderbook and
        trade channels, each of which has its own sid)."""
        sids = self._sids or ([self._sid] if self._sid is not None else [])
        if not self._ws or not sids:
            return
        await self._ws.send(json.dumps({
            "id": self._next_id(),
            "cmd": "update_subscription",
            "params": {
                "sids": sids,
                "market_tickers": tickers,
                "action": "add_markets",
            },
        }))


def _ws_price(msg: dict, dollars_key: str, cents_key: str) -> float | None:
    """Pull a price from a WS trade message, tolerant of either schema:
    a ``*_dollars`` string ("0.6200") or an integer-cents field (62).

    Observed (2026-05): the ``trade`` channel sends ``*_price_dollars``
    strings — the same shape as the REST tape and the orderbook channel.
    The integer-cents branch is kept as defensive tolerance only; we treat
    a bare value > 1 as cents (no probability price exceeds $1)."""
    if dollars_key in msg and msg[dollars_key] is not None:
        try:
            return float(msg[dollars_key])
        except (TypeError, ValueError):
            return None
    if cents_key in msg and msg[cents_key] is not None:
        try:
            v = float(msg[cents_key])
        except (TypeError, ValueError):
            return None
        return v / 100.0 if v > 1 else v
    return None


def _normalize_ws_trade(msg: dict) -> dict | None:
    """Normalize a ``trade``-channel message into a trade_archive row.

    Returns None if the message lacks a ticker or a usable price. A
    ``trade_id`` is synthesized from the trade's identifying fields when
    the message doesn't carry one, so re-delivered prints dedupe against
    the archive's primary key instead of duplicating."""
    ticker = msg.get("market_ticker") or msg.get("ticker")
    if not ticker:
        return None
    ts_unix = msg.get("ts")
    try:
        ts_unix = int(ts_unix) if ts_unix is not None else int(time.time())
    except (TypeError, ValueError):
        ts_unix = int(time.time())
    yes_price = _ws_price(msg, "yes_price_dollars", "yes_price")
    no_price = _ws_price(msg, "no_price_dollars", "no_price")
    if yes_price is None and no_price is None:
        return None
    if yes_price is None and no_price is not None:
        yes_price = 1.0 - no_price
    if no_price is None and yes_price is not None:
        no_price = 1.0 - yes_price
    # Size: the WS trade channel sends the same shape as REST — fractional
    # `count_fp` (string), with an integer `count` as the older fallback.
    raw_count = msg.get("count_fp")
    if raw_count is None:
        raw_count = msg.get("count")
    try:
        count = float(raw_count) if raw_count is not None else 0.0
    except (TypeError, ValueError):
        count = 0.0
    taker_side = str(msg.get("taker_side") or "")
    trade_id = msg.get("trade_id") or (
        f"{ticker}:{ts_unix}:{yes_price}:{count}:{taker_side}"
    )
    return {
        "trade_id": str(trade_id),
        "ticker": str(ticker),
        "ts_unix": ts_unix,
        "count": count,
        "yes_price": yes_price,
        "no_price": no_price,
        "taker_side": taker_side,
    }


def _bulk_archive(snapshots: list[dict]) -> int:
    """Write pre-serialized snapshots to book_archive in one transaction."""
    import sqlite3

    from pm.archive import book_archive
    path = book_archive._path()
    try:
        with sqlite3.connect(str(path), timeout=30.0) as conn:
            sqlite_schema.set_wal(conn)   # busy-safe (R-27)
            conn.execute("PRAGMA synchronous=NORMAL")
            n = 0
            for s in snapshots:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO book_snapshots "
                        "(venue, market_id, token_id, snapshot_ts, book_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (s["venue"], s["market_id"], s["token_id"],
                         s["ts"], s["book_json"]),
                    )
                    n += 1
                except sqlite3.Error:
                    pass
            conn.commit()
            return n
    except sqlite3.Error as e:
        log.warning("bulk archive failed: %s", e)
        return 0


# ─── MLB ticker discovery ────────────────────────────────────────────────

def discover_mlb_tickers() -> list[str]:
    """Fetch all open MLB game market tickers via the REST API.
    Reuses the mlb_book_poller discovery logic."""
    from pm.capture.mlb_book_poller import _discover_markets
    return _discover_markets()


# ─── CLI entry point ─────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> int:
    logctx.setup(args.log_level.upper())

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    else:
        log.info("discovering MLB tickers...")
        tickers = discover_mlb_tickers()
        if not tickers:
            log.error("no MLB tickers found")
            return 1
        log.info("found %d tickers", len(tickers))

    stream = OrderBookStream()

    stats_interval = 60.0

    async def print_stats():
        while True:
            await asyncio.sleep(stats_interval)
            n_books = sum(1 for b in stream.books.values() if b.snapshot_received)
            mids = {t: f"${b.mid:.3f}" for t, b in stream.books.items()
                    if b.mid is not None and b.last_update_ts > time.time() - 30}
            log.info("stats: %s | active_books=%d | sample_mids=%s",
                     stream._stats, n_books,
                     dict(list(mids.items())[:3]))

    stats_task = asyncio.create_task(print_stats())

    try:
        await stream.connect_and_stream(
            tickers,
            archive_interval=args.archive_interval,
        )
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        stats_task.cancel()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default=None,
                    help="Comma-separated tickers. Default: auto-discover MLB.")
    ap.add_argument("--archive-interval", type=float, default=15.0,
                    help="Seconds between book_archive flushes (default 15).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
