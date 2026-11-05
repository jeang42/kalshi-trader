"""Reactive scalper — in-game reaction (speed) paper arm.

The one positive signal from the data mine (see reaction_decay.py /
project_strategy_research_findings): the Kalshi book LAGS high-leverage
scoring plays by tens of seconds. This arm exploits the lag directly —
it watches EVERY live game (no pre-existing position needed) and, the
moment a run scores in a high-leverage spot, BUYS the benefiting (scoring)
side at the current ask while the book is still catching up, then SELLS
once the repricing plays out.

Both sides of the trade are explicit:
* BUY  — a NEW play whose model **WPA** clears the gate (|WPA| >=
         cfg.wpa_threshold, inning >= cfg.min_inning): buy the side whose
         win probability went UP, at ask (capped so we don't buy the top),
         only if not already held and the quote is fresh + the play recent.
* SELL — take-profit (net gain >= cfg.take_profit_pct, capture the pop) OR
         time-stop (after cfg.hold_seconds — the ~60s repricing window has
         passed, harvest whatever moved) OR resolution close at $1/$0 when
         the game goes Final (via the baseball sidecar).

**WPA-conditioned** (Win Probability Added — the change in win prob a play
caused, from `baseball.analysis.wpa_for_play`, the RE24/normal model over
base-out + score + inning). WPA is the direct measure of a play's impact,
which is what the book reprices on. It catches high-impact NON-scoring
plays (a rally-killing double play / bases-loaded strikeout — where the
FIELDING side gains) and skips low-impact runs (a run in a near-decided
game) — fixing the coarse "any scoring play" trigger. ~22% of plays clear
|WPA| 0.10; that subset is where reaction_decay.py found the ~+2.5c edge.
(The MLB feed's win-prob is NOT in raw_json — the model computes WPA from
the situation it does carry.)

Paper-only via the execution broker (so it COULD go live later behind the
same gates), but NOT in any live allow-list. Profile ``agentic_reactive``.
Exits → that profile's exits.jsonl; entries → data/shared/reactive_log.jsonl.
Pure decision fns (``is_high_leverage`` / ``benefiting_side`` /
``evaluate_exit``) are unit-tested. CLI: single cycle or ``--loop``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import baseball
from pm.archive import book_archive
from pm.archive.book_archive import best_bid_ask
from pm.arms.strategy_runner import (
    _log_exit,
    _net_pnl_pct,
    _pin_profile_env,
    _restore_profile_env,
)
from pm.core import emergency_stop, logctx, profiles, provenance, shutdown
from pm.ledger import position_meta as pm
from pm.trading import execution

log = logging.getLogger("reactive_scalper")

DEFAULT_REACTIVE_LOG = Path("data/shared/reactive_log.jsonl")


def reactive_log_path() -> Path:
    """Entry/rejection audit log, resolved per call (env: REACTIVE_LOG_FILE).

    Lazy for the reason docs/file-map.md gives for every other path here: a frozen
    module constant cannot be redirected, so the test suite wrote real audit
    rows into the live log on every run.
    """
    return Path(os.getenv("REACTIVE_LOG_FILE", str(DEFAULT_REACTIVE_LOG)))
STRATEGY_KEY = "reactive_scalp"
STATE_DB = "data/shared/baseball_state.sqlite"


@dataclass
class ReactiveConfig:
    profile_name: str = "agentic_reactive"
    bankroll: float = 1000.0
    max_position_pct: float = 0.05
    min_stake: float = 0.50
    # entry gate — WPA-conditioned: react only to plays that genuinely
    # swung win probability (the model's |WPA|), not just any run. This
    # catches high-impact NON-scoring plays (rally-killing DP, bases-loaded
    # K) and skips low-impact runs (a run in a near-decided game).
    wpa_threshold: float = 0.10      # |WPA| ≥ this (~top 22% of plays)
    min_inning: int = 6              # soft floor; WPA already encodes leverage
    min_entry_price: float = 0.25    # price FLOOR — don't buy a side that
                                     # "improved" but is still a deep underdog
                                     # (a 9-12¢ team riding to $0, not to 0.99).
                                     # Mirrors longshot_scalper's cg_min_ask.
    max_entry_ask: float = 0.92      # leave room — don't buy the top
    # Book-thinness gate: the reaction edge is thin-book concentrated
    # (reaction_capacity.py — thin tercile +7.7¢/contract @ 69% vs deep −4.9¢;
    # the lag lives where few market-makers reprice). Skip an entry when the
    # ask-side depth within `depth_within_cents` exceeds `max_entry_depth`
    # (≈ the q33 thin/mid boundary). 0 disables the gate.
    max_entry_depth: float = 60000.0
    depth_within_cents: float = 0.03
    # Absolute-dollar tiered sizing (opt-in; None → legacy bankroll*max_position_pct).
    # The confidence signal is BOOK THINNESS (the validated edge concentrator:
    # thin tercile +7.7¢/contract vs deep −4.9¢; |WPA| size is a noisy selector).
    # Entries pass the max_entry_depth gate; the THINNEST of those (depth ≤
    # high_conf_depth) are the highest-confidence → larger stake.
    base_stake_dollars: float | None = None
    high_conf_stake_dollars: float | None = None
    high_conf_depth: float = 20000.0   # thin-tercile mean — strongest edge
    max_quote_age_s: float = 90.0
    max_play_age_s: float = 120.0    # only react to RECENT plays (the window)
    # exit
    take_profit_pct: float = 0.10    # net gain → sell into the pop
    hold_seconds: float = 150.0      # time-stop: repricing window has passed
    # hold_to_resolution=True → REACT-AND-HOLD: same WPA entry, but NO scalp
    # exit (no TP/time-stop) — hold to $1/$0. Validated +EV (buys ~2.5¢ below
    # fair on the reaction lag; holding pays the transaction cost ONCE and
    # skips the whipsaw that made the scalp net-negative).
    hold_to_resolution: bool = False


# ─── Pure decision logic (unit-tested) ────────────────────────────────────

def qualifies(wpa: float, inning: int, cfg: ReactiveConfig) -> bool:
    """A play worth reacting to: it swung win probability by at least
    ``wpa_threshold`` and it's at/after the inning floor. WPA already
    encodes leverage (score + base-out + inning), so the inning floor is
    just a soft guard against acting too early with lots of game left.

    >>> c = ReactiveConfig(wpa_threshold=0.10, min_inning=6)
    >>> qualifies(0.35, 9, c)            # big swing, late
    True
    >>> qualifies(-0.30, 8, c)           # big NEGATIVE swing (fielding side)
    True
    >>> qualifies(0.04, 9, c)            # tiny swing → skip
    False
    >>> qualifies(0.35, 3, c)            # big swing but too early
    False
    """
    return abs(wpa) >= cfg.wpa_threshold and inning >= cfg.min_inning


def batting_side(half_inning: str) -> str:
    """Which team is batting. Top → away; bottom → home.

    >>> batting_side("top"), batting_side("Bottom")
    ('away', 'home')
    """
    return "away" if str(half_inning).lower().startswith("top") else "home"


def beneficiary_from_wpa(wpa: float, half_inning: str) -> str:
    """The side whose win probability went UP. WPA is the batting team's
    perspective: wpa>0 helped the batting team (buy them); wpa<0 helped the
    FIELDING team (a rally-killing DP/strikeout — buy the fielding side).

    >>> beneficiary_from_wpa(0.3, "bottom")   # home batting, helped
    'home'
    >>> beneficiary_from_wpa(-0.3, "bottom")  # home batting, hurt → away gains
    'away'
    >>> beneficiary_from_wpa(0.3, "top")      # away batting, helped
    'away'
    """
    bat = batting_side(half_inning)
    if wpa >= 0:
        return bat
    return "home" if bat == "away" else "away"


def evaluate_exit(*, entry: dict, side_price: float, now: datetime,
                  cfg: ReactiveConfig, ticker: str) -> str | None:
    """Return ``"take_profit"``, ``"time_stop"``, or None for one open
    position. Take-profit on **net** P&L (after fees) captures the pop; the
    time-stop harvests whatever the repricing did once the window has passed
    (so a position never lingers exposed to the rest of the game).

    >>> c = ReactiveConfig(take_profit_pct=0.10, hold_seconds=150)
    >>> e = {"entry_price": 0.50, "opened_at": "2026-06-05T00:00:00+00:00"}
    >>> from datetime import datetime, UTC
    >>> evaluate_exit(entry=e, side_price=0.60, ticker="KXMLBGAME-X-NYM",
    ...     now=datetime(2026,6,5,0,0,30,tzinfo=UTC), cfg=c)   # +20% net
    'take_profit'
    >>> evaluate_exit(entry=e, side_price=0.505, ticker="KXMLBGAME-X-NYM",
    ...     now=datetime(2026,6,5,0,5,0,tzinfo=UTC), cfg=c)    # flat, 5min later
    'time_stop'
    """
    entry_price = float(entry.get("entry_price", 0.0) or 0.0)
    if entry_price <= 0:
        return None
    if _net_pnl_pct(entry_price, side_price, ticker=ticker) >= cfg.take_profit_pct:
        return "take_profit"
    opened = entry.get("opened_at")
    if opened:
        try:
            age = (now - datetime.fromisoformat(str(opened))).total_seconds()
            if age >= cfg.hold_seconds:
                return "time_stop"
        except (ValueError, TypeError):
            pass
    return None


# ─── Live-data IO ─────────────────────────────────────────────────────────

def _state_path(profile_name: str) -> Path:
    return profiles.profile_dir(profile_name) / "reactive_state.json"


def _load_watermark(profile_name: str) -> str:
    p = _state_path(profile_name)
    if p.exists():
        try:
            return json.loads(p.read_text()).get("watermark", "")
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


def _save_watermark(profile_name: str, ts: str) -> None:
    p = _state_path(profile_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"watermark": ts}))


def _new_high_wpa_plays(since_ts: str, cfg: ReactiveConfig,
                        *, recent_window_s: float = 900.0) -> list[dict]:
    """New plays since ``since_ts`` whose model |WPA| clears the gate. Each
    dict: game_pk, play_id, ts, half_inning, inning, wpa, side (the team
    whose win prob went up). Bounded to the recent window so a fresh start
    (empty watermark) doesn't recompute WPA over all history."""
    from baseball import analysis
    floor = (datetime.now(UTC).timestamp() - recent_window_s)
    floor_iso = datetime.fromtimestamp(floor, UTC).isoformat()
    lo = max(since_ts, floor_iso) if since_ts else floor_iso
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT game_pk, play_id, ts, half_inning, inning FROM live_plays "
        "WHERE ts > ? ORDER BY ts", (lo,)).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        wpa = analysis.wpa_for_play(r["game_pk"], r["play_id"])
        if wpa is None:
            continue
        inning = r["inning"] or 0
        out.append({
            "game_pk": r["game_pk"], "play_id": r["play_id"], "ts": str(r["ts"]),
            "half_inning": r["half_inning"], "inning": inning, "wpa": wpa,
            "side": beneficiary_from_wpa(wpa, r["half_inning"]),
            "qualifies": qualifies(wpa, inning, cfg),
        })
    return out


