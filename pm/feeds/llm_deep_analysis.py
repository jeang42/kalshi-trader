"""Deep-analysis LLM pipeline (Phase A — local context only).

Different role from ``llm_data.get_forecast``:

- ``llm_data`` does fast batch single-prompt probability extraction.
  One short prompt, one JSON number out, ~1 sec per call on a small
  model. Used by the Edges / Recommendations pages.
- This module does slow per-market reasoning. Many context fields
  packed into one long prompt, structured multi-field JSON out,
  minutes per call on a 70B model. Used by the 🧠 Deep Analysis page.

Phase A gathers context exclusively from the active market provider
(price history, market metadata, order book, sibling sub-markets).
Phase B will add web-fetched context (news, Wikipedia) — deliberately
deferred so we can ship a working honest baseline first.

Calibration tracks ``llm_deep:<model>`` separately from the fast
``llm:<model>`` source so the two have independent Brier tracks.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import requests

from pm.feeds import llm_cache, llm_data

log = logging.getLogger(__name__)

# Populated on the most recent failure inside analyze_market so the UI
# can render the actual server response instead of silently failing.
# Read via ``last_error()``.
_last_error: str = ""


def last_error() -> str:
    """Most recent error message from a failed ``analyze_market`` call,
    or empty string if the last call succeeded. Reset by every new
    successful run."""
    return _last_error


# Substring tokens that almost never indicate a chat-completion model:
# embed/vision-only/captioner/coder backends often return 500 when
# asked for `format: "json"` chat output.
_NON_CHAT_TOKENS = (
    "embed", "vision", "captioner", "image",
    "moondream", "minicpm-v", "llava",
)


def is_likely_chat_model(name: str) -> bool:
    """Heuristic filter — drops embedding / vision / captioner models
    from the picker. Conservative: when in doubt, allow."""
    n = name.lower()
    return not any(tok in n for tok in _NON_CHAT_TOKENS)


# Models we've found work well for the deep-analysis JSON task on the
# owner's hardware, in priority order. The picker pins these to the
# top of the list (when installed) and defaults to the first one
# that's actually present. Override via env if your local set differs.
RECOMMENDED_MODELS: tuple[str, ...] = (
    "qwen3:32b",          # default — fastest 32B-class with strong quality
    "qwen2.5:32b",        # solid fallback
    "deepseek-r1:32b",    # explicit-reasoning model, longer outputs
    "gemma3:27b",         # similar tier, different family
    "qwen2.5:14b",        # faster, still capable
    "llama3.1:latest",    # cheapest reliable baseline
)
# qwen3.5:35b was previously in this list and the nightly cron but
# was dropped 2026-05-22 — it ran 6.6× slower than qwen3:32b with
# a 20% per-call failure rate and no measurable quality benefit
# (median |dev from consensus| 4.8pp vs qwen3's 3.5pp on overlap data).


def sort_models_recommended_first(installed: list[str]) -> list[str]:
    """Reorder ``installed`` so RECOMMENDED_MODELS that are present
    come first (in recommended order), then everything else in its
    original order. Pure / no side effects."""
    installed_set = set(installed)
    head = [m for m in RECOMMENDED_MODELS if m in installed_set]
    seen = set(head)
    tail = [m for m in installed if m not in seen]
    return head + tail


def default_model(installed: list[str]) -> str | None:
    """First recommended model that's actually installed, falling back
    to the first installed model overall, or None if the list is empty."""
    for m in RECOMMENDED_MODELS:
        if m in installed:
            return m
    return installed[0] if installed else None


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class DeepAnalysis:
    """Structured output of a single deep-analysis run."""

    model: str
    market_id: str
    question: str
    as_of: str

    # The forecast itself. ``probability`` is YES; mirror to NO via 1-p.
    probability: float
    confidence: str  # "low" | "medium" | "high"

    # Reasoning artifacts — what the model claims about the market.
    # Validated client-side as lists of strings only (the prompt
    # constrains the model to those types).
    key_drivers: list[str] = field(default_factory=list)
    key_risks: list[str] = field(default_factory=list)
    evidence_used: list[str] = field(default_factory=list)

    recommended_action: str = "WAIT"   # "BUY_YES" | "BUY_NO" | "WAIT"
    reasoning: str = ""

    # Raw context blob the model saw — saved so calibration audit
    # can verify what the analysis was actually grounded in.
    context_summary: str = ""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a calibrated forecaster analysing a single
binary prediction market. Your job is to estimate the probability that
this market resolves YES.

Strict rules:
1. Ground every claim in CONTEXT fields. Do not invent facts.
2. If the CONTEXT includes RECENT NEWS or WIKIPEDIA SUMMARY, prioritize
   them — they are real-world signal. Cite the specific headline or
   passage in evidence_used.
3. If you don't have enough information, output low confidence — never
   bluff certainty. A wrong confident forecast hurts Brier worse than
   a hedged honest one.
4. Resolution criteria are authoritative. If the criteria are unclear,
   say so in key_risks and lower confidence.
5. The market price is informative but not authoritative. A 35% market
   reflects what traders think; your job is to estimate the actual
   probability. They are often close; sometimes the market is wrong.
6. Output ONLY valid JSON matching the schema. No prose around it.

Schema:
{
  "probability_yes": <float 0..1>,
  "confidence": "low" | "medium" | "high",
  "key_drivers": [<short strings, max 5>],
  "key_risks": [<short strings, max 5>],
  "evidence_used": [<which CONTEXT fields you actually relied on, max 5>],
  "recommended_action": "BUY_YES" | "BUY_NO" | "WAIT",
  "reasoning": <one paragraph, max 600 chars>
}
"""


