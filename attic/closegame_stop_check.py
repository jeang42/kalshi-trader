"""Head-to-head check: close-game scalper -25% stop arm vs hold arm.

Both arms share the same gemma3 deep gate (``--forecast-source deep``), so
the only variable is the downside stop. This compares their realized paper
P&L and answers the out-of-sample question from
``docs/close-game-stop-loss.md``: did the -25% stop confirm the in-sample
finding (-$— hold -> +$— with the stop)?

Run by the ``closegame-stop-check`` systemd timer on 2026-06-16, writing a
report to ``data/shared/closegame_stop_check_report.txt``. Also runnable
by hand: ``python closegame_stop_check.py``. Read-only.

False-stop detection: a stop is "false" if it cut a position whose game
ultimately resolved YES (the side we held). We learn the outcome by
cross-referencing the HOLD arm's exits.jsonl — it runs the same gate on the
same games and holds to resolution, so its resolution_won/lost rows tell us
how each ticker actually settled.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

STOP_PROFILE = "agentic_closegame_scalp_stop"
HOLD_PROFILE = "agentic_closegame_scalp"
REPORT_PATH = Path("data/shared/closegame_stop_check_report.txt")
IN_SAMPLE = "in-sample baseline: hold -$— -> -25% stop +$— (0 false-stops)"


def _load_exits(profile: str) -> list[dict]:
    path = Path("data") / profile / "exits.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _summary(rows: list[dict]) -> dict:
    total = sum(float(r.get("pnl_dollars", 0.0) or 0.0) for r in rows)
    by_reason: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    wins = 0
    for r in rows:
        reason = str(r.get("exit_reason", "?"))
        pnl = float(r.get("pnl_dollars", 0.0) or 0.0)
        by_reason[reason]["n"] += 1
        by_reason[reason]["pnl"] += pnl
        if pnl > 0:
            wins += 1
    n = len(rows)
    return {
        "n": n, "total": total, "by_reason": dict(by_reason),
        "win_rate": (wins / n) if n else None,
    }


def _hold_outcomes(hold_rows: list[dict]) -> dict[str, bool]:
    """condition_id -> resolved_yes (True=won) from the hold arm's
    resolution exits. The hold arm shares the gate + games, so it tells us
    how each ticker actually settled."""
    out: dict[str, bool] = {}
    for r in hold_rows:
        reason = str(r.get("exit_reason", ""))
        cid = str(r.get("condition_id", ""))
        if reason == "resolution_won":
            out[cid] = True
        elif reason == "resolution_lost":
            out[cid] = False
    return out


def _false_stops(stop_rows: list[dict], outcomes: dict[str, bool]) -> dict:
    """Stops that cut a position whose game ultimately resolved YES."""
    cut = [r for r in stop_rows if str(r.get("exit_reason")) == "stop_loss"]
    false_n = 0
    unknown_n = 0
    true_n = 0
    for r in cut:
        cid = str(r.get("condition_id", ""))
        if cid not in outcomes:
            unknown_n += 1
        elif outcomes[cid]:
            false_n += 1   # game resolved YES — the stop cut a would-be winner
        else:
            true_n += 1     # game resolved NO — the stop correctly cut a loser
    return {"stops": len(cut), "true_stops": true_n,
            "false_stops": false_n, "unknown": unknown_n}


def _recommend(stop: dict, hold: dict, fs: dict) -> str:
    if stop["n"] < 8:
        return (f"WAIT — only {stop['n']} stop-arm exits so far; too few to "
                "judge out-of-sample. Re-check after more games resolve.")
    delta = stop["total"] - hold["total"]
    if delta > 50 and fs["false_stops"] <= max(1, int(0.1 * stop["n"])):
        return (f"PROMOTE — stop arm beats hold by ${delta:.2f} with only "
                f"{fs['false_stops']} false-stops; consider making -25% the "
                "close-game default.")
    if delta > 0:
        return (f"KEEP — stop arm ahead by ${delta:.2f} but margin/false-stops "
                "not yet decisive; keep gathering.")
    return (f"KILL/REVISIT — stop arm trails hold by ${-delta:.2f}; the "
            f"in-sample edge did not hold out-of-sample "
            f"(false-stops={fs['false_stops']}).")


def build_report() -> str:
    stop_rows = _load_exits(STOP_PROFILE)
    hold_rows = _load_exits(HOLD_PROFILE)
    stop = _summary(stop_rows)
    hold = _summary(hold_rows)
    fs = _false_stops(stop_rows, _hold_outcomes(hold_rows))

    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines = []
    lines.append("=" * 68)
    lines.append("CLOSE-GAME SCALPER STOP-LOSS A/B CHECK (MLB)")
    lines.append(f"generated {now}")
    lines.append(IN_SAMPLE)
    lines.append("=" * 68)
    for name, s in ((f"-25% STOP  [{STOP_PROFILE}]", stop),
                    (f"HOLD       [{HOLD_PROFILE}]", hold)):
        wr = "n/a" if s["win_rate"] is None else f"{100 * s['win_rate']:.0f}%"
        lines.append("")
        lines.append(name)
        lines.append(f"  exits={s['n']}  net=${s['total']:+.2f}  win_rate={wr}")
        for reason, d in sorted(s["by_reason"].items()):
            lines.append(f"    {reason:<20} n={d['n']:<3}  ${d['pnl']:+.2f}")
    lines.append("")
    lines.append("FALSE-STOP AUDIT (stop arm):")
    lines.append(f"  stops={fs['stops']}  correct(loser cut)={fs['true_stops']}  "
                 f"FALSE(winner cut)={fs['false_stops']}  "
                 f"unknown_outcome={fs['unknown']}")
    lines.append("")
    lines.append(f"DELTA (stop - hold net): ${stop['total'] - hold['total']:+.2f}")
    lines.append("")
    lines.append("RECOMMENDATION: " + _recommend(stop, hold, fs))
    lines.append("=" * 68)
    lines.append("(Mechanical summary. Review alongside the research log for a "
                 "judgement call + next step.)")
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report + "\n")
    print(report)
    print(f"\n[written to {REPORT_PATH}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
