#!/usr/bin/env python3
"""Read-only small-order incentive coverage and orderbook-depth screen."""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

from scan_incentives import get

OUT = Path(__file__).resolve().parent
UTC = dt.timezone.utc


def parse_date(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def batches(items: list, count: int):
    for offset in range(0, len(items), count):
        yield items[offset:offset + count]


def main() -> None:
    asof = dt.datetime.now(UTC)
    programs = json.loads((OUT / "active_liquidity_programs.json").read_text())["programs"]
    candidates = []
    for program in programs:
        start, end = parse_date(program["start_date"]), parse_date(program["end_date"])
        if not start <= asof < end:
            continue
        days = (end - start).total_seconds() / 86400
        target = float(program.get("target_size_fp") or 0)
        if days <= 0 or target <= 0:
            continue
        # period_reward is in centi-cents (1/10000 dollar).
        bound_day = program["period_reward"] / 10000 / days * min(1, 10 / target)
        if bound_day >= 1:
            candidates.append({**program, "bound_dollars_day": bound_day,
                               "pool_dollars_period": program["period_reward"] / 10000,
                               "duration_hours": days * 24})
    tickers = list(dict.fromkeys(p["market_ticker"] for p in candidates))
    cache = OUT / "incentive_markets_cache.json"
    markets = json.loads(cache.read_text()) if cache.exists() else {}
    for i, chunk in enumerate(batches([t for t in tickers if t not in markets], 30), 1):
        response = get("/markets", {"tickers": ",".join(chunk), "mve_filter": "exclude", "limit": 1000})
        for market in response.get("markets", []):
            markets[market["ticker"]] = market
        if response.get("cursor"):
            raise RuntimeError(f"unexpected market cursor on batch {i}")
        print(f"markets batch={i} found={len(markets)}", flush=True)
        time.sleep(.1)
    cache.write_text(json.dumps(markets) + "\n")
    funded = [p for p in candidates if markets.get(p["market_ticker"], {}).get("exchange_index") == 0]
    books = {}
    for i, chunk in enumerate(batches([p["market_ticker"] for p in funded], 10), 1):
        response = get("/markets/orderbooks", {"tickers": chunk})
        for book in response.get("orderbooks", []):
            books[book["ticker"]] = book
        print(f"books batch={i} found={len(books)}", flush=True)
        time.sleep(.1)
    rows = []
    for p in funded:
        ticker = p["market_ticker"]
        market = markets[ticker]
        book = books.get(ticker, {}).get("orderbook_fp") or {}
        yes = sum(float(level[1]) for level in book.get("yes_dollars", []))
        no = sum(float(level[1]) for level in book.get("no_dollars", []))
        target = float(p["target_size_fp"])
        rows.append({"ticker": ticker, "event": market.get("event_ticker"),
                     "series": ticker.split("-", 1)[0], "pool_dollars_period": p["pool_dollars_period"],
                     "duration_hours": p["duration_hours"], "target_size": target,
                     "discount_factor_bps": p.get("discount_factor_bps"),
                     "bound_dollars_day": p["bound_dollars_day"],
                     "bound_dollars_period": p["pool_dollars_period"] * min(1, 10 / target),
                     "yes_book_total": yes, "no_book_total": no,
                     "two_sided_target_met": yes >= target and no >= target,
                     "yes_bid": market.get("yes_bid_dollars"), "yes_ask": market.get("yes_ask_dollars"),
                     "yes_bid_size": market.get("yes_bid_size_fp"), "yes_ask_size": market.get("yes_ask_size_fp"),
                     "volume_24h": market.get("volume_24h_fp"),
                     "close_time": market.get("close_time")})
    qualifying = [r for r in rows if r["two_sided_target_met"]]
    summary = {"asof_utc": asof.isoformat(), "programs_stored": len(programs),
               "active_bound_ge_1_dollar_day": len(candidates), "funded_market_count": len(funded),
               "books_returned": len(books), "two_sided_target_met": len(qualifying),
               "sum_bound_dollars_day": sum(r["bound_dollars_day"] for r in qualifying),
               "rows": rows}
    (OUT / "incentive_depth_screen.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    print("top", json.dumps(sorted(qualifying, key=lambda r: -r["bound_dollars_day"])[:5], indent=2))


if __name__ == "__main__":
    main()
