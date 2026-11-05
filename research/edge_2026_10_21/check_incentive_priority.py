#!/usr/bin/env python3
"""Read-only top incentive book audit under the preregistered priority rule."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from scan_incentives import get

OUT = Path(__file__).resolve().parent


def main() -> None:
    data = json.loads((OUT / "incentive_depth_screen.json").read_text())
    rows = [r for r in data["rows"] if r["two_sided_target_met"] and r["bound_dollars_period"] >= 2]
    response = get("/markets/orderbooks", {"tickers": [r["ticker"] for r in rows]})
    books = {b["ticker"]: b["orderbook_fp"] for b in response["orderbooks"]}
    result = []
    for row in rows:
        book = books.get(row["ticker"], {})
        sides = {}
        for side in ("yes", "no"):
            levels = book.get(side + "_dollars", [])
            # API arrays are ascending by price in observed responses; select
            # by numeric price explicitly instead of assuming array order.
            best = max(levels, key=lambda level: float(level[0])) if levels else None
            opposite = book.get(("no" if side == "yes" else "yes") + "_dollars", [])
            opposite_best = max(opposite, key=lambda level: float(level[0])) if opposite else None
            spread = 1 - float(best[0]) - float(opposite_best[0]) if best and opposite_best else None
            inside = spread is not None and spread >= .019999
            few_ahead = best is not None and float(best[1]) < row["target_size"] / 5
            sides[side] = {"best": best, "opposite_best": opposite_best,
                           "spread_dollars": spread, "inside_tick_available": inside,
                           "best_size_below_reference_target": few_ahead,
                           "plausibly_scorable": inside or few_ahead}
        ceiling = row["bound_dollars_period"] / 2
        result.append({**row, "sides": sides, "one_sided_ceiling_dollars_period": ceiling,
                       "passes": ceiling >= 1 and any(s["plausibly_scorable"] for s in sides.values())})
    passing = [r for r in result if r["passes"]]
    output = {"asof_utc": dt.datetime.now(dt.UTC).isoformat(),
              "screened_markets": len(result), "passing_markets": len(passing),
              "passing_events": len({r["event"] for r in passing}),
              "sum_ceiling_dollars_period": sum(r["one_sided_ceiling_dollars_period"] for r in passing),
              "rows": result}
    (OUT / "incentive_priority_screen.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({k: v for k, v in output.items() if k != "rows"}, indent=2))
    print(json.dumps([{"ticker": r["ticker"], "event": r["event"],
                       "ceiling": r["one_sided_ceiling_dollars_period"], "sides": r["sides"]}
                      for r in passing], indent=2))


if __name__ == "__main__":
    main()
