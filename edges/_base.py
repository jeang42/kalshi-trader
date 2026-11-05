"""Shared primitives for every edge source.

Split out of the 2,175-line `edges.py` (P3/R-24, 2026-08-24). Everything here
is used by two or more source modules; nothing here knows about any particular
source. `edges/__init__.py` re-exports all of it, so `edges.Edge` and
`edges.edges_to_dataframe` keep working exactly as before.

`_row_to_snapshot` lived in the crypto section but is used by all eight
sources, so it belongs here — that is the only symbol that changed neighbours
in the split.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from pm.research import backtest as bt

log = logging.getLogger("edges")

UTC = UTC



@dataclass(frozen=True)
class Edge:
    """One flagged edge candidate from any external source.

    ``gap = model_implied - market_yes_price``. Positive = market is
    under-pricing YES; negative = over-pricing. ``severity`` is a coarse
    label ("low" | "medium" | "high") derived from |gap|.
    """

    source: str               # registry key, e.g. "crypto_spot"
    market_id: str
    question: str
    category: str
    market_yes_price: float
    model_implied: float
    gap: float
    direction: str            # "YES" | "NO" — the side the gap favors
    severity: str             # "low" | "medium" | "high"
    note: str                 # one-line human summary (asset/spot/strike/etc.)


def _severity_for_gap(gap_abs: float) -> str:
    if gap_abs >= 0.30:
        return "high"
    if gap_abs >= 0.15:
        return "medium"
    return "low"


def edges_to_dataframe(edges: list[Edge]) -> pd.DataFrame:
    """Tabular view for the Edges page. Empty DataFrame on no edges."""
    if not edges:
        return pd.DataFrame(columns=[
            "source", "category", "market_id", "question",
            "market_yes_price", "model_implied", "gap",
            "direction", "severity", "note",
        ])
    return pd.DataFrame([e.__dict__ for e in edges])


def _row_to_snapshot(row: pd.Series, *, as_of: datetime) -> bt.MarketSnapshot:
    """Build a single-point MarketSnapshot from a current markets row.
    Used by the live edge path — the snapshot's "price history" is the
    one current quote."""
    end_date = row.get("end_date")
    if isinstance(end_date, str):
        try:
            end_dt: datetime | None = datetime.fromisoformat(
                end_date.replace("Z", "+00:00"),
            )
        except ValueError:
            end_dt = None
    elif isinstance(end_date, datetime):
        end_dt = end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
    else:
        end_dt = None

    outcome_prices = row.get("outcome_prices") or []
    yes_price = float(outcome_prices[0]) if outcome_prices and outcome_prices[0] is not None else 0.5
    prices_df = pd.DataFrame({"timestamp": [as_of], "price": [yes_price]})
    return bt.MarketSnapshot(
        condition_id=str(row.get("condition_id", "")),
        question=str(row.get("question", "")),
        category=str(row.get("category", "") or ""),
        as_of=as_of,
        prices=prices_df,
        end_date=end_dt,
    )


