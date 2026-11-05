# Polymarket API Reference

This project uses two public Polymarket APIs — no authentication required.

| API | Base URL | Purpose |
|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | Market metadata, prices, liquidity |
| CLOB | `https://clob.polymarket.com` | Order book, price history, token midpoints |

---

## Gamma API

### `GET /markets`

Returns a list of markets.

**Key parameters:**

| Param | Type | Notes |
|---|---|---|
| `limit` | int | Max 100 per request. Paginate with `offset`. |
| `offset` | int | Pagination offset. |
| `active` | bool string | `"true"` or `"false"` |
| `closed` | bool string | `"true"` or `"false"` |
| `order` | string | `volume24hr`, `liquidity`, `volume`, `createdAt` |
| `ascending` | bool string | Sort direction |
| `condition_ids` | string | **Plural, snake_case.** Filter to a specific market by its condition ID. **Do not use `conditionId`** — it's silently ignored and the API returns the default first market. |

**Key response fields per market:**

| Field | Type | Notes |
|---|---|---|
| `conditionId` | string | Hex ID used to look up market detail. Different from CLOB token IDs. |
| `question` | string | The prediction question. |
| `outcomes` | JSON string | e.g. `'["Yes", "No"]'` — must be `json.loads()`'d. |
| `outcomePrices` | JSON string | e.g. `'["0.42", "0.58"]'` — mid-quotes, always sum to 1.0. |
| `clobTokenIds` | JSON string | e.g. `'["9802...", "5383..."]'` — numeric token IDs for CLOB calls. |
| `bestBid` | float string | Best bid for the YES (first) outcome token. |
| `bestAsk` | float string | Best ask for the YES (first) outcome token. |
| `spread` | float string | `bestAsk − bestBid` for the YES token. |
| `lastTradePrice` | float string | Price of the most recent trade. |
| `volume24hr` | float string | 24-hour trading volume in USD. |
| `liquidity` | float string | Total available liquidity in USD. |
| `events` | list | Embedded event objects. May contain `seriesSlug` for sports markets. |
| `tokens` | null | Always `null` — do not use. Token IDs come from `clobTokenIds`. |

**Gotcha — JSON string fields:** `outcomes`, `outcomePrices`, and `clobTokenIds` are returned as JSON-encoded strings, not native arrays. Always parse them:
```python
import json
prices = json.loads(market["outcomePrices"])   # ['0.42', '0.58']
token_ids = json.loads(market["clobTokenIds"]) # ['98022...', '53831...']
```

**Gotcha — category:** The `category` field on market objects is always an empty string. Categories are not available on this endpoint. The project infers them from question keywords.

---

## CLOB API

### `GET /prices-history`

Returns time-series price data for a single outcome token.

**Parameters:**

| Param | Type | Required | Notes |
|---|---|---|---|
| `market` | string | Yes | A numeric CLOB token ID from `clobTokenIds`. NOT a `conditionId`. Passing a `conditionId` returns HTTP 400. |
| `startTs` | int | Yes | Unix timestamp. Required — API returns empty results without it. |
| `endTs` | int | No | Unix timestamp. Optional upper bound. |
| `fidelity` | int | Yes | Candle size in minutes: 1, 5, 15, 60, or 1440. |

**Response:**
```json
{
  "history": [
    {"t": 1746835206, "p": 0.645},
    {"t": 1746838806, "p": 0.655}
  ]
}
```

`t` is a Unix timestamp (seconds). `p` is the price (0–1).

**Gotcha — startTs required:** Without `startTs` the API returns `{"history": []}`. Always compute it from wall-clock time:
```python
import time
start_ts = int(time.time()) - days * 86400
```

**Gotcha — token ID format:** Use the raw numeric string from `clobTokenIds`, not the hex `conditionId`. These are completely different identifier systems. The numeric IDs look like 77-digit integers.

### `GET /book`

Returns the full order book for a token.

**Parameters:** `token_id` (numeric CLOB token ID)