def _benefiting_ticker(game_pk: int, side: str) -> str | None:
    from baseball import analysis
    grow = analysis._get_game_row(game_pk)
    if not grow:
        return None
    abbr = grow["away_abbr"] if side == "away" else grow["home_abbr"]
    for t in analysis.tickers_for_game(game_pk):
        if t.endswith("-" + abbr):
            return t
    return None


def _quote(ticker: str, *, now: datetime, max_age_s: float) -> tuple[float | None, float | None]:
    snap = book_archive.get_snapshot_at(
        venue="kalshi", market_id=ticker, token_id=ticker, ts=now)
    if snap is None or now.timestamp() - snap.snapshot_ts > max_age_s:
        return None, None
    return best_bid_ask(snap.book)


def _ask_depth_contracts(book, *, within_cents: float = 0.03) -> float | None:
    """Contracts available to BUY within ``within_cents`` of the best ask, from
    a parse_order_book-shaped DataFrame (book_archive snapshot). The reaction
    edge is thin-book concentrated (attic/reaction_capacity.py: thin tercile
    +7.7¢/contract vs deep −4.9¢), so this measures the depth the entry gate
    uses to skip already-liquid (efficiently-priced) books. None if no asks."""
    if book is None or getattr(book, "empty", True):
        return None
    asks = book[book["side"] == "ask"]
    if asks.empty:
        return None
    best = float(asks["price"].min())
    near = asks[asks["price"] <= best + within_cents]
    return float(near["size"].sum())


