"""Counterfactual analysis harness over gathered scalper data. Read-only.

Data sources:
- data/shared/scalper_observer.jsonl : per-cycle per-side snapshots
    (ts, game_pk, ticker, team, home_abbr, away_abbr, inning, outs,
     home_score, away_score, bid, ask, fc_baseball, fc_deep, fc_consensus,
     cg_edge_*, cg_blocker_*, ls_blocker)
- baseball_state.sqlite games        : final scores (ground-truth outcomes)

Builds:
- traj[ticker] = list of snapshot dicts sorted by ts (the price path)
- won[ticker]  = bool, did the YES side win its game (None if unknown)
- meta[ticker] = {game_pk, team, home_abbr, away_abbr, first/last forecasts}

Public: load() -> Data ; then strategy modules call into it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

OBS = "data/shared/scalper_observer.jsonl"


@dataclass
class Data:
    traj: dict[str, list[dict]] = field(default_factory=dict)
    won: dict[str, bool] = field(default_factory=dict)
    meta: dict[str, dict] = field(default_factory=dict)
    game_of: dict[str, int] = field(default_factory=dict)


def _outcomes() -> dict[int, tuple[int, int]]:
    """game_pk -> (home_score, away_score) for finished games."""
    from baseball import state
    c = sqlite3.connect(state._state_path())
    c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute(
        "SELECT game_pk, home_score, away_score FROM games "
        "WHERE status IN ('Final','Game Over') AND home_score IS NOT NULL"
    ):
        out[r["game_pk"]] = (r["home_score"], r["away_score"])
    c.close()
    return out


def load() -> Data:
    d = Data()
    scores = _outcomes()
    n_obs = 0
    with open(OBS) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "obs":
                continue
            n_obs += 1
            t = r["ticker"]
            d.traj.setdefault(t, []).append(r)
            if t not in d.meta:
                d.meta[t] = {
                    "game_pk": r["game_pk"], "team": r["team"],
                    "home_abbr": r["home_abbr"], "away_abbr": r["away_abbr"],
                }
                d.game_of[t] = r["game_pk"]
    # sort trajectories by ts; attach outcomes
    for t, rows in d.traj.items():
        rows.sort(key=lambda x: x["ts"])
        m = d.meta[t]
        gpk = m["game_pk"]
        if gpk in scores:
            hs, as_ = scores[gpk]
            if hs == as_:
                continue  # tie (shouldn't happen in MLB) -> unknown
            home_won = hs > as_
            yes_is_home = (m["team"] == m["home_abbr"])
            d.won[t] = (home_won if yes_is_home else not home_won)
    return d


def summary(d: Data) -> dict[str, Any]:
    n_tick = len(d.traj)
    n_resolved = len(d.won)
    snaps = sum(len(v) for v in d.traj.values())
    games = len(set(d.game_of.values()))
    games_resolved = len({d.game_of[t] for t in d.won})
    return {
        "tickers": n_tick, "tickers_resolved": n_resolved,
        "snapshots": snaps, "games": games, "games_resolved": games_resolved,
        "avg_snaps_per_ticker": round(snaps / max(n_tick, 1), 1),
    }


if __name__ == "__main__":
    d = load()
    s = summary(d)
    out = open("/tmp/crunch_step1.txt", "w")
    for k, v in s.items():
        out.write(f"{k}: {v}\n")
    # show a sample trajectory length distribution
    lens = sorted(len(v) for v in d.traj.values())
    if lens:
        import statistics
        out.write(f"traj_len min/median/max: {lens[0]}/"
                  f"{statistics.median(lens)}/{lens[-1]}\n")
    # win rate of YES sides overall (sanity: should be ~50% on coin-flips,
    # but this is ALL sides observed, both favorites and dogs)
    if d.won:
        wr = sum(d.won.values()) / len(d.won)
        out.write(f"overall YES win rate (all resolved sides): {wr:.3f}\n")
    out.close()
    print("wrote /tmp/crunch_step1.txt")
