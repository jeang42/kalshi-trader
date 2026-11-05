"""Consensus calibration check — is the ESPN de-vigged consensus gate
well-calibrated, or systematically over/under-confident?

Hypothesis (from the live favorite-hold canary running ~35% vs an implied
~58%): the consensus the strategies gate on may be OVERCONFIDENT on moderate
MLB favorites. If so it undercuts everything that uses it (favorite-hold AND
the close-game consensus arm). This measures it directly.

Method: join each consensus-gated arm's resolved outcomes to the consensus
forecast it was given at entry (from the shared scalp log), then compute the
Brier score + a calibration curve (predicted P(backed side wins) vs the
observed win rate per probability bucket). A negative observed−predicted gap
= overconfidence.

Attribution: prefer scalp-log entries tagged ``forecast_source=consensus``
(added 2026-06-04). Legacy entries (no tag) are only used when unambiguous —
a single distinct forecast for that ticker — so close-game tickers bet by
multiple arms into the shared log are skipped rather than mis-attributed.
The clean sample grows as tagged data accumulates.

Read-only. CLI: ``python consensus_calibration_check.py``. Writes
``data/shared/consensus_calibration_report.txt``.
"""

from __future__ import annotations

import json
from pathlib import Path

CONSENSUS_PROFILES = ["agentic_favorite_hold", "agentic_closegame_consensus"]
SCALP_LOG = Path("data/shared/longshot_scalp_log.jsonl")
REPORT = Path("data/shared/consensus_calibration_report.txt")


def _consensus_forecast_by_ticker() -> dict[str, float]:
    """ticker → consensus forecast at entry. Tagged-consensus entries win;
    legacy entries used only when unambiguous (one distinct value)."""
    if not SCALP_LOG.exists():
        return {}
    tagged: dict[str, float] = {}
    legacy: dict[str, set] = {}
    for line in SCALP_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("kind") != "entry" or r.get("forecast") is None:
            continue
        tk = str(r.get("ticker", ""))
        fc = round(float(r["forecast"]), 4)
        src = r.get("forecast_source")
        if src == "consensus":
            tagged[tk] = fc
        elif src is None:  # legacy, untagged
            legacy.setdefault(tk, set()).add(fc)
    out = dict(tagged)
    for tk, vals in legacy.items():
        if tk in out:
            continue
        if len(vals) == 1:          # unambiguous → trust it
            out[tk] = next(iter(vals))
        # else: bet by multiple arms, can't attribute → skip
    return out


# Which arm produced the outcome — matters for interpretation:
#   favorite-hold takes ALL moderate favorites (no edge filter) → an
#     UNBIASED calibration test of consensus on that band.
#   close-game-consensus only bets sides where consensus DISAGREES with the
#     market (forecast above the ask) → SELECTION-BIASED toward exactly the
#     cases where consensus is most likely wrong if the market is efficient.
#     Its win rate is informative about the strategy's (negative) edge, but
#     it overstates consensus mis-calibration.
_ARM = {"agentic_favorite_hold": "favorite (unbiased)",
        "agentic_closegame_consensus": "close-game (selection-biased)"}


def _outcomes() -> dict[str, tuple[int, str]]:
    """ticker → (1 if backed side won else 0, arm-label)."""
    out: dict[str, tuple[int, str]] = {}
    for prof in CONSENSUS_PROFILES:
        p = Path("data") / prof / "exits.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            reason = r.get("exit_reason", "")
            if reason in ("resolution_won", "resolution_lost"):
                out[str(r["condition_id"])] = (
                    1 if reason == "resolution_won" else 0, _ARM[prof])
    return out


def build_pairs() -> list[tuple[float, int, str]]:
    fc = _consensus_forecast_by_ticker()
    oc = _outcomes()
    return [(fc[t], oc[t][0], oc[t][1]) for t in fc if t in oc]


def _calib(pairs: list[tuple[float, int, str]]) -> dict:
    """Brier + calibration summary for (forecast, actual, arm) pairs.

    >>> s = _calib([(0.6, 1, "a"), (0.6, 0, "a"), (0.6, 1, "a")])
    >>> round(s["brier"], 3), round(s["mean_fc"], 3), round(s["obs"], 3)
    (0.227, 0.6, 0.667)
    """
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    brier = sum((f - a) ** 2 for f, a, _ in pairs) / n
    mean_fc = sum(f for f, _, _ in pairs) / n
    base = sum(a for _, a, _ in pairs) / n
    return {"n": n, "brier": brier, "mean_fc": mean_fc, "obs": base,
            "baseline_brier": base * (1 - base)}


def _block(title: str, pairs: list[tuple[float, int, str]]) -> list[str]:
    s = _calib(pairs)
    if s["n"] == 0:
        return [f"\n{title}: no data"]
    out = [f"\n{title}  (n={s['n']})",
           f"  mean forecast {s['mean_fc']:.3f}  vs  observed win rate "
           f"{s['obs']:.3f}   gap {s['obs'] - s['mean_fc']:+.3f}",
           f"  Brier {s['brier']:.4f}  vs predict-base-rate "
           f"{s['baseline_brier']:.4f}  "
           f"({'has skill' if s['brier'] < s['baseline_brier'] else 'WORSE than base rate'})"]
    return out


def build_report() -> str:
    pairs = build_pairs()
    fav = [p for p in pairs if p[2].startswith("favorite")]
    cg = [p for p in pairs if p[2].startswith("close")]
    lines = ["=" * 68, "CONSENSUS CALIBRATION CHECK (ESPN de-vigged gate)", "=" * 68]
    if len(pairs) < 5:
        lines += [f"\nOnly {len(pairs)} cleanly-attributable pairs — too few. "
                  "Sample grows as forecast_source-tagged data accumulates.",
                  "=" * 68]
        return "\n".join(lines)

    lines.append("\n*** CLEAN SIGNAL — favorite-hold takes ALL moderate "
                 "favorites (no edge filter), so this is an UNBIASED")
    lines.append("    calibration of consensus. Trust this one. ***")
    lines += _block("FAVORITE-HOLD slice", fav)

    lines.append("\n--- close-game arm only bets where consensus DISAGREES with "
                 "the market, so it's SELECTION-BIASED toward")
    lines.append("    consensus's worst calls — it overstates mis-calibration. "
                 "Read as strategy-edge, not pure calibration. ---")
    lines += _block("CLOSE-GAME slice", cg)
    lines += _block("COMBINED (biased upward by the close-game selection)", pairs)

    lines.append("\nCalibration curve (FAVORITE-HOLD slice — the unbiased one):")
    lines.append(f"  {'bucket':>12}  {'n':>3}  {'mean_pred':>9}  {'observed':>8}  gap")
    for lo, hi in [(0.55, 0.60), (0.60, 0.65), (0.65, 0.71)]:
        b = [(f, a) for f, a, _ in fav if lo <= f < hi]
        if not b:
            continue
        mp = sum(f for f, _ in b) / len(b)
        ob = sum(a for _, a in b) / len(b)
        lines.append(f"  [{lo:.2f},{hi:.2f}) {len(b):>4}  {mp:>9.3f}  {ob:>8.3f}  "
                     f"{ob - mp:+.3f}")

    lines.append("\nREAD: negative gap = consensus predicts higher than reality "
                 "(overconfident). The favorite-hold slice is the honest")
    lines.append("measure; the close-game slice confirms direction but is "
                 "inflated by selection. Small-sample — watch the trend.")
    lines.append("=" * 68)
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report + "\n")
    print(report)
    print(f"\n[written to {REPORT}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
