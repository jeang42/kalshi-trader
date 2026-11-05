#!/usr/bin/env python3
"""Read-only public liquidity-program census; never submits an order."""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent
BASE = "https://external-api.kalshi.com/trade-api/v2"


def get(path: str, params: dict | None = None) -> dict:
    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "edge-research/1.0"}), timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc
            time.sleep(min(2**attempt, 20))
        except (TimeoutError, urllib.error.URLError):
            if attempt == 5:
                raise
            time.sleep(min(2**attempt, 20))
    raise RuntimeError("unreachable")


def main() -> None:
    started = dt.datetime.now(dt.UTC)
    cursor = ""
    programs = []
    pages = 0
    while True:
        params = {"status": "active", "type": "liquidity", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = get("/incentive_programs", params)
        pages += 1
        programs += page.get("incentive_programs", [])
        next_cursor = page.get("next_cursor") or ""
        if not next_cursor:
            break
        if next_cursor == cursor:
            raise RuntimeError("cursor did not advance")
        cursor = next_cursor
        time.sleep(.15)
    summary = {"started_utc": started.isoformat(), "pages": pages, "program_count": len(programs), "programs": programs}
    (OUT / "active_liquidity_programs.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "programs"}, indent=2))
    print("sample", json.dumps(programs[:2], indent=2))


if __name__ == "__main__":
    main()
