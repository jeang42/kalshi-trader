"""Local LLM forecasting via Ollama.

Why Ollama, not a paid API: the development environment has a dedicated
RTX 5000 Ada (32 GB VRAM) running Ollama at ``localhost:11434``. Local
inference is free, fast (sub-second to a few seconds per call depending
on model size), and private. The user can pull additional models on
demand.

This module is intentionally **un-opinionated about which model**. The
Edges page enumerates available models via :func:`list_models` and
exposes a selector — different models calibrate very differently, and
the only honest way to know which works is to backtest each.

The forecast contract is a JSON object::

    {
      "probability": float in [0, 1],
      "reasoning":   str (one or two short sentences)
    }

Ollama supports ``format: "json"`` natively which constrains the
response to valid JSON. We validate the shape on top of that.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from pm.core import circuit_breaker
from pm.feeds import llm_cache

log = logging.getLogger(__name__)

UTC = UTC

OLLAMA_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
HTTP_TIMEOUT = float(os.getenv("OLLAMA_HTTP_TIMEOUT", "120"))

_session = requests.Session()


# ---------------------------------------------------------------------------
# Forecast dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMForecast:
    """One LLM-derived probability estimate for a single market."""

    model: str
    probability: float
    reasoning: str


# ---------------------------------------------------------------------------
# Health / capability discovery
# ---------------------------------------------------------------------------

def is_available(timeout: float = 2.0) -> bool:
    """Quick health check — does an Ollama server respond at the
    configured base URL? Used by the UI to decide whether to render the
    LLM source at all.
    """
    try:
        r = _session.get(f"{OLLAMA_BASE}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def list_models() -> list[str]:
    """Names of all models currently pulled into the local Ollama
    instance. Returns ``[]`` if the server is unreachable.
    """
    try:
        r = _session.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Ollama list_models failed: %s", e)
        return []
    data = r.json()
    return [m["name"] for m in data.get("models", []) if "name" in m]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an independent forecaster estimating the \
probability that prediction-market questions resolve YES. Your output \
is a single JSON object with two keys:
  "probability": number in [0, 1] — P(this market resolves YES).
  "reasoning":   string under 240 chars — one or two short sentences.

Forecasting rules:
- Form your OWN estimate from what you know about the topic. Do not \
default to 0.5 out of laziness — commit to a direction when you have \
any information at all.
- Use base rates when available (e.g. MLB home teams win ~54%, \
incumbents win ~70% of elections).
- Avoid extreme probabilities (>0.95 or <0.05) unless the evidence is \
overwhelming.
- State your reasoning concisely — what is the key factor driving your \
estimate?"""