def _baseball_block(ticker: str) -> str:
    """For Kalshi MLB tickers, return a small block summarizing the
    baseball sidecar's prediction. Empty string otherwise.

    Gives the LLM a quantitative anchor — it shouldn't dramatically
    disagree with a transparent stats model without articulating *why*.
    The model isn't authoritative (Brier baseline still unproven), but
    in the prompt it acts as a sanity check the LLM has to argue against.
    """
    try:
        import baseball as _baseball
    except ImportError:
        return ""
    try:
        bb = _baseball.predict_for_ticker(ticker)
    except Exception:  # noqa: BLE001
        return ""
    if bb is None:
        return ""
    return (
        f"  Baseball sidecar v0.1 model (regressed Pythagorean + SP-ERA):\n"
        f"    P({bb['yes_team']} wins) = {bb['yes_prob']*100:.1f}%  "
        f"({bb['source']} prediction)\n"
        f"    Game: {bb['away']} @ {bb['home']} on {bb['game_date']}; "
        f"YES side = {bb['yes_side']} team\n"
        f"  NOTE: this model is unvalidated — treat as one signal, not "
        f"authoritative. If your reasoning differs by more than ~10pp, "
        f"explain why in your output."
    )


def _format_context(*, ticker: str, question: str, category: str,
                     days_to_close: float | None,
                     market_detail: dict,
                     yes_bid: float | None, yes_ask: float | None,
                     last_trade: float | None,
                     price_history_summary: str,
                     order_book_summary: str,
                     sibling_summary: str,
                     news_block: str = "",
                     wiki_block: str = "") -> str:
    """Pack every available context source into one prompt string.

    Two date fields land in the prompt: ``TODAY`` (the model's anchor
    point for any "how long until X?" reasoning) and ``EXPECTED CLOSE
    DATE`` (Kalshi's `expected_expiration_time` when set, falling back
    to `close_time`). The model otherwise has no way to know that
    "168 days" might actually be "1 day" when the venue resolves early
    on its own trigger condition.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    expected = (market_detail.get("expected_expiration_time") or "").strip()
    close_time = (market_detail.get("close_time") or "").strip()
    early_cond = (market_detail.get("early_close_condition") or "").strip()
    can_close_early = bool(market_detail.get("can_close_early", False))

    # Build a date-section that's clear about which is which.
    date_lines = [f"  TODAY: {today}"]
    if expected:
        date_lines.append(f"  EXPECTED RESOLUTION (early-close): {expected[:10]}")
    if close_time and close_time != expected:
        date_lines.append(f"  LATEST POSSIBLE CLOSE: {close_time[:10]}")
    if days_to_close is not None:
        date_lines.append(f"  DAYS UNTIL EXPECTED RESOLUTION: {days_to_close:.1f}")
    if can_close_early and early_cond:
        date_lines.append(f"  EARLY-CLOSE TRIGGER: {early_cond}")

    parts = [
        f"TICKER: {ticker}",
        f"QUESTION: {question}",
        f"CATEGORY: {category or '(unknown)'}",
        "",
        "TIMING:",
        *date_lines,
        "",
        "CURRENT PRICING:",
        f"  YES bid:   {yes_bid:.3f}" if yes_bid is not None else "  YES bid:   (none)",
        f"  YES ask:   {yes_ask:.3f}" if yes_ask is not None else "  YES ask:   (none)",
        f"  Last:      {last_trade:.3f}" if last_trade is not None else "  Last:      (none)",
        "",
        "RESOLUTION CRITERIA:",
        f"  {market_detail.get('rules_primary', '') or market_detail.get('subtitle', '') or '(none provided)'}",
        "",
        "PRICE HISTORY (last 90 days):",
        f"  {price_history_summary}",
        "",
        "ORDER BOOK:",
        f"  {order_book_summary}",
        "",
        "SIBLING SUB-MARKETS (same event, when applicable):",
        f"  {sibling_summary}",
    ]
    if news_block:
        parts.extend([
            "",
            "RECENT NEWS (from Google News, sorted recent-first):",
            news_block,
        ])
    if wiki_block:
        parts.extend([
            "",
            "WIKIPEDIA SUMMARY:",
            wiki_block,
        ])
    bb_block = _baseball_block(ticker)
    if bb_block:
        parts.extend([
            "",
            "DOMAIN MODEL (from local baseball sidecar):",
            bb_block,
        ])
    return "\n".join(parts)


def _summarize_price_history(df: pd.DataFrame) -> str:
    """One-sentence summary of price-history movement."""
    if df is None or df.empty or "price" not in df.columns:
        return "(no history available)"
    first = float(df["price"].iloc[0])
    last = float(df["price"].iloc[-1])
    mn = float(df["price"].min())
    mx = float(df["price"].max())
    std = float(df["price"].std()) if len(df) > 1 else 0.0
    return (f"started {first:.2f}, ended {last:.2f} (Δ={last-first:+.2f}); "
            f"range [{mn:.2f}, {mx:.2f}], stdev {std:.3f}, {len(df)} samples")


def _summarize_order_book(book_df: pd.DataFrame) -> str:
    if book_df is None or book_df.empty:
        return "(empty / no liquidity)"
    bids = book_df[book_df["side"] == "bid"]
    asks = book_df[book_df["side"] == "ask"]
    if bids.empty or asks.empty:
        return f"one-sided book ({len(bids)} bids, {len(asks)} asks)"
    best_bid = float(bids["price"].max())
    best_ask = float(asks["price"].min())
    bid_size = float(bids["size"].sum())
    ask_size = float(asks["size"].sum())
    return (f"best bid {best_bid:.3f} (depth ${bid_size:,.0f}), "
            f"best ask {best_ask:.3f} (depth ${ask_size:,.0f}), "
            f"spread {best_ask - best_bid:.3f}")


def _summarize_siblings(df_siblings: pd.DataFrame, this_ticker: str) -> str:
    if df_siblings is None or df_siblings.empty:
        return "(no sibling markets)"
    sibs = df_siblings[df_siblings["condition_id"] != this_ticker]
    if sibs.empty:
        return "(no siblings besides this one)"
    rows = []
    for _, r in sibs.head(8).iterrows():
        q = str(r.get("question", ""))[:80]
        prices = r.get("outcome_prices") or []
        yes_p = float(prices[0]) if prices else None
        rows.append(f"    {q} → YES={yes_p:.2f}" if yes_p is not None else q)
    return "\n".join(rows) if rows else "(none)"


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

def _gather_context(provider: Any, ticker: str,
                     df_markets: pd.DataFrame) -> dict[str, Any]:
    """Collect every piece of local context the model will see for
    this market. Each fetch is wrapped so a single network blip doesn't
    abort the whole analysis — we'd rather analyse with a partial
    context blob than fail outright.
    """
    market_row = df_markets[df_markets["condition_id"] == ticker]
    row = market_row.iloc[0] if not market_row.empty else None

    question = str(row["question"]) if row is not None else ticker
    category = str(row.get("category", "")) if row is not None else ""
    days_to_close = (float(row["days_to_resolution"])
                     if row is not None and pd.notna(row.get("days_to_resolution"))
                     else None)
    yes_bid = float(row["best_bid"]) if row is not None and pd.notna(row.get("best_bid")) else None
    yes_ask = float(row["best_ask"]) if row is not None and pd.notna(row.get("best_ask")) else None
    last_trade = (float(row["last_trade_price"])
                  if row is not None and pd.notna(row.get("last_trade_price"))
                  else None)

    market_detail: dict[str, Any] = {}
    try:
        market_detail = provider.get_market(ticker) or {}
    except Exception as e:
        log.warning("deep_analysis: get_market(%s) failed: %s", ticker, e)

    # Price history — best-effort; markets that haven't traded much
    # yet may return empty.
    df_hist = pd.DataFrame()
    try:
        df_hist = provider.get_price_history(ticker, days=90, fidelity=1440)
    except Exception as e:
        log.warning("deep_analysis: price_history(%s) failed: %s", ticker, e)

    # Order book
    book_df = pd.DataFrame()
    try:
        from pm.venue.polymarket_client import parse_order_book
        raw_book = provider.get_order_book(ticker)
        if raw_book:
            book_df = parse_order_book(raw_book)
    except Exception as e:
        log.warning("deep_analysis: order_book(%s) failed: %s", ticker, e)

    # Sibling markets (same event_id)
    df_siblings = pd.DataFrame()
    event_id = str(row.get("event_id", "")) if row is not None else ""
    if event_id and "event_id" in df_markets.columns:
        df_siblings = df_markets[df_markets["event_id"] == event_id]

    return {
        "ticker": ticker, "question": question, "category": category,
        "days_to_close": days_to_close,
        "yes_bid": yes_bid, "yes_ask": yes_ask, "last_trade": last_trade,
        "market_detail": market_detail,
        "price_history_summary": _summarize_price_history(df_hist),
        "order_book_summary": _summarize_order_book(book_df),
        "sibling_summary": _summarize_siblings(df_siblings, ticker),
    }


# ---------------------------------------------------------------------------
# Parse the model's JSON
# ---------------------------------------------------------------------------

def _parse_response(content: str, *, model: str, ticker: str,
                     question: str, context_summary: str) -> DeepAnalysis | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        log.warning("deep_analysis: model returned non-JSON: %s", e)
        return None
    try:
        prob = float(data["probability_yes"])
        if not (0.0 <= prob <= 1.0):
            log.warning("deep_analysis: probability out of [0,1]: %s", prob)
            return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("deep_analysis: bad probability_yes: %s", e)
        return None

    def _coerce_strlist(v: Any, max_n: int = 5) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x)[:200] for x in v[:max_n]]

    conf = str(data.get("confidence", "low")).lower()
    if conf not in ("low", "medium", "high"):
        conf = "low"
    action = str(data.get("recommended_action", "WAIT")).upper()
    if action not in ("BUY_YES", "BUY_NO", "WAIT"):
        action = "WAIT"

    return DeepAnalysis(
        model=model, market_id=ticker, question=question,
        as_of=datetime.now(UTC).isoformat(),
        probability=prob, confidence=conf,
        key_drivers=_coerce_strlist(data.get("key_drivers")),
        key_risks=_coerce_strlist(data.get("key_risks")),
        evidence_used=_coerce_strlist(data.get("evidence_used")),
        recommended_action=action,
        reasoning=str(data.get("reasoning", ""))[:1500],
        context_summary=context_summary,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _wiki_entity_from_question(question: str) -> str | None:
    """Best-effort: pluck a likely Wikipedia article title from a market
    question. Uses ``yes_sub_title``-style suffix when present (the bit
    after the em-dash in our composed questions — usually a candidate
    name). Returns None if nothing useful is found.

    Cheap heuristic; the right long-term answer is an LLM-driven entity
    extraction pass, but that's a separate Phase B refinement.
    """
    if " — " in question:
        return question.split(" — ", 1)[1].strip()
    return None


def analyze_market(
    *,
    provider: Any,
    df_markets: pd.DataFrame,
    ticker: str,
    model: str = "llama3.1:latest",
    bypass_cache: bool = False,
    cache_ttl_seconds: int = 24 * 3600,
    include_web_context: bool = False,
    max_news_items: int = 5,
) -> DeepAnalysis | None:
    """Run a deep analysis on one market. Returns ``None`` on transport
    failure or unparseable model output.

    Cached for ``cache_ttl_seconds`` (default 24h) on (ticker, model,
    web-context-on/off). Set ``bypass_cache=True`` to force a fresh
    run. ``include_web_context=True`` adds Google News headlines and a
    Wikipedia summary for the most-likely entity in the question.
    """
    ctx = _gather_context(provider, ticker, df_markets)

    news_block = ""
    wiki_block = ""
    if include_web_context:
        # Defer the import so the module is importable on machines
        # without the web_context module installed.
        from pm.feeds import web_context as wc
        news = wc.search_news(ctx["question"], max_items=max_news_items)
        news_block = wc.format_news_for_prompt(news, max_items=max_news_items)
        entity = _wiki_entity_from_question(ctx["question"])
        if entity:
            wiki = wc.wikipedia_summary(entity)
            wiki_block = wc.format_wiki_for_prompt(wiki)

    context_summary = _format_context(
        ticker=ctx["ticker"], question=ctx["question"],
        category=ctx["category"], days_to_close=ctx["days_to_close"],
        market_detail=ctx["market_detail"],
        yes_bid=ctx["yes_bid"], yes_ask=ctx["yes_ask"],
        last_trade=ctx["last_trade"],
        price_history_summary=ctx["price_history_summary"],
        order_book_summary=ctx["order_book_summary"],
        sibling_summary=ctx["sibling_summary"],
        news_block=news_block,
        wiki_block=wiki_block,
    )

    cache_key = llm_cache.make_key(
        "deep_analysis/v2", model, ticker,
        # Context-hash so a refreshed context (book moved, price
        # history changed) busts the cache. Bumped to v2 when web
        # context was added — old cache rows have no news/wiki
        # blocks and shouldn't be reused as if they did.
        f"ctx={hashlib.sha256(context_summary.encode()).hexdigest()[:16]}",
        f"web={1 if include_web_context else 0}",
    )
    if not bypass_cache:
        cached = llm_cache.get(cache_key)
        if cached is not None:
            try:
                return DeepAnalysis(**cached)
            except TypeError:
                log.warning("deep_analysis cache schema mismatch — refetching")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"CONTEXT:\n{context_summary}\n\n"
                "Produce ONLY the JSON object specified in the schema. "
                "No prose."},
        ],
        "stream": False,
        "format": "json",
        # Don't override num_ctx — let each model use its own default.
        # Setting num_ctx=8192 on a model whose default is smaller is
        # the most common cause of 500s from /api/chat. The prompt
        # itself is comfortably under 4k tokens for Phase A.
        "options": {"temperature": 0.2},
    }
    try:
        r = llm_data._session.post(
            f"{llm_data.OLLAMA_BASE}/api/chat",
            json=body, timeout=llm_data.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        # Capture the response body too — Ollama puts the actual error
        # (model loading failed, format=json unsupported, OOM) in the
        # body, which str(e) doesn't include.
        body_text = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                body_text = e.response.text[:500]
            except Exception:
                pass
        if body_text:
            log.warning("deep_analysis Ollama call failed: %s — body: %s",
                        e, body_text)
        else:
            log.warning("deep_analysis Ollama call failed: %s", e)
        # Stash the error on a module attr so the UI can read it after
        # an analyze_market returns None. Lightweight; not thread-safe
        # but the UI is single-threaded.
        global _last_error
        _last_error = f"{e}: {body_text}" if body_text else str(e)
        return None

    content = data.get("message", {}).get("content", "")
    if not content:
        _last_error = "Ollama returned an empty message body."
        return None
    analysis = _parse_response(
        content, model=model, ticker=ticker,
        question=ctx["question"], context_summary=context_summary,
    )
    if analysis is None:
        _last_error = (
            f"Model returned content that didn't parse as the expected "
            f"JSON schema. Raw start: {content[:200]!r}"
        )
        return None
    _last_error = ""  # success path clears prior error
    # Store everything except context_summary in cache — the context
    # is regenerated on hit and we don't want to bloat the SQLite
    # file with multi-KB blobs per row.
    cache_payload = {
        k: v for k, v in analysis.__dict__.items()
        if k != "context_summary"
    }
    cache_payload["context_summary"] = ""
    llm_cache.set(cache_key, cache_payload,
                   ttl_seconds=cache_ttl_seconds)
    return analysis


__all__ = [
    "DeepAnalysis", "analyze_market", "SYSTEM_PROMPT",
    "last_error", "is_likely_chat_model",
    "RECOMMENDED_MODELS", "sort_models_recommended_first",
    "default_model",
]