**Response shape:**
```json
{
  "market": "0x...",
  "asset_id": "...",
  "timestamp": "...",
  "hash": "...",
  "bids": [{"price": "0.40", "size": "100"}, {"price": "0.45", "size": "50"}, ...],
  "asks": [{"price": "0.60", "size": "200"}, {"price": "0.55", "size": "30"}, ...],
  "tick_size": "0.001",
  "neg_risk": false,
  "last_trade_price": "0.48"
}
```

**Gotcha — sort order is inverted from intuition:**

- `bids` are in **ascending** price order — the best (highest) bid is at the **end** of the list.
- `asks` are in **descending** price order — the best (lowest) ask is at the **end** of the list.

`polymarket_client.parse_order_book()` handles this by reversing both lists.

### `GET /midpoint`

Returns the current mid-market price for a token.

**Parameters:** `token_id`

**Response:** `{"mid": "0.415"}`

---

## Known Quirks and Limitations

### Silent filter failures on `/markets`

The Gamma API does **not** return an error when you pass an unknown query parameter — it ignores the param and returns its default response. The most painful instance: the filter for a single market is `condition_ids` (plural, snake_case). The intuitive-looking `conditionId` is silently dropped, and every "lookup one market" call returns the first market in the default list.

`polymarket_client.get_market()` defends against this by comparing the returned `conditionId` against the request and returning `{}` if they don't match.

### Near-resolution prices can be as low as $0.0005

For markets effectively resolved (e.g. a sports game in its final seconds), the losing outcome can be quoted at $0.0005. Any UI control with a `min_value` of `0.001` will crash on prefill. Use `0.0001` as the floor for `st.number_input` / `st.slider`.

### Binary markets always sum to 1

`outcomePrices[0] + outcomePrices[1]` is always exactly `1.0` for binary YES/NO markets — it's derived, not independently quoted. The Gamma API enforces this invariant. Real spread information comes from `bestBid` / `bestAsk`, not from `outcomePrices`.

### bestBid / bestAsk cover YES only

The Gamma API only exposes bid/ask for the YES (first) outcome token. For NO: `NO_ask ≈ 1 − YES_bid` and `NO_bid ≈ 1 − YES_ask`. To get exact NO-side book depth, call `GET /book` on the second `clobTokenIds` entry.

### Category not on markets endpoint

The `/markets` endpoint returns `"category": ""` for every market. Category data lives on the `/events` endpoint but is not propagated to embedded event objects in market responses. This project infers categories from question keywords.

### Tags not propagated to embedded events

Market objects include an `events` array with embedded event detail. However, the `tags` and `category` fields on those embedded events are always null/empty — they are present only when fetching events directly from `/events`.

### Pagination cap

`/markets` returns at most 100 results per request regardless of the `limit` parameter. Paginate using `offset`.

### Price history needs startTs

`/prices-history` returns `{"history": []}` if `startTs` is omitted. There is no documented default window.

---

### `GET /events`

Returns a list of events — higher-level groupings of related markets (e.g. one event "2026 NBA Finals" with sub-markets for each team). This is where authoritative category data lives.

**Key parameters:** same shape as `/markets` — `limit` (max 100), `offset`, `active`, `closed`, `order`, `ascending`.

**Key response fields per event:**

| Field | Type | Notes |
|---|---|---|
| `id` | string | Numeric event ID. Matches `events[0].id` on a market response. |
| `title` | string | Human-readable event title. |
| `slug` | string | URL slug. |
| `category` | null | **Always null** — don't use. Real categorization is in `tags`. |
| `tags` | JSON string | A list of tag dicts, e.g. `'[{"label":"Soccer"}, {"label":"Sports"}]'`. The first label that matches our `_TAG_TO_CATEGORY` map is the category. |
| `volume24hr` | float string | Aggregate 24h volume across all markets in this event. |

**Gotcha — tags is a JSON string** (same shape as `outcomes`/`outcomePrices` on the markets endpoint). Parse with `json.loads()` before iterating.

