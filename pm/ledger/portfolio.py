"""File-backed portfolio with append-only transaction log.

Stores a list of buy/sell transactions and derives open positions and
realized P&L from them. Each transaction records what happened at a point
in time; positions are computed by replaying the log.

Storage format (portfolio.json):

    {
      "version": 2,
      "transactions": [
        {
          "id": "uuid4",
          "timestamp": "2026-05-17T20:00:00+00:00",
          "type": "buy" | "sell",
          "question": "Will X happen?",
          "condition_id": "0xabc...",
          "outcome": "Yes",
          "shares": 100.0,
          "price": 0.42
        }
      ]
    }

Old portfolio.json files (version 1, with a "positions" list) are migrated
transparently on first read — each position becomes a single buy transaction.
"""

import contextlib
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import atomic_io

log = logging.getLogger(__name__)

VERSION = 2


# ---------------------------------------------------------------------------
# Storage path — read lazily on every operation so profile switching takes
# effect without re-importing modules. Env var override wins (used by tests
# and explicit overrides); otherwise the active profile's directory is used.
# ---------------------------------------------------------------------------

def _portfolio_path() -> Path:
    env = os.getenv("POLYMARKET_PORTFOLIO_FILE")
    if env:
        return Path(env)
    # Local import keeps profiles.py out of the import cycle. Imported on
    # every call but Python's module cache makes this near-free.
    from pm.core import profiles
    return profiles.file_path("portfolio")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _empty_store() -> dict:
    return {"version": VERSION, "transactions": [], "snapshots": []}


def _migrate_v1_to_v2(data: dict) -> dict:
    """Convert old {'positions': [...]} format to the transaction log."""
    txns = []
    for pos in data.get("positions", []):
        txns.append({
            "id": str(uuid.uuid4()),
            "timestamp": pos.get("added_at") or datetime.now(UTC).isoformat(),
            "type": "buy",
            "question": pos.get("question", ""),
            "condition_id": pos.get("condition_id", ""),
            "outcome": pos.get("outcome", ""),
            "shares": float(pos.get("shares", 0)),
            "price": float(pos.get("avg_price", 0)),
        })
    return {"version": VERSION, "transactions": txns, "snapshots": []}


def _normalize(data: dict) -> tuple[dict, bool]:
    """Bring a freshly-read store up to the current shape.

    Returns ``(data, changed)`` so the caller can decide whether a write is
    warranted; a pure reader must never write, or every read of a v1 file
    from a read-only code path would try to mutate the ledger.
    """
    changed = False
    if data.get("version") != VERSION or "transactions" not in data:
        log.info("Migrating portfolio.json v1 → v2 (%d positions → buy transactions)",
                 len(data.get("positions", [])))
        data = _migrate_v1_to_v2(data)
        changed = True
    # Forward-compat: snapshots is a newer field; older v2 files won't have it
    if "snapshots" not in data:
        data.setdefault("snapshots", [])
        changed = True
    return data, changed


def _load() -> dict:
    """Read the store, tolerating a damaged file (recovers from ``.bak``,
    quarantines the bad copy) instead of raising — these callers are daemons
    and a crash-loop is worse than a stale read.

    A v1 file is migrated **and the migration is persisted**, which is the
    documented contract and is what the rest of the code assumes. The write is
    taken under the lock and only when something actually changed, so the
    common case (an already-current file) is a pure read. Safe to call from
    anywhere *except* inside :func:`_mutate`, which already holds the lock —
    it reads through ``atomic_io`` directly for exactly that reason.
    """
    path = _portfolio_path()
    data = atomic_io.read_json(path, None)
    if data is None:
        return _empty_store()
    data, changed = _normalize(data)
    if changed:
        with atomic_io.locked(path):
            atomic_io.write_json(path, data)
    return data


def _save(data: dict) -> None:
    """Publish the store atomically. Prefer :func:`_mutate` for anything that
    reads-then-writes — a bare save cannot exclude a competing writer."""
    atomic_io.write_json(_portfolio_path(), data)


