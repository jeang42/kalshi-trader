"""Ollama-backed chat for the baseball sidecar.

A simple stateless chat surface: builds a context-rich system prompt
from the sidecar's current data (team rankings, today's slate, league
leaders, current edges), then sends each user turn to local Ollama via
the existing ``llm_data`` plumbing.

Design choices
--------------
- **Stateless across requests** for v1: no persistent chat history.
  The HTMX UI keeps the visible turn-by-turn on the page; if you
  reload, you start fresh. Persistence is a future TODO.
- **Context injected every turn** rather than just at start. Refreshes
  any stale numbers as the conversation progresses (e.g. score updates
  if a game has resolved).
- **Sidecar imports llm_data** — it's a stateless Ollama HTTP client,
  not market-app concern. Follows the same "shared utility" reasoning
  that lets baseball import `requests`.

Tool use
--------
Not implemented v1. The system prompt is big enough that the LLM has
plenty to work with without function calls. If a user asks a question
the context doesn't answer, the LLM is instructed to say so rather
than hallucinate. Tool-calling is a future TODO once we know what
patterns of question the user actually asks.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from baseball import insights
from pm.feeds import llm_data

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("BASEBALL_CHAT_MODEL", "llama3.1:latest")
DEFAULT_TIMEOUT = float(os.environ.get("BASEBALL_CHAT_TIMEOUT", "60"))

# Cap context size so we don't blow past smaller models' context limits.
# Even with 30 teams + ~15 games + aggregates, this stays well under 4K
# tokens — fine for any modern Ollama model.
_MAX_TEAMS_IN_CONTEXT = 30


def list_available_models() -> list[str]:
    """Same list as the main app's LLM picker. Used by the chat UI to
    let the user pick a model from the same set."""
    try:
        return llm_data.list_models()
    except Exception as e:  # noqa: BLE001
        log.warning("list_models failed: %s", e)
        return []


def is_available() -> bool:
    """Quick health check — used by the chat page to hide itself / show
    a helpful error if Ollama isn't running."""
    try:
        return llm_data.is_available()
    except Exception:  # noqa: BLE001
        return False


# ─── System prompt assembly ───────────────────────────────────────────────


_SYSTEM_PREAMBLE = """\
You are the in-house baseball analyst for the polymarket-baseball sidecar.
You have access to the data block below — a live snapshot of the
sidecar's state. Use it to answer questions about MLB teams, games,
matchups, model predictions, and market edges.

Rules:
1. Stay grounded in the data. If a question can't be answered from what's
   shown, say so plainly — don't invent stats or scores.
2. Be honest about model limitations: model v0.6 uses regressed
   Pythagorean + halved home-field (52%) + lineup OPS only. SP ERA
   and bullpen ERA were disabled after empirical analysis showed they
   added noise (zero correlation with outcomes on 42 games). The
   model's Brier score (0.264) is currently worse than coin flip —
   the Kalshi market is more accurate for predicting game winners.
3. The model's real value is NOT in predicting winners but in
   understanding game state for in-game positioning. The WPA model
   and lead-lag analysis are more actionable than the pre-game
   win probability.
4. When the model disagrees with the market, explain WHY but never
   claim the model is right. The market prices in information the
   model doesn't have (injuries, weather, sharp money).
5. Prefer short, direct answers. Use bullet points when listing data.
6. Currency: probabilities are YES-side on Kalshi binary markets.
   "Edge" / "Gap" is model_probability − market_probability in
   percentage points. Positive gap → model thinks YES is underpriced.
"""


