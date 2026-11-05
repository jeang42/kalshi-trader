"""Example user strategy — bet YES whenever the market is pricing
something cheap, with a small assumed edge.

This is intentionally simplistic — its job is to demonstrate the
plugin API, not to make money. Copy this file, rename it, and edit
the function body to ship your own strategy. The decorator's
``key`` argument is what shows up in the URL / CSV exports; the
``display_name`` is what shows up in the UI dropdown.
"""

from __future__ import annotations

from pm.research import backtest as bt


@bt.register_strategy(
    key="example_buy_low_yes",
    display_name="Example: buy YES when < threshold (user plugin)",
)
def buy_low_yes(
    snap: bt.MarketSnapshot,
    *,
    threshold: float = 0.25,
    assumed_edge: float = 0.10,
) -> bt.Signal | None:
    """Bet YES at probability ``current_price + assumed_edge`` whenever
    the market YES price is below ``threshold``.

    Strategy parameters defined as keyword-only arguments with numeric
    defaults are auto-exposed as sliders on the Backtest page's
    "⚙️ Strategy parameters" expander — same mechanism the built-in
    strategies use.
    """
    p = snap.latest_price
    if p is None or p >= threshold:
        return None
    return bt.Signal(
        direction="YES",
        probability=min(0.99, p + assumed_edge),
    )