@contextlib.contextmanager
def _mutate():
    """Locked read-modify-write over the store.

    Every mutation goes through here so two writers cannot interleave and drop
    one another's transactions — the concrete risk being ``snapshot_runner``
    (all profiles, 4x/day) landing between an arm daemon's read and its write.
    """
    path = _portfolio_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None)
        data = _empty_store() if data is None else _normalize(data)[0]
        yield data
        atomic_io.write_json(path, data)


# ---------------------------------------------------------------------------
# Recording transactions
# ---------------------------------------------------------------------------

def record_transaction(
    tx_type: str,
    question: str,
    condition_id: str,
    outcome: str,
    shares: float,
    price: float,
) -> dict:
    """Append a transaction. tx_type must be 'buy' or 'sell'.

    Sells are validated against the currently-held position — selling more
    than you hold raises ValueError. Returns the recorded transaction dict.
    """
    if tx_type not in ("buy", "sell"):
        raise ValueError(f"tx_type must be 'buy' or 'sell', got {tx_type!r}")
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}")
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0, 1], got {price}")

    if tx_type == "sell":
        positions = get_positions()
        if positions.empty:
            raise ValueError("Cannot sell — no open positions.")
        match = positions[
            (positions["condition_id"] == condition_id)
            & (positions["outcome"] == outcome)
        ]
        if match.empty:
            raise ValueError(
                f"Cannot sell — no open position for {outcome} on {condition_id}."
            )
        held = float(match.iloc[0]["shares"])
        if shares > held + 1e-9:
            raise ValueError(
                f"Cannot sell {shares} shares — only {held:g} held."
            )

    tx = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "type": tx_type,
        "question": question,
        "condition_id": condition_id,
        "outcome": outcome,
        "shares": float(shares),
        "price": float(price),
    }
    with _mutate() as data:
        data["transactions"].append(tx)
    log.info("recorded %s: %g shares of %s on %s @ $%.4f",
             tx_type, shares, outcome, condition_id[:18], price)
    return tx


def add_buy(question: str, condition_id: str, outcome: str,
            shares: float, price: float) -> dict:
    return record_transaction("buy", question, condition_id, outcome, shares, price)


def add_sell(question: str, condition_id: str, outcome: str,
             shares: float, price: float) -> dict:
    return record_transaction("sell", question, condition_id, outcome, shares, price)


def delete_transaction(index: int) -> bool:
    """Delete a transaction by its 0-based index in chronological order.

    Returns True if a transaction was deleted, False otherwise. Use carefully:
    deleting an old buy will retroactively invalidate any later sell that
    depended on it. There's no automatic check for this.
    """
    with _mutate() as data:
        txns = data.get("transactions", [])
        deleted = 0 <= index < len(txns)
        if deleted:
            txns.pop(index)
    return deleted


# ---------------------------------------------------------------------------
# Backward-compat aliases (used by older callers and the original tests)
# ---------------------------------------------------------------------------

def add_position(question: str, condition_id: str, outcome: str,
                 shares: float, avg_price: float) -> None:
    """Legacy alias for add_buy()."""
    add_buy(question, condition_id, outcome, shares, avg_price)


def remove_position(index: int) -> None:
    """Legacy: delete the transaction at index. No-op if out of range.

    The original semantic ("remove a position by index from the positions list")
    doesn't map cleanly onto the transaction log. We interpret it as
    "delete the transaction at the given chronological index", which matches
    the original behavior when there's only one buy per position.
    """
    delete_transaction(index)


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------

def get_transactions() -> pd.DataFrame:
    """All transactions, in chronological order."""
    data = _load()
    txns = data.get("transactions", [])
    cols = ["id", "timestamp", "type", "question", "condition_id",
            "outcome", "shares", "price"]
    if not txns:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(txns)
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df.sort_values("timestamp_dt").reset_index(drop=True)