def _target_stake(cfg: ReactiveConfig, depth: float | None) -> float:
    """Confidence-tiered dollar stake. Legacy (base_stake_dollars=None) →
    bankroll*max_position_pct. Otherwise base_stake, bumped to high_conf_stake
    for the thinnest books (depth ≤ high_conf_depth — the strongest-edge,
    highest-confidence entries)."""
    if cfg.base_stake_dollars is None:
        return cfg.bankroll * cfg.max_position_pct
    if (cfg.high_conf_stake_dollars is not None and depth is not None
            and depth <= cfg.high_conf_depth):
        return cfg.high_conf_stake_dollars
    return cfg.base_stake_dollars


def _entry_depth(ticker: str, *, now: datetime, max_age_s: float,
                 within_cents: float = 0.03) -> float | None:
    """Live ask-side depth for the entry gate, from the same fresh snapshot
    source as ``_quote``."""
    snap = book_archive.get_snapshot_at(
        venue="kalshi", market_id=ticker, token_id=ticker, ts=now)
    if snap is None or now.timestamp() - snap.snapshot_ts > max_age_s:
        return None
    return _ask_depth_contracts(snap.book, within_cents=within_cents)


def _play_age_s(play_ts: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(play_ts.replace("Z", "+00:00"))).total_seconds()
    except (ValueError, TypeError):
        return 1e9


