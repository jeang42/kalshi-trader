#!/usr/bin/env python3
"""Read-only Kalshi index-0 near-term coverage census.

Writes only to this research directory. Imports no project or trading modules.
The public endpoint is paginated to exhaustion; a partial fetch is explicitly
reported as incomplete and cannot support a no-candidate conclusion.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
API = "https://external-api.kalshi.com/trade-api/v2/markets"
PANEL = ROOT / "data/shared/candle_panel.sqlite"
UTC = dt.UTC


def request_page(cursor: str) -> dict:
    params = {"status": "open", "mve_filter": "exclude", "limit": "1000"}
    if cursor:
        params["cursor"] = cursor
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "edge-coverage-research/1.0"})
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 20))
        except (TimeoutError, urllib.error.URLError):
            if attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError("unreachable")


def category_map() -> dict[str, str]:
    con = sqlite3.connect(f"file:{PANEL}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    rows = con.execute(
        "SELECT substr(ticker,1,instr(ticker,'-')-1),category,COUNT(*) n "
        "FROM markets WHERE instr(ticker,'-')>0 GROUP BY 1,2 ORDER BY n DESC"
    )
    result = {}
    for series, category, _ in rows:
        result.setdefault(series, category)
    con.close()
    return result


def decimal(value: object) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def main() -> int:
    started = dt.datetime.now(UTC)
    cutoff = started + dt.timedelta(days=7)
    cats = category_map()
    cursor = ""
    digest = hashlib.sha256()
    total = pages = 0
    filtered = collections.Counter()
    rows = []
    error = None
    try:
        while True:
            page = request_page(cursor)
            markets = page.get("markets", [])
            pages += 1
            total += len(markets)
            for market in markets:
                digest.update(str(market.get("ticker", "")).encode())
                filtered["all_open_non_mve"] += 1
                if market.get("exchange_index") != 0:
                    continue
                filtered["index_0"] += 1
                close_text = market.get("close_time")
                if not close_text:
                    continue
                try:
                    close = dt.datetime.fromisoformat(close_text.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not started < close <= cutoff:
                    continue
                filtered["index_0_closing_7d"] += 1
                ticker = market.get("ticker", "")
                series = ticker.split("-", 1)[0]
                cat = cats.get(series, "Unknown")
                if cat in ("Sports", "Crypto"):
                    continue
                filtered["index_0_non_sport_crypto_closing_7d"] += 1
                bid = decimal(market.get("yes_bid_dollars"))
                ask = decimal(market.get("yes_ask_dollars"))
                volume_24h = decimal(market.get("volume_24h_fp"))
                bid_size = decimal(market.get("yes_bid_size_fp"))
                ask_size = decimal(market.get("yes_ask_size_fp"))
                quoted = 0 < bid <= ask < 1 and ask - bid <= .10
                liquid = quoted and volume_24h > 0 and min(bid_size, ask_size) >= 1
                if quoted:
                    filtered["two_sided_spread_le_10c"] += 1
                if liquid:
                    filtered["volume_and_depth_gate"] += 1
                rows.append({
                    "ticker": ticker,
                    "event": market.get("event_ticker"),
                    "series": series,
                    "category": cat,
                    "title": market.get("title", ""),
                    "subtitle": market.get("subtitle", ""),
                    "close_time": close_text,
                    "rules_primary": market.get("rules_primary", ""),
                    "rules_secondary": market.get("rules_secondary", ""),
                    "yes_bid": bid,
                    "yes_ask": ask,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    "volume_24h": volume_24h,
                    "quoted": quoted,
                    "liquid": liquid,
                })
            next_cursor = page.get("cursor") or ""
            print(f"page={pages} markets={total} index0_7d={filtered['index_0_closing_7d']}", flush=True)
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise RuntimeError("cursor did not advance")
            cursor = next_cursor
            time.sleep(.15)
    except Exception as exc:  # Preserve a partial census but mark it incomplete.
        error = repr(exc)

    by_series = collections.defaultdict(list)
    for row in rows:
        by_series[row["series"]].append(row)
    series = []
    for key, members in by_series.items():
        events = {r["event"] for r in members}
        liquid_events = {r["event"] for r in members if r["liquid"]}
        spreads = sorted(r["yes_ask"] - r["yes_bid"] for r in members if r["quoted"])
        example = max(members, key=lambda r: (r["liquid"], r["volume_24h"]))
        series.append({
            "series": key,
            "category": example["category"],
            "markets": len(members),
            "events_7d": len(events),
            "liquid_events_7d": len(liquid_events),
            "quoted_markets": sum(r["quoted"] for r in members),
            "liquid_markets": sum(r["liquid"] for r in members),
            "median_quoted_spread_cents": round(100 * spreads[len(spreads)//2], 2) if spreads else None,
            "volume_24h_sum": round(sum(r["volume_24h"] for r in members), 2),
            "example": example,
        })
    series.sort(key=lambda s: (-s["liquid_events_7d"], -s["events_7d"], -s["volume_24h_sum"]))
    summary = {
        "started_utc": started.isoformat(), "cutoff_utc": cutoff.isoformat(),
        "complete": error is None, "error": error, "pages": pages,
        "markets_seen": total, "ticker_sha256": digest.hexdigest(),
        "counts": dict(filtered), "series_count": len(series),
        "series": series,
    }
    (OUT / "live_census.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k:v for k,v in summary.items() if k != "series"}, indent=2))
    return 0 if error is None else 2


if __name__ == "__main__":
    sys.exit(main())