**Gotcha — events on the markets endpoint don't have tags.** The `events` array embedded in `/markets` responses has the event ID and title but the `tags` field is empty. To get tag data you have to call `/events` separately and join by ID.

---

## Kalshi API gotchas (discovered during baseball edges work)

Base: `https://api.elections.kalshi.com/trade-api/v2`

### Ticker date+time is **Eastern**, not UTC

A ticker like `KXMLBGAME-26MAY222215CWSSF-CWS` parses as `MAY 22, 22:15` — but **22:15 is EDT/EST (America/New_York), not UTC**. Confirmed empirically: the corresponding MLB game has `game_date = 2026-05-23T02:15:00+00:00` (UTC), which is 22:15 EDT on May 22 — exactly matching the ticker.

This matters for date-prefix matching: a "today's MLB markets" query keyed on UTC will mismatch by a full day for any game starting after ~7-8pm Eastern.

The baseball sidecar's `analysis.tickers_for_game` and `insights.current_edges_for_date` both handle this by parsing the ticker's date+time as Eastern and converting to the display TZ.

### Bid/ask prices live in `*_dollars` fields, not `yes_bid` / `yes_ask`

The plain `yes_bid` / `yes_ask` fields on the `/markets` endpoint are **frequently None even on active markets**. The real source of truth is `yes_bid_dollars` / `yes_ask_dollars` (returned as strings like `"0.42"`).

Code that reads pricing should prefer the dollars variants and fall back to the plain fields only as a safety net. The `kalshi_client.lookup_markets_by_id` wrapper already does this; direct API consumers (like our `insights.current_edges_for_date`) need to remember.

When both are missing, `last_price` (cents) is the last-resort fallback.

### Status='open' vs status='active'

Kalshi markets accepting trades return `status: 'open'` on the /markets endpoint, NOT `status: 'active'`. Querying `?status=active` returns zero MLB markets even when they're trading.

### MLB market close_time is **4 days after game time**

Kalshi keeps MLB markets `open` for ~4 days after the underlying game so settlement disputes can resolve. This means:
- `get_markets_closing_within(2_days)` MISSES MLB games (their close_time is 5-6 days out)
- `auto_resolve_runner` based on Kalshi's `closed_markets` feed lags reality by 4 days
- The sidecar resolver overlay in `auto_resolve_runner.overlay_sidecar_resolutions` short-circuits this for MLB by resolving from MLB game finals directly

### Order book `parse_order_book` works venue-agnostically

`polymarket_client.parse_order_book` consumes both Polymarket and Kalshi order book shapes thanks to `kalshi_client.get_order_book` pre-shaping its response. Use it for either venue's books.

### Executed-trades tape — `GET /markets/trades`

The public trade tape (the "trades placed" list in the Kalshi UI sidebar). Unauthenticated. `kalshi_client.get_trades` / `get_all_trades` wrap it; `pm/archive/trade_archive.py` persists it; `pm/capture/trade_poller.py` backfills/polls it.

- Params: optional `ticker` (omit → cross-market firehose), `min_ts`/`max_ts` (unix **seconds**), `limit` (≤1000), `cursor`.
- Cursor-paginated like everything else on Kalshi. Because `min_ts`/`max_ts` filter server-side, this is **both** a live feed and a historical backfill source — you can pull a *finished* market's entire tape after the fact. This is what fills the "book_archive is forward-only" gap.
- Each trade: `trade_id` (stable PK — dedupe on it), `count_fp` (fractional contract size; `count` is the integer fallback), `yes_price_dollars` / `no_price_dollars` (strings), `taker_side` / `taker_outcome_side` (the **aggressor** outcome side — the direction of pressure), `created_time` (ISO-8601, ms precision).
- **WS `trade` channel shape** (subscribed alongside `orderbook_delta` in `pm/capture/kalshi_ws.py`): observed 2026-05 to be the *same* as the REST tape — `trade_id`, `count_fp` (string), `yes_price_dollars`/`no_price_dollars` (strings), `taker_side`, plus `ts` (unix seconds) / `ts_ms` instead of REST's `created_time`. `kalshi_ws._normalize_ws_trade` reads `count_fp` (NOT `count` — an early bug archived live trades with size 0), tolerates integer-cents prices defensively (bare value > 1 ⇒ cents), and synthesizes a `trade_id` if ever absent so re-delivered prints dedupe against the archive.

