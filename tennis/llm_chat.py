"""Ollama-backed chat for the tennis sidecar.

Reuses the shared Ollama plumbing in ``llm_data`` and injects a tennis Elo
snapshot (top ATP/WTA ladders + today's model-vs-market edges). Mirrors
``wnba.llm_chat``. Env: ``TENNIS_CHAT_MODEL`` (default llama3.1:latest),
``TENNIS_CHAT_TIMEOUT`` (default 60s).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import requests

import tennis
from pm.feeds import llm_data

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("TENNIS_CHAT_MODEL", "llama3.1:latest")
DEFAULT_TIMEOUT = float(os.environ.get("TENNIS_CHAT_TIMEOUT", "60"))

_SYSTEM_PREAMBLE = (
    "You are a concise tennis analyst embedded in a prediction-market tool. "
    "You answer using the DATA SNAPSHOT below — Elo ratings (computed from "
    "this season's match results, whole field, both ATP and WTA) and the "
    "model's win-probabilities. Be specific and numeric. If asked about a "
    "player not in the snapshot, say so. The Elo model is coarse (single "
    "season, fixed K, hot young players can over-rate) — don't overstate "
    "confidence."
)


def list_available_models() -> list[str]:
    return llm_data.list_models()


def is_available() -> bool:
    return llm_data.is_available()


def build_system_prompt() -> str:
    parts = [_SYSTEM_PREAMBLE, ""]
    parts.append(f"DATA SNAPSHOT (as of {datetime.now(UTC).isoformat(timespec='minutes')} UTC).")
    for tour in ("atp", "wta"):
        parts.append("")
        parts.append(f"{tour.upper()} ELO — top 15:")
        try:
            for i, p in enumerate(tennis.ratings_table(tour, limit=15), 1):
                parts.append(f"  {i:2d}. {p['name']}  Elo {p['elo']:.0f}  "
                             f"({p['matches_played']} matches"
                             + (f", ATP/WTA #{p['rank']}" if p.get('rank') else "") + ")")
        except Exception:  # noqa: BLE001
            parts.append("  (unavailable)")
    return "\n".join(parts)


def chat(messages: list[dict[str, str]], *, model: str | None = None,
         temperature: float = 0.4, timeout: float | None = None) -> dict[str, Any]:
    chosen = model or DEFAULT_MODEL
    timeout_s = float(timeout) if timeout else DEFAULT_TIMEOUT
    if not is_available():
        return {"ok": False, "reply": "", "model": chosen,
                "error": "Ollama isn't reachable. Start it with `ollama serve` "
                "or check OLLAMA_API_BASE."}
    try:
        system_prompt = build_system_prompt()
    except Exception as e:  # noqa: BLE001
        log.warning("build_system_prompt failed: %s", e)
        system_prompt = _SYSTEM_PREAMBLE
    api_messages = [{"role": "system", "content": system_prompt}, *messages]
    try:
        r = llm_data._session.post(
            f"{llm_data.OLLAMA_BASE}/api/chat",
            json={"model": chosen, "messages": api_messages, "stream": False,
                  "options": {"temperature": temperature}},
            timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"ok": False, "reply": "", "model": chosen,
                "error": f"Ollama request failed: {e}"}
    reply = (data.get("message") or {}).get("content", "").strip()
    return {"ok": True, "reply": reply, "model": chosen, "error": None}
