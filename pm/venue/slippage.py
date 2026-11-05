"""Live order-book slippage estimation.

Why this module exists: every Tier-4 edge source advertises a gap
between a model probability and a market YES price. That gap assumes
you can fill at the top-of-book quote. In practice a stake of any
meaningful size walks the book — you lift multiple ask levels,
pay progressively worse prices, and the realized edge shrinks.

This module exposes pure DataFrame-in / dataclass-out helpers that
walk a parsed order book (the shape ``polymarket_client.parse_order_
book`` returns) and report what would actually fill at a chosen
stake. Callers decide *which* book to fetch and *which* side to walk
based on venue conventions:

  - Buying YES (any venue): walk **asks** of the YES token's book.
  - Buying NO on Polymarket: walk asks of the NO token's book (each
    binary market has separate YES and NO order books).
  - Buying NO on Kalshi: walk **bids** of the YES book — Kalshi has
    one token per market; selling YES is equivalent to buying NO.

The dataclass is frozen and JSON-friendly so the Edges page can
serialize estimates into the row notes and CSV downloads.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Effective slippage of zero relative to best price is the bound for a
# trivially-small fill that doesn't move the top of book. We surface
# slippage in absolute price units (cents per share) so it's directly
# comparable to the ``half_spread`` parameter the backtest engine
# already uses.

_EPSILON = 1e-9


@dataclass(frozen=True)
class SlippageEstimate:
    """What walking one side of an order book at ``requested_stake``
    would actually fill."""

    side: str                       # "ask" (buying) or "bid" (selling)
    requested_stake: float          # dollars the caller wanted to deploy
    fillable_stake: float           # dollars actually filled (≤ requested)
    shares_filled: float            # number of shares acquired (or sold)
    avg_fill_price: float           # weighted-average price per share
    best_price: float               # top-of-book reference at the chosen side
    slippage_per_share: float       # |avg_fill_price − best_price| (>= 0)
    levels_consumed: int            # number of book levels walked
    fully_filled: bool              # requested stake was fully filled


def walk_book_for_stake(
    book: pd.DataFrame, *, side: str, stake_dollars: float,
) -> SlippageEstimate | None:
    """Walk one side of a parsed order book accumulating cost (``side='ask'``)
    or proceeds (``side='bid'``) until ``stake_dollars`` is hit or the
    book is exhausted.

    Returns None when the requested side has no levels or when nothing
    fills (e.g. requested stake is 0 or all prices are degenerate).

    The ``book`` argument is the DataFrame from
    ``polymarket_client.parse_order_book`` — columns ``side`` /
    ``price`` / ``size`` / ``cumulative_size``. Inside this helper we
    re-sort by price to be robust against callers that pass an
    already-aggregated or differently-sorted frame.
    """
    if book is None or book.empty:
        return None
    if side not in ("ask", "bid"):
        return None

    levels = book[book["side"] == side].copy()
    if levels.empty:
        return None

    if side == "ask":
        # Cheapest offer first when buying.
        levels = levels.sort_values("price")
    else:
        # Highest bid first when selling.
        levels = levels.sort_values("price", ascending=False)

    remaining = float(max(0.0, stake_dollars))
    total_shares = 0.0
    total_value = 0.0
    levels_consumed = 0
    best_price = float(levels["price"].iloc[0])

    for _, level in levels.iterrows():
        price = float(level["price"])
        size = float(level["size"])
        if price <= 0 or size <= 0:
            continue
        if remaining < _EPSILON:
            break
        # How many shares the remaining dollars can lift at this level?
        # When buying (ask): cost = shares × price. We're capped by
        #   either the level's size or the dollar budget.
        # When selling (bid): proceeds = shares × price. Same cap — the
        #   caller is "spending" `stake_dollars` worth of YES shares to
        #   receive proceeds at the bid.
        affordable_shares = remaining / price
        shares_to_take = min(size, affordable_shares)
        if shares_to_take <= 0:
            break
        value = shares_to_take * price
        total_shares += shares_to_take
        total_value += value
        remaining -= value
        levels_consumed += 1

    if total_shares <= 0:
        return None

    avg_fill = total_value / total_shares
    fully_filled = remaining < 0.005   # within half a cent
    # Slippage is always non-negative regardless of side — it's the
    # absolute distance from top-of-book that you actually paid (asks)
    # or gave up (bids).
    slippage = abs(avg_fill - best_price)

    return SlippageEstimate(
        side=side,
        requested_stake=float(stake_dollars),
        fillable_stake=float(total_value),
        shares_filled=float(total_shares),
        avg_fill_price=float(avg_fill),
        best_price=float(best_price),
        slippage_per_share=float(slippage),
        levels_consumed=int(levels_consumed),
        fully_filled=bool(fully_filled),
    )


def effective_edge(
    model_probability: float, est: SlippageEstimate,
) -> float:
    """Edge after slippage, in absolute probability units, from the
    *buyer's* perspective. Positive means the buyer paid less per
    share than the strategy's model says is fair.

    For a YES bet (``est.side == 'ask'``):
        paid = avg_fill_price (a YES price)
        fair = model_probability (a YES price)
        edge = fair − paid = model_probability − avg_fill_price

    For a NO bet expressed via a YES-side ``bid`` walk
    (Kalshi-style NO trade, where buying NO means selling YES):
        YES sold at avg_fill_price → received (1 − avg_fill_price)
        worth of NO position per share.
        paid (per NO share) = 1 − avg_fill_price
        fair NO            = 1 − model_probability
        edge = fair − paid = (1 − model_probability) − (1 − avg_fill_price)
                          = avg_fill_price − model_probability

    ``model_probability`` is always in YES-side units; this helper
    handles the sign flip so positive edge consistently means "buyer
    got a good deal" regardless of direction.
    """
    if est.side == "ask":
        return model_probability - est.avg_fill_price
    # bid walk: edge formula simplifies to avg − model (NOT
    # model − avg). Buyer got a discount when avg_yes_price (what
    # they sold at) exceeds model_yes_price (what they thought YES
    # was worth) — because that means they effectively paid less
    # than model fair for the NO contract they acquired.
    return est.avg_fill_price - model_probability


__all__ = [
    "SlippageEstimate",
    "walk_book_for_stake",
    "effective_edge",
]
