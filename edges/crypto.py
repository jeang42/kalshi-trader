"""Crypto-threshold edges — parse "Will BTC reach $X by DATE?", price it
with a lognormal terminal / one-touch barrier model against Coinbase spot.

Split out of `edges.py` (P3/R-24, 2026-08-24). Re-exported by
`edges/__init__.py`, so every public name here is still reachable as
`edges.<name>` exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from pm.feeds import crypto_data
from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap


@dataclass(frozen=True)
class ParsedCryptoQuestion:
    asset: str           # canonical symbol, e.g. "BTC"
    strike_usd: float
    direction: str       # "above" | "below"
    deadline: datetime   # market deadline as a UTC datetime
    model_type: str = "barrier"   # "barrier" (one-touch) | "terminal" (close-on-date)


_ASSET_ALIASES: dict[str, list[str]] = {
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "ether", "eth"],
    "SOL":  ["solana", "sol"],
    "DOGE": ["dogecoin", "doge"],
    "XRP":  ["ripple", "xrp"],
    "ADA":  ["cardano", "ada"],
}


def _detect_asset(text_lower: str) -> str | None:
    # Match longest alias first so "bitcoin" beats a stray "btc".
    candidates: list[tuple[int, str]] = []
    for sym, aliases in _ASSET_ALIASES.items():
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", text_lower):
                candidates.append((len(a), sym))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _parse_amount(raw: str) -> float | None:
    """Parse ``$150,000``, ``150K``, ``1.5M`` into a USD float. Returns
    None when the string isn't a clean numeric magnitude."""
    s = raw.replace("$", "").replace(",", "").strip().upper()
    mult = 1.0
    if s.endswith("K"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000.0, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


# Match: <direction-word> <optional "$"> <number> <optional k/m/b>
#
# IGNORECASE is deliberately OFF here: with it on, ``[kmb]`` matches the
# leading ``b`` of ``by`` in "below $50,000 by July" — strike ends up
# parsed as 50 trillion. The caller lowercases the question before the
# search, and the trailing ``\b`` forces the suffix letter to actually
# stand alone as a magnitude rather than be the first letter of a word.
_AMOUNT_PATTERN = re.compile(
    r"(above|below|under|over|reach|hit|exceed|to|surpass|cross|top)\s+"
    r"\$?\s*([\d,]+(?:\.\d+)?(?:\s*[kmb])?)\b",
)

# Verbs that flip the direction when followed by "to": "dip to $X",
# "fall to $X", "drop to $X", "crash to $X" all mean "the price falls
# down to $X" — barrier-from-above, not barrier-from-below.
_DOWN_TO_PATTERN = re.compile(r"\b(dip|fall|drop|crash|sink|plunge)\s+to\b")

_ABOVE_VERBS = {"above", "over", "reach", "hit", "exceed",
                "surpass", "cross", "top"}

# Phrases that indicate a *terminal* (close-on-date) question rather
# than a one-touch barrier question. Examples:
#   "Will BTC **be above** $X **on** DATE?"
#   "Will BTC **close above** $X **on** DATE?"
# These settle on the closing price, not any-touch, so the barrier
# model overstates the YES probability when spot is already past strike.
_TERMINAL_PATTERN = re.compile(
    r"\b(be|close|finish|end|settle)\s+(above|below|under|over)\b",
)


def parse_crypto_question(
    question: str, *, deadline: datetime | None,
) -> ParsedCryptoQuestion | None:
    """Best-effort parser for crypto threshold questions. Returns None
    when the structure can't be confidently extracted.

    >>> from datetime import datetime, timezone
    >>> q = parse_crypto_question(
    ...     "Will Bitcoin reach $150,000 by August 1, 2026?",
    ...     deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
    ... )
    >>> q.asset, q.strike_usd, q.direction
    ('BTC', 150000.0, 'above')

    >>> # No deadline → None (we never invent one)
    >>> parse_crypto_question("Will BTC hit $200k?", deadline=None) is None
    True

    >>> # No matching asset → None
    >>> parse_crypto_question("Will the Fed cut rates?",
    ...     deadline=datetime(2026, 8, 1, tzinfo=timezone.utc)) is None
    True
    """
    if deadline is None:
        return None
    q_lower = question.lower()

    asset = _detect_asset(q_lower)
    if asset is None:
        return None

    m = _AMOUNT_PATTERN.search(q_lower)
    if not m:
        return None
    word, amount_raw = m.group(1).lower(), m.group(2)
    strike = _parse_amount(amount_raw)
    if strike is None or strike <= 0:
        return None

    if word in _ABOVE_VERBS:
        direction = "above"
    elif word == "to":
        # Bare "to" is direction-ambiguous; flip to "below" only when
        # paired with a falling verb ("dip to", "fall to", ...).
        direction = "below" if _DOWN_TO_PATTERN.search(q_lower) else "above"
    else:
        direction = "below"

    model_type = "terminal" if _TERMINAL_PATTERN.search(q_lower) else "barrier"

    return ParsedCryptoQuestion(
        asset=asset, strike_usd=strike, direction=direction, deadline=deadline,
        model_type=model_type,
    )


# ---------------------------------------------------------------------------
# Crypto lognormal strategy — also usable inside `backtest.run_backtest`
# ---------------------------------------------------------------------------

def _model_prob(
    *, spot: float, strike: float, direction: str, model_type: str,
    vol_annualized: float, days_to_expiry: float,
) -> float | None:
    """YES probability under the chosen model:

    - ``barrier`` (one-touch any-time): default for "reach"/"hit"/"dip to"
      questions where the market settles on whether spot ever touched
      the threshold during the window.
    - ``terminal`` (close-on-date): for "be above/below" or "close above/
      below" questions where the market settles on the spot price at
      the deadline.

    The barrier model overstates YES when spot is already past strike;
    the terminal model is the right tool for those questions.
    """
    if model_type == "terminal":
        if direction == "above":
            return crypto_data.lognormal_above(spot, strike, vol_annualized, days_to_expiry)
        return crypto_data.lognormal_below(spot, strike, vol_annualized, days_to_expiry)
    # default: barrier
    if direction == "above":
        return crypto_data.barrier_hit_above(spot, strike, vol_annualized, days_to_expiry)
    return crypto_data.barrier_hit_below(spot, strike, vol_annualized, days_to_expiry)


def strategy_crypto_spot_lognormal(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.10,
    vol_window_days: int = 30,
    min_days_to_expiry: float = 1.0,
) -> bt.Signal | None:
    """Bet when the lognormal model implies a probability that disagrees
    with the market by at least ``min_gap``.

    Registered in ``backtest.STRATEGIES`` so it shows up in the Backtest
    page alongside the in-process baselines. Strategies that depend on
    external HTTP are slower than the pure ones — keep that in mind when
    sizing the backtest.
    """
    if snap.end_date is None:
        return None
    parsed = parse_crypto_question(snap.question, deadline=snap.end_date)
    if parsed is None:
        return None

    spot = crypto_data.spot_at(parsed.asset, snap.as_of)
    if spot is None:
        return None
    vol = crypto_data.realized_volatility_at(
        parsed.asset, snap.as_of, window=vol_window_days,
    )
    if vol is None:
        return None

    days = (parsed.deadline - snap.as_of).total_seconds() / 86_400.0
    if days < min_days_to_expiry:
        # Too close to expiry — the model only sees future path, not
        # whether the barrier was already touched. Suppress to avoid
        # phantom edges on near-decided markets.
        return None
    model_p = _model_prob(
        spot=spot, strike=parsed.strike_usd, direction=parsed.direction,
        model_type=parsed.model_type,
        vol_annualized=vol, days_to_expiry=days,
    )
    if model_p is None:
        return None

    market_p = snap.latest_price
    if market_p is None:
        return None

    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO", probability=model_p)


# ---------------------------------------------------------------------------
# Live edge surfacing (Edges page)
# ---------------------------------------------------------------------------

def find_crypto_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.10,
    vol_window_days: int = 30,
    min_days_to_expiry: float = 1.0,
) -> list[Edge]:
    """Scan the active markets DataFrame for crypto-threshold questions
    where the lognormal model disagrees with the market by ``min_gap``
    or more. The output is ordered by ``|gap|`` descending.
    """
    if df_markets is None or df_markets.empty:
        return []

    now = datetime.now(UTC)
    edges: list[Edge] = []

    # Pull each asset's history once and pass it through the strategy
    # implicitly via the in-module HTTP caching — but for the live page
    # we want to surface every flagged market, not just the ones where
    # `strategy_crypto_spot_lognormal` would emit a Kelly-eligible signal.
    for _, row in df_markets.iterrows():
        snap = _row_to_snapshot(row, as_of=now)
        if snap.end_date is None:
            continue
        parsed = parse_crypto_question(snap.question, deadline=snap.end_date)
        if parsed is None:
            continue
        if parsed.deadline <= now:
            continue   # already past

        spot = crypto_data.get_spot_price(parsed.asset)
        if spot is None:
            continue
        klines = crypto_data.get_klines(
            parsed.asset, days=max(vol_window_days + 5, 60),
        )
        vol = crypto_data.realized_volatility(klines, window=vol_window_days)
        if vol is None:
            continue
        days = (parsed.deadline - now).total_seconds() / 86_400.0
        if days < min_days_to_expiry:
            continue
        model_p = _model_prob(
            spot=spot, strike=parsed.strike_usd, direction=parsed.direction,
            model_type=parsed.model_type,
            vol_annualized=vol, days_to_expiry=days,
        )
        if model_p is None:
            continue

        market_p = snap.latest_price
        if market_p is None:
            continue

        gap = model_p - market_p
        if abs(gap) < min_gap:
            continue

        verb = "close" if parsed.model_type == "terminal" else "touch"
        model_label = ("lognormal terminal" if parsed.model_type == "terminal"
                       else "GBM one-touch barrier")
        note = (
            f"{parsed.asset} spot ${spot:,.0f}; {verb} "
            f"${parsed.strike_usd:,.0f} from {parsed.direction}; "
            f"~{days:.0f}d to deadline; vol {vol*100:.0f}%/yr "
            f"({model_label})"
        )
        edges.append(Edge(
            source="crypto_spot",
            market_id=snap.condition_id,
            question=snap.question,
            category=snap.category,
            market_yes_price=float(market_p),
            model_implied=float(model_p),
            gap=float(gap),
            direction="YES" if gap > 0 else "NO",
            severity=_severity_for_gap(abs(gap)),
            note=note,
        ))

    edges.sort(key=lambda e: abs(e.gap), reverse=True)
    return edges


# ---------------------------------------------------------------------------
# LLM-as-forecaster source (Ollama, local)
# ---------------------------------------------------------------------------
#
# **Important caveat for backtesting**: most local models have a knowledge
# cutoff in the past, so scoring them on markets that resolved BEFORE
# that cutoff risks the model recalling actual outcomes from training
# rather than forecasting them. When using the registered backtest
# strategy, restrict the dataset to markets that closed after the
# model's training cutoff for honest results.
