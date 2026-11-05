# Sidecar Protocol

Status: **draft v1** — pinned before the first sidecar (`baseball/`) lands so subsequent sidecars (`news/`, `nba/`, `nfl/`, etc.) inherit a consistent shape rather than re-deriving it.

## What is a sidecar?

A **sidecar** is a domain-isolated sub-package inside this repo that:

1. **Owns its own state** — its own SQLite store / JSON files under `data/shared/<sidecar>_*` (path conventions below).
2. **Runs on its own schedule** — typically a cron-driven `runner.py` plus on-demand "refresh now" via the control plane.
3. **Exposes a narrow public API** to the market app via `<sidecar>/__init__.py`. The market app imports that one module; it never reaches into the sub-package's internals.
4. **Implements the operational protocol** in `pm/core/sidecars.py` so the control plane can introspect it generically.

The pattern exists to keep the market app's blast radius small. The baseball model can change everything about how it represents team strength, what data it ingests, what storage it uses — and as long as `baseball.predict_game(...)` keeps the same signature, the market app's MLB strategy adapter doesn't care.

## Import discipline (the load-bearing rule)

```
sidecar/  ──imports──>  (stdlib, third-party, sidecars.py)
sidecar/  ──MUST NOT import──>  kalshi_client, provider, polymarket_client,
                                 backtest, edges, calibration, portfolio,
                                 risk, alerts, shared, app.py, pages/*

market app  ──imports──>  sidecar (via the sub-package's public surface only)
```

A sidecar that imports `kalshi_client` is no longer a sidecar — it's just another tangled module. Enforce this by code review for now; long-term a small `tests/test_sidecar_isolation.py` should grep the sub-package for forbidden imports.

The single legitimate place the market app imports a sidecar is the **strategy adapter** — since R-24, one file per sport under `edges/` (`edges/baseball.py`, `edges/wnba.py`, `edges/tennis.py`) that calls the sidecar's domain method and converts the result into an `Edge` / `Signal`. The adapter is market-app-side, not sidecar-side. A new sport adds `edges/<sport>.py` and one import line in `edges/__init__.py`.

## File layout (canonical)

```
<sidecar>/
  __init__.py    — public API + Sidecar protocol implementation
  data.py        — upstream HTTP/API clients + parsing
  model.py       — pure functions: domain logic (rate, score, predict)
  state.py       — read/write the persistent store
  runner.py      — cron-able refresh entry point
  README.md      — what this sidecar does, in one screen
tests/
  test_<sidecar>_data.py
  test_<sidecar>_model.py
  test_<sidecar>_state.py
data/shared/
  <sidecar>_state.sqlite   — primary persistent store
  <sidecar>.log            — runner output (logrotate sweeps it)
```

Standard (every *major* sidecar, unless explicitly waived):

- `<sidecar>/web.py` + `templates/` + `static/` — a FastAPI+Jinja2+HTMX UI on its own port (baseball :8770, WNBA :8771 — claim the next), run as a `<sidecar>-web` systemd service, launched by `scripts/<sidecar>_web.sh`, linked from the Streamlit app sidebar via `app._SIDECAR_WEB_PORTS`. **Reuse baseball's `static/style.css` byte-for-byte** for visual consistency. NOT Streamlit (its session model doesn't scale to concurrent users). See the playbook's "every major sidecar ships a web UI" section for the page checklist.

Optional:

- `pages/<sidecar>.py` — Streamlit page if the sidecar additionally wants a market-app-side surface. The FastAPI UI above is the primary one.

## Public surface

Every sidecar's `__init__.py` exposes **two layers**:

### Operational layer (required, fixed shape)

```python
name: str            # short identifier, also the registry key
display_name: str    # human-readable

def health() -> sidecars.SidecarHealth: ...
def stats() -> dict[str, Any]: ...
def recent_activity(limit: int = 20) -> list[sidecars.SidecarActivityEvent]: ...
```

These three functions:

- **Must not raise.** Use `sidecars.safe_health` to convert exceptions to `status="down"`.
- **Must be cheap.** Control plane polls `health()` on every page render. No HTTP. No full-table scans. If health requires expensive computation, cache it.
- **Must be honest.** A sidecar with stale data must report `status="degraded"` or `"down"`, not `"ok"`.

### Optional: `resolve_pending_tickers(tickers)`

Sidecars MAY implement this to **short-circuit slow venue settlement**. Many venues (Kalshi MLB markets, e.g.) keep markets `open` for days after the event resolves, as a settlement buffer. Forecasts on those markets sit pending during that window. A sidecar that already knows the answer — the baseball sidecar knows the final score the moment MLB does — can resolve forecasts earlier than `get_closed_markets()`.

```python
def resolve_pending_tickers(tickers: list[str]) -> dict[str, bool]:
    """Return {ticker: resolved_yes} for tickers this sidecar can
    confidently resolve. Omit tickers we can't or shouldn't resolve.
    MUST NOT raise — catch + return {} on failure."""
```