def _replay(transactions: list[dict]) -> dict:
    """Replay transactions in chronological order using the average-cost method.

    Returns a dict keyed by (condition_id, outcome) with running state:
        shares          float   currently held
        cost            float   total cost basis still tied up in open shares
        realized_pnl    float   cumulative realized profit/loss
        avg_buy_price   float   cost / shares (or 0 if shares == 0)
        question        str     most recent question text for this market
    """
    state: dict = {}
    for tx in transactions:
        key = (tx["condition_id"], tx["outcome"])
        s = state.setdefault(key, {
            "shares": 0.0,
            "cost": 0.0,
            "realized_pnl": 0.0,
            "question": tx.get("question", ""),
            "first_tx_at": tx.get("timestamp"),
            "last_tx_at": tx.get("timestamp"),
        })
        shares = float(tx["shares"])
        price = float(tx["price"])
        s["question"] = tx.get("question", s["question"])
        s["last_tx_at"] = tx.get("timestamp", s["last_tx_at"])

        if tx["type"] == "buy":
            s["shares"] += shares
            s["cost"] += shares * price
        elif tx["type"] == "sell":
            if s["shares"] <= 0:
                # Bad data — sell with nothing held. Skip rather than crash.
                continue
            avg_price = s["cost"] / s["shares"]
            sell_qty = min(shares, s["shares"])
            s["realized_pnl"] += sell_qty * (price - avg_price)
            # Reduce cost proportionally (average-cost method)
            remaining = max(0.0, s["shares"] - sell_qty)
            s["cost"] = s["cost"] * (remaining / s["shares"]) if s["shares"] > 0 else 0
            s["shares"] = remaining

    # Derive avg_buy_price
    for s in state.values():
        s["avg_buy_price"] = (s["cost"] / s["shares"]) if s["shares"] > 0 else 0.0
    return state


def get_positions() -> pd.DataFrame:
    """Derived open positions from the transaction log (average-cost method).

    Returns one row per (condition_id, outcome) with shares > 0. Columns:
        question, condition_id, outcome, shares, avg_price, cost_basis, added_at
    """
    data = _load()
    txns = data.get("transactions", [])
    cols = ["question", "condition_id", "outcome", "shares",
            "avg_price", "cost_basis", "added_at"]
    if not txns:
        return pd.DataFrame(columns=cols)

    # Sort chronologically before replaying
    sorted_txns = sorted(
        txns, key=lambda t: t.get("timestamp") or ""
    )
    state = _replay(sorted_txns)

    rows = []
    for (cid, outcome), s in state.items():
        rounded_shares = round(s["shares"], 6)
        if rounded_shares <= 0:
            continue
        rows.append({
            "question": s["question"],
            "condition_id": cid,
            "outcome": outcome,
            "shares": rounded_shares,
            "avg_price": round(s["avg_buy_price"], 6),
            "cost_basis": round(s["cost"], 4),
            "added_at": s["first_tx_at"],
        })
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)