### Candlesticks carry volume + open interest

`GET /series/{series}/markets/{ticker}/candlesticks` (the data behind the "chance over time" graph) returns per-candle `yes_bid`/`yes_ask` OHLC **plus `volume` and `open_interest`**. `get_price_history` keeps only the mid-close; `get_candlesticks` surfaces the full row (`yes_bid_close`, `yes_ask_close`, `volume`, `open_interest`). `period_interval` is still constrained to 1 / 60 / 1440 minutes.

### Order `type` is `limit` or `market` only — no stop / trigger orders

Verified against the [Create Order reference](https://docs.kalshi.com/api-reference/orders/create-order) (2026-06-02): `POST /portfolio/orders` accepts `type: "limit"` or `type: "market"` — there is **no stop, stop-loss, or price-trigger order type**. Consequence for automated exits: a **downside stop cannot be a resting venue order** (a limit sell *below* the market fills immediately — "sell at X or better" — so it can't mean "sell only if it falls to X"). A stop must be managed client-side (poll the price, then fire). To make such an exit actually fill on a moved book, place a **marketable limit** — a sell crossed down through the bid behaves like a market sell while staying on the limit path. An *upside* take-profit (limit sell above entry) is a normal resting order and can live on the venue. Other `CreateOrder` fields of note: `time_in_force` ∈ {`fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`} (omitting it defaults to GTC; Kalshi 400s on `time_in_force: "gtc"`), `buy_max_cost` (forces FoK), `post_only`, `reduce_only` (`sell_position_floor` is deprecated → use `reduce_only`).

---

## MLB Stats API (used by the baseball sidecar)

Base URL: `https://statsapi.mlb.com/api/v1` (override via `MLB_STATS_API_BASE`). Free, public, no auth, no published rate limits — well-behaved clients are not a problem at this scale. Used by MLB.com itself + heavily by Baseball Savant, FanGraphs, Baseball-Reference.

### `GET /teams?sportId=1&activeStatus=Y`

All 30 active MLB teams. Returns `teams[].{id, abbreviation, name, teamName, league.abbreviation, division.name, sport.id}`.

**Gotcha — `sport.id` filter is required.** Without `?sportId=1` you'll get minor league teams mixed in (sport IDs 11 / 12 / 13 / 14 etc.). Filter again client-side on `sport.id == 1` for safety.

### `GET /schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher,linescore`

Schedule for one date. The `hydrate` parameter adds nested objects — without `probablePitcher` you get the games but not the starters. Returns `dates[].games[].{gamePk, gameDate, status.detailedState, teams.{home,away}.{team.id, score, probablePitcher.{id, fullName}}}`.

**Gotcha — `gameDate` is UTC** even when the game is local-evening US time. A 7pm Yankees home game shows up as next day in UTC. Be careful when querying "today's games" if you mean US-local "today."

**Gotcha — `probablePitcher` may be missing.** Bullpen games and early-week schedule lookahead don't have announced starters yet. Treat missing as "TBD / league-average pitcher" — don't crash.

### `GET /teams/{team_id}/stats?season=YYYY&stats=season&group=hitting,pitching`

Aggregated team-level stats for the season. Returns `stats[].{group.displayName, splits[].stat.{runs, gamesPlayed, wins, losses, ...}}`.

**Gotcha — wins/losses live in the pitching group, not hitting.** Hitting group has `runs` (runs scored); pitching group has `runs` (runs allowed) AND `wins`/`losses`. The "runs" field meaning depends on the group, which is easy to get wrong.

### `GET /people/{pitcher_id}/stats?stats=season&season=YYYY&group=pitching`

Per-pitcher season stats. Returns `stats[].splits[].stat.{era, whip, inningsPitched, strikeoutsPer9Inn, ...}`. ERA / WHIP are returned as strings — coerce to float in the parser and guard for `NaN`.

### `GET /schedule?sportId=1&date=YYYY-MM-DD&hydrate=lineups`

Adds `lineups.homePlayers` / `lineups.awayPlayers` arrays to each game in the response. Each player is `{id, fullName, primaryPosition}`. Empty arrays mean the lineup hasn't been posted yet — typically 60-90 min before first pitch.

**Gotcha**: lineup arrays are empty for games more than a few hours out. The runner's in-day `--lineups-only` cron compensates by re-fetching every 30 min.

### `GET /people/{player_id}/stats?stats=season&season=YYYY&group=hitting`

Per-batter season stats. Returns `stats[].splits[].stat.{ops, avg, homeRuns, atBats, ...}`. OPS is returned as a `.NNN` string — coerce to float and guard `NaN`. AB-zero pitchers / rookies who haven't batted return effectively no stats.

### `GET /teams/{team_id}/stats?season=YYYY&stats=statSplits&group=pitching&sitCodes=rp&gameType=R`

The SP/RP split filter. `sitCodes=rp` filters to relief appearances only — this is how we get **bullpen** ERA / WHIP separately from the team's total pitching line. Equivalent filter for starters is `sitCodes=sp`.

**Gotcha**: most teams' bullpens have wildly different ERAs from their full-staff number. Don't approximate bullpen ERA from total team ERA — pull it separately.

### `GET https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live`

**Note the URL — this one is on `v1.1`, not `v1`.** The live feed is a beefy JSON document: every pitch, batter, runner, defensive shift, replay review, weather, you name it. We touch just two paths:

- `gameData.status.detailedState` — `"In Progress"`, `"Final"`, `"Delayed: Rain"`, `"Manager challenge"`, `"Scheduled"`, etc. Anything not in `{"Final", "Game Over", "Completed Early", "Postponed", "Cancelled"}` is treated as "should keep polling" (defensive — MLB invents new states regularly).
- `liveData.plays.allPlays[]` — array of plays in atBatIndex order. Each play has `about.{atBatIndex, halfInning, inning, startTime, endTime, homeTeamWinProbability}` and `result.{event, eventType, description, homeScore, awayScore}`. The live collector keys uniqueness on `(game_pk, atBatIndex)`.

**Gotcha — `homeTeamWinProbability` is sometimes absent.** MLB doesn't hydrate it on every play, and the field disappears entirely for older games. We persist `None` in those cases rather than fabricating a value. Future analysis can derive WP from (inning, outs, base state, score gap) via the standard MLB WP tables.

**Gotcha — response is large (often 500KB-2MB per game).** Polling 15 in-progress games at 60s = 15-30 MB/min of JSON received. The collector handles this fine but keep it in mind if you spin up a parallel poller.

**Gotcha — "Scheduled" with plays present is the pre-game transition window.** Brief but real; the collector treats this as live by counting `allPlays` length when status is "Scheduled".

**Gotcha — game_pk is the natural key, NOT the event ticker.** Kalshi's `KXMLBGAME-...` tickers encode date + team abbreviations but NOT MLB's `game_pk`. To bridge: our state's `games` table has `game_pk` indexed by date + team_id; the Kalshi ticker matcher in `edges.py` finds the right `game_pk` via `(game_date, home_abbr, away_abbr)`.

---

## API Endpoints Not Currently Used

These are available but not yet integrated:

| Endpoint | Notes |
|---|---|
| `GET /sampling-markets` | Returns a random sample of markets. Useful for discovery. |
| `GET /sampling-simplified-markets` | Lightweight market list for fast polling. |
| `POST /order` | Place an order (requires API key + wallet auth). |
| `GET /trades` | Trade history for a token. |
