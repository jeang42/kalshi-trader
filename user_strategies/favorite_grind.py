"""Favorite-grinding strategy — bet whichever side is heavily favored.

The premise (and its honest failure mode):

A market priced YES=$0.95 implies a 95% probability. Betting YES at
$0.95 pays $1 if YES happens (+$0.05 per share = ~5% return) and $0
if it doesn't (–$0.95). At fair prices, EV is exactly $0. The
strategy only profits when the *true* probability is even higher
than the price — say the market says 95% but the underlying is 96%.

This is intentionally a "picks pennies in front of a steamroller"
strategy:
- **Volume is high**: it fires on most markets that are clearly
  favored on either side.
- **Per-trade margin is tiny**: 5% on a 95% favorite, 2% on a 98%.
- **One miss wipes ~19 wins**: lose $0.95 to win 19 × $0.05.

Whether it works empirically depends on whether the markets we trade
on are systematically under-confident in their favorites. Polymarket
in particular is known to be sometimes *over*-confident on near-certain
markets (anchoring at $0.99 on things that aren't quite there), in
which case this strategy bleeds. The agentic profile + the Calibration
page's Brier-by-source breakdown will tell us which regime we're in.

Suggested run-pattern: pair this with the `agentic_favorites` profile
via `strategy_runner.py` so its calibration history accrues separately
from the existing `agentic` profile's `market_consensus` run.
"""

from __future__ import annotations

from pm.research import backtest as bt


@bt.register_strategy(
    key="favorite_grind",
    display_name="Favorite grind (high-volume, low-margin)",
)
def favorite_grind(
    snap: bt.MarketSnapshot,
    *,
    threshold: float = 0.85,
    assumed_edge: float = 0.02,
) -> bt.Signal | None:
    """Bet whichever side is favored above ``threshold``, with a small
    assumed edge on top of the market price.

    - YES side: triggered when ``latest_price >= threshold``. We claim
      ``probability = market_yes + assumed_edge`` (clamped to 0.99).
    - NO side: triggered when ``latest_price <= 1 - threshold``. We
      claim YES is overpriced; effective NO probability is
      ``(1 − latest_price) + assumed_edge``, expressed back in YES
      terms as ``probability = latest_price − assumed_edge``.

    Defaults to ``threshold=0.85`` (only fires on clear favorites) and
    ``assumed_edge=0.02`` (tiny — anything larger and Kelly would size
    huge on cheap entries, which is exactly the failure mode the
    strategy archetype invites). Both are auto-exposed as Backtest-page
    sliders since they're keyword-only numeric params.
    """
    p = snap.latest_price
    if p is None:
        return None
    if p >= threshold:
        return bt.Signal(
            direction="YES",
            probability=min(0.99, p + assumed_edge),
        )
    if p <= (1.0 - threshold):
        # Cheap YES = expensive NO. Bet NO; in YES-side units our
        # probability is the market price minus a small edge.
        return bt.Signal(
            direction="NO",
            probability=max(0.01, p - assumed_edge),
        )
    return None