def _log_event(kind: str, **fields: Any) -> None:
    _log_path = reactive_log_path()
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_log_path, "a") as f:
        f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                            "commit": provenance.label(),   # R-18
                            "kind": kind, **fields}) + "\n")


# ─── Cycle phases ─────────────────────────────────────────────────────────

# Defensive re-entry guard for resolved games (hold mode resolution-closes every
# position). Reactive is play-driven so a finished game yields no new plays and
# can't normally re-enter — but this mirrors the longshot_scalper fix for the
# enter↔close dup-exit bug, cheaply, in case a quote/play arrives post-final.
_REENTRY_TTL_S = 24 * 3600


def _resolved_path():
    return profiles.profile_dir(profiles.active_profile()) / "resolved_tickers.json"


def _load_resolved() -> dict[str, float]:
    p = _resolved_path()
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
    data = {k: v for k, v in _load_resolved().items()
            if float(v) >= now - _REENTRY_TTL_S}
    data[str(ticker)] = now
    try:
        p = _resolved_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError as e:  # noqa: BLE001
        log.warning("could not persist resolved guard for %s: %s", ticker, e)


def _is_resolved(ticker: str | None) -> bool:
    if not ticker:
        return False
    v = _load_resolved().get(str(ticker))
    return v is not None and float(v) >= datetime.now(UTC).timestamp() - _REENTRY_TTL_S


def _close_resolved(*, profile_name: str, broker: execution.Broker) -> int:
    df = broker.get_positions()
    if df.empty:
        return 0
    try:
        resolved = baseball.resolve_pending_tickers(
            list(df["condition_id"].astype(str).unique()))
    except Exception as e:  # noqa: BLE001
        log.debug("resolve failed: %s", e)
        return 0
    n = 0
    for _, pos in df.iterrows():
        cid = str(pos["condition_id"])
        if cid not in resolved:
            continue
        won = bool(resolved[cid])
        close_price = 1.0 if won else 0.0
        outcome = str(pos["outcome"])
        with shutdown.deferred(f"resolution close {cid}"):
            try:
                if not broker.closes_settle_on_venue:
                    broker.sell(question=str(pos["question"]), ticker=cid,
                                outcome=outcome, count=float(pos["shares"]),
                                price=close_price)
                entry = pm.get_entry(cid, outcome) or {
                    "entry_price": float(pos.get("avg_price") or close_price),
                    "peak_price": float(pos.get("avg_price") or close_price)}
                _log_exit(profile_name=profile_name, condition_id=cid,
                          question=str(pos["question"]), outcome=outcome,
                          shares=float(pos["shares"]),
                          entry_price=float(entry["entry_price"]),
                          exit_price=close_price,
                          peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                          opened_at=entry.get("opened_at"),
                          exit_reason="resolution_won" if won else "resolution_lost",
                          strategy_key=STRATEGY_KEY)
                pm.clear_entry(cid, outcome)
                _mark_resolved(cid)  # don't re-enter this finished game
                n += 1
                log.info("closed %s at $%.2f (%s)", cid, close_price,
                         "WON" if won else "lost")
            except Exception as e:  # noqa: BLE001
                log.warning("close failed for %s: %s", cid, e)
    return n


