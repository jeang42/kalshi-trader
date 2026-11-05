"""Tennis sidecar runner — backfill + nightly refresh.

A cycle:
1. Pull current rankings (both tours) → id→name map + rank/points.
2. Pull tournaments in a date window → their matches (inline results) →
   store in the matches table.
3. Recompute Elo from the full matches table (chronological replay) and
   snapshot each player's global + per-surface Elo into ``players``,
   joining names from the rankings map (or "Player {id}" fallback).
4. Stamp last_refresh_ts.

``--backfill`` widens the date window to season-start (default Jan 1 of the
current year) so Elo has history. The default (no flag) uses a short recent
window — cheap nightly top-up; Elo is still recomputed from the full stored
matches table so ratings stay correct.

CLI:
    python -m tennis.runner --backfill     # full-season history (slow, once)
    python -m tennis.runner                # nightly top-up
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from tennis import data, elo, state

log = logging.getLogger("tennis.runner")

SURFACE_AWARE = True
SURFACE_WEIGHT = 0.5


def _recompute_elo(name_map: dict[tuple[int, str], dict[str, Any]]) -> dict[str, Any]:
    """Replay the full matches table per tour, snapshot Elo into players."""
    matches = state.all_matches()
    by_tour: dict[str, list[dict[str, Any]]] = {}
    for m in matches:
        if m.get("winner_id") is None:
            continue
        by_tour.setdefault(m["tour"], []).append(m)

    # Rank-seed map (player_id → initial Elo from ranking points). This is
    # the accuracy fix — without it everyone starts at 1500 and hot
    # small-sample players rate ≈ true elites. Built from the rankings pull.
    seeds = {pid: elo.seed_for_points(meta.get("points"))
             for (pid, _tour), meta in name_map.items()}

    total_players = 0
    brier_sum = 0.0
    brier_n = 0
    for tour, rows in by_tour.items():
        rows.sort(key=lambda r: r["date"])
        # Walk-forward out-of-sample Brier for THIS tour's ladder (predict
        # each match from pre-match Elo, then apply) — the model's honest
        # accuracy. Separate quick replay; cost is negligible.
        wf = elo.walk_forward_brier(
            [(r["p1_id"], r["p2_id"], r["winner_id"], r.get("surface"))
             for r in rows],
            seeds=seeds, surface_aware=SURFACE_AWARE, surface_weight=SURFACE_WEIGHT)
        wf_brier, wf_n = wf.get("brier"), wf.get("n")
        if wf_brier is not None and wf_n:
            brier_sum += float(wf_brier) * int(wf_n)
            brier_n += int(wf_n)
        model = elo.EloModel(surface_aware=SURFACE_AWARE,
                             surface_weight=SURFACE_WEIGHT, seeds=seeds)
        # track per-surface participation for snapshotting
        for r in rows:
            w = r["winner_id"]
            loser = r["p2_id"] if w == r["p1_id"] else r["p1_id"]
            model.update(w, loser, surface=r.get("surface"))
        # snapshot every rated player
        for pid, rating in model.ratings.items():
            meta = name_map.get((pid, tour), {})
            # Index rather than .get(): the `pid in d` guard already proves
            # membership, and .get() typed every value Optional.
            surface_elo = {s: d[pid] for s, d in model.surface_ratings.items()
                           if pid in d}
            state.upsert_player(
                pid=pid, tour=tour,
                name=meta.get("name") or f"Player {pid}",
                elo=rating, surface_elo=surface_elo,
                rank=meta.get("rank"), points=meta.get("points"),
                matches_played=model.n_played.get(pid, 0),
            )
            total_players += 1
    # Persist the model's walk-forward Brier so stats()/health surface a real
    # accuracy number (the predictions table was never populated — the sidecar
    # can't see Kalshi tickers; this scores the model against actual results).
    wf_brier = (brier_sum / brier_n) if brier_n else None
    state.set_meta("walk_forward_brier", "" if wf_brier is None else f"{wf_brier:.5f}")
    state.set_meta("walk_forward_brier_n", str(brier_n))
    return {"rated_players": total_players,
            "matches": sum(len(v) for v in by_tour.values()),
            "walk_forward_brier": wf_brier, "walk_forward_brier_n": brier_n}


def run_cycle(*, force: bool = False, backfill: bool = False,
              since: str | None = None) -> dict[str, Any]:
    started = datetime.now(UTC)
    state.migrate()
    out: dict[str, Any] = {"started_at": started.isoformat(), "backfill": backfill}

    # 1. Rankings (id→name map, both tours)
    name_map: dict[tuple[int, str], dict[str, Any]] = {}
    for tour in data.TOURS:
        try:
            for p in data.get_rankings(tour):
                name_map[(p.id, tour)] = {"name": p.name, "rank": p.rank,
                                          "points": p.points}
        except Exception as e:  # noqa: BLE001
            log.warning("rankings %s failed: %s", tour, e)
            state.log_activity("error", f"rankings {tour} failed: {e}")
    out["ranked_names"] = len(name_map)

    # 2. Tournaments → matches
    today = datetime.now(UTC).date()
    if since:
        start = since.replace("-", "")
    elif backfill:
        start = f"{today.year}0101"
    else:
        start = (today - timedelta(days=10)).strftime("%Y%m%d")
    end = (today + timedelta(days=14)).strftime("%Y%m%d")

    stored = 0
    for tour in data.TOURS:
        try:
            tourneys = data.get_tournaments(tour, start=start, end=end)
        except Exception as e:  # noqa: BLE001
            log.warning("tournaments %s failed: %s", tour, e)
            continue
        for t in tourneys:
            try:
                for m in data.get_matches_for_tournament(t["ref"], tour):
                    state.upsert_match(m)
                    stored += 1
            except Exception as e:  # noqa: BLE001
                log.debug("matches fetch failed (%s): %s", t.get("ref"), e)
    out["matches_stored"] = stored

    # 3. Recompute Elo + snapshot players
    out.update(_recompute_elo(name_map))

    state.set_meta("last_refresh_ts", datetime.now(UTC).isoformat())
    elapsed = (datetime.now(UTC) - started).total_seconds()
    out["elapsed_s"] = round(elapsed, 1)
    state.log_activity("refresh",
                       f"cycle ok in {elapsed:.1f}s ({'backfill' if backfill else 'top-up'})",
                       out)
    log.info("tennis cycle ok in %.1fs: %s", elapsed, out)
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tennis sidecar runner")
    p.add_argument("--backfill", action="store_true",
                   help="pull full season-to-date history (slow; run once)")
    p.add_argument("--since", help="YYYY-MM-DD window start override")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import sys
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run_cycle(backfill=args.backfill, since=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