_KNOWLEDGE_BLOCK = """\
SYSTEM KNOWLEDGE — facts about how this system works. Cite when asked.

Model composition (v0.6.0):
  P(home wins) = sigmoid(
      logit(home_strength) - logit(away_strength)
    + 0.080  (home-field, ~52% baseline, halved from 54% in v0.5)
    + 2.25  × (home_lineup_OPS - 0.720) - 2.25 × (away_lineup_OPS - 0.720)
  )
  SP ERA and bullpen ERA adjustments are DISABLED (weight = 0). Analysis
  of 42 settled games showed r=0.001 between SP delta and outcome, and
  bullpen ERA was double-counting team quality (r=0.636 with Pythagorean
  strength). Every pitching layer added made the Brier score worse.
  See docs/failed-experiments.md for the full post-mortem.

  Strength = 0.7 × season_pythagorean + 0.3 × recent_pythagorean(15d)
  Pythagorean inputs are park-neutralized.

Track record (as of late May 2026):
  - v0.5 Brier: 0.272 (n=23 settled) — worse than coin flip (0.250)
  - v0.6 Brier: too early (n=2 settled, 90 pending)
  - The Kalshi market Brier is 0.277 on the same games — the market
    is more accurate at predicting game winners
  - Model accuracy (favorite-wins): 52% — barely above coin flip

In-game analysis (the real edge):
  Lead-lag data: 35K+ plays paired with 620K+ book snapshots. The
  system captures play-by-play every 10s and book updates via Kalshi
  WebSocket (<100ms). Key finding: the market reprices scoring plays
  in <10 seconds (95% of the move done at first observation). The
  opportunity is PRE-POSITIONING — placing limit orders at WPA-derived
  target prices before plays occur, not reacting afterward.

  Pre-positioning retroactive evaluation (7 targetable plays):
    - Pre-position fills: 4/7, avg edge $0.050 per fill
    - Reactive approach: avg edge $0.019 per play
    - Pre-positioning captured 2.6x more per fill

WPA model (baseball.wpa):
  RE24 matrix + normal approximation for P(home wins) given any game
  state (inning, half, outs, bases, score diff). Used by the pre-
  positioning strategy to compute "fair price if next run scores."

Kalshi API quirks:
  - Ticker date+time is EASTERN, not UTC
  - MLB markets keep close_time = game_time + 4 days for settlement
  - The sidecar resolver short-circuits by resolving from game Finals

What we DO NOT have yet:
  - Weather, umpire data, sportsbook lines
  - Injury news beyond day-of lineups
  - Live radio transcription (pipeline built, needs stream URL)
"""


def _format_team_table(teams: list[dict]) -> str:
    """Render team-strength rows as a compact table."""
    if not teams:
        return "(no teams loaded)"
    lines = ["Rank  Team    Record   RD/G   Pyth   Strength"]
    for i, t in enumerate(teams, 1):
        rec = f"{t['wins']}-{t['losses']}"
        lines.append(
            f"{i:>3d}.  {t['abbreviation']:5s}  {rec:7s}  "
            f"{t['run_diff_per_game']:+5.2f}  {t['pythagorean']:.3f}  "
            f"{t['strength']:.3f}"
        )
    return "\n".join(lines)


def _format_today_games(games: list[dict]) -> str:
    if not games:
        return "(no games today)"
    out = []
    for g in games:
        score = ""
        if g.get("home_score") is not None and g.get("away_score") is not None:
            score = f"  [{g['away_score']}-{g['home_score']}]"
        ap = g.get("away_pitcher_name") or "TBD"
        hp = g.get("home_pitcher_name") or "TBD"
        prob = g.get("home_win_prob")
        prob_str = f"  P({g['home_abbr']} wins)={prob*100:.0f}%" if prob else ""
        out.append(
            f"  {g['away_abbr']:4s} ({ap}) @ {g['home_abbr']:4s} ({hp})"
            f"{score}  status={g['status']}{prob_str}"
        )
    return "\n".join(out)


def _format_edges(edges: list[dict]) -> str:
    if not edges:
        return "(no current edges available)"
    out = []
    for e in edges:
        out.append(
            f"  {e['matchup']:15s}  YES={e['yes_team']:4s}  "
            f"market={e['market_yes']*100:>5.1f}%  "
            f"model={e['model_yes']*100:>5.1f}%  "
            f"gap={e['gap']*100:+5.1f}pp"
        )
    return "\n".join(out)


def _format_leaders(rows: list[dict], value_key: str, label: str,
                     fmt: str = "{}") -> str:
    if not rows:
        return f"(no {label} data)"
    lines = []
    for r in rows[:6]:
        v = r[value_key]
        formatted_v = fmt.format(v) if isinstance(v, float) else str(v)
        lines.append(f"  {r['abbreviation']:5s}  {formatted_v}")
    return "\n".join(lines)


