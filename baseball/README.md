# ⚾ Baseball Sidecar

MLB win-probability model + standalone FastAPI web UI. First instance of the sidecar pattern defined in `docs/sidecar-protocol.md`.

## Quick start

```bash
# One-time refresh of state (also runs nightly via cron at 04:15)
.venv/bin/python -m baseball.runner

# Start the web UI on port 8770
scripts/baseball_web.sh                 # foreground
scripts/baseball_web.sh --background    # detached

# Open http://localhost:8770
```

The main Streamlit app has a sidebar link "⚾ Baseball ↗" that opens this UI.

## Architecture

```
data.py    ─ MLB Stats API client (statsapi.mlb.com, public, no auth)
model.py   ─ Pure functions: regressed Pythagorean + log-odds matchup
state.py   ─ SQLite at data/shared/baseball_state.sqlite
runner.py  ─ Cron-able refresher
web.py     ─ FastAPI app, Jinja2 templates, HTMX interactivity
__init__.py ─ Public surface + Sidecar protocol implementation
```

## Model (v0.1)

`P(home wins)` is computed in log-odds space:

1. **Team strength**: `regressed_pythagorean(rs, ra, gp)` — Bill James Pythagorean (`rs² / (rs² + ra²)`) regressed toward .500 with a 40-game Bayesian prior so cold starts aren't punished.
2. **Home-field advantage**: `+0.161` log-odds (≈54% baseline).
3. **Starting pitcher**: `0.13 × (4.30 - era)` log-odds per side. Ace vs replacement (2.30 vs 4.30) ≈ 5pp swing.

Why log-odds: rate probabilities don't compose additively (50% + 5% + 5% ≠ 60%), log-odds do.

Defaults assume league-average for missing pitcher data (bullpen games / TBD announcements).

## Public domain API

```python
import baseball

baseball.predict_game("NYY", "BOS")  # P(NYY wins at home vs BOS)
# → {"home_win_prob": 0.62, "home_team": "NYY", ..., "source": "stored"}

baseball.today_schedule()       # today's slate + predictions
baseball.team_ratings_table()   # all teams sorted by strength
baseball.refresh_all()          # synchronous full refresh
```

## Sidecar operational API

Per `sidecars.Sidecar` protocol:

```python
baseball.health()             # SidecarHealth(status, last_update_ts, ...)
baseball.stats()              # {season, teams_rated, brier_n, brier_score, ...}
baseball.recent_activity(20)  # last 20 runner events
```

## Storage

`data/shared/baseball_state.sqlite` (env: `BASEBALL_STATE_PATH`).

Tables: `teams`, `team_season`, `pitchers`, `games`, `predictions`, `activity`, `meta`. WAL + `synchronous=NORMAL`.

## Tests

```bash
.venv/bin/python -m pytest tests/test_baseball_*.py tests/test_sidecars.py -v
```

Pure-function tests for the model, SQLite tests for state, public-surface tests for the package. No network in tests — `data.py` is exercised by smoke tests run manually.

## Live event collection (data only, no analysis yet)

`baseball/live.py` polls MLB's `feed/live` endpoint every 60s for every in-progress game and persists every play to the `live_plays` table. Pure data collection — the dataset feeds a future lead-lag analysis vs the market book (TBD).

```bash
scripts/baseball_live.sh                # foreground
scripts/baseball_live.sh --background   # detach
scripts/baseball_live.sh --status
scripts/baseball_live.sh --stop
```

Cheap when no games are running (one local SQLite query, no HTTP). Safe to leave running 24/7.

## Open work

- **Lead-lag analysis** of `live_plays` × `book_archive` (the existing `book_poller` snapshots Kalshi order books at 5min during the same period). Question: after a game event of type X, how long until the market reprices, and by how much? Build the analysis only after a few weeks of live data have accumulated. Faster book polling for MLB tickers (30-60s instead of 5min) is the obvious follow-up if the 5min granularity shows promising lag windows.
- **Trading layer** (front-run the predicted reaction): far future. Needs lead-lag analysis to even define the entry rule + the existing emergency-stop infra (TODO) before going live.
- **Validation**: roll Brier on the `predictions` table as games settle; compare to `pm/feeds/sports_data.py`'s ESPN-consensus baseline. Single best metric for "is the model worth listening to."
- **Lineups + injury list**: MLB API has both; not yet ingested. Lineups especially are 1-2pp swings on individual game projections.
- **Lookahead-safe historical backtest**: predictions are recorded forward-only today. For walk-forward validation we'd need to replay historical schedules + stats. Possible via MLB API's date-parameterized endpoints; not yet wired.
