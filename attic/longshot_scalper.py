"""Longshot scalper — live in-game baseball trading.

The thesis (UNTESTED): a baseball underdog priced ``$0.10–$0.35`` mid-game
is volatile. One swing — a run, runners on, a lead change — repriced the
contract sharply. We don't need the underdog to *win* (that's the
negative-EV lottery the pre-event scanner already tests). We buy the cheap
side and **sell into the first meaningful pop** (+25% net), banking the
bounce rather than holding for the unlikely resolution. A high per-trade
hit rate on small gains, not a few 5× lottery wins.

Two managed exits, both keyed on **net** P&L (after Kalshi entry+exit fees,
via ``kalshi_fees``):

* **Take-profit** (+``take_profit_pct``): sell into the pop. The point of
  the strategy.
* **Flip / stop-and-reverse** (−``flip_drop_pct``): the underdog is fading,
  so sell it and buy the **opponent** (the favorite that's pulling away)
  with the proceeds — ride the momentum we bet against. One flip per
  position (``from_flip`` guard in position_meta) so we don't thrash; a
  flipped position then only takes profit or rides to resolution.

Positions still open when the game goes Final are closed at $1/$0 via the
baseball sidecar's instant resolution (``baseball.resolve_pending_tickers``).

Why a dedicated daemon and not ``strategy_runner``: the 30-min cron is far
too slow — these pops last seconds. This loops every ``--interval`` seconds
(default 15, matching the WS book-flush cadence) and prices exits off the
live ``book_archive`` bid/ask the Kalshi WebSocket keeps fresh. Paper only:
all writes land in the ``agentic_longshot_scalp`` profile's portfolio.

Scoping note (see docs/failed-experiments.md): scalp/flip logic only makes
sense on FAST, mean-reverting prices. The same idea on slow-resolution
markets repeats the disabled-stop-loss mistake — keep this baseball-only.

Two entry modes (``--mode``), sharing the same exit/cash/resolution loop:

* ``longshot`` (default): buy a cheap underdog ($0.10–$0.35) in innings 4–7.
* ``close_game``: buy the **underpriced side of a coin-flip game** — when a
  model forecast puts the game at ~50% (``cg_min_prob..cg_max_prob``) and the
  market prices a side ≥ ``cg_min_edge`` BELOW that forecast ("trading low"),
  buy it early (innings 1–``cg_max_entry_inning``) and ride the expected
  in-game pop. Forecast source is pluggable (``--forecast-source``):
  ``baseball`` (Pythagorean, instant) now, ``deep`` (gemma3 deep-analysis log)
  once an MLB primer populates it. Flip is off by default in this mode.
* ``favorite``: buy the **moderate-favorite side** (forecast in
  ``fav_min_prob..fav_max_prob``, default 0.55–0.70) early and **HOLD to
  resolution** — no scalp, no flip. Counterfactual analysis over the
  gathered scalper data found this the only positive-EV thread: the market
  underprices favorites (sides forecast 0.55–0.70 won ~80%), and any scalp
  exit destroyed the edge. A coarse directional value bet, not a scalp.

Run each mode as its own daemon/profile for clean P&L attribution
(``agentic_longshot_scalp`` vs e.g. ``agentic_closegame_scalp``).

Usage::

    python longshot_scalper.py --loop --interval 15                  # longshot
    python longshot_scalper.py --mode close_game \\
        --profile agentic_closegame_scalp --loop --interval 15       # close-game
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import book_archive
import emergency_stop
import execution
import position_meta as pm
import profiles

# Reuse the strategy_runner internals that are the single source of truth
# for fee-aware net P&L and the exits.jsonl schema, plus the profile-env
# pinning that makes concurrent runners write to the right files.
from strategy_runner import (
    _log_exit,
    _net_pnl_pct,
    _pin_profile_env,
    _restore_profile_env,
)

log = logging.getLogger("longshot_scalper")

SCALP_LOG_PATH = Path("data/shared/longshot_scalp_log.jsonl")
STRATEGY_KEY = "longshot_scalp"


@dataclass
class ScalpConfig:
    """Tunable knobs. CLI flags override; defaults match the agreed v1."""

    profile_name: str = "agentic_longshot_scalp"
    bankroll: float = 1000.0
    mode: str = "longshot"             # "longshot" | "close_game"
    take_profit_pct: float = 0.25      # net gain → sell into the pop
    flip_drop_pct: float | None = 0.15  # net loss → flip to opponent; None disables
    max_position_pct: float = 0.05     # cap per position
    max_quote_age_s: float = 120.0     # skip stale books — can't scalp blind
    min_stake: float = 0.50
    freeroll_profit: bool = False      # on a TP, recycle realized profit into
    #                                    the opposite longshot (house money)
    # ── longshot-mode entry ───────────────────────────────────────────────
    min_price: float = 0.10            # entry band floor (on the ask)
    max_price: float = 0.35            # entry band ceiling
    min_inning: int = 4                # don't enter before the market is liquid
    max_entry_inning: int = 7          # don't open longshots with no time to pop
    # ── close-game-mode entry ─────────────────────────────────────────────
    # Buy a coin-flip game's side when the market prices it BELOW a model
    # forecast (it's "trading low"), early, and ride the expected in-game pop.
    forecast_source: str = "baseball"  # "baseball" (Pythagorean) | "deep" (gemma3 log) | "consensus" (ESPN de-vig)
    cg_min_prob: float = 0.40          # game is a toss-up: forecast ∈ [min,max]
    cg_max_prob: float = 0.60
    cg_min_edge: float = 0.05          # market ask must be ≥ this below forecast
    cg_min_ask: float = 0.25           # price floor — a real coin-flip side
    #                                    trades near $0.50; a side priced far
    #                                    below this isn't a toss-up no matter
    #                                    what the model says (the market is
    #                                    telling you it's a longshot). Excludes
    #                                    the deep-underdog entries that rode to
    #                                    $0 (e.g. KC @ $0.07 forecast 0.45).
    cg_max_entry_inning: int = 3       # "buy early" — innings 1..this
    cg_stop_loss_pct: float | None = None  # net loss → cut the position to
    #                                    cash (NO flip). Off by default; the
    #                                    A/B arm runs it at 0.25. Book-archive
    #                                    replay of the deep close-game arm
    #                                    showed close-game losers bleed through
    #                                    sellable bids (min bid $0.01–0.14) on
    #                                    the way to $0, so a −25% stop turned a
    #                                    −$— hold into +$— with ZERO
    #                                    false-stops on winners. See
    #                                    docs/close-game-stop-loss.md.
    # ── favorite-mode entry (mode="favorite") ─────────────────────────────
    # Counterfactual analysis over ~45 games found the only positive-EV
    # thread: buy the MODERATE-FAVORITE side (forecast ∈ [fav_min,fav_max])
    # and HOLD to resolution — no scalp, no flip. Calibration showed sides
    # forecast 0.55–0.70 actually won ~80% (the market underprices
    # favorites). Requiring an entry "edge" only weakened it, so the gate is
    # simply "forecast in band" — buy at ask, hold. See scratch_favorites.py.
    fav_min_prob: float = 0.55         # favorite band floor (forecast)
    fav_max_prob: float = 0.70         # favorite band ceiling
    fav_max_entry_inning: int = 3      # buy early, same as close_game


@dataclass
class ScalpStats:
    cycle_started: datetime
    games_live: int = 0
    positions_open: int = 0
    opened: int = 0
    take_profits: int = 0
    stop_losses: int = 0
    flips: int = 0
    freerolls: int = 0
    resolved_closed: int = 0
    errors: int = 0
    exit_reasons: dict[str, int] = field(default_factory=dict)


# ─── Pure decision logic (no IO — unit-tested directly) ───────────────────

def best_bid_ask(book: pd.DataFrame | None) -> tuple[float | None, float | None]:
    """Best bid (highest bid price) and best ask (lowest ask price) from a
    ``parse_order_book``-shaped frame. None per side when that side is empty."""
    if book is None or book.empty:
        return None, None
    bids = book[book["side"] == "bid"]["price"]
    asks = book[book["side"] == "ask"]["price"]
    bid = float(bids.max()) if not bids.empty else None
    ask = float(asks.min()) if not asks.empty else None
    return bid, ask


def team_from_ticker(ticker: str) -> str:
    """Team abbreviation = the ticker's suffix after the last hyphen.

    >>> team_from_ticker("KXMLBGAME-26MAY281810NYMMIA-NYM")
    'NYM'
    """
    return ticker.rsplit("-", 1)[-1] if "-" in ticker else ticker


def select_longshot(
    quotes: dict[str, tuple[float | None, float | None]],
    *,
    min_price: float,
    max_price: float,
) -> str | None:
    """Pick the entry ticker from a game's two sides.

    Candidate = a ticker whose **ask** (what we'd pay to enter) is inside
    ``[min_price, max_price]``. When both sides qualify (a near-pick'em
    game), take the cheaper one — the bigger longshot has more room to pop.
    Returns None when neither side is a tradeable longshot right now.
    """
    best_ticker: str | None = None
    best_ask = float("inf")
    for ticker, (_bid, ask) in quotes.items():
        if ask is None or ask <= 0:
            continue
        if min_price <= ask <= max_price and ask < best_ask:
            best_ticker, best_ask = ticker, ask
    return best_ticker


def select_close_game_side(
    quotes: dict[str, tuple[float | None, float | None]],
    forecasts: dict[str, float | None],
    *,
    min_prob: float,
    max_prob: float,
    min_edge: float,
    min_ask: float = 0.0,
) -> str | None:
    """Close-game-mode entry: pick the **underpriced side of a coin-flip
    game**.

    A side qualifies when (1) its model forecast ``yes_prob`` is in
    ``[min_prob, max_prob]`` — i.e. the game is a genuine toss-up — AND
    (2) its **ask** is at least ``min_edge`` below that forecast (the market
    is "trading it low") AND (3) its ask is at least ``min_ask`` — a price
    floor. Among qualifying sides, the one with the largest edge
    (``forecast − ask``) wins. Returns None when no side qualifies.

    The forecast band gate naturally excludes lopsided games: if home is
    forecast at 0.62, away is 0.38 — neither sits in [0.40, 0.60], so the
    game is skipped.

    The ``min_ask`` floor guards against the model and market
    *disagreeing hard*: a genuine coin-flip side trades near $0.50, so a
    side priced far below the floor (e.g. $0.07) is one the market is
    pricing as a longshot regardless of the model's toss-up call — exactly
    the deep-underdog entries that rode to $0. The floor trusts the market
    over the model when they diverge that much.
    """
    best_ticker: str | None = None
    best_edge = -1.0
    for ticker, (_bid, ask) in quotes.items():
        fp = forecasts.get(ticker)
        if fp is None or ask is None or ask <= 0:
            continue
        if ask < min_ask:
            continue
        if not (min_prob <= fp <= max_prob):
            continue
        edge = fp - ask
        if edge >= min_edge and edge > best_edge:
            best_ticker, best_edge = ticker, edge
    return best_ticker


def select_favorite_side(
    quotes: dict[str, tuple[float | None, float | None]],
    forecasts: dict[str, float | None],
    *,
    min_prob: float,
    max_prob: float,
) -> str | None:
    """Favorite-mode entry: buy the **moderate-favorite** side — the one
    whose model forecast sits in ``[min_prob, max_prob]`` (e.g. 0.55–0.70).

    No edge/underpricing requirement: counterfactual analysis found that
    adding one only weakened results. The thesis is coarse — the market
    underprices favorites generically — so we just take the favorite at the
    ask and hold to resolution. When both sides somehow fall in band (only
    possible with a wide band), take the stronger favorite (higher
    forecast). Returns None when neither side qualifies.
    """
    best_ticker: str | None = None
    best_fc = -1.0
    for ticker, (_bid, ask) in quotes.items():
        fp = forecasts.get(ticker)
        if fp is None or ask is None or ask <= 0:
            continue
        if not (min_prob <= fp <= max_prob):
            continue
        if fp > best_fc:
            best_ticker, best_fc = ticker, fp
    return best_ticker


def evaluate_exit(
    *, entry: dict, side_price: float, cfg: ScalpConfig, ticker: str,
) -> str | None:
    """Return ``"scalp_take_profit"``, ``"stop_loss"``, ``"flip"``, or None
    for one open position, on **net** P&L. Precedence (downside conflicts
    can't co-occur on the same side): take-profit → stop-loss → flip.

    ``stop_loss`` cuts a fading position to cash (no re-entry) — the A/B
    arm's downside protection, distinct from the flip (which sells AND
    re-buys the opponent). A stop is checked before the flip so an arm
    configured with both would cut rather than reverse; in practice the
    close-game stop arm runs with the flip disabled.

    Positions that are themselves a reversal (``from_flip``) or a house-
    money recycle (``from_freeroll``) never flip — flipping either would be
    circular (back into the side we just left). They only take profit, stop
    out, or ride to resolution.

    favorite mode holds to resolution unconditionally — no scalp, no stop,
    no flip — so this returns None regardless of price."""
    if cfg.mode == "favorite":
        return None
    entry_price = float(entry.get("entry_price", 0.0) or 0.0)
    if entry_price <= 0:
        return None
    net = _net_pnl_pct(entry_price, side_price, ticker=ticker)
    if net >= cfg.take_profit_pct:
        return "scalp_take_profit"
    if (cfg.cg_stop_loss_pct is not None
            and net <= -abs(cfg.cg_stop_loss_pct)):
        return "stop_loss"
    can_flip = not entry.get("from_flip") and not entry.get("from_freeroll")
    if (cfg.flip_drop_pct is not None
            and net <= -abs(cfg.flip_drop_pct) and can_flip):
        return "flip"
    return None


# ─── Live-data IO ─────────────────────────────────────────────────────────

def _ticker_quote(
    ticker: str, *, now: datetime, max_age_s: float,
) -> tuple[float | None, float | None]:
    """Fresh (bid, ask) for a ticker from book_archive, or (None, None)
    when there's no snapshot or the newest one is staler than
    ``max_age_s`` — we won't scalp on a stale book."""
    snap = book_archive.get_snapshot_at(
        venue="kalshi", market_id=ticker, token_id=ticker, ts=now,
    )
    if snap is None:
        return None, None
    if now.timestamp() - snap.snapshot_ts > max_age_s:
        return None, None
    return best_bid_ask(snap.book)


def _live_games() -> list[Any]:
    """Current in-progress game states. Reuses pre_position's extractor so
    the two live-baseball daemons agree on what 'live' means."""
    from pre_position import _current_game_states
    return _current_game_states()


def _build_ticker_maps(
    states: list[Any], *, now: datetime, max_age_s: float,
) -> tuple[dict[str, str], dict[str, Any], dict[str, tuple[float | None, float | None]]]:
    """For every live game, map each side ticker → opponent ticker, ticker
    → its game state, and ticker → current quote. Games without exactly two
    discoverable tickers are skipped for opponent pairing (no flip target)."""
    from baseball import analysis
    opp_of: dict[str, str] = {}
    game_of: dict[str, Any] = {}
    quotes: dict[str, tuple[float | None, float | None]] = {}
    for st in states:
        tickers = analysis.tickers_for_game(st.game_pk)
        for t in tickers:
            game_of[t] = st
            quotes[t] = _ticker_quote(t, now=now, max_age_s=max_age_s)
        if len(tickers) == 2:
            opp_of[tickers[0]] = tickers[1]
            opp_of[tickers[1]] = tickers[0]
    return opp_of, game_of, quotes


def _question_for(ticker: str, state: Any) -> str:
    team = team_from_ticker(ticker)
    away = getattr(state, "away_abbr", "?")
    home = getattr(state, "home_abbr", "?")
    return f"{team} wins ({away}@{home})"


def _latest_deep_forecast(ticker: str, *, model: str = "gemma3:27b") -> float | None:
    """Most recent ``llm_deep:<model>`` forecast for ``ticker`` from the
    ``agentic_deep`` profile's forecast log (where bulk_deep_analysis / a
    future MLB primer write deep forecasts). Returns the YES probability,
    or None if no such forecast exists yet.

    Read straight off disk (no profile switch) so it's safe to call from
    the scalper's pinned-profile cycle. Returns None until a primer
    populates MLB deep forecasts — making the ``deep`` source a drop-in
    upgrade over ``baseball`` without any other code change."""
    path = profiles.profile_dir("agentic_deep") / "forecasts.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    rows = data if isinstance(data, list) else data.get("forecasts", [])
    want = f"llm_deep:{model}"
    best_ts = ""
    best_p: float | None = None
    for r in rows:
        if str(r.get("condition_id")) != ticker or r.get("source") != want:
            continue
        ts = str(r.get("timestamp", ""))
        if ts >= best_ts and r.get("your_probability") is not None:
            best_ts, best_p = ts, float(r["your_probability"])
    return best_p


def _forecast_for_ticker(ticker: str, source: str) -> float | None:
    """Pluggable P(YES) for a game ticker. ``"baseball"`` → the Pythagorean
    model (instant, every game); ``"deep"`` → the latest gemma3 deep
    forecast from the log (None until primed); ``"consensus"`` → ESPN
    de-vigged sportsbook consensus (the sharpest cheap baseline — see
    docs/prior-art-survey.md #1; None when no live line). None means "no
    forecast — skip this game"."""
    if source == "baseball":
        try:
            import baseball
            r = baseball.predict_for_ticker(ticker)
            return float(r["yes_prob"]) if r else None
        except Exception:  # noqa: BLE001
            return None
    if source == "deep":
        return _latest_deep_forecast(ticker)
    if source == "consensus":
        try:
            import edges
            return edges.sports_consensus_for_ticker(ticker)
        except Exception:  # noqa: BLE001
            return None
    return None


def _log_event(kind: str, **fields: Any) -> None:
    """Append one row to the scalper's own activity log (buys, flips). Sells
    also go to exits.jsonl via _log_exit for the shared analytics pipeline."""
    SCALP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **fields}
    with open(SCALP_LOG_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


# ─── Re-entry-after-stop guard ────────────────────────────────────────────
# Once we stop out of a game, do NOT re-enter that same game. Without this the
# daemon re-buys the dip every cycle and a single choppy game gets whipsawed
# into multiple stop-losses — observed live on CWS@MIN (4 stop-outs on a game
# that ultimately WON). The in-sample replay never caught this because it
# modeled one entry per game. Keyed by game_pk, persisted per-profile so a
# daemon restart mid-game doesn't reopen the whipsaw. Entries auto-expire
# after 24h so the file stays small (a game's guard only needs to outlast the
# game).

_STOP_GUARD_TTL_S = 24 * 3600


def _stopped_games_path():
    return profiles.profile_dir(profiles.active_profile()) / "stopped_games.json"


def _load_stopped() -> dict[str, float]:
    p = _stopped_games_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _mark_stopped(game_pk: Any) -> None:
    if game_pk is None:
        return
    now = datetime.now(UTC).timestamp()
    cutoff = now - _STOP_GUARD_TTL_S
    data = {k: v for k, v in _load_stopped().items() if float(v) >= cutoff}
    data[str(game_pk)] = now
    try:
        p = _stopped_games_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError as e:  # noqa: BLE001
        log.warning("could not persist stop guard for %s: %s", game_pk, e)


def _is_stopped(game_pk: Any) -> bool:
    if game_pk is None:
        return False
    v = _load_stopped().get(str(game_pk))
    if v is None:
        return False
    return float(v) >= datetime.now(UTC).timestamp() - _STOP_GUARD_TTL_S


# Re-entry guard for RESOLVED games. The entry gate (`_current_game_states`)
# excludes games whose `games.status` is 'Final', but that field can lag or
# never sync, while `_close_resolved` resolves off the known final SCORE — so a
# finished game can be closeable yet still appear "live", and the daemon loops:
# enter -> close at $0 -> enter -> close ... (163 dup resolution exits on one
# game, 2026-06-17). Once we close a ticker on resolution we mark it (and the
# entry loop skips it + its opponent), unifying the two Final signals. Keyed by
# ticker because `_close_resolved` works from positions, not game_pk.
def _resolved_tickers_path():
    return profiles.profile_dir(profiles.active_profile()) / "resolved_tickers.json"


def _load_resolved() -> dict[str, float]:
    p = _resolved_tickers_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _mark_resolved(ticker: str) -> None:
    if not ticker:
        return
    now = datetime.now(UTC).timestamp()
    cutoff = now - _STOP_GUARD_TTL_S
    data = {k: v for k, v in _load_resolved().items() if float(v) >= cutoff}
    data[str(ticker)] = now
    try:
        p = _resolved_tickers_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError as e:  # noqa: BLE001
        log.warning("could not persist resolved guard for %s: %s", ticker, e)


def _is_resolved(ticker: str | None) -> bool:
    if not ticker:
        return False
    v = _load_resolved().get(str(ticker))
    if v is None:
        return False
    return float(v) >= datetime.now(UTC).timestamp() - _STOP_GUARD_TTL_S


# ─── Resting cash-out limit (live take-profit) ────────────────────────────
# In live mode the broker can rest a take-profit limit on the venue, so the
# +25% pop is captured instantly instead of polled (sub-cycle latency, and it
# survives a daemon crash). The downside stop CANNOT rest (Kalshi has no stop
# order) so it stays the polled marketable sell. Coordination so the two
# exits never collide:
#   * on entry → arm the resting TP, store its order_id in position_meta
#   * upside pop → the venue fills the resting TP; _reconcile_resting_tps
#     detects the fill and logs the exit (the daemon's polled TP is skipped
#     in live so it can't double-sell)
#   * downside stop / flip / resolution → cancel the resting TP first, then
#     act, so it can't fill after we've left
# All of this is gated on broker.rests_take_profit → inert in paper (polled
# TP unchanged) and in favorite mode (holds to resolution, no TP).


def _arm_resting_tp(broker: execution.Broker, cfg: ScalpConfig, *,
                    ticker: str, outcome: str, entry_price: float,
                    count: float, question: str) -> None:
    if not getattr(broker, "rests_take_profit", False):
        return
    if cfg.mode == "favorite" or cfg.take_profit_pct <= 0 or count <= 0:
        return
    tp_price = entry_price * (1 + cfg.take_profit_pct)
    res = broker.place_resting_take_profit(
        ticker=ticker, outcome=outcome, count=count, tp_price=tp_price)
    if res.order_id:
        pm.set_field(ticker, outcome, "tp_order_id", res.order_id)
        pm.set_field(ticker, outcome, "question", question)
        _log_event("resting_tp", ticker=ticker, tp_price=round(tp_price, 3),
                   order_id=res.order_id, count=count)


def _cancel_resting_tp(broker: execution.Broker, cid: str, outcome: str) -> None:
    """Cancel a position's resting cash-out limit before we close it another
    way (stop / flip / resolution), so it can't fill afterward."""
    if not getattr(broker, "rests_take_profit", False):
        return
    tp_oid = (pm.get_entry(cid, outcome) or {}).get("tp_order_id")
    if tp_oid:
        broker.cancel(tp_oid)


def _reconcile_resting_tps(*, profile_name: str, broker: execution.Broker,
                           stats: ScalpStats) -> None:
    """Live only: detect resting cash-out limits that filled and record them
    as take-profit exits (the venue already closed the position)."""
    if not getattr(broker, "rests_take_profit", False):
        return
    for key, entry in pm.all_entries().items():
        tp_oid = entry.get("tp_order_id")
        if not tp_oid:
            continue
        cid, _, outcome = key.partition("|")
        filled, avg = broker.fills_for_order(ticker=cid, order_id=tp_oid)
        if filled <= 0:
            continue
        exit_price = avg if avg is not None else float(entry.get("entry_price", 0))
        try:
            _log_exit(
                profile_name=profile_name, condition_id=cid,
                question=str(entry.get("question", cid)), outcome=outcome,
                shares=filled, entry_price=float(entry["entry_price"]),
                exit_price=exit_price,
                peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                opened_at=entry.get("opened_at"),
                exit_reason="scalp_take_profit", strategy_key=STRATEGY_KEY,
            )
            pm.clear_entry(cid, outcome)
            stats.take_profits += 1
            stats.exit_reasons["scalp_take_profit"] = \
                stats.exit_reasons.get("scalp_take_profit", 0) + 1
            log.info("CASH-OUT LIMIT FILLED %s exit=%.3f (resting TP)",
                     cid, exit_price)
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile resting TP failed for %s: %s", cid, e)
            stats.errors += 1


def _reconcile_settlements(*, profile_name: str, broker: execution.Broker,
                           stats: ScalpStats) -> None:
    """Live safety net: log positions that SETTLED on the venue before the
    in-cycle resolver (``_close_resolved``) caught them.

    Kalshi can settle a position seconds after the game ends — faster than
    our cycle — which leaves it orphaned in ``position_meta`` and ABSENT from
    the exits.jsonl track record. That silently drops settled bets (a losing
    one biases the recorded record optimistically). This reads Kalshi
    settlements (ground truth) and logs/clears any still-tracked position
    that has resolved. Live-only; paper closes via ``_close_resolved``.

    No double-logging: ``_close_resolved`` clears meta when it logs, so an
    already-recorded position isn't in ``all_entries()`` here."""
    if not getattr(broker, "is_live", False):
        return
    entries = pm.all_entries()
    if not entries:
        return
    try:
        import kalshi_client as kc
        settled = kc.get_settlements(limit=200)
    except Exception as e:  # noqa: BLE001
        log.warning("settlement reconcile: get_settlements failed: %s", e)
        return
    if settled is None or settled.empty:
        return
    by_ticker: dict[str, Any] = {}
    for _, s in settled.iterrows():
        by_ticker.setdefault(str(s.get("ticker", "")), s)
    for key, entry in entries.items():
        cid, _, outcome = key.partition("|")
        s = by_ticker.get(cid)
        if s is None:
            continue  # not settled yet — leave it tracked
        revenue = float(s.get("revenue_cents", 0) or 0)
        won = revenue > 0  # a held-to-resolution YES pays out iff it won
        shares = float((s.get("yes_count") if outcome == "Yes"
                        else s.get("no_count")) or 0)
        if shares <= 0:
            shares = float(s.get("yes_count") or s.get("no_count") or 0)
        try:
            _cancel_resting_tp(broker, cid, outcome)  # void any leftover TP
            _log_exit(
                profile_name=profile_name, condition_id=cid,
                question=str(entry.get("question", cid)), outcome=outcome,
                shares=shares, entry_price=float(entry["entry_price"]),
                exit_price=1.0 if won else 0.0,
                peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                opened_at=entry.get("opened_at"),
                exit_reason="resolution_won" if won else "resolution_lost",
                strategy_key=STRATEGY_KEY,
            )
            pm.clear_entry(cid, outcome)
            stats.resolved_closed += 1
            log.info("SETTLEMENT RECONCILED %s %s (revenue=$%.2f, %g contracts)",
                     cid, "WON" if won else "lost", revenue / 100, shares)
        except Exception as e:  # noqa: BLE001
            log.warning("settlement reconcile failed for %s: %s", cid, e)
            stats.errors += 1


# ─── Cycle phases ─────────────────────────────────────────────────────────

def _close_resolved(*, profile_name: str, broker: execution.Broker,
                    stats: ScalpStats) -> None:
    """Close positions whose game is Final (instant resolution via the
    baseball sidecar) at $1/$0, log the exit, clear meta.

    In live mode the venue auto-settles resolved markets, so we place NO
    sell order (``broker.closes_settle_on_venue``) — we only record the
    known outcome to exits.jsonl and clear local meta."""
    df_pos = broker.get_positions()
    if df_pos.empty:
        return
    tickers = list(df_pos["condition_id"].astype(str).unique())
    try:
        import baseball
        resolved = baseball.resolve_pending_tickers(tickers)
    except Exception as e:  # noqa: BLE001
        log.debug("resolve_pending_tickers failed: %s", e)
        return
    if not resolved:
        return
    for _, pos in df_pos.iterrows():
        cid = str(pos["condition_id"])
        if cid not in resolved:
            continue
        won = bool(resolved[cid])  # YES side won == our position won (always YES)
        close_price = 1.0 if won else 0.0
        outcome = str(pos["outcome"])
        try:
            # Cancel any resting cash-out limit so it can't fill after the
            # game resolves (Kalshi voids it on settlement, but the game can
            # be Final via the fast resolver before the venue settles).
            _cancel_resting_tp(broker, cid, outcome)
            if not broker.closes_settle_on_venue:
                broker.sell(question=str(pos["question"]), ticker=cid,
                            outcome=outcome, count=float(pos["shares"]),
                            price=close_price)
            entry = pm.get_entry(cid, outcome) or {
                "entry_price": float(pos.get("avg_price") or close_price),
                "opened_at": pos.get("added_at"),
                "peak_price": float(pos.get("avg_price") or close_price),
            }
            _log_exit(
                profile_name=profile_name, condition_id=cid,
                question=str(pos["question"]), outcome=outcome,
                shares=float(pos["shares"]),
                entry_price=float(entry["entry_price"]), exit_price=close_price,
                peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                opened_at=entry.get("opened_at"),
                exit_reason="resolution_won" if won else "resolution_lost",
                strategy_key=STRATEGY_KEY,
            )
            pm.clear_entry(cid, outcome)
            _mark_resolved(cid)  # don't re-enter this finished game
            stats.resolved_closed += 1
            log.info("closed %s at $%.2f (%s)", cid, close_price,
                     "WON" if won else "lost")
        except Exception as e:  # noqa: BLE001
            log.warning("close failed for %s: %s", cid, e)


def _manage_positions(
    *, profile_name: str, broker: execution.Broker, cfg: ScalpConfig,
    opp_of: dict[str, str],
    game_of: dict[str, Any], quotes: dict[str, tuple[float | None, float | None]],
    remaining_cash: float, stats: ScalpStats,
) -> float:
    """Take-profit + flip every open position. Returns updated
    remaining_cash (a flip's re-buy consumes the sale proceeds; a free-roll
    re-buy consumes only the realized profit)."""
    df_pos = broker.get_positions()
    stats.positions_open = len(df_pos)
    now = datetime.now(UTC)
    for _, pos in df_pos.iterrows():
        cid = str(pos["condition_id"])
        outcome = str(pos["outcome"])
        bid, _ask = quotes.get(cid, (None, None))
        if bid is None:
            continue  # no fresh quote this cycle — manage next time
        side_price = bid  # we sell our YES into the bid
        entry = pm.update_peak(cid, outcome, current_price=side_price, now=now) \
            or pm.get_entry(cid, outcome)
        if entry is None:
            continue
        reason = evaluate_exit(entry=entry, side_price=side_price, cfg=cfg,
                               ticker=cid)
        if reason is None:
            continue
        # Live: the resting cash-out limit owns the upside. Let the venue fill
        # it — don't also poll-sell the take-profit (that would race / double
        # sell). Reconciliation logs the resting-TP fill separately.
        if reason == "scalp_take_profit" and getattr(broker, "rests_take_profit", False):
            continue
        shares = float(pos["shares"])
        try:
            # A stop / flip is closing the position a different way — cancel the
            # resting cash-out limit first so it can't fill after we sell.
            _cancel_resting_tp(broker, cid, outcome)
            # marketable=True: a managed exit (stop / take-profit / flip-out)
            # must actually fill — cross the spread so a moved book doesn't
            # leave the order resting. This is what makes the live stop a stop.
            broker.sell(question=str(pos["question"]), ticker=cid,
                        outcome=outcome, count=shares, price=side_price,
                        current_mid=side_price, marketable=True)
            _log_exit(
                profile_name=profile_name, condition_id=cid,
                question=str(pos["question"]), outcome=outcome, shares=shares,
                entry_price=float(entry["entry_price"]), exit_price=side_price,
                peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                opened_at=entry.get("opened_at"),
                exit_reason="flip_out" if reason == "flip" else reason,
                strategy_key=STRATEGY_KEY,
            )
            pm.clear_entry(cid, outcome)
            proceeds = shares * side_price
            stats.exit_reasons[reason] = stats.exit_reasons.get(reason, 0) + 1
            net = _net_pnl_pct(float(entry["entry_price"]), side_price, ticker=cid)
            if reason == "scalp_take_profit":
                stats.take_profits += 1
                remaining_cash += proceeds
                log.info("TAKE-PROFIT %s entry=%.3f exit=%.3f net=%+.1f%%",
                         cid, float(entry["entry_price"]), side_price, 100 * net)
                # Free-roll: recycle ONLY the realized net profit into the
                # opposite longshot. Principal is back in cash, so the
                # opposite leg is a free option (~break-even worst case).
                # Only base entries free-roll — never a flip/free-roll
                # position (that would chain or circle back).
                is_base = not entry.get("from_flip") and not entry.get("from_freeroll")
                if cfg.freeroll_profit and is_base:
                    cost_basis = shares * float(entry["entry_price"])
                    net_profit = net * cost_basis
                    if net_profit >= cfg.min_stake:
                        new_cash, did = _execute_freeroll(
                            profile_name=profile_name, broker=broker, cfg=cfg,
                            sold_ticker=cid,
                            opp_of=opp_of, game_of=game_of, quotes=quotes,
                            amount=net_profit, remaining_cash=remaining_cash,
                        )
                        remaining_cash = new_cash
                        if did:
                            stats.freerolls += 1
            elif reason == "stop_loss":  # cut the fader to cash — no re-entry
                stats.stop_losses += 1
                remaining_cash += proceeds
                # Block re-entry into this game for the rest of the day so we
                # don't whipsaw it with repeated stop-outs (see _mark_stopped).
                gstate = game_of.get(cid)
                _mark_stopped(getattr(gstate, "game_pk", None))
                log.info("STOP-LOSS %s entry=%.3f exit=%.3f net=%+.1f%%",
                         cid, float(entry["entry_price"]), side_price, 100 * net)
            else:  # flip → buy the opponent (favorite) with the proceeds
                stats.flips += 1
                remaining_cash = _execute_flip(
                    profile_name=profile_name, broker=broker, cfg=cfg,
                    sold_ticker=cid,
                    opp_of=opp_of, game_of=game_of, quotes=quotes,
                    proceeds=proceeds, remaining_cash=remaining_cash, net=net,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("manage-exit failed for %s: %s", cid, e)
            stats.errors += 1
    return remaining_cash


def _execute_flip(
    *, profile_name: str, broker: execution.Broker, cfg: ScalpConfig,
    sold_ticker: str,
    opp_of: dict[str, str], game_of: dict[str, Any],
    quotes: dict[str, tuple[float | None, float | None]],
    proceeds: float, remaining_cash: float, net: float,
) -> float:
    """Buy the opponent ticker with the sale proceeds. Returns updated cash.

    We've already sold ``sold_ticker``; the proceeds are back in cash. If we
    can't find/price the opponent, the proceeds just stay in cash (no
    re-entry) — better than buying blind."""
    opp = opp_of.get(sold_ticker)
    if not opp:
        log.info("flip %s: no opponent ticker — staying in cash", sold_ticker)
        return remaining_cash + proceeds
    _bid, opp_ask = quotes.get(opp, (None, None))
    if opp_ask is None or opp_ask <= 0:
        log.info("flip %s → %s: no opponent quote — staying in cash",
                 sold_ticker, opp)
        return remaining_cash + proceeds
    # Spend the proceeds (capped to remaining cash, which already includes
    # them) on the opponent at its ask.
    cash = remaining_cash + proceeds
    stake = min(proceeds, cash)
    if stake < cfg.min_stake:
        return cash
    shares = stake / opp_ask
    state = game_of.get(opp)
    question = _question_for(opp, state) if state is not None else opp
    broker.buy(question=question, ticker=opp, outcome="Yes",
               count=shares, price=opp_ask, current_mid=opp_ask)
    pm.record_entry(opp, "Yes", entry_price=float(opp_ask),
                    extra={"from_flip": True})
    _log_event("flip_in", sold_ticker=sold_ticker, bought_ticker=opp,
               opp_ask=opp_ask, stake=stake, shares=shares,
               sold_net_pct=net)
    log.info("FLIP %s → %s at %.3f (stake $%.2f, sold net %+.1f%%)",
             sold_ticker, opp, opp_ask, stake, 100 * net)
    return cash - stake


def _execute_freeroll(
    *, profile_name: str, broker: execution.Broker, cfg: ScalpConfig,
    sold_ticker: str,
    opp_of: dict[str, str], game_of: dict[str, Any],
    quotes: dict[str, tuple[float | None, float | None]],
    amount: float, remaining_cash: float,
) -> tuple[float, bool]:
    """After a take-profit, spend ``amount`` (the realized net profit) on the
    **opposite** longshot. Returns ``(remaining_cash, did_freeroll)``.

    Unlike the flip, the TP sale's full proceeds are already back in
    ``remaining_cash``; this only redeploys the profit. We require the
    opposite side to *still be a longshot* (ask in ``[min_price, max_price]``)
    so the profit buys real convexity rather than an expensive favorite. No
    opponent / no quote / out-of-band → the profit just stays in cash, and we
    return ``did_freeroll=False``. The opposite position is tagged
    ``from_freeroll`` so it never flips (which would circle back to the side
    we just sold) and never free-rolls again."""
    opp = opp_of.get(sold_ticker)
    if not opp:
        return remaining_cash, False
    _bid, opp_ask = quotes.get(opp, (None, None))
    if opp_ask is None or opp_ask <= 0:
        return remaining_cash, False
    if not (cfg.min_price <= opp_ask <= cfg.max_price):
        log.info("free-roll %s → %s: opposite ask %.3f outside band — skipping",
                 sold_ticker, opp, opp_ask)
        return remaining_cash, False
    stake = min(amount, remaining_cash)
    if stake < cfg.min_stake:
        return remaining_cash, False
    shares = stake / opp_ask
    state = game_of.get(opp)
    question = _question_for(opp, state) if state is not None else opp
    broker.buy(question=question, ticker=opp, outcome="Yes",
               count=shares, price=opp_ask, current_mid=opp_ask)
    pm.record_entry(opp, "Yes", entry_price=float(opp_ask),
                    extra={"from_freeroll": True})
    _log_event("freeroll_in", sold_ticker=sold_ticker, bought_ticker=opp,
               opp_ask=opp_ask, stake=stake, shares=shares)
    log.info("FREE-ROLL %s → %s at %.3f (profit $%.2f into opposite longshot)",
             sold_ticker, opp, opp_ask, stake)
    return remaining_cash - stake, True


def _open_positions(
    *, profile_name: str, broker: execution.Broker, cfg: ScalpConfig,
    states: list[Any],
    opp_of: dict[str, str], quotes: dict[str, tuple[float | None, float | None]],
    open_tickers: set[str], remaining_cash: float, stats: ScalpStats,
) -> float:
    """Open one position per eligible live game, per ``cfg.mode``. Returns
    updated cash.

    ``longshot`` mode: cheap underdog ($0.10–$0.35) in innings 4–7.
    ``close_game`` mode: the underpriced side of a coin-flip game (forecast
    in [cg_min_prob, cg_max_prob], market ≥ cg_min_edge below it) in the
    early innings (1..cg_max_entry_inning).
    ``favorite`` mode: the moderate-favorite side (forecast in
    [fav_min_prob, fav_max_prob]) bought early and held to resolution."""
    from baseball import analysis
    max_position_dollars = cfg.bankroll * cfg.max_position_pct
    close_game = cfg.mode == "close_game"
    favorite = cfg.mode == "favorite"
    needs_forecast = close_game or favorite
    if close_game:
        lo, hi = 1, cfg.cg_max_entry_inning
    elif favorite:
        lo, hi = 1, cfg.fav_max_entry_inning
    else:
        lo, hi = cfg.min_inning, cfg.max_entry_inning
    for st in states:
        inning = getattr(st, "inning", None) or 0
        if not (lo <= inning <= hi):
            continue
        # Late blowout — the book barely moves on any single play.
        if abs(getattr(st, "score_diff", 0) or 0) >= 5:
            continue
        # Already stopped out of this game today — don't whipsaw back in.
        if _is_stopped(getattr(st, "game_pk", None)):
            continue
        tickers = analysis.tickers_for_game(st.game_pk)
        game_quotes = {t: quotes.get(t, (None, None)) for t in tickers}
        forecasts = ({t: _forecast_for_ticker(t, cfg.forecast_source)
                      for t in tickers} if needs_forecast else {})
        if close_game:
            cand = select_close_game_side(
                game_quotes, forecasts, min_prob=cfg.cg_min_prob,
                max_prob=cfg.cg_max_prob, min_edge=cfg.cg_min_edge,
                min_ask=cfg.cg_min_ask,
            )
        elif favorite:
            cand = select_favorite_side(
                game_quotes, forecasts, min_prob=cfg.fav_min_prob,
                max_prob=cfg.fav_max_prob,
            )
        else:
            cand = select_longshot(game_quotes, min_price=cfg.min_price,
                                   max_price=cfg.max_price)
        if cand is None:
            continue
        # Skip if we already hold either side of this game.
        if cand in open_tickers or opp_of.get(cand) in open_tickers:
            continue
        # Skip if this game already resolved (its position was closed at $1/$0)
        # — prevents the enter↔close loop when `games.status` lags the final
        # score. Check both sides since either ticker could be re-selected.
        if _is_resolved(cand) or _is_resolved(opp_of.get(cand)):
            continue
        _bid, ask = game_quotes[cand]
        if ask is None or ask <= 0:
            continue
        stake = min(max_position_dollars, remaining_cash)
        if stake < cfg.min_stake:
            continue
        shares = stake / ask
        question = _question_for(cand, st)
        try:
            buy_res = broker.buy(question=question, ticker=cand, outcome="Yes",
                                 count=shares, price=ask, current_mid=ask)
            pm.record_entry(cand, "Yes", entry_price=float(ask))
            # Live scalp modes: rest a cash-out limit at +take_profit_pct on
            # the actual filled count (no-op in paper / favorite mode).
            _arm_resting_tp(broker, cfg, ticker=cand, outcome="Yes",
                            entry_price=float(ask),
                            count=float(buy_res.filled_count or shares),
                            question=question)
            _log_event("entry", mode=cfg.mode, ticker=cand, ask=ask,
                       stake=stake, shares=shares, inning=inning,
                       forecast=(forecasts.get(cand) if needs_forecast else None),
                       # profile + forecast_source make the shared scalp log
                       # cleanly attributable for per-source calibration (the
                       # consensus vs deep arms both write here).
                       profile=profile_name, forecast_source=cfg.forecast_source,
                       question=question)
            open_tickers.add(cand)
            remaining_cash -= stake
            stats.opened += 1
            extra = (f", fcast {forecasts[cand]:.2f}"
                     if needs_forecast and forecasts.get(cand) is not None else "")
            log.info("OPEN [%s] %s at %.3f (stake $%.2f, inn %d%s)",
                     cfg.mode, cand, ask, stake, inning, extra)
        except Exception as e:  # noqa: BLE001
            log.warning("open failed for %s: %s", cand, e)
            stats.errors += 1
    return remaining_cash


# ─── Orchestration ────────────────────────────────────────────────────────

def run_cycle(cfg: ScalpConfig) -> ScalpStats:
    """One scalper cycle. Honors the emergency-stop kill switch; pins
    profile-aware storage to ``cfg.profile_name`` for the duration."""
    stats = ScalpStats(cycle_started=datetime.now(UTC))
    if emergency_stop.is_active():
        st = emergency_stop.get_status() or {}
        log.warning("EMERGENCY STOP active — cycle short-circuited (reason=%s)",
                    st.get("reason") or "(none)")
        return stats

    profiles.initialize_profiles()
    if cfg.profile_name not in profiles.list_profiles():
        profiles.create_profile(cfg.profile_name)
    saved_env = _pin_profile_env(cfg.profile_name)
    # Pin the active-profile identity process-local too, so the kalshi_auth
    # owner-profile lock (is_configured → active_profile() == owner) resolves
    # to THIS daemon's profile rather than whatever the UI last selected
    # globally. Without this a live daemon would fail is_configured() even
    # with valid creds. Restored in finally.
    saved_active = os.environ.get(profiles.ACTIVE_PROFILE_ENV)
    os.environ[profiles.ACTIVE_PROFILE_ENV] = cfg.profile_name
    # Resolve the execution backend once per cycle. PaperBroker unless this
    # profile is explicitly allow-listed for live AND all the live gates pass
    # (see execution.get_broker) — so this is inert for paper trading.
    broker = execution.get_broker(cfg.profile_name)
    try:
        # 0. Reconcile resting cash-out limits that filled (live only) — log
        # them as take-profits before settlement so a filled TP isn't
        # mistaken for a resolution.
        try:
            _reconcile_resting_tps(profile_name=cfg.profile_name, broker=broker,
                                   stats=stats)
        except Exception as e:  # noqa: BLE001
            log.error("resting-TP reconcile phase failed: %s", e)
            stats.errors += 1

        # 1. Settle finished games (fast path: game Final via the sidecar,
        #    before Kalshi settles).
        try:
            _close_resolved(profile_name=cfg.profile_name, broker=broker,
                            stats=stats)
        except Exception as e:  # noqa: BLE001
            log.error("close-resolved phase failed: %s", e)
            stats.errors += 1

        # 1b. Live safety net: log anything that already SETTLED on the venue
        #     before the fast path caught it (else it orphans, unrecorded).
        try:
            _reconcile_settlements(profile_name=cfg.profile_name, broker=broker,
                                   stats=stats)
        except Exception as e:  # noqa: BLE001
            log.error("settlement-reconcile phase failed: %s", e)
            stats.errors += 1

        # 2. Live games + ticker/quote maps.
        states = _live_games()
        stats.games_live = len(states)
        now = datetime.now(UTC)
        opp_of, game_of, quotes = _build_ticker_maps(
            states, now=now, max_age_s=cfg.max_quote_age_s,
        )

        remaining_cash = broker.available_cash(cfg.bankroll)

        # 3. Manage open positions (TP / stop / flip).
        remaining_cash = _manage_positions(
            profile_name=cfg.profile_name, broker=broker, cfg=cfg, opp_of=opp_of,
            game_of=game_of, quotes=quotes, remaining_cash=remaining_cash,
            stats=stats,
        )

        # 4. Open new positions.
        pos_now = broker.get_positions()
        open_tickers = set(pos_now["condition_id"].astype(str)) \
            if not pos_now.empty else set()
        _open_positions(
            profile_name=cfg.profile_name, broker=broker, cfg=cfg, states=states,
            opp_of=opp_of, quotes=quotes, open_tickers=open_tickers,
            remaining_cash=remaining_cash, stats=stats,
        )
    finally:
        _restore_profile_env(saved_env)
        if saved_active is None:
            os.environ.pop(profiles.ACTIVE_PROFILE_ENV, None)
        else:
            os.environ[profiles.ACTIVE_PROFILE_ENV] = saved_active
    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="agentic_longshot_scalp")
    p.add_argument("--mode", choices=["longshot", "close_game", "favorite"],
                   default="longshot",
                   help="longshot = cheap underdog scalp (default). "
                        "close_game = buy the underpriced side of a coin-flip "
                        "game early and ride the pop. "
                        "favorite = buy the moderate-favorite side (forecast "
                        "in [fav-min-prob, fav-max-prob]) early and HOLD to "
                        "resolution (no scalp, no flip).")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--take-profit-pct", type=float, default=0.25,
                   help="Net gain that triggers a scalp sell (0.25 = +25%%).")
    p.add_argument("--flip-drop-pct", type=float, default=None,
                   help="Net loss that triggers a flip to the opponent "
                        "(0.15 = -15%%). Default: 0.15 in longshot mode, "
                        "disabled in close_game mode. Pass explicitly to override.")
    p.add_argument("--max-position-pct", type=float, default=0.05)
    p.add_argument("--max-quote-age-s", type=float, default=120.0)
    # longshot-mode entry
    p.add_argument("--min-price", type=float, default=0.10)
    p.add_argument("--max-price", type=float, default=0.35)
    p.add_argument("--min-inning", type=int, default=4)
    p.add_argument("--max-entry-inning", type=int, default=7)
    # close-game-mode entry
    p.add_argument("--forecast-source", choices=["baseball", "deep", "consensus"],
                   default="baseball",
                   help="close_game gate forecast: baseball Pythagorean "
                        "(default, instant), deep (gemma3 log, needs primer), "
                        "or consensus (ESPN de-vigged sportsbook line — the "
                        "sharpest cheap baseline, MLB only).")
    p.add_argument("--cg-min-prob", type=float, default=0.40)
    p.add_argument("--cg-max-prob", type=float, default=0.60)
    p.add_argument("--cg-min-edge", type=float, default=0.05,
                   help="close_game: market ask must be ≥ this below the "
                        "forecast to buy (0.05 = 5pp).")
    p.add_argument("--cg-min-ask", type=float, default=0.25,
                   help="close_game: price floor — skip sides priced below "
                        "this (a real coin-flip trades near 0.50; far below "
                        "means the market is pricing a longshot). Default 0.25.")
    p.add_argument("--cg-max-entry-inning", type=int, default=3,
                   help="close_game: only enter in innings 1..this (buy early).")
    p.add_argument("--cg-stop-loss-pct", type=float, default=None,
                   help="close_game: net loss that cuts the position to cash "
                        "(0.25 = -25%%). Off by default; the A/B arm runs 0.25. "
                        "Book-archive replay showed close-game losers bleed "
                        "through sellable bids, so a -25%% stop flipped the "
                        "deep arm from -$— to +$—. See "
                        "docs/close-game-stop-loss.md.")
    p.add_argument("--fav-min-prob", type=float, default=0.55,
                   help="favorite: forecast band floor (default 0.55).")
    p.add_argument("--fav-max-prob", type=float, default=0.70,
                   help="favorite: forecast band ceiling (default 0.70).")
    p.add_argument("--fav-max-entry-inning", type=int, default=3,
                   help="favorite: only enter in innings 1..this (buy early).")
    p.add_argument("--freeroll-profit", action="store_true",
                   help="On a take-profit, recycle the realized net profit "
                        "into the OPPOSITE longshot (when it's still in the "
                        "price band) — house money / free option. Off by "
                        "default.")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=15,
                   help="Seconds between cycles in --loop mode (default 15).")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Load .env so this daemon sees the same Kalshi credentials the Streamlit
    # UI does (app.py loads it; a bare `python longshot_scalper.py` otherwise
    # wouldn't, leaving is_configured() False even when creds exist). Harmless
    # when no .env is present.
    try:
        import dotenv
        dotenv.load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Flip default is mode-dependent: on (0.15) for longshot, off for
    # close_game — unless the user passed --flip-drop-pct explicitly.
    flip = args.flip_drop_pct
    if flip is None and args.mode == "longshot":
        flip = 0.15
    cfg = ScalpConfig(
        profile_name=args.profile, mode=args.mode, bankroll=args.bankroll,
        min_price=args.min_price, max_price=args.max_price,
        take_profit_pct=args.take_profit_pct, flip_drop_pct=flip,
        max_position_pct=args.max_position_pct, min_inning=args.min_inning,
        max_entry_inning=args.max_entry_inning, max_quote_age_s=args.max_quote_age_s,
        freeroll_profit=args.freeroll_profit,
        forecast_source=args.forecast_source, cg_min_prob=args.cg_min_prob,
        cg_max_prob=args.cg_max_prob, cg_min_edge=args.cg_min_edge,
        cg_min_ask=args.cg_min_ask,
        cg_max_entry_inning=args.cg_max_entry_inning,
        cg_stop_loss_pct=args.cg_stop_loss_pct,
        fav_min_prob=args.fav_min_prob, fav_max_prob=args.fav_max_prob,
        fav_max_entry_inning=args.fav_max_entry_inning,
    )

    if not args.loop:
        stats = run_cycle(cfg)
        log.info("cycle: %s", stats)
        return 0

    log.info("longshot_scalper starting (mode=%s interval=%ds, profile=%s)",
             cfg.mode, args.interval, cfg.profile_name)
    while True:
        try:
            t0 = time.time()
            stats = run_cycle(cfg)
            if (stats.opened or stats.take_profits or stats.stop_losses
                    or stats.flips or stats.freerolls or stats.resolved_closed):
                log.info("cycle in %.1fs: live=%d open=%d opened=%d TP=%d SL=%d "
                         "flips=%d freerolls=%d resolved=%d",
                         time.time() - t0, stats.games_live, stats.positions_open,
                         stats.opened, stats.take_profits, stats.stop_losses,
                         stats.flips, stats.freerolls, stats.resolved_closed)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    sys.exit(main())