Rules:
- **Read-only** — no state mutation.
- **Bool only** — never `None`, never ambiguous.
- **Omit on uncertainty** — if the game isn't final, or the ticker doesn't match the domain, leave it out.
- **No raises** — wrap the whole body in try/except and return `{}` on any failure.

`auto_resolve_runner.overlay_sidecar_resolutions()` iterates `sidecars.list_sidecars()` looking for this method via `getattr(sc, "resolve_pending_tickers", None)`, so sidecars that don't implement it stay forward-compatible.

### Domain layer (sidecar-specific)

```python
# baseball/__init__.py
def predict_game(home: str, away: str, date: datetime,
                  pitchers: tuple[str, str] | None = None) -> float: ...
def team_rating(team: str, as_of: datetime | None = None) -> dict: ...
def refresh_all(force: bool = False) -> dict: ...
```

```python
# news/__init__.py
def recent_since(timestamp: datetime, tags: list[str] | None = None) -> list[NewsItem]: ...
def relevant_to_ticker(ticker: str) -> list[NewsItem]: ...
def register_source(source: NewsSource) -> None: ...
```

Domain APIs are unique per sidecar. The protocol intentionally doesn't constrain them.

## Registering

At import time the sidecar registers itself with `sidecars.register_sidecar`:

```python
# baseball/__init__.py
from sidecars import register_sidecar, SidecarHealth, SidecarActivityEvent, safe_health

NAME = "baseball"
DISPLAY_NAME = "⚾ Baseball"

def health() -> SidecarHealth: ...
def stats() -> dict: ...
def recent_activity(limit: int = 20) -> list[SidecarActivityEvent]: ...

# Domain layer
def predict_game(...): ...

register_sidecar(__name__)
```

`register_sidecar` validates that `name` / `health` / `stats` / `recent_activity` exist and registers the module itself as the Sidecar.

## Storage conventions

- **Persistent state**: `data/shared/<sidecar>_state.sqlite`. One file per sidecar. WAL + `synchronous=NORMAL` for HDD-friendly fsync behaviour (mirrors `book_archive`).
- **Path override**: env var `<SIDECAR>_STATE_PATH` (e.g. `BASEBALL_STATE_PATH`). Used by tests and by anyone moving state to a different disk.
- **Per-profile state**: not supported by the protocol — sidecars are global. If you need per-profile data, it doesn't belong in a sidecar. (The thinking: "what does baseball predict for the Yankees tonight" has one right answer; profile context is irrelevant.)
- **Schema migration**: each sidecar owns its own. SQLite `user_version` pragma + an idempotent `migrate()` called from `runner.py` startup is the recommended pattern. No central migration framework — overkill for the scale.

## Cron entry conventions

```
# <sidecar> nightly refresh — runs at 04:15 to dodge bulk_deep_analysis
15 4 * * *  flock -n /tmp/<sidecar>_runner.lock \
              -c 'cd /home/USER/Coding/kalshi-trader && \
                  .venv/bin/python -m <sidecar>.runner >> data/shared/<sidecar>.log 2>&1'
```

- `flock -n` is **non-negotiable** — overlapping runs corrupt state.
- Logrotate sweeps via the existing `data/shared/*.log` glob.

## What the control plane shows

(`pm/ui/pages/sidecars.py`, deferred until 2nd sidecar exists.) Per registered sidecar:

- Badge: 🟢 / 🟡 / 🔴 from `health().status`.
- Stats card: rows from `stats()` rendered as a key→value table.
- Activity feed: last 20 `recent_activity()` entries.
- Quick action: "refresh now" — shells out to `python -m <sidecar>.runner --force`. The control plane never runs domain logic directly.

## What this protocol intentionally does NOT specify

- **How a sidecar computes things.** Stats engine, ML model, scraping, hand-coded heuristic — all fine. The protocol only governs the shape.
- **What `stats()` returns.** Domain-specific. Baseball's stats look nothing like news's stats. Both are dicts of `str → JSON-serializable`.
- **How sidecars talk to each other.** They don't, by design. If baseball wants news, baseball imports `news.recent_since` — that's normal Python. No event bus.
- **Long-running execution.** Sidecars are batch-mode: a runner runs, updates state, exits. Streaming / live updates are a future concern.

## When to bend the rules

The rules exist to keep the system maintainable. They're not religion. If a sidecar genuinely needs to break one (e.g. needs per-profile state), bring it to the strategy doc and adjust the protocol in writing first. Don't quietly violate.

## Open questions (revisit when the 2nd sidecar exists)

- Should `health()` include a structured failure-mode taxonomy beyond just status?
- Should `recent_activity()` support filtering by `kind`?
- Should there be a `Sidecar.config_schema()` so the control plane can render a settings drawer per sidecar?
- Is one SQLite per sidecar the right grain, or should sidecars share a single `data/shared/sidecars.sqlite` with namespaced tables?