def build_user_prompt(
    *,
    question: str,
    category: str | None,
    market_yes_price: float | None,
    days_to_deadline: float | None,
    as_of: datetime | None = None,
) -> str:
    """User-message body for the forecasting prompt.

    ``market_yes_price`` is accepted for backward compatibility but
    intentionally NOT included in the prompt. Showing the market price
    caused small models to echo it back verbatim (zero signal). The
    forecast is blind — the comparison with the market price happens
    afterward in the strategy layer.
    """
    lines = [f"Question: {question}"]
    if category:
        lines.append(f"Category: {category}")
    if days_to_deadline is not None and days_to_deadline > 0:
        lines.append(f"Days to deadline: {days_to_deadline:.1f}")
    if as_of is not None:
        lines.append(f"As of: {as_of.isoformat()}")
    lines.append(
        "\nReturn JSON only: "
        '{"probability": <0..1>, "reasoning": "<one or two short sentences>"}'
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------

_PROB_KEY_PATTERN = re.compile(r'"probability"\s*:\s*(-?\d+(?:\.\d+)?)')


def _parse_response(raw: str, *, model: str) -> LLMForecast | None:
    """Robustly pull the (probability, reasoning) pair out of an LLM
    response. We try strict JSON first; if the model wrapped it in
    extra prose, fall back to a regex sniff for the probability key.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = _PROB_KEY_PATTERN.search(raw)
        if not m:
            return None
        try:
            p = float(m.group(1))
        except ValueError:
            return None
        return _validated(model=model, probability=p, reasoning=raw[:240])

    try:
        prob = float(obj["probability"])
    except (KeyError, TypeError, ValueError):
        return None
    reasoning = str(obj.get("reasoning", "")).strip()
    return _validated(model=model, probability=prob, reasoning=reasoning)


def _validated(*, model: str, probability: float, reasoning: str) -> LLMForecast | None:
    if not (0.0 <= probability <= 1.0):
        # The model gave us something outside [0, 1] — refuse rather
        # than coerce silently.
        log.warning("LLM %s returned probability %r outside [0,1]; dropping",
                    model, probability)
        return None
    return LLMForecast(model=model, probability=probability, reasoning=reasoning)


def get_forecast(
    *,
    question: str,
    model: str,
    category: str | None = None,
    market_yes_price: float | None = None,
    days_to_deadline: float | None = None,
    as_of: datetime | None = None,
    options: dict | None = None,
    bypass_cache: bool = False,
    cache_ttl_seconds: int = 7 * 86_400,
) -> LLMForecast | None:
    """Single-shot forecast for one market. Returns None on transport
    failure, model unavailability, or unparseable output.

    ``options`` is passed straight through to Ollama (``temperature``,
    ``num_ctx``, etc.). The default options keep the temperature low
    (0.2) for reproducibility — forecasting is a calibration problem,
    not a creativity problem.

    Persistent cache: by default we consult ``llm_cache`` first and
    short-circuit on a hit. The key includes ``question``, ``model``,
    ``category``, and quantized ``days_to_deadline`` (the parameters
    that actually enter the prompt). ``market_yes_price`` is NOT in
    the key or the prompt (v2) — the forecast is blind so the LLM
    can't anchor on the market price. ``as_of`` is also not in the
    key — it changes every call and the LLM doesn't use the absolute
    timestamp anyway. ``bypass_cache=True`` forces a fresh HTTP call;
    the result is still written back unless caching is disabled by
    the caller.
    """
    cache_key = llm_cache.make_key(
        "forecast/v2",
        model,
        question,
        category or "_",
        f"{days_to_deadline:.0f}" if days_to_deadline is not None else "_",
    )
    if not bypass_cache:
        cached = llm_cache.get(cache_key)
        if cached is not None:
            try:
                return LLMForecast(**cached)
            except TypeError:
                # Schema drift between cache entry and current dataclass:
                # treat as a miss and fall through to a fresh fetch.
                log.warning("llm_cache: forecast schema mismatch, refetching")

    user_prompt = build_user_prompt(
        question=question, category=category,
        market_yes_price=market_yes_price,
        days_to_deadline=days_to_deadline,
        as_of=as_of,
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2, **(options or {})},
    }
    def _post_chat() -> dict:
        r = _session.post(
            f"{OLLAMA_BASE}/api/chat", json=body, timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    try:
        # ``ollama`` circuit breaker (R-21): a stopped Ollama fails fast for
        # the cooldown instead of a full HTTP timeout per forecast.
        data = circuit_breaker.get("ollama").call(_post_chat)
    except requests.RequestException as e:
        log.warning("Ollama /api/chat failed for model=%s: %s", model, e)
        return None
    except ValueError as e:
        log.warning("Ollama returned non-JSON envelope: %s", e)
        return None

    content = data.get("message", {}).get("content", "")
    if not content:
        return None
    forecast = _parse_response(content, model=model)
    if forecast is not None:
        llm_cache.set(
            cache_key,
            {"model": forecast.model, "probability": forecast.probability,
             "reasoning": forecast.reasoning},
            ttl_seconds=cache_ttl_seconds,
        )
    return forecast


__all__ = [
    "LLMForecast",
    "is_available",
    "list_models",
    "build_user_prompt",
    "get_forecast",
    "SYSTEM_PROMPT",
]


if __name__ == "__main__":
    # Smoke when run directly.
    print("Ollama available:", is_available())
    print("Models:", list_models()[:5])
    if is_available():
        models = list_models()
        if models:
            # Pick a small / fast model if present
            preferred = next((m for m in models if "llama3.1" in m.lower()
                              or "mistral-small" in m.lower()), models[0])
            print(f"\nForecasting with {preferred}...")
            f = get_forecast(
                question="Will Bitcoin be above $100,000 on December 31, 2026?",
                model=preferred,
                category="Crypto",
                market_yes_price=0.65,
                days_to_deadline=230,
            )
            print(f)
