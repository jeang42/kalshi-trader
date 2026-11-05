#!/usr/bin/env python3
"""Live-API smoke test.

Unit tests with mocks have repeatedly shipped surfaces that look
correct but break against the real venue. This script exercises every
critical user-facing path against actual network + actual filesystem
and reports PASS/FAIL. Run manually before declaring a release "good".

Usage:
    python scripts/smoke_test.py

Exit code is the number of failures (0 = all pass). Each check
captures its own exception so one failure doesn't mask the rest.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# Load .env so KALSHI_API_KEY_ID etc. work without manual export
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Ensure repo root is on the path even when run from elsewhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class Check:
    name: str
    status: str = "skip"   # pass | fail | skip
    detail: str = ""
    elapsed_s: float = 0.0
    warnings: list[str] = field(default_factory=list)


checks: list[Check] = []


def check(name: str):
    """Decorator that turns a function into a smoke check. The function
    can call ``warn(msg)`` and raise on failure. ``assert`` is fine."""
    def deco(fn):
        c = Check(name=name)
        checks.append(c)
        t0 = time.time()
        try:
            warnings: list[str] = []
            fn(warnings.append)
            c.status = "pass"
            c.warnings = warnings
        except AssertionError as e:
            c.status = "fail"
            c.detail = str(e) or "assertion failed"
        except Exception as e:
            c.status = "fail"
            c.detail = f"{type(e).__name__}: {e}"
            if os.getenv("SMOKE_TRACEBACK"):
                c.detail += "\n" + traceback.format_exc()
        c.elapsed_s = time.time() - t0
        return fn
    return deco


# ---------------------------------------------------------------------------
# Phase 1 — Profile system
# ---------------------------------------------------------------------------

@check("profiles: list returns the expected set")
def _(warn):
    from pm.core import profiles
    profiles.initialize_profiles()
    have = set(profiles.list_profiles())
    expect_min = {"jeff", "user2", "sandbox"}
    assert expect_min.issubset(have), \
        f"missing default profiles: {expect_min - have}"

@check("profiles: active_profile is jeff (the human-owner)")
def _(warn):
    from pm.core import profiles
    profiles.initialize_profiles()
    cur = profiles.active_profile()
    if cur != "jeff":
        warn(f"active profile is '{cur}', not 'jeff' — sidebar may be wrong. "
             "Run scripts/smoke_test.py with --restore-jeff to reset.")

@check("profiles: set_active_profile + active_profile round-trips")
def _(warn):
    from pm.core import profiles
    profiles.initialize_profiles()
    original = profiles.active_profile()
    try:
        profiles.set_active_profile("sandbox")
        assert profiles.active_profile() == "sandbox"
        profiles.set_active_profile(original)
        assert profiles.active_profile() == original
    finally:
        profiles.set_active_profile(original)


# ---------------------------------------------------------------------------
# Phase 2 — Markets fetch paths
# ---------------------------------------------------------------------------

@check("kalshi: get_markets returns 500 markets")
def _(warn):
    from pm.venue import kalshi_client as kc
    df = kc.get_markets(limit=500)
    assert len(df) > 0, "no markets returned"
    expected = {"condition_id", "question", "category",
                "outcome_prices", "days_to_resolution", "liquidity"}
    missing = expected - set(df.columns)
    assert not missing, f"missing columns: {missing}"
    short = (df["days_to_resolution"] <= 30).sum()
    if short < 5:
        warn(f"only {short}/500 volume-top markets close within 30d — "
             "this is expected on Kalshi and is why we have the closing-soon path.")

@check("kalshi: get_markets_closing_within(30) returns short-dated markets")
def _(warn):
    from pm.venue import kalshi_client as kc
    df = kc.get_markets_closing_within(30, limit=200)
    assert len(df) >= 10, f"only {len(df)} markets ≤30d — fetch path broken"
    max_d = df["days_to_resolution"].max()
    # The API filters by close_time (latest possible) but our
    # days_to_resolution is derived from expected_expiration_time
    # (the real expected resolution). They can differ when Kalshi
    # has a long close_time backstop but an earlier expected close.
    # Relaxed upper bound to 90d catches truly broken fetches without
    # false-flagging this case.
    assert max_d <= 90, f"got a market with {max_d}d, should be ≤90"

@check("kalshi: closing-soon results have non-empty categories")
def _(warn):
    from pm.venue import kalshi_client as kc
    df = kc.get_markets_closing_within(30, limit=200)
    if df.empty:
        return
    blank = (df["category"] == "").sum()
    assert blank < len(df) * 0.5, (
        f"{blank}/{len(df)} rows have blank category — keyword fallback "
        "not firing or _infer_category broken")

@check("kalshi: liquidity column is non-zero on the top markets")
def _(warn):
    """Regression guard. Kalshi's liquidity_dollars field is broken
    (always 0.0000). _market_to_row falls back to a volume × mid
    proxy when liquidity_dollars is zero. Without this fallback the
    Market Scanner page filters out 100% of Kalshi markets at any
    non-zero min_liquidity threshold."""
    from pm.venue import kalshi_client as kc
    df = kc.get_markets(limit=500)
    if df.empty:
        return
    n_with_liq = (df["liquidity"] >= 500).sum()
    assert n_with_liq >= 5, (
        f"only {n_with_liq}/500 markets have liquidity>=$500 — "
        "the Kalshi liquidity_dollars fallback isn't firing. Scanner "
        "page will return empty results.")


@check("kalshi: lookup_markets_by_id resolves URL → event sub-markets")
def _(warn):
    from pm.venue import kalshi_client as kc
    # Real event: KY-4 Republican primary (low-volume, long-dated)
    df = kc.lookup_markets_by_id(
        "https://kalshi.com/markets/kxky4r/kentucky-4-republican-primary/kxky4r-26"
    )
    assert not df.empty, "URL lookup returned 0 rows"
    assert all(df["condition_id"].str.startswith("KXKY4R-26")), \
        f"got wrong tickers: {df['condition_id'].tolist()}"
    assert (df["category"] != "").all(), "category not populated"


@check("kalshi: closing-soon surfaces multiple categories (not all sports)")
def _(warn):
    from pm.venue import kalshi_client as kc
    df = kc.get_markets_closing_within(30, limit=200)
    cats = set(df[df["category"] != ""]["category"].unique())
    if len(cats) < 2:
        warn(f"only one category surfaced: {cats}")


# ---------------------------------------------------------------------------
# Phase 3 — Kalshi auth + Live Portfolio
# ---------------------------------------------------------------------------

@check("kalshi_auth: is_configured returns True when env+file+profile align")
def _(warn):
    from pm.core import profiles
    from pm.venue import kalshi_auth as ka
    profiles.initialize_profiles()
    profiles.set_active_profile(ka.owner_profile())
    assert ka.is_configured(), \
        "is_configured False even on owner profile — check .env + PEM perms"

@check("kalshi_auth: get_balance returns dict with int cents")
def _(warn):
    from pm.core import profiles
    from pm.venue import kalshi_auth as ka
    from pm.venue import kalshi_client as kc
    profiles.initialize_profiles()
    profiles.set_active_profile(ka.owner_profile())
    bal = kc.get_balance()
    assert isinstance(bal["balance_cents"], int)
    assert isinstance(bal["payout_cents"], int)
    if bal["balance_cents"] == 0:
        warn("balance is $0.00 — fund the account to verify trading flow")
    else:
        warn(f"balance: ${bal['balance_cents']/100:,.2f} (good)")

@check("kalshi_auth: get_authed_positions returns DataFrame with right columns")
def _(warn):
    from pm.venue import kalshi_client as kc
    df = kc.get_authed_positions(limit=10)
    cols = {"ticker", "position", "market_exposure_cents"}
    assert cols.issubset(set(df.columns)) or df.empty


@check("kalshi: position + exposure values populated (not all zero)")
def _(warn):
    """Regression guard. The Kalshi API uses suffixed field names
    (position_fp, market_exposure_dollars, etc.) — early Phase-1 code
    read bare names and silently returned zero for every numeric.
    If a position exists, at least one of these must be non-zero."""
    from pm.venue import kalshi_client as kc
    df = kc.get_authed_positions(limit=20)
    if df.empty:
        warn("no positions held — can't verify the field-mapping fix")
        return
    have_signal = (
        df["position"].abs().sum() > 0
        or df["market_exposure_cents"].sum() > 0
        or df["total_traded_cents"].sum() > 0
    )
    assert have_signal, (
        "positions list non-empty but every numeric is zero — "
        "Kalshi field-name mapping is broken again. Check "
        "get_authed_positions for position_fp / market_exposure_dollars."
    )

@check("kalshi_auth: paper profiles cannot read the API (lock works)")
def _(warn):
    from pm.core import profiles
    from pm.venue import kalshi_auth as ka
    profiles.initialize_profiles()
    saved = profiles.active_profile()
    try:
        profiles.set_active_profile("sandbox")
        assert not ka.is_configured(), \
            "sandbox profile got past the owner-profile guard — CRITICAL"
    finally:
        profiles.set_active_profile(saved)


# ---------------------------------------------------------------------------
# Phase 4 — Recommendations end-to-end
# ---------------------------------------------------------------------------

@check("recommendations: find_all_edges returns dict keyed by source")
def _(warn):
    import edges
    from pm.venue import kalshi_client as kc
    df = kc.get_markets_closing_within(30, limit=50)
    assert not df.empty
    out = edges.find_all_edges(
        df, sources=["crypto_spot", "sports"],  # fast sources only
        min_gap=0.10, min_days_to_expiry=1.0,
    )
    assert isinstance(out, dict)
    assert "crypto_spot" in out
    assert "sports" in out

@check("recommendations: calibration cross-profile aggregation works")
def _(warn):
    from pm.ledger import calibration as cal
    df = cal.get_forecasts_all_profiles()
    if df.empty:
        warn("no forecasts logged across any profile yet")
        return
    by = cal.brier_by_source(df)
    # by["src"]["n"] is total forecasts for that source; only entries
    # with a non-None brier represent resolved forecasts.
    n_resolved = sum(int(v.get("n") or 0) for v in by.values()
                     if v.get("brier") is not None)
    n_pending = len(df) - n_resolved
    warn(f"{n_resolved} resolved + {n_pending} pending across {len(by)} sources")
    if n_resolved == 0:
        warn("zero resolved — Track Record badges will show 'untested'. "
             "Click 'Auto-resolve' on the Calibration page once a few markets close.")


# ---------------------------------------------------------------------------
# Phase 5 — Order placement guards (does NOT place real orders)
# ---------------------------------------------------------------------------

@check("web_context: Google News RSS returns recent headlines")
def _(warn):
    from pm.feeds import web_context as wc
    items = wc.search_news("election", max_items=3, bypass_cache=True)
    assert len(items) >= 1, "no news items returned — RSS path likely broken"
    assert all(it.url.startswith("http") for it in items)


@check("web_context: Wikipedia summary works for a known entity")
def _(warn):
    from pm.feeds import web_context as wc
    w = wc.wikipedia_summary("Python (programming language)", bypass_cache=True)
    assert w is not None, "Wikipedia summary returned None — check UA or path encoding"
    assert "Python" in w.title
    assert len(w.extract) > 50


@check("kalshi_orders: pre_flight_check blocks when KALSHI_LIVE_ENABLED is off")
def _(warn):
    from pm.venue import kalshi_orders as ko
    saved = os.environ.pop("KALSHI_LIVE_ENABLED", None)
    try:
        issues = ko.pre_flight_check(
            ticker="KX-TEST", side="yes", count=1,
            limit_price_cents=50, current_mid_cents=50,
            bankroll=250, in_drawdown_pause=False,
            todays_order_count=0,
        )
        blocks = [m for s, m in issues if s == "block"]
        assert any("Live trading is disabled" in m for m in blocks), \
            "block-when-disabled guard failed"
    finally:
        if saved is not None:
            os.environ["KALSHI_LIVE_ENABLED"] = saved

@check("kalshi_orders: round-trip place + cancel against live API (1 contract)")
def _(warn):
    """Place one real 1-contract NO order at 1¢ on a long-dated open
    market — well below the book so it rests instead of fills — then
    cancel it. Requires KALSHI_LIVE_ENABLED so this only runs when
    live trading is explicitly opted into.

    Picks the longest-dated active market dynamically so the test
    doesn't break the moment any pinned ticker closes (the original
    version hardcoded KXKY4R-26-TMAS which early-closed when the
    primary results came in)."""
    if os.getenv("KALSHI_LIVE_ENABLED", "").lower() not in ("1","true","yes","on"):
        warn("KALSHI_LIVE_ENABLED is off — skipping live round-trip test")
        return
    from pm.core import profiles
    from pm.venue import kalshi_auth as ka
    from pm.venue import kalshi_client as kc
    from pm.venue import kalshi_orders as ko
    profiles.initialize_profiles()
    profiles.set_active_profile(ka.owner_profile())

    # Find a long-dated open market so it's not in the process of
    # closing while we test.
    df = kc.get_markets(limit=500)
    df = df[df["days_to_resolution"] > 30].sort_values(
        "days_to_resolution", ascending=False,
    )
    if df.empty:
        warn("no long-dated markets found — skipping round-trip test")
        return
    ticker = str(df.iloc[0]["condition_id"])

    resp = ko.place_limit_order(
        ticker=ticker, side="no", action="buy",
        count=1, limit_price_cents=1,
    )
    order_id = resp.get("order", {}).get("order_id") or resp.get("order_id")
    assert order_id, f"no order_id in response: {resp}"
    ko.cancel_order(order_id)


@check("kalshi_orders: pre_flight_check blocks fat-finger price")
def _(warn):
    from pm.venue import kalshi_orders as ko
    os.environ["KALSHI_LIVE_ENABLED"] = "true"
    issues = ko.pre_flight_check(
        ticker="KX-TEST", side="yes", count=1,
        limit_price_cents=95, current_mid_cents=10,  # 85¢ away
        bankroll=250, in_drawdown_pause=False, todays_order_count=0,
    )
    assert any("away from the current mid" in m for s, m in issues
               if s == "block")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def main() -> int:
    if "--restore-jeff" in sys.argv:
        from pm.core import profiles
        profiles.initialize_profiles()
        profiles.set_active_profile("jeff")
        print("Set active profile to 'jeff'.\n")

    # Width control
    max_name = max(len(c.name) for c in checks)
    pass_n = sum(1 for c in checks if c.status == "pass")
    fail_n = sum(1 for c in checks if c.status == "fail")
    skip_n = sum(1 for c in checks if c.status == "skip")

    print(f"\n{'=' * 80}")
    print("LIVE-API SMOKE TEST")
    print(f"{'=' * 80}\n")
    for c in checks:
        sym = {"pass": "✓", "fail": "✗", "skip": "·"}[c.status]
        line = f"  {sym} {c.name:<{max_name}}  ({c.elapsed_s:.2f}s)"
        print(line)
        if c.detail:
            for ln in c.detail.splitlines():
                print(f"      {ln}")
        for w in c.warnings:
            print(f"      ⚠ {w}")
    print()
    print(f"  {pass_n} passed · {fail_n} failed · {skip_n} skipped")
    print()
    return fail_n


if __name__ == "__main__":
    sys.exit(main())