def get_realized_pnl() -> pd.DataFrame:
    """Realized profit/loss per (condition_id, outcome), summed over all sells.

    Columns: question, condition_id, outcome, realized_pnl
    """
    data = _load()
    txns = data.get("transactions", [])
    if not txns:
        return pd.DataFrame(columns=["question", "condition_id", "outcome", "realized_pnl"])

    sorted_txns = sorted(txns, key=lambda t: t.get("timestamp") or "")
    state = _replay(sorted_txns)

    rows = []
    for (cid, outcome), s in state.items():
        if abs(s["realized_pnl"]) < 1e-9:
            continue
        rows.append({
            "question": s["question"],
            "condition_id": cid,
            "outcome": outcome,
            "realized_pnl": round(s["realized_pnl"], 4),
        })
    if not rows:
        return pd.DataFrame(columns=["question", "condition_id", "outcome", "realized_pnl"])
    return pd.DataFrame(rows).sort_values("realized_pnl", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Live enrichment
# ---------------------------------------------------------------------------

def enrich_with_current_prices(df: pd.DataFrame, df_markets: pd.DataFrame) -> pd.DataFrame:
    """Add current_price, current_value, unrealized_pnl, pnl_pct columns.

    Joins positions against live market data. If a market is not found in
    df_markets, current_price stays NaN and the P&L columns fall back to
    cost basis (so the row visibly shows 0 P&L rather than masking the absence).
    """
    if df.empty or df_markets.empty:
        return df

    # Build (condition_id, outcome) -> price map. outcomes/outcome_prices
    # are native lists after get_markets() parsing.
    price_map: dict[tuple[str, str], float] = {}
    for _, m in df_markets.iterrows():
        outcomes = m.get("outcomes") or []
        prices = m.get("outcome_prices") or []
        cid = m.get("condition_id", "")
        if not isinstance(outcomes, list) or not isinstance(prices, list):
            continue
        for o, p in zip(outcomes, prices, strict=False):
            if p is None:
                continue
            try:
                price_map[(cid, str(o))] = float(p)
            except (TypeError, ValueError):
                pass

    df = df.copy()
    df["current_price"] = df.apply(
        lambda r: price_map.get((r["condition_id"], str(r["outcome"]))), axis=1
    )
    effective_price = df["current_price"].fillna(df["avg_price"])
    df["current_value"] = df["shares"] * effective_price
    df["unrealized_pnl"] = df["current_value"] - df["cost_basis"]
    df["pnl_pct"] = (df["unrealized_pnl"] / df["cost_basis"].replace(0, float("nan"))) * 100
    return df


# ---------------------------------------------------------------------------
# Reconciliation (paper portfolio vs authenticated venue state)
# ---------------------------------------------------------------------------
# Phase-2 helper for the Kalshi authenticated layer: takes the paper
# positions DataFrame (from get_positions) and the real positions
# DataFrame (from kalshi_client.get_authed_positions) and returns a
# drift report keyed by ticker.
#
# Position semantics
# ------------------
# Kalshi reports a single signed integer per ticker: positive = net
# YES contracts long, negative = net NO contracts long. Paper trading
# tracks one row per (condition_id, outcome) so a user can hold both
# sides simultaneously. To compare honestly we net the paper side per
# ticker — the same way Kalshi does — and surface any user who held
# both YES and NO as a "netted" entry in the by-side breakdown.

def reconcile_positions(df_paper: pd.DataFrame,
                         df_real: pd.DataFrame) -> pd.DataFrame:
    """Side-by-side paper-vs-real position report, netted per ticker.

    `df_paper` is the output of `get_positions()`. `df_real` is the
    output of `kalshi_client.get_authed_positions()`. Returns a
    DataFrame with one row per distinct ticker, columns:

        ticker, question, paper_net, real_net, drift,
        paper_yes, paper_no, status

    `paper_net` = paper YES shares − paper NO shares. `drift` =
    real_net − paper_net (positive means the venue holds more YES /
    less NO than the log says). `status` ∈ {"matched", "paper_only",
    "real_only", "mismatch"}.
    """
    cols = ["ticker", "question", "paper_net", "real_net", "drift",
            "paper_yes", "paper_no", "status"]

    # Net paper rows by condition_id. YES/NO matching is case-
    # insensitive substring — Kalshi outcomes are "Yes"/"No" but
    # different markets have used variations in the past, so anchor
    # on the leading letter.
    paper_by_ticker: dict[str, dict] = {}
    if df_paper is not None and not df_paper.empty:
        for _, r in df_paper.iterrows():
            cid = str(r.get("condition_id", "") or "")
            if not cid:
                continue
            shares = float(r.get("shares", 0) or 0)
            outcome = str(r.get("outcome", "") or "").strip().lower()
            entry = paper_by_ticker.setdefault(cid, {
                "question": r.get("question", ""),
                "yes": 0.0, "no": 0.0,
            })
            if outcome.startswith("y"):
                entry["yes"] += shares
            elif outcome.startswith("n"):
                entry["no"] += shares
            # Multi-outcome (e.g. candidate-name) markets fall through
            # silently; reconciliation is binary-only for Phase 2.

    real_by_ticker: dict[str, int] = {}
    if df_real is not None and not df_real.empty:
        for _, r in df_real.iterrows():
            t = str(r.get("ticker", "") or "")
            if not t:
                continue
            real_by_ticker[t] = int(r.get("position", 0) or 0)

    rows: list[dict] = []
    for ticker in sorted(set(paper_by_ticker) | set(real_by_ticker)):
        paper_entry = paper_by_ticker.get(ticker)
        paper_yes = paper_entry["yes"] if paper_entry else 0.0
        paper_no = paper_entry["no"] if paper_entry else 0.0
        paper_net = paper_yes - paper_no
        real_net = real_by_ticker.get(ticker, 0)
        drift = real_net - paper_net
        if paper_entry is None:
            status = "real_only"
        elif ticker not in real_by_ticker:
            status = "paper_only"
        elif abs(drift) < 1e-6:
            status = "matched"
        else:
            status = "mismatch"
        rows.append({
            "ticker": ticker,
            "question": paper_entry["question"] if paper_entry else "",
            "paper_net": round(paper_net, 6),
            "real_net": real_net,
            "drift": round(drift, 6),
            "paper_yes": round(paper_yes, 6),
            "paper_no": round(paper_no, 6),
            "status": status,
        })

    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Portfolio value snapshots
# ---------------------------------------------------------------------------
#
# A snapshot is a periodic point-in-time record of total portfolio value.
# Snapshots are appended automatically when the Portfolio page loads
# (debounced via `maybe_snapshot`) so the history builds itself.
#
# Snapshot schema (stored under the "snapshots" key in portfolio.json):
#     {
#         "timestamp": "2026-05-17T20:00:00+00:00",
#         "cost_basis": 1500.00,        # cost basis of open positions
#         "current_value": 1820.00,     # mark-to-market value of open positions
#         "unrealized_pnl": 320.00,     # current_value - cost_basis
#         "realized_pnl": 142.50,       # cumulative realized P&L
#         "open_positions": 7,
#     }


def add_snapshot(
    cost_basis: float,
    current_value: float,
    unrealized_pnl: float,
    realized_pnl: float,
    open_positions: int = 0,
) -> dict:
    """Append a portfolio value snapshot. Returns the snapshot dict."""
    snap = {
        "timestamp": datetime.now(UTC).isoformat(),
        "cost_basis": float(cost_basis),
        "current_value": float(current_value),
        "unrealized_pnl": float(unrealized_pnl),
        "realized_pnl": float(realized_pnl),
        "open_positions": int(open_positions),
    }
    with _mutate() as data:
        data.setdefault("snapshots", []).append(snap)
    return snap


def get_snapshots() -> pd.DataFrame:
    """All snapshots as a DataFrame, oldest-first."""
    data = _load()
    snaps = data.get("snapshots", [])
    cols = ["timestamp", "cost_basis", "current_value",
            "unrealized_pnl", "realized_pnl", "open_positions"]
    if not snaps:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(snaps)
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["net_value"] = df["current_value"] + df["realized_pnl"]
    df["total_pnl"] = df["unrealized_pnl"] + df["realized_pnl"]
    return df.sort_values("timestamp_dt").reset_index(drop=True)


def maybe_snapshot(
    cost_basis: float,
    current_value: float,
    unrealized_pnl: float,
    realized_pnl: float,
    open_positions: int = 0,
    min_interval_hours: float = 1.0,
) -> bool:
    """Record a snapshot only if the most recent one is older than
    `min_interval_hours`. Returns True if a snapshot was recorded.

    Skips when there's nothing to track (zero cost basis, zero positions,
    and no realized P&L) — we don't want a long flatline before the first
    trade.
    """
    if cost_basis == 0 and realized_pnl == 0 and open_positions == 0:
        return False

    data = _load()
    snaps = data.get("snapshots", [])
    if snaps:
        last_ts = pd.to_datetime(snaps[-1]["timestamp"], utc=True, errors="coerce")
        if pd.notna(last_ts):
            now = pd.Timestamp.now(tz="UTC")
            if (now - last_ts).total_seconds() < min_interval_hours * 3600:
                return False

    add_snapshot(cost_basis, current_value, unrealized_pnl, realized_pnl, open_positions)
    return True