def _manage(*, profile_name: str, broker: execution.Broker,
            cfg: ReactiveConfig) -> int:
    df = broker.get_positions()
    if df.empty:
        return 0
    now = datetime.now(UTC)
    n = 0
    for _, pos in df.iterrows():
        cid, outcome = str(pos["condition_id"]), str(pos["outcome"])
        bid, _ask = _quote(cid, now=now, max_age_s=cfg.max_quote_age_s)
        if bid is None:
            continue
        entry = pm.update_peak(cid, outcome, current_price=bid, now=now) \
            or pm.get_entry(cid, outcome)
        if entry is None:
            continue
        reason = evaluate_exit(entry=entry, side_price=bid, now=now,
                               cfg=cfg, ticker=cid)
        if reason is None:
            continue
        shares = float(pos["shares"])
        with shutdown.deferred(f"manage exit {cid}"):
            try:
                res = broker.sell(question=str(pos["question"]), ticker=cid,
                                  outcome=outcome, count=shares, price=bid,
                                  current_mid=bid, marketable=True)
            except Exception as e:  # noqa: BLE001
                log.warning("manage-exit errored for %s: %s", cid, e)
                continue
            # broker.sell returns an OrderResult and does NOT raise when a
            # marketable sell fails to fill (a dying longshot can have no bid).
            # Logging the exit + clearing meta on a non-fill would record a
            # fictional clean exit and orphan a position that actually rides to
            # $0 — so only a real fill closes the position; otherwise leave meta
            # intact (we retry the marketable exit next cycle, and _close_resolved
            # catches it at game end) and log the miss for visibility.
            if not (res.ok and res.filled_count and res.filled_count > 0):
                _log_event("exit_unfilled", ticker=cid, bid=bid, reason=reason,
                           status=res.status, detail=res.detail,
                           live=broker.is_live)
                log.info("manage-exit NOT filled for %s: %s (%s) — meta kept",
                         cid, res.status, res.detail or "")
                continue
            fill_price = float(res.avg_fill_price or bid)
            _log_exit(profile_name=profile_name, condition_id=cid,
                      question=str(pos["question"]), outcome=outcome,
                      shares=res.filled_count, entry_price=float(entry["entry_price"]),
                      exit_price=fill_price,
                      peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                      opened_at=entry.get("opened_at"),
                      exit_reason=reason, strategy_key=STRATEGY_KEY)
            pm.clear_entry(cid, outcome)
        n += 1
        net = _net_pnl_pct(float(entry["entry_price"]), fill_price, ticker=cid)
        log.info("%s %s exit=%.3f net=%+.1f%%", reason.upper(), cid,
                 fill_price, 100 * net)
    return n