def build_system_prompt(context: dict[str, Any] | None = None) -> str:
    """Compose the system prompt with the current state snapshot.

    ``context=None`` triggers a fresh ``insights.context_for_llm()`` pull.
    Passing one in lets callers (or tests) override.
    """
    if context is None:
        context = insights.context_for_llm(max_teams=_MAX_TEAMS_IN_CONTEXT)

    parts = [_SYSTEM_PREAMBLE, "", _KNOWLEDGE_BLOCK, ""]
    parts.append(
        f"DATA SNAPSHOT (as of {context['as_of_local']} — "
        f"timezone: {context.get('tz_name', 'America/Chicago')})."
    )
    parts.append(
        f"  Today = {context['today_local']} (local), "
        f"Tomorrow = {context['tomorrow_local']} (local)."
    )
    parts.append(
        f"  When the user says 'today' or 'tonight', they mean "
        f"{context['today_local']} in {context.get('tz_name','America/Chicago')}, "
        f"not UTC. Use these local dates in your answers."
    )
    parts.append("")
    cov = context["coverage"]
    parts.append(
        f"Coverage: {cov['teams_loaded']} teams, "
        f"{cov['plays_total']:,} plays, "
        f"{cov['final_games']} final games, "
        f"date range {cov['date_min']} → {cov['date_max']}."
    )
    parts.append("")
    parts.append("TEAM-STRENGTH RANKINGS (regressed Pythagorean):")
    parts.append(_format_team_table(context["team_ratings"]))
    parts.append("")
    parts.append(f"TODAY'S GAMES ({context['today_local']}):")
    parts.append(_format_today_games(context["today_games"]))
    parts.append("")
    parts.append(f"TODAY'S MODEL EDGES ({context['today_local']}, top by gap magnitude):")
    parts.append(_format_edges(context["today_edges"]))
    parts.append("")
    parts.append(f"TOMORROW'S MODEL EDGES ({context['tomorrow_local']}, top by gap magnitude):")
    parts.append(_format_edges(context["tomorrow_edges"]))
    parts.append("")
    parts.append("OFFENSE LEADERS (Runs/Game):")
    parts.append(_format_leaders(context["offense_leaders"],
                                   "runs_per_game", "offense",
                                   fmt="{:.2f}"))
    parts.append("")
    parts.append("PITCHING LEADERS (lowest Runs-Allowed/Game):")
    parts.append(_format_leaders(context["pitching_leaders"],
                                   "runs_allowed_per_game", "pitching",
                                   fmt="{:.2f}"))
    parts.append("")
    parts.append("HOME-RUN LEADERS (by team, last ~30 days):")
    parts.append(_format_leaders(context["hr_leaders"], "n", "HR"))
    parts.append("")
    parts.append("STRIKEOUT-PITCHING LEADERS (by team, last ~30 days):")
    parts.append(_format_leaders(context["k_leaders"], "n", "K"))
    parts.append("")
    parts.append("HIGHEST 1-RUN-GAME % (high-variance teams):")
    one_run = context.get("one_run_leaders", [])
    if one_run:
        parts.append("\n".join(
            f"  {r['abbreviation']:5s}  {r['one_run']}/{r['games']} "
            f"({r['pct']*100:.0f}% of finals)"
            for r in one_run
        ))
    return "\n".join(parts)


# ─── Chat call ────────────────────────────────────────────────────────────


def chat(messages: list[dict[str, str]], *,
          model: str | None = None,
          temperature: float = 0.4,
          timeout: float | None = None) -> dict[str, Any]:
    """Send a chat turn to Ollama and return the response.

    ``messages`` is a list of ``{"role": "user"|"assistant", "content": str}``
    dicts in chronological order. The system prompt is prepended
    automatically with the fresh data snapshot.

    Returns ``{"ok": bool, "reply": str, "model": str, "error": str|None}``.
    Never raises — failures land in ``ok=False`` with a human-readable
    error.
    """
    chosen = model or DEFAULT_MODEL
    timeout_s = float(timeout) if timeout else DEFAULT_TIMEOUT
    if not is_available():
        return {"ok": False, "reply": "",
                "model": chosen, "error": "Ollama isn't reachable. "
                "Start it with `ollama serve` or check OLLAMA_API_BASE."}

    try:
        system_prompt = build_system_prompt()
    except Exception as e:  # noqa: BLE001
        log.warning("build_system_prompt failed: %s", e)
        system_prompt = _SYSTEM_PREAMBLE  # degrade gracefully

    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages)

    try:
        r = llm_data._session.post(
            f"{llm_data.OLLAMA_BASE}/api/chat",
            json={
                "model": chosen,
                "messages": api_messages,
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"ok": False, "reply": "", "model": chosen,
                "error": f"Ollama request failed: {e}"}
    except json.JSONDecodeError as e:
        return {"ok": False, "reply": "", "model": chosen,
                "error": f"Ollama returned non-JSON: {e}"}

    reply = data.get("message", {}).get("content", "")
    if not reply:
        return {"ok": False, "reply": "", "model": chosen,
                "error": "Ollama returned empty content."}

    return {"ok": True, "reply": reply, "model": chosen, "error": None}
