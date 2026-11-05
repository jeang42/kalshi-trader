"""Ollama-backed chat for the WNBA sidecar.

Reuses the shared Ollama plumbing in ``llm_data`` (session + base URL +
health) and injects a WNBA state snapshot (team ratings + today's games +
model win-probs) as the system prompt. Same shape as ``baseball.llm_chat``
but trimmed — WNBA has no play-by-play / pitcher data to summarize.

No paid API — local GPU via Ollama. Env: ``WNBA_CHAT_MODEL`` (default
``llama3.1:latest``), ``WNBA_CHAT_TIMEOUT`` (default 60s).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import requests

import wnba
from pm.feeds import llm_data

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("WNBA_CHAT_MODEL", "llama3.1:latest")
DEFAULT_TIMEOUT = float(os.environ.get("WNBA_CHAT_TIMEOUT", "60"))

_SYSTEM_PREAMBLE = (
    "You are a concise WNBA analyst embedded in a prediction-market tool. "
    "You answer using the DATA SNAPSHOT below — model win-probabilities from "
    "a regressed basketball-Pythagorean model, team strength ratings, and "
    "today's slate. Be specific and numeric. If asked about something not in "
    "the snapshot, say so rather than guessing. The model is coarse (early in "
    "the season, small samples) — don't overstate confidence."
)


def list_available_models() -> list[str]:
    return llm_data.list_models()


def is_available() -> bool:
    return llm_data.is_available()


def build_system_prompt() -> str:
    """Compose the system prompt with a fresh WNBA state snapshot."""
    parts = [_SYSTEM_PREAMBLE, ""]
    now_local = datetime.now(UTC).isoformat(timespec="minutes")
    parts.append(f"DATA SNAPSHOT (as of {now_local} UTC).")
    parts.append("")

    try:
        ratings = wnba.team_ratings_table()
    except Exception:  # noqa: BLE001
        ratings = []
    parts.append("TEAM-STRENGTH RANKINGS (regressed basketball-Pythagorean):")
    for i, t in enumerate(ratings, 1):
        parts.append(
            f"  {i:2d}. {t['abbreviation']:4s} {t.get('name','')}  "
            f"{t['wins']}-{t['losses']}  strength={t['strength']:.3f}  "
            f"ptdiff/g={t['point_diff_per_game']:+.1f}")
    parts.append("")

    try:
        games = wnba.today_schedule()
    except Exception:  # noqa: BLE001
        games = []
    parts.append("TODAY'S GAMES (model P(home win)):")
    if games:
        for g in games:
            hp = g.get("home_win_prob")
            hp_s = f"{hp*100:.0f}%" if hp is not None else "—"
            parts.append(
                f"  {g.get('away_abbr')} @ {g.get('home_abbr')}  "
                f"status={g.get('status')}  P(home)={hp_s}")
    else:
        parts.append("  (no games today)")
    return "\n".join(parts)


def chat(messages: list[dict[str, str]], *, model: str | None = None,
         temperature: float = 0.4, timeout: float | None = None) -> dict[str, Any]:
    """Send a chat turn to Ollama. ``messages`` = chronological
    ``{"role","content"}`` dicts. System prompt is prepended automatically.
    Returns ``{"ok", "reply", "model", "error"}``. Never raises."""
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

    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages)
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