def _react(*, profile_name: str, broker: execution.Broker, cfg: ReactiveConfig,
           remaining_cash: float) -> tuple[int, str]:
    """Enter on new high-leverage scoring plays. Returns (n_opened,
    new_watermark)."""
    watermark = _load_watermark(profile_name)
    plays = _new_high_wpa_plays(watermark, cfg)
    if not plays:
        return 0, watermark
    now = datetime.now(UTC)
    held = set(broker.get_positions()["condition_id"].astype(str)) \
        if not broker.get_positions().empty else set()
    opened = 0
    new_watermark = watermark
    for p in sorted(plays, key=lambda x: x["ts"]):
        new_watermark = max(new_watermark, p["ts"])  # advance past every seen play
        if not p["qualifies"]:                       # |WPA| / inning gate
            continue
        if _play_age_s(p["ts"], now) > cfg.max_play_age_s:
            continue  # stale — repricing window already gone
        tk = _benefiting_ticker(p["game_pk"], p["side"])
        if tk is None or tk in held or _is_resolved(tk):
            continue
        bid, ask = _quote(tk, now=now, max_age_s=cfg.max_quote_age_s)
        if ask is None or ask <= 0 or ask > cfg.max_entry_ask:
            continue
        if ask < cfg.min_entry_price:
            # The benefiting side is still a deep underdog — the reaction
            # thesis is "ride a contested side up", not "buy a 10¢ team that
            # ticked up on its way to $0." Every sub-floor buy in the live
            # canary went to zero.
            _log_event("react_reject", ticker=tk, ask=ask,
                       status="below_price_floor", live=broker.is_live,
                       inning=p["inning"], wpa=round(p["wpa"], 3),
                       side=p["side"], play_ts=p["ts"])
            continue
        # Book depth at entry — drives BOTH the thin-book gate and the
        # confidence-tiered stake (computed once, reused).
        depth = _entry_depth(tk, now=now, max_age_s=cfg.max_quote_age_s,
                             within_cents=cfg.depth_within_cents)
        # Book-thinness gate: the edge is concentrated in THIN books (a deep
        # book = many market-makers = already-repriced = no lag to capture).
        if cfg.max_entry_depth > 0 and depth is not None \
                and depth > cfg.max_entry_depth:
            _log_event("react_reject", ticker=tk, ask=ask,
                       status="book_too_deep", depth=round(depth),
                       live=broker.is_live, inning=p["inning"],
                       wpa=round(p["wpa"], 3), side=p["side"], play_ts=p["ts"])
            continue
        target = _target_stake(cfg, depth)            # confidence-tiered $ target
        stake = min(target, remaining_cash)
        if stake < cfg.min_stake:
            continue
        high_conf = (cfg.high_conf_stake_dollars is not None and depth is not None
                     and depth <= cfg.high_conf_depth)
        shares = stake / ask
        question = f"{tk.rsplit('-', 1)[-1]} wins (reactive)"
        with shutdown.deferred(f"react entry {tk}"):
            try:
                res = broker.buy(question=question, ticker=tk, outcome="Yes",
                                 count=shares, price=ask, current_mid=ask)
            except Exception as e:  # noqa: BLE001
                log.warning("react-buy errored for %s: %s", tk, e)
                _log_event("react_reject", ticker=tk, ask=ask, stake=stake,
                           status="error", detail=str(e), live=broker.is_live,
                           inning=p["inning"], wpa=round(p["wpa"], 3),
                           side=p["side"], play_ts=p["ts"])
                continue
            # broker.buy returns an OrderResult and does NOT raise on a
            # rejected/resting/error order — only a genuine fill is a real entry.
            # Logging every attempt as react_entry would overstate the live track
            # record and hide rejections, so a non-fill is logged as react_reject
            # and does not count toward opened / held / cash / position_meta.
            if not (res.ok and res.filled_count and res.filled_count > 0):
                _log_event("react_reject", ticker=tk, ask=ask, stake=stake,
                           status=res.status, detail=res.detail,
                           live=broker.is_live, inning=p["inning"],
                           wpa=round(p["wpa"], 3), side=p["side"], play_ts=p["ts"])
                log.info("react-buy not filled for %s: %s (%s)",
                         tk, res.status, res.detail or "")
                continue
            fill_price = float(res.avg_fill_price or ask)
            filled_cost = round(fill_price * res.filled_count, 4)
            pm.record_entry(tk, "Yes", entry_price=fill_price)
            _log_event("react_entry", ticker=tk, ask=ask, fill_price=fill_price,
                       filled_count=res.filled_count, cost=filled_cost,
                       stake=stake, high_conf=high_conf,
                       depth=round(depth) if depth is not None else None,
                       status=res.status, order_id=res.order_id,
                       live=broker.is_live, inning=p["inning"],
                       wpa=round(p["wpa"], 3), side=p["side"], play_ts=p["ts"],
                       play_age_s=round(_play_age_s(p["ts"], now), 1))
        held.add(tk)
        remaining_cash -= filled_cost
        opened += 1
        log.info("REACT-BUY %s %sx at %.3f (inn %d, WPA %+.3f, play %.0fs old)",
                 tk, res.filled_count, fill_price, p["inning"], p["wpa"],
                 _play_age_s(p["ts"], now))
    return opened, new_watermark


