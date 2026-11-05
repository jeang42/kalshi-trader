"""LLM-assisted semantic matching for free-text question pairs.

Built to fix a concrete bug: ``manifold_data.jaccard_similarity`` is
direction-blind, so "Will the Fed CUT rates?" and "Will the Fed HIKE
rates?" return a strong-looking match (5 of 6 content tokens overlap)
despite asking semantic opposites. The Edges-page advisory warning
tells users to eyeball every flag — this module is the tool that lets
them outsource that eyeballing to a local LLM.

Design rationale:

- **Verification, not search.** This module does NOT search Manifold
  / Metaculus / etc. for matches; it takes two pre-selected questions
  and decides whether they ask the same thing. The cheap-Jaccard
  pre-filter still runs to narrow 50+ candidates down to 1–3; the LLM
  only judges the survivors. That keeps LLM call count bounded by the
  number of *plausible* matches, not the candidate-set size.

- **Reuses the local Ollama setup.** Pulls models via ``llm_data.list_
  models`` and forecasts via the same JSON-constrained ``/api/chat``
  endpoint. No new auth, no new infrastructure.

- **Strict-by-default judging.** The system prompt biases toward
  "different question" when in doubt — false positives (saying two
  questions match when they don't) propagate into spurious edges
  upstream. False negatives just mean a real match gets skipped,
  which is recoverable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import requests

from pm.feeds import llm_cache, llm_data

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchVerdict:
    """One LLM judgment on whether two questions are the same."""

    same_question: bool
    confidence: float       # 0..1, the LLM's self-reported confidence
    reasoning: str          # one short sentence
    model: str


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a question-matching judge for prediction \
markets. Given two questions, decide whether they ask the same thing \
in a way that means a YES on one implies a YES on the other.

Output a single JSON object:
  {"same_question": <true|false>, "confidence": <0..1>, "reasoning": "<short>"}

Strictness guidelines (false positives are worse than false negatives):

- "Will X CUT rates" vs "Will X HIKE rates" → DIFFERENT (semantic opposites — direction-blind keyword overlap is the failure mode this judge exists to catch)
- "by July" vs "by August" → DIFFERENT (different deadlines)
- "above $200k" vs "above $250k" → DIFFERENT (different thresholds)
- "Will Trump be impeached" vs "Will Trump be impeached by the House" → SAME (more specific mechanism but same underlying event)
- Identical or near-identical wording → SAME
- Different sub-events under the same broader topic → DIFFERENT

When in doubt, say DIFFERENT. Keep reasoning under 200 characters."""


def _build_user_prompt(a: str, b: str) -> str:
    return (
        f"Question A: {a}\n"
        f"Question B: {b}\n\n"
        'Return JSON only: {"same_question": <bool>, "confidence": <0..1>, "reasoning": "<one short sentence>"}'
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_SAME_KEY_PATTERN = re.compile(
    r'"same_question"\s*:\s*(true|false)', re.IGNORECASE,
)
_CONF_KEY_PATTERN = re.compile(
    r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)',
)


def _parse_verdict(raw: str, *, model: str) -> MatchVerdict | None:
    """Pull (same_question, confidence, reasoning) out of an LLM reply.
    Tries strict JSON first; falls back to regex sniffing for the two
    boolean/numeric keys when the model wrapped its answer in prose.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        same_m = _SAME_KEY_PATTERN.search(raw)
        if same_m is None:
            return None
        same = same_m.group(1).lower() == "true"
        conf_m = _CONF_KEY_PATTERN.search(raw)
        try:
            conf = float(conf_m.group(1)) if conf_m else 0.5
        except ValueError:
            conf = 0.5
        return _validated(model=model, same=same, conf=conf,
                          reasoning=raw[:200])

    same = obj.get("same_question")
    if not isinstance(same, bool):
        return None
    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    reasoning = str(obj.get("reasoning", "")).strip()
    return _validated(model=model, same=same, conf=conf, reasoning=reasoning)


def _validated(*, model: str, same: bool, conf: float, reasoning: str
                ) -> MatchVerdict:
    # Clamp confidence to [0, 1]; out-of-range values are treated as
    # noise rather than rejected outright (small LLMs sometimes emit
    # 1.1 or -0.05; the verdict is still usable).
    conf = max(0.0, min(1.0, conf))
    return MatchVerdict(
        same_question=bool(same), confidence=conf, reasoning=reasoning,
        model=model,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_match(
    venue_question: str,
    candidate_question: str,
    *,
    model: str = "llama3.1:latest",
    options: dict | None = None,
    bypass_cache: bool = False,
    cache_ttl_seconds: int = 30 * 86_400,
) -> MatchVerdict | None:
    """Ask the LLM whether two questions ask the same thing. Returns
    None on transport failure / model unavailability / unparseable
    output — caller decides whether to fall back to keyword similarity
    or reject the match outright.

    Latency is dominated by the model: ~0.5–1.5s for a 7-13B model on
    the dev box, several seconds for a 30B+ model. Caller should bound
    how many candidates it verifies per scan.

    Persistent cache: by default we consult ``llm_cache`` first and
    short-circuit on a hit. The cache key sorts the two questions so
    ``verify_match(A, B)`` and ``verify_match(B, A)`` share the same
    entry — matching is symmetric. ``bypass_cache=True`` forces a
    fresh HTTP call; the result is still written back to the cache.
    """
    if not venue_question.strip() or not candidate_question.strip():
        return None

    # Symmetric: order-invariant cache key.
    canon_pair = sorted([venue_question.strip(), candidate_question.strip()])
    cache_key = llm_cache.make_key("match/v1", model, canon_pair[0], canon_pair[1])
    if not bypass_cache:
        cached = llm_cache.get(cache_key)
        if cached is not None:
            try:
                return MatchVerdict(**cached)
            except TypeError:
                log.warning("llm_cache: match schema mismatch, refetching")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(
                venue_question, candidate_question,
            )},
        ],
        "stream": False,
        "format": "json",
        # Low temperature: matching is a discrimination problem, not
        # a creativity problem. Allow caller to override if they're
        # experimenting with calibration.
        "options": {"temperature": 0.1, **(options or {})},
    }
    try:
        r = llm_data._session.post(
            f"{llm_data.OLLAMA_BASE}/api/chat", json=body,
            timeout=llm_data.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("verify_match HTTP failed model=%s: %s", model, e)
        return None
    except ValueError:
        return None

    content = (data.get("message") or {}).get("content", "")
    if not content:
        return None
    verdict = _parse_verdict(content, model=model)
    if verdict is not None:
        llm_cache.set(
            cache_key,
            {"same_question": verdict.same_question,
             "confidence": verdict.confidence,
             "reasoning": verdict.reasoning,
             "model": verdict.model},
            ttl_seconds=cache_ttl_seconds,
        )
    return verdict


__all__ = [
    "MatchVerdict",
    "verify_match",
    "SYSTEM_PROMPT",
]


if __name__ == "__main__":
    # Smoke when run directly.
    pairs = [
        ("Will the Fed cut rates in May 2026?",
         "Will the Federal Reserve hike interest rates in 2026?"),
        ("Will Trump be impeached in 2026?",
         "Will Trump be impeached by the House in 2026?"),
        ("Will Bitcoin reach $200k by 2027?",
         "Will Ethereum reach $10k by 2027?"),
    ]
    for a, b in pairs:
        v = verify_match(a, b, model="llama3.1:latest")
        print(f"\nA: {a}\nB: {b}")
        print(f"  {v}")
