"""Polymarket API client using the public Gamma and CLOB APIs."""

import ast
import json
import logging
import os
import re
import time

import pandas as pd
import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

# Env-configurable for testing / mirrors / future replatform.
# Defaults are the public Polymarket endpoints — no auth required.
GAMMA_BASE = os.getenv("POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com")
CLOB_BASE = os.getenv("POLYMARKET_CLOB_BASE", "https://clob.polymarket.com")
HTTP_TIMEOUT = float(os.getenv("POLYMARKET_HTTP_TIMEOUT", "15"))

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


def _get(url: str, params: dict | None = None, retries: int = 3) -> dict | list:
    """HTTP GET under the ``polymarket`` circuit breaker (R-21) — see
    ``circuit_breaker``. Fails fast with ``CircuitOpenError`` while open."""
    return circuit_breaker.get("polymarket").call(_get_unguarded, url, params, retries)


def _get_unguarded(url: str, params: dict | None = None, retries: int = 3) -> dict | list:
    """HTTP GET with retry for transient errors and 429 rate limits.

    - 4xx/5xx (except 429): raised immediately. They're deterministic; retrying wastes time.
    - 429 (rate limited): retried up to `retries` times, respecting the `Retry-After`
      header (capped at 30s per wait so a single misbehaving response can't hang the UI).
    - Connection/timeout errors: retried with exponential backoff (1.5^attempt seconds).
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
            continue

        if r.status_code == 429:
            if attempt == retries - 1:
                r.raise_for_status()  # final attempt — give up and let HTTPError propagate
                return {}              # unreachable, but keeps the type checker happy
            retry_after = r.headers.get("Retry-After")
            try:
                sleep_s = float(retry_after) if retry_after else 1.0
            except ValueError:
                sleep_s = 1.0
            sleep_s = min(max(sleep_s, 0.1), 30.0)  # clamp [0.1, 30]
            log.warning("Rate limited (429) by %s; retrying in %.1fs", url, sleep_s)
            time.sleep(sleep_s)
            continue

        try:
            r.raise_for_status()
        except requests.HTTPError:
            raise

        return r.json()

    # Loop exit without return means we exhausted retries on a transient error
    # whose last `raise` slipped through. Re-raise if we have it, else empty.
    if last_exc:
        raise last_exc
    return {}


# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

_CATEGORY_RULES = [
    ("Crypto",        ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "xrp", "doge", "coinbase", "microstrategy", "altcoin", "defi", "nft", "blockchain"]),
    # 'match'/'tour' removed — too broad. 'world series' added explicitly.
    ("Sports",        ["nba", "nfl", "mlb", "nhl", "mls", "ufc", "nascar", "pga", "golf", "tennis", "soccer", "football", "basketball", "baseball", "hockey", "fight", "league", "championship", "world cup", "world series", "superbowl", "super bowl", "playoff", "tournament", "win the", " vs. ", "fc win", "fc lose", "earthquakes", "whitecaps", "sounders", "rapids", "galaxy", "sporting kc", "austin fc"]),
    ("Politics",      ["trump", "biden", "election", "president", "senate", "congress", "republican", "democrat", "vote", "ballot", "governor", "mayor", "policy", "legislation", "supreme court", "white house", "oval office", "starmer", "musk tweet", "elon musk post"]),
    ("Finance",       ["fed", "fomc", "rate cut", "interest rate", "gdp", "inflation", "recession", "s&p", "nasdaq", "dow", "stock", "bond", "yield", "economy", "market cap", "ipo", "tariff", "trade war"]),
    ("Tech",          ["ai", "openai", "chatgpt", "gpt", "apple", "google", "microsoft", "meta", "amazon", "tesla", "nvidia", "spacex", "starlink", "robot", "chip", "semiconductor", "anthropic", "gemini"]),
    ("Geopolitics",   ["war", "russia", "ukraine", "china", "taiwan", "iran", "israel", "hamas", "nato", "sanctions", "nuclear", "military", "geopolit", "middle east", "ceasefire", "invasion", "missile", "houthi", "strait of hormuz"]),
    # 'chart'/'tour'/'series' removed — too broad. 'concert tour'/'world tour' specific phrases instead.
    ("Entertainment", ["oscar", "grammy", "emmy", "eurovision", "rihanna", "taylor swift", "beyoncé", "nicki", "kanye", "movie", "album", "netflix", "disney", "gta", "mrbeast", "youtube", "twitch", "streamer", "celebrity", "award", "box office", "concert", "world tour", "pop star", "rapper", "singer", "actor", "actress", "reality tv", "game show"]),
]

_SPORTS_SERIES_SLUGS = {"nba", "nfl", "mlb", "nhl", "mls", "ufc", "pga", "nascar", "tennis", "golf", "soccer"}

# Polymarket event tag labels → our standard category buckets.
# Lower-cased lookup. First matching tag on the event wins.
_TAG_TO_CATEGORY = {
    # Sports — leagues, sports themselves, and event types
    "sports": "Sports", "games": "Sports", "soccer": "Sports", "football": "Sports",
    "basketball": "Sports", "baseball": "Sports", "hockey": "Sports", "tennis": "Sports",
    "golf": "Sports", "mma": "Sports", "ufc": "Sports", "boxing": "Sports", "f1": "Sports",
    "formula 1": "Sports", "nascar": "Sports", "esports": "Sports", "nba": "Sports",
    "nfl": "Sports", "mlb": "Sports", "nhl": "Sports", "mls": "Sports", "pga": "Sports",
    "premier league": "Sports", "epl": "Sports", "champions league": "Sports",
    "fifa world cup": "Sports", "world cup": "Sports", "olympics": "Sports",
    # Crypto
    "crypto": "Crypto", "crypto prices": "Crypto", "bitcoin": "Crypto", "ethereum": "Crypto",
    "btc": "Crypto", "eth": "Crypto", "altcoin": "Crypto", "altcoins": "Crypto",
    "defi": "Crypto", "nft": "Crypto", "blockchain": "Crypto", "solana": "Crypto",
    # Politics
    "politics": "Politics", "elections": "Politics", "us elections": "Politics",
    "world elections": "Politics", "trump": "Politics", "biden": "Politics",
    "starmer": "Politics", "republican": "Politics", "democrat": "Politics",
    "congress": "Politics", "senate": "Politics", "president": "Politics",
    "supreme court": "Politics",
    # Finance
    "finance": "Finance", "business": "Finance", "economic policy": "Finance",
    "fed": "Finance", "fomc": "Finance", "stocks": "Finance", "markets": "Finance",
    "inflation": "Finance", "recession": "Finance",
    # Tech
    "tech": "Tech", "big tech": "Tech", "ai": "Tech", "openai": "Tech",
    "ai companies": "Tech", "best ai company": "Tech", "anthropic": "Tech",
    # Geopolitics
    "geopolitics": "Geopolitics", "macro geopolitics": "Geopolitics",
    "middle east": "Geopolitics", "foreign policy": "Geopolitics", "world": "Geopolitics",
    "russia": "Geopolitics", "ukraine": "Geopolitics", "iran regime": "Geopolitics",
    "iran": "Geopolitics", "china": "Geopolitics", "war": "Geopolitics",
    "ceasefire": "Geopolitics",
    # Entertainment
    "entertainment": "Entertainment", "music": "Entertainment", "movies": "Entertainment",
    "tv": "Entertainment", "awards": "Entertainment", "celebrities": "Entertainment",
    "mrbeast": "Entertainment", "eurovision": "Entertainment", "gta": "Entertainment",
}


def _category_from_event_tags(tags) -> str | None:
    """Return the first matching category from an event's tags, or None.

    Tags arrive as a JSON-encoded string or a list of tag dicts. Either is fine.

    >>> _category_from_event_tags([{"label": "Soccer"}, {"label": "Sports"}])
    'Sports'
    >>> _category_from_event_tags([{"label": "Bitcoin"}])
    'Crypto'
    >>> _category_from_event_tags([{"label": "Unknown thing"}]) is None
    True
    >>> _category_from_event_tags(None) is None
    True
    """
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (ValueError, TypeError):
            return None
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        label = (tag.get("label") or "").strip().lower()
        if label in _TAG_TO_CATEGORY:
            return _TAG_TO_CATEGORY[label]
    return None

def _kw_match(keyword: str, text: str) -> bool:
    """Return True if keyword appears as a whole word/phrase in text.

    Keywords containing spaces are matched literally (the surrounding context
    already acts as a boundary). Single-token keywords use regex word boundaries
    so short tokens like 'eth' don't false-positive inside longer words.

    >>> _kw_match("eth", "will eth hit $5000")
    True
    >>> _kw_match("eth", "something completely unrelated")
    False
    >>> _kw_match(" vs. ", "Team A vs. Team B")
    True
    """
    if " " in keyword:
        return keyword in text
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


def _infer_category(question: str, events: list) -> str:
    """Infer a display category from the question text and embedded event slugs.

    seriesSlug (set by Polymarket for live sports events) takes priority over
    keyword matching.

    Examples
    --------
    >>> _infer_category("Will Bitcoin hit $100k?", [])
    'Crypto'
    >>> _infer_category("NBA Finals Game 7", [])
    'Sports'
    >>> _infer_category("Some game", [{"seriesSlug": "nba"}])
    'Sports'
    >>> _infer_category("Will Trump sign the bill?", [])
    'Politics'
    >>> _infer_category("Will the Fed cut rates?", [])
    'Finance'
    >>> _infer_category("Something completely unrelated", [])
    'Other'
    """
    q = question.lower()
    for event in (events or []):
        slug = (event.get("seriesSlug") or "").lower()
        if any(s in slug for s in _SPORTS_SERIES_SLUGS):
            return "Sports"
    for category, keywords in _CATEGORY_RULES:
        if any(_kw_match(kw, q) for kw in keywords):
            return category
    return "Other"


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100  # API hard cap per request


def parse_json_field(val):
    """Parse a Gamma API JSON-string field into a Python list.

    Gamma returns `outcomes`, `outcomePrices`, and `clobTokenIds` as JSON-encoded
    strings, not native arrays. This helper handles both string and already-parsed
    inputs, and never raises — invalid input returns an empty list.

    >>> parse_json_field('["Yes", "No"]')
    ['Yes', 'No']
    >>> parse_json_field(["Yes", "No"])
    ['Yes', 'No']
    >>> parse_json_field(None)
    []
    >>> parse_json_field("not valid json")
    []
    """
    if val is None:
        return []
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            return []
    if isinstance(val, list):
        return val
    return []


def get_markets(
    limit: int = 500,
    active: bool = True,
    order: str = "volume24hr",
) -> pd.DataFrame:
    """Return a DataFrame of Polymarket markets, paginating as needed."""
    all_data: list[dict] = []
    offset = 0
    while len(all_data) < limit:
        batch_size = min(_PAGE_SIZE, limit - len(all_data))
        params = {
            "limit": batch_size,
            "offset": offset,
            "active": str(active).lower(),
            "closed": "false",
            "order": order,
            "ascending": "false",
        }
        page = _get(f"{GAMMA_BASE}/markets", params)
        if not page:
            break
        all_data.extend(page)
        if len(page) < batch_size:
            break  # no more pages
        offset += batch_size

    if not all_data:
        return pd.DataFrame()

    rows = []
    for m in all_data:
        events_list = m.get("events") or []
        category = _infer_category(m.get("question", ""), events_list)
        # First event ID lets us later join against /events for authoritative tags
        event_id = str(events_list[0].get("id", "")) if events_list else ""
        outcomes = parse_json_field(m.get("outcomes"))
        prices_raw = parse_json_field(m.get("outcomePrices"))
        # Convert prices to floats; preserve length alignment with outcomes
        outcome_prices: list[float | None] = []
        for p in prices_raw:
            try:
                outcome_prices.append(float(p))
            except (TypeError, ValueError):
                outcome_prices.append(None)

        rows.append({
            "id": m.get("id"),
            "condition_id": m.get("conditionId"),
            "question": m.get("question", ""),
            "category": category,
            "event_id": event_id,              # first embedded event's ID, "" if none
            "volume": float(m.get("volume", 0) or 0),
            "volume_24hr": float(m.get("volume24hr", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "end_date": m.get("endDate"),
            "outcomes": outcomes,              # ALWAYS a Python list now
            "outcome_prices": outcome_prices,  # ALWAYS a list of floats (or None)
            "active": m.get("active", True),
            "spread": float(m.get("spread") or 0),
            "best_bid": float(m.get("bestBid") or 0),
            "best_ask": float(m.get("bestAsk") or 0),
            "last_trade_price": float(m.get("lastTradePrice") or 0),
        })

    df = pd.DataFrame(rows)
    if "volume_24hr" in df.columns:
        df = df.sort_values("volume_24hr", ascending=False)

    # Derive days-to-resolution from end_date. Past-due markets show 0.
    if "end_date" in df.columns:
        df["end_date_parsed"] = pd.to_datetime(df["end_date"], errors="coerce", utc=True)
        now = pd.Timestamp.now(tz="UTC")
        df["days_to_resolution"] = (
            (df["end_date_parsed"] - now).dt.total_seconds() / 86400.0
        ).clip(lower=0).round(1)

    return df


def get_closed_markets(
    limit: int = 100,
    order: str = "closedTime",
) -> pd.DataFrame:
    """Fetch recently-closed markets with their final resolutions.

    Returns the same columns as `get_markets`, **plus**:
        resolved_yes (bool)  — True if the YES outcome won; False if NO won;
                                None for markets that closed without clean
                                resolution (e.g. outcomePrices weren't 0/1).

    Used as the foundation for calibration backfill and the backtest framework
    (Tier 3 of the recommendations roadmap). Note that *closed* markets are
    paginated by the API the same way as active markets — same 100-per-page
    hard cap.
    """
    all_data: list[dict] = []
    offset = 0
    while len(all_data) < limit:
        batch_size = min(_PAGE_SIZE, limit - len(all_data))
        params = {
            "limit": batch_size,
            "offset": offset,
            "active": "false",
            "closed": "true",
            "order": order,
            "ascending": "false",
        }
        page = _get(f"{GAMMA_BASE}/markets", params)
        if not page:
            break
        all_data.extend(page)
        if len(page) < batch_size:
            break
        offset += batch_size

    if not all_data:
        return pd.DataFrame()

    rows = []
    for m in all_data:
        events_list = m.get("events") or []
        category = _infer_category(m.get("question", ""), events_list)
        event_id = str(events_list[0].get("id", "")) if events_list else ""
        outcomes = parse_json_field(m.get("outcomes"))
        prices_raw = parse_json_field(m.get("outcomePrices"))
        outcome_prices: list[float | None] = []
        for p in prices_raw:
            try:
                outcome_prices.append(float(p))
            except (TypeError, ValueError):
                outcome_prices.append(None)

        # Derive `resolved_yes` from the final outcome prices.
        # Clean binary resolution: [1, 0] = YES won, [0, 1] = NO won.
        resolved_yes: bool | None
        if (
            len(outcome_prices) == 2
            and outcome_prices[0] is not None
            and outcome_prices[1] is not None
            and abs(outcome_prices[0] + outcome_prices[1] - 1.0) < 0.01
            and (outcome_prices[0] > 0.99 or outcome_prices[1] > 0.99)
        ):
            resolved_yes = outcome_prices[0] > 0.5
        else:
            resolved_yes = None

        rows.append({
            "id": m.get("id"),
            "condition_id": m.get("conditionId"),
            "question": m.get("question", ""),
            "category": category,
            "event_id": event_id,
            "volume": float(m.get("volume", 0) or 0),
            "volume_24hr": float(m.get("volume24hr", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "end_date": m.get("endDate"),
            "closed_time": m.get("closedTime"),
            "outcomes": outcomes,
            "outcome_prices": outcome_prices,
            "active": m.get("active", False),
            "resolved_yes": resolved_yes,
        })

    df = pd.DataFrame(rows)
    df["closed_time_dt"] = pd.to_datetime(df["closed_time"], errors="coerce", utc=True)
    return df


def get_market(condition_id: str) -> dict:
    """Return full market details for a single conditionId.

    Uses the Gamma API's `condition_ids` filter (plural, snake_case). The
    similar-looking `conditionId` camelCase param is silently ignored by the
    API and returns the default first market — defending against that here
    by verifying the returned conditionId matches the request.
    """
    data = _get(f"{GAMMA_BASE}/markets", params={"condition_ids": condition_id})
    if not isinstance(data, list) or not data:
        return {}
    market = data[0]
    # Defensive check — if the API didn't actually filter, refuse to return
    # mismatched data rather than silently mislead callers.
    returned_cid = market.get("conditionId", "")
    if returned_cid and returned_cid != condition_id:
        log.warning(
            "get_market(%s) returned a different market (%s). API filter may have failed.",
            condition_id, returned_cid,
        )
        return {}
    return market


# ---------------------------------------------------------------------------
# Events (for authoritative category tags)
# ---------------------------------------------------------------------------

def get_events(
    limit: int = 500,
    active: bool = True,
    order: str = "volume24hr",
) -> pd.DataFrame:
    """Fetch active Polymarket events with parsed category from tags.

    Events are higher-level groupings of related markets (e.g. one event
    "2026 NBA Finals" with sub-markets for each team's win probability).
    Each event has tag data (Soccer, Crypto, etc.) that this app uses to
    assign authoritative categories to the markets within.

    Returns a DataFrame indexed by event ID with columns:
        id, title, slug, category, volume_24hr, active
    `category` is mapped from event tags via `_TAG_TO_CATEGORY`; empty
    string if no recognised tag was present.
    """
    all_data: list[dict] = []
    offset = 0
    while len(all_data) < limit:
        batch_size = min(_PAGE_SIZE, limit - len(all_data))
        params = {
            "limit": batch_size,
            "offset": offset,
            "active": str(active).lower(),
            "closed": "false",
            "order": order,
            "ascending": "false",
        }
        page = _get(f"{GAMMA_BASE}/events", params)
        if not page:
            break
        all_data.extend(page)
        if len(page) < batch_size:
            break
        offset += batch_size

    if not all_data:
        return pd.DataFrame(columns=["id", "title", "slug", "category", "volume_24hr", "active"])

    rows = []
    for e in all_data:
        rows.append({
            "id": str(e.get("id", "")),
            "title": e.get("title", ""),
            "slug": e.get("slug", ""),
            "category": _category_from_event_tags(e.get("tags") or []) or "",
            "volume_24hr": float(e.get("volume24hr", 0) or 0),
            "active": e.get("active", True),
        })
    return pd.DataFrame(rows)


def enrich_categories_from_events(
    df_markets: pd.DataFrame, df_events: pd.DataFrame
) -> pd.DataFrame:
    """Override `category` with event-tag-derived category where available.

    The keyword-inferred category from `get_markets` stays as the fallback
    for markets whose event isn't in `df_events` or whose event has no
    recognised tag. The result is "API category if known, otherwise keyword".

    Adds an `event_title` column for downstream UI use.

    >>> import pandas as pd
    >>> markets = pd.DataFrame([
    ...     {"event_id": "1", "category": "Other", "question": "Q1"},
    ...     {"event_id": "2", "category": "Crypto", "question": "Q2"},
    ...     {"event_id": "",  "category": "Sports", "question": "Q3"},
    ... ])
    >>> events = pd.DataFrame([
    ...     {"id": "1", "title": "World Cup", "category": "Sports"},
    ...     {"id": "2", "title": "BTC Price", "category": ""},
    ... ])
    >>> out = enrich_categories_from_events(markets, events)
    >>> out["category"].tolist()
    ['Sports', 'Crypto', 'Sports']
    >>> out["event_title"].tolist()
    ['World Cup', 'BTC Price', '']
    """
    if df_markets.empty:
        return df_markets

    if df_events.empty or "id" not in df_events.columns:
        # No event data — nothing to merge. Still surface an empty event_title col.
        df = df_markets.copy()
        df["event_title"] = ""
        return df

    event_cat = dict(zip(df_events["id"].astype(str), df_events["category"], strict=False))
    event_title = dict(zip(df_events["id"].astype(str), df_events["title"], strict=False))

    df = df_markets.copy()
    if "event_id" not in df.columns:
        df["event_title"] = ""
        return df

    eid = df["event_id"].astype(str)
    api_cat = eid.map(event_cat).fillna("")
    df["category"] = api_cat.where(api_cat != "", df["category"])
    df["event_title"] = eid.map(event_title).fillna("")
    return df


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

def get_price_history(
    market_id: str,
    days: int = 30,
    fidelity: int = 60,
) -> pd.DataFrame:
    """
    Fetch CLOB price history for a token / market.

    Fetches the last `days` of data relative to the newest candle available
    from the API (not relative to wall-clock time, so it works even when the
    API lags behind the system clock).

    fidelity: minutes per candle (1, 5, 15, 60, 1440)
    """
    start_ts = int(time.time()) - days * 86400
    params: dict = {"market": market_id, "fidelity": fidelity, "startTs": start_ts}

    try:
        data = _get(f"{CLOB_BASE}/prices-history", params)
    except requests.HTTPError:
        return pd.DataFrame()
    history = data.get("history", []) if isinstance(data, dict) else []

    if not history:
        return pd.DataFrame()

    df = pd.DataFrame(history)
    df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df["p"] = df["p"].astype(float)
    df = df.rename(columns={"t": "timestamp", "p": "price"})
    return df.sort_values("timestamp").reset_index(drop=True)


def market_price_at_offset_before_close(
    token_id: str,
    closed_at: pd.Timestamp | str,
    lead_hours: float,
    fidelity: int = 60,
    window_days: int = 90,
) -> float | None:
    """Find the market's YES price approximately `lead_hours` before close.

    Used for backfilling calibration data — what was the market predicting
    `lead_hours` before resolution, and was it right? Returns None if there's
    no history covering the target time.

    Implementation: fetches price history within a window of `window_days`
    before close, then returns the price of the candle whose timestamp is
    closest to `closed_at - lead_hours`.
    """
    if closed_at is None:
        return None
    closed_ts = pd.to_datetime(closed_at, utc=True, errors="coerce")
    if pd.isna(closed_ts):
        return None

    target_ts = closed_ts - pd.Timedelta(hours=lead_hours)
    if pd.isna(target_ts):
        return None

    # Use a larger window so older closed markets still have visible history.
    df = get_price_history(token_id, days=window_days, fidelity=fidelity)
    if df.empty:
        return None

    # Find the closest candle by absolute time difference
    df = df.copy()
    df["delta"] = (df["timestamp"] - target_ts).abs()
    closest = df.loc[df["delta"].idxmin()]
    return float(closest["price"])


def get_market_calibration_data(
    df_closed: pd.DataFrame,
    lead_hours: float = 24,
    max_markets: int = 100,
    progress_callback=None,
) -> pd.DataFrame:
    """For each closed market in `df_closed`, fetch its YES price at
    `lead_hours` before resolution and compare to the final outcome.

    The output is the raw dataset for computing the *market's* Brier score —
    answering "how well does the price T-N hours before close predict the
    actual resolution?" This is the backfill calibration baseline the user
    can use as a reference for their own forecasts.

    Each row: condition_id, question, category, predicted_prob, resolved_yes,
              brier_term (= (predicted - actual)^2), lead_hours.

    Markets without sufficient history or without a clean resolution are
    silently skipped. `progress_callback(i, total)` is called for each
    market processed so the UI can show progress on a slow fetch.
    """
    if df_closed is None or df_closed.empty:
        return pd.DataFrame()

    df = df_closed[df_closed["resolved_yes"].notna()].head(max_markets).copy()
    if df.empty:
        return pd.DataFrame()

    rows = []
    total = len(df)
    for i, (_, row) in enumerate(df.iterrows()):
        if progress_callback is not None:
            try:
                progress_callback(i, total)
            except Exception:  # callbacks shouldn't break the loop
                pass

        cid = row["condition_id"]
        # Need the token id from the per-market detail; outcome_prices alone
        # isn't enough to identify the token. Fetch detail.
        detail = get_market(cid)
        clob_ids = parse_json_field(detail.get("clobTokenIds"))
        if not clob_ids:
            continue
        yes_token = str(clob_ids[0])

        predicted = market_price_at_offset_before_close(
            yes_token, row["closed_time_dt"], lead_hours=lead_hours,
        )
        if predicted is None:
            continue

        resolved_yes = bool(row["resolved_yes"])
        actual = 1.0 if resolved_yes else 0.0
        brier_term = (predicted - actual) ** 2
        rows.append({
            "condition_id": cid,
            "question": row["question"],
            "category": row.get("category", ""),
            "predicted_prob": predicted,
            "resolved_yes": resolved_yes,
            "actual": actual,
            "brier_term": brier_term,
            "lead_hours": lead_hours,
        })

    if progress_callback is not None:
        try:
            progress_callback(total, total)
        except Exception:
            pass

    if not rows:
        return pd.DataFrame(columns=[
            "condition_id", "question", "category",
            "predicted_prob", "resolved_yes", "actual",
            "brier_term", "lead_hours",
        ])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Order book / spread
# ---------------------------------------------------------------------------

def get_order_book(token_id: str) -> dict:
    """Return raw bids/asks for a CLOB token."""
    data = _get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    return data if isinstance(data, dict) else {}


def parse_order_book(book: dict) -> pd.DataFrame:
    """Normalize a raw `/book` response into a tidy DataFrame.

    Polymarket returns bids sorted *ascending* by price and asks sorted
    *descending* — i.e., the best (top-of-book) price is at the END of each
    list. We reverse them so best price comes first per side, then compute
    cumulative size from best outward.

    Columns: side ('bid' | 'ask'), price (float), size (float),
             cumulative_size (float).

    >>> raw = {"bids": [{"price": "0.40", "size": "100"},
    ...                 {"price": "0.45", "size": "50"}],   # best bid at end
    ...        "asks": [{"price": "0.60", "size": "200"},
    ...                 {"price": "0.55", "size": "30"}]}   # best ask at end
    >>> df = parse_order_book(raw)
    >>> df[df['side']=='bid'][['price','size','cumulative_size']].to_dict('records')
    [{'price': 0.45, 'size': 50.0, 'cumulative_size': 50.0}, {'price': 0.4, 'size': 100.0, 'cumulative_size': 150.0}]
    >>> df[df['side']=='ask'][['price','size','cumulative_size']].to_dict('records')
    [{'price': 0.55, 'size': 30.0, 'cumulative_size': 30.0}, {'price': 0.6, 'size': 200.0, 'cumulative_size': 230.0}]
    """
    rows = []
    if not isinstance(book, dict):
        return pd.DataFrame(columns=["side", "price", "size", "cumulative_size"])

    # Bids: API order is ascending. Reverse so best (highest) bid is first.
    bids_raw = list(reversed(book.get("bids") or []))
    cum = 0.0
    for level in bids_raw:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        cum += size
        rows.append({"side": "bid", "price": price, "size": size,
                     "cumulative_size": cum})

    # Asks: API order is descending. Reverse so best (lowest) ask is first.
    asks_raw = list(reversed(book.get("asks") or []))
    cum = 0.0
    for level in asks_raw:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        cum += size
        rows.append({"side": "ask", "price": price, "size": size,
                     "cumulative_size": cum})

    if not rows:
        return pd.DataFrame(columns=["side", "price", "size", "cumulative_size"])
    return pd.DataFrame(rows)


def order_book_stats(df_book: pd.DataFrame) -> dict:
    """Summary metrics from a parsed order book.

    >>> import pandas as pd
    >>> df = pd.DataFrame([
    ...     {"side": "bid", "price": 0.45, "size": 50, "cumulative_size": 50},
    ...     {"side": "bid", "price": 0.40, "size": 100, "cumulative_size": 150},
    ...     {"side": "ask", "price": 0.55, "size": 30, "cumulative_size": 30},
    ...     {"side": "ask", "price": 0.60, "size": 200, "cumulative_size": 230},
    ... ])
    >>> stats = order_book_stats(df)
    >>> stats['best_bid'], stats['best_ask'], round(stats['spread'], 3)
    (0.45, 0.55, 0.1)
    >>> stats['total_bid_size'], stats['total_ask_size']
    (150.0, 230.0)
    """
    if df_book is None or df_book.empty:
        return {"best_bid": None, "best_ask": None, "spread": None,
                "total_bid_size": 0.0, "total_ask_size": 0.0,
                "bid_levels": 0, "ask_levels": 0}
    bids = df_book[df_book["side"] == "bid"]
    asks = df_book[df_book["side"] == "ask"]
    best_bid = float(bids.iloc[0]["price"]) if not bids.empty else None
    best_ask = float(asks.iloc[0]["price"]) if not asks.empty else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "total_bid_size": float(bids["size"].sum()) if not bids.empty else 0.0,
        "total_ask_size": float(asks["size"].sum()) if not asks.empty else 0.0,
        "bid_levels": int(len(bids)),
        "ask_levels": int(len(asks)),
    }


def get_midpoint(token_id: str) -> float | None:
    """Return mid-market price for a token, or None if unavailable."""
    try:
        data = _get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
        if not isinstance(data, dict):
            return None
        return float(data.get("mid", 0))
    except (requests.RequestException, ValueError, TypeError, KeyError) as e:
        log.warning("get_midpoint(%s) failed: %s", token_id, e)
        return None


# ---------------------------------------------------------------------------
# Kelly Criterion
# ---------------------------------------------------------------------------

def kelly_sizing(market_price: float, your_probability: float) -> dict:
    """
    Compute Kelly Criterion position sizing for a binary prediction market outcome.

    Compares your probability estimate against the market's implied probability
    (the current price) to find your edge, then calculates the fraction of
    bankroll that maximizes long-run compounding.

    Parameters
    ----------
    market_price : float
        Current market price for the outcome (0–1). Equals the market's
        implied probability that the outcome resolves YES.
    your_probability : float
        Your estimate of the true probability (0–1).

    Returns
    -------
    dict with keys (all values are plain floats / strings — no callables, so
    the result is JSON-serializable):
        edge                float   your_probability − market_price
        direction           str     "YES" (bet for) or "NO" (bet against)
        kelly_fraction      float   optimal fraction of bankroll to bet (0–1)
        half_kelly_fraction float   kelly_fraction / 2
        expected_value      float   expected profit per $1 bet at full Kelly

    To turn the fraction into a dollar amount, just multiply by your bankroll:
        kelly_dollars      = bankroll * result["kelly_fraction"]
        half_kelly_dollars = bankroll * result["half_kelly_fraction"]

    Formula
    -------
    Betting YES at price m when true probability is p:
        edge     = p − m
        fraction = edge / (1 − m)      [net odds denominator for a YES bet]

    Betting NO at price m when true probability is p:
        edge     = m − p
        fraction = edge / m            [net odds denominator for a NO bet]

    A negative fraction means you have no edge in that direction — the
    returned fraction is clamped to 0 in that case.

    Examples
    --------
    You think YES is 50% likely; market prices it at 30% — clear edge:

    >>> r = kelly_sizing(market_price=0.30, your_probability=0.50)
    >>> r['direction']
    'YES'
    >>> round(r['edge'], 4)
    0.2
    >>> round(r['kelly_fraction'], 4)
    0.2857
    >>> round(1000 * r['kelly_fraction'], 2)   # dollar amount on a $1000 bankroll
    285.71

    When your estimate matches the market there is no edge:

    >>> r = kelly_sizing(market_price=0.50, your_probability=0.50)
    >>> r['kelly_fraction']
    0.0

    Overpriced outcome — bet NO instead:

    >>> r = kelly_sizing(market_price=0.70, your_probability=0.50)
    >>> r['direction']
    'NO'
    >>> round(r['kelly_fraction'], 4)
    0.2857

    The fraction is always between 0 and 1:

    >>> r = kelly_sizing(market_price=0.01, your_probability=0.99)
    >>> 0.0 <= r['kelly_fraction'] <= 1.0
    True

    Inputs outside [0, 1] raise ValueError:

    >>> kelly_sizing(market_price=1.5, your_probability=0.5)
    Traceback (most recent call last):
        ...
    ValueError: market_price must be in [0, 1], got 1.5
    """
    m = float(market_price)
    p = float(your_probability)
    if not (0.0 <= m <= 1.0):
        raise ValueError(f"market_price must be in [0, 1], got {m}")
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"your_probability must be in [0, 1], got {p}")
    edge = p - m

    if edge >= 0:
        # Bet YES: payout is (1/m - 1) per $1 wagered, i.e. net odds b = (1-m)/m
        direction = "YES"
        raw_fraction = edge / (1 - m) if m < 1 else 0.0
    else:
        # Bet NO: payout is (1/(1-m) - 1) per $1 wagered, i.e. net odds b = m/(1-m)
        direction = "NO"
        raw_fraction = (m - p) / m if m > 0 else 0.0

    fraction = max(0.0, min(1.0, raw_fraction))
    half_fraction = fraction / 2

    # Expected value per $1 bet (full Kelly)
    if direction == "YES":
        net_odds = (1 - m) / m if m > 0 else 0
    else:
        net_odds = m / (1 - m) if m < 1 else 0
    ev = p * net_odds - (1 - p) if direction == "YES" else (1 - p) * net_odds - p

    return {
        "edge": edge,
        "direction": direction,
        "kelly_fraction": fraction,
        "half_kelly_fraction": half_fraction,
        "expected_value": ev,
    }


# ---------------------------------------------------------------------------
# Market scanner
# ---------------------------------------------------------------------------

def _parse_prices(raw) -> list[float]:
    """Parse outcome_prices into a list of floats.

    Accepts either a JSON-encoded string (legacy Gamma format), a Python list
    of numbers/strings, or None. Returns an empty list on any parse failure.

    >>> _parse_prices('["0.40", "0.60"]')
    [0.4, 0.6]
    >>> _parse_prices([0.4, 0.6])
    [0.4, 0.6]
    >>> _parse_prices(None)
    []
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return []
    try:
        return [float(p) for p in raw if p is not None]
    except (TypeError, ValueError):
        return []


def scan_narrow_spreads(df: pd.DataFrame, max_spread: float = 0.02, min_liquidity: float = 500) -> pd.DataFrame:
    """Markets with tight bid-ask spreads — cheapest to enter/exit."""
    df = df[(df["spread"] > 0) & (df["spread"] <= max_spread) & (df["liquidity"] >= min_liquidity)].copy()
    if df.empty:
        return df
    prices_col = df["outcome_prices"].apply(_parse_prices)
    df["yes_price"] = prices_col.apply(lambda p: p[0] if len(p) >= 1 else None)
    return (
        df[["question", "category", "yes_price", "spread", "best_bid", "best_ask",
            "volume_24hr", "liquidity"]]
        .sort_values("spread")
        .reset_index(drop=True)
    )


def scan_price_divergence(df: pd.DataFrame, min_divergence: float = 0.05, min_liquidity: float = 500) -> pd.DataFrame:
    """
    Markets where the last trade price diverges from the current mid-quote.
    Large divergence = price moved since last trade, or quote is stale.

    **Binary markets only.** The Gamma API's `lastTradePrice` is a single
    per-market value representing the YES outcome's last trade. For multi-
    outcome markets (3+ outcomes), `outcome_prices[0]` is not a "YES" but
    one specific outcome (e.g. Alice in {Alice, Bob, Carol}), so comparing
    it to a market-level `lastTradePrice` is not a meaningful divergence
    signal. We filter those out explicitly to avoid bogus matches.

    For multi-outcome divergence detection we'd need per-token last-trade
    data from the CLOB — not currently implemented.
    """
    df = df[df["liquidity"] >= min_liquidity].copy()
    if df.empty:
        return df
    prices_col = df["outcome_prices"].apply(_parse_prices)
    # Binary-only: restrict to markets with exactly two outcomes
    df = df[prices_col.apply(lambda p: len(p) == 2)]
    if df.empty:
        return df
    prices_col = df["outcome_prices"].apply(_parse_prices)
    df["mid_price"] = prices_col.apply(lambda p: p[0] if p else None)
    df = df.dropna(subset=["mid_price", "last_trade_price"])
    df = df[df["last_trade_price"] > 0]
    df["divergence"] = (df["last_trade_price"] - df["mid_price"]).abs()
    df = df[df["divergence"] >= min_divergence]
    if df.empty:
        return df
    return (
        df[["question", "category", "mid_price", "last_trade_price", "divergence",
            "spread", "volume_24hr", "liquidity"]]
        .sort_values("divergence", ascending=False)
        .reset_index(drop=True)
    )


def scan_multi_outcome_gaps(df: pd.DataFrame, min_deviation: float = 0.03, min_liquidity: float = 200) -> pd.DataFrame:
    """
    Multi-outcome markets (3+ outcomes) where prices don't sum to 1.
    A sum < 1 means at least one outcome is underpriced relative to the others.
    """
    rows = []
    for _, row in df[df["liquidity"] >= min_liquidity].iterrows():
        prices = _parse_prices(row.get("outcome_prices", []))
        outcomes_raw = row.get("outcomes", [])
        outcomes = parse_json_field(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
        if len(prices) < 3:
            continue
        total = sum(prices)
        deviation = total - 1.0
        if abs(deviation) >= min_deviation:
            rows.append({
                "question": row["question"],
                "category": row["category"],
                "outcomes": ", ".join(str(o) for o in outcomes),
                "prices": ", ".join(f"{p:.3f}" for p in prices),
                "sum": round(total, 4),
                "deviation": round(deviation, 4),
                "volume_24hr": row["volume_24hr"],
                "liquidity": row["liquidity"],
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("deviation", key=abs, ascending=False).reset_index(drop=True)


def scan_cross_market_arb(
    df_markets: pd.DataFrame,
    min_markets: int = 3,
    min_total_liquidity: float = 1000.0,
    plausible_band: tuple[float, float] = (0.7, 1.3),
    min_deviation: float = 0.02,
) -> pd.DataFrame:
    """Find events whose constituent binary markets' YES prices don't sum to 1.

    Polymarket groups related binary markets under a single ``event_id``.
    Example: "2026 FIFA World Cup Winner" has many YES/NO sub-markets, one
    per team. If those teams are mutually exclusive and exhaustive (exactly
    one wins), the sum of YES prices should equal 1. When it doesn't, an
    arbitrage candidate exists:

    - sum < 1: buying one YES in every market guarantees a $1 payout when
      one resolves YES — implied edge = (1 - sum) / sum.
    - sum > 1: the inverse — buying NO across all (or selling YES) yields
      arbitrage when only one resolves YES.

    Parameters
    ----------
    df_markets
        DataFrame from ``get_markets()``. Must have ``event_id``,
        ``outcome_prices``, ``liquidity`` columns.
    min_markets
        Skip events with fewer constituent markets. Lower values produce
        more noise (single binary markets always sum to 1).
    min_total_liquidity
        Sum of ``liquidity`` across an event's markets must reach this to
        be reported. Filters out untradeable phantom arbs.
    plausible_band
        Only events whose YES-sum falls inside this range are reported.
        Wider ranges include events whose markets clearly aren't mutually
        exclusive (e.g., "Will inflation be > 5%?" AND "Will unemployment
        be > 4%?" — independent events whose sum has no constraint).
    min_deviation
        Minimum ``|sum - 1|`` to report. Events with sums near 1.0 don't
        have enough edge to bother with after transaction costs.

    Returns
    -------
    DataFrame sorted by absolute deviation (largest opportunities first):
        event_id, event_title, n_markets, sum_yes_prices, deviation,
        implied_edge_pct, total_liquidity, total_volume_24hr, category, markets_sample

    ``implied_edge_pct`` is ``(1 - sum) / sum`` (the profit fraction if you
    buy one YES of every market and exactly one resolves YES). Negative for
    sum > 1 — the math symmetrically supports selling/buying-NO arbitrage,
    but Polymarket doesn't directly support shorting, so that side requires
    buying every NO outcome.

    **This output is candidates, not guaranteed arbitrages.** Mutual
    exclusivity is the user's responsibility to verify by reading the
    sub-market questions. The plausibility band filters out obvious
    non-arbs but isn't perfect.
    """
    if df_markets is None or df_markets.empty:
        return pd.DataFrame()
    if "event_id" not in df_markets.columns:
        return pd.DataFrame()

    # Drop markets without event grouping
    df = df_markets[df_markets["event_id"].astype(str).ne("")].copy()
    if df.empty:
        return pd.DataFrame()

    # Per-row YES price (outcome 0). Skip rows without one.
    df["_yes"] = df["outcome_prices"].apply(
        lambda p: p[0] if isinstance(p, list) and len(p) >= 1 else None
    )
    df = df.dropna(subset=["_yes"])
    if df.empty:
        return pd.DataFrame()

    rows = []
    for event_id, group in df.groupby("event_id"):
        n = len(group)
        if n < min_markets:
            continue
        total_liq = float(group["liquidity"].sum())
        if total_liq < min_total_liquidity:
            continue
        sum_yes = float(group["_yes"].sum())
        # Filter to the plausible-arb band
        if not (plausible_band[0] <= sum_yes <= plausible_band[1]):
            continue
        deviation = sum_yes - 1.0
        if abs(deviation) < min_deviation:
            continue
        implied_edge_pct = (1.0 - sum_yes) / sum_yes if sum_yes > 0 else 0.0

        # Most common category in the event (usually all the same)
        cats = group["category"].dropna()
        category = cats.mode().iloc[0] if not cats.empty else ""

        # Title comes from enrich_categories_from_events; falls back to ""
        title = ""
        if "event_title" in group.columns:
            titles = group["event_title"].dropna()
            if not titles.empty:
                title = str(titles.iloc[0])

        sample = ", ".join(
            f"{q[:35]} @{p:.3f}"
            for q, p in zip(
                group["question"].head(4),
                group["_yes"].head(4),
                strict=False,
            )
        )

        rows.append({
            "event_id": event_id,
            "event_title": title,
            "category": category,
            "n_markets": n,
            "sum_yes_prices": round(sum_yes, 4),
            "deviation": round(deviation, 4),
            "implied_edge_pct": round(implied_edge_pct, 4),
            "total_liquidity": total_liq,
            "total_volume_24hr": float(group["volume_24hr"].sum()),
            "markets_sample": sample,
        })

    if not rows:
        return pd.DataFrame(columns=[
            "event_id", "event_title", "category", "n_markets",
            "sum_yes_prices", "deviation", "implied_edge_pct",
            "total_liquidity", "total_volume_24hr", "markets_sample",
        ])
    return (
        pd.DataFrame(rows)
        .sort_values("deviation", key=abs, ascending=False)
        .reset_index(drop=True)
    )