def run_cycle(cfg: ReactiveConfig) -> dict[str, int]:
    if emergency_stop.is_active():
        log.warning("EMERGENCY STOP active — skipping cycle")
        return {"opened": 0, "exits": 0, "resolved": 0}
    profiles.initialize_profiles()
    if cfg.profile_name not in profiles.list_profiles():
        profiles.create_profile(cfg.profile_name)
    saved = _pin_profile_env(cfg.profile_name)
    # Pin the active-profile identity process-local so the kalshi_auth
    # owner-lock (is_configured → active_profile() ∈ authorized) resolves to
    # THIS daemon's profile rather than whatever the UI last selected — else a
    # live-allow-listed reactive arm would silently fall back to PaperBroker.
    saved_active = os.environ.get(profiles.ACTIVE_PROFILE_ENV)
    os.environ[profiles.ACTIVE_PROFILE_ENV] = cfg.profile_name
    broker = execution.get_broker(cfg.profile_name)
    try:
        resolved = _close_resolved(profile_name=cfg.profile_name, broker=broker)
        # Hold mode: NO scalp exit — positions ride to resolution (_close_resolved).
        exits = (0 if cfg.hold_to_resolution
                 else _manage(profile_name=cfg.profile_name, broker=broker, cfg=cfg))
        remaining = broker.available_cash(cfg.bankroll)
        opened, wm = _react(profile_name=cfg.profile_name, broker=broker,
                            cfg=cfg, remaining_cash=remaining)
        if wm:
            _save_watermark(cfg.profile_name, wm)
    finally:
        _restore_profile_env(saved)
        if saved_active is None:
            os.environ.pop(profiles.ACTIVE_PROFILE_ENV, None)
        else:
            os.environ[profiles.ACTIVE_PROFILE_ENV] = saved_active
    return {"opened": opened, "exits": exits, "resolved": resolved}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="agentic_reactive")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--max-position-pct", type=float, default=0.05)
    p.add_argument("--wpa-threshold", type=float, default=0.10,
                   help="|WPA| a play must swing to trigger a reaction.")
    p.add_argument("--min-inning", type=int, default=6)
    p.add_argument("--min-entry-price", type=float, default=0.25,
                   help="Price floor — skip a benefiting side priced below "
                        "this (a deep underdog that ticked up, not a "
                        "contested side riding toward 1.0).")
    p.add_argument("--max-entry-depth", type=float, default=60000.0,
                   help="Skip entry when ask-side depth within 3c exceeds this "
                        "(thin-book gate; the edge is thin-book concentrated). "
                        "0 disables.")
    p.add_argument("--base-stake", type=float, default=None,
                   help="Absolute $ per order (opt-in; else bankroll*max_position_pct).")
    p.add_argument("--high-conf-stake", type=float, default=None,
                   help="Larger $ per order for highest-confidence (thinnest-book) "
                        "entries. Requires --base-stake.")
    p.add_argument("--high-conf-depth", type=float, default=20000.0,
                   help="Depth at/below which an entry is high-confidence "
                        "(thinnest book = strongest edge).")
    p.add_argument("--take-profit-pct", type=float, default=0.10)
    p.add_argument("--hold-seconds", type=float, default=150.0)
    p.add_argument("--hold", action="store_true",
                   help="React-and-HOLD: same WPA entry, no scalp exit, ride to "
                        "resolution (the validated +EV variant).")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=12)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Load .env so the live-trading gates (KALSHI_LIVE_ENABLED,
    # LIVE_TRADING_PROFILES, KALSHI_OWNER_PROFILE, LIVE_MAX_STAKE_DOLLARS) are
    # visible to this daemon process — without this the LiveBroker gate can
    # never pass and the arm silently stays on paper.
    try:
        import dotenv
        dotenv.load_dotenv()
    except Exception as e:  # noqa: BLE001
        # The comment above is the point: without .env the live gate cannot
        # pass and this arm silently runs on paper. Never fail silently here.
        logging.getLogger(__name__).error(
            "could not load .env (%s) — live gates will NOT pass and this arm "
            "will run on PAPER regardless of configuration", e)
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())
    cfg = ReactiveConfig(
        profile_name=args.profile, bankroll=args.bankroll,
        max_position_pct=args.max_position_pct, min_inning=args.min_inning,
        wpa_threshold=args.wpa_threshold, min_entry_price=args.min_entry_price,
        max_entry_depth=args.max_entry_depth,
        base_stake_dollars=args.base_stake,
        high_conf_stake_dollars=args.high_conf_stake,
        high_conf_depth=args.high_conf_depth,
        take_profit_pct=args.take_profit_pct, hold_seconds=args.hold_seconds,
        hold_to_resolution=args.hold)
    if not args.loop:
        log.info("cycle: %s", run_cycle(cfg))
        return 0
    log.info("reactive_scalper starting (interval=%ds, profile=%s)",
             args.interval, cfg.profile_name)
    shutdown.install()          # SIGTERM finishes the current step, then exits (R-20)
    while not shutdown.requested():
        try:
            s = run_cycle(cfg)
            if s["opened"] or s["exits"] or s["resolved"]:
                log.info("cycle: opened=%d exits=%d resolved=%d",
                         s["opened"], s["exits"], s["resolved"])
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        if shutdown.sleep(max(1, args.interval)):
            break
    log.info("clean shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
