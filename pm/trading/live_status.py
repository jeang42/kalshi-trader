"""Live account status — read-only readout of real Kalshi positions + P&L.

Shows settled balance + pending payout, every open real position (entry vs
current mark + unrealized P&L), and recent settlements (won/lost + realized
P&L) so you can watch a live canary resolve at a glance.

Read-only: calls only authenticated GET endpoints (balance / positions /
fills / settlements) — places nothing, cancels nothing. Needs
``kalshi_auth.is_configured()`` (loads ``.env`` like the daemons); pass
``--profile`` to resolve the owner lock to the live profile.

CLI::

    python live_status.py --profile agentic_favorite_hold
    python live_status.py --profile agentic_favorite_hold --watch 30   # refresh
"""

from __future__ import annotations

import argparse
import os
import time

import pandas as pd


def cost_basis_by_ticker(fills: pd.DataFrame) -> dict[str, dict]:
    """Per-ticker buy-side cost basis from the fills tape.

    Returns ``{ticker: {"buy_count", "buy_cost", "avg_price"}}`` summing the
    BUY fills (favorite-hold is buy-only; for the side actually bought the
    price is yes_price_cents for a yes buy, no_price_cents for a no buy).
    avg_price is in dollars. Sells are ignored — this is entry cost, used to
    mark open positions and to compute realized P&L on settlement.

    >>> import pandas as pd
    >>> f = pd.DataFrame([
    ...     {"ticker": "T", "action": "buy", "side": "yes",
    ...      "count": 3, "yes_price_cents": 60, "no_price_cents": 40},
    ...     {"ticker": "T", "action": "buy", "side": "yes",
    ...      "count": 1, "yes_price_cents": 64, "no_price_cents": 36}])
    >>> cb = cost_basis_by_ticker(f)["T"]
    >>> cb["buy_count"], round(cb["buy_cost"], 2), round(cb["avg_price"], 3)
    (4.0, 2.44, 0.61)
    """
    out: dict[str, dict] = {}
    if fills is None or fills.empty:
        return out
    for _, f in fills.iterrows():
        if str(f.get("action")) != "buy":
            continue
        side = str(f.get("side"))
        price_c = float(f.get("yes_price_cents") if side == "yes"
                        else f.get("no_price_cents") or 0)
        count = float(f.get("count") or 0)
        d = out.setdefault(str(f.get("ticker")),
                           {"buy_count": 0.0, "buy_cost": 0.0})
        d["buy_count"] += count
        d["buy_cost"] += count * price_c / 100.0
    for d in out.values():
        d["avg_price"] = (d["buy_cost"] / d["buy_count"]) if d["buy_count"] else 0.0
    return out


def placed_tickers() -> set[str]:
    """Tickers our system actually placed orders for, from the audit log
    (``data/shared/orders.log``). Lets the readout show just our daemon's /
    UI's live bets rather than the whole Kalshi account (which may hold many
    manually-placed positions). Empty set if the log is absent.

    Reads the **rotated** copies too (``orders.log.1``, ``orders.log.*.gz``):
    logrotate truncates the live file daily, so the current orders.log can be
    empty even though we placed orders — without this the readout would scope
    to nothing after a rotation."""
    import gzip
    import json

    from pm.venue import kalshi_orders as ko
    out: set[str] = set()
    base = ko._orders_log_path()
    paths = [base] + sorted(base.parent.glob(base.name + ".*"))
    for path in paths:
        if not path.exists():
            continue
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tk = (row.get("body") or {}).get("ticker")
            if tk:
                out.add(str(tk))
    return out


def _fmt_money(cents: float) -> str:
    return f"${cents / 100:,.2f}"


def build_report(only_tickers: set[str] | None = None) -> str:
    from pm.venue import kalshi_client as kc

    lines: list[str] = []
    bal = kc.get_balance()
    settled = bal.get("balance_cents", 0)
    payout = bal.get("payout_cents", 0)
    positions = kc.get_authed_positions()
    fills = kc.get_fills(limit=500)
    settlements = kc.get_settlements(limit=200)
    cb = cost_basis_by_ticker(fills)

    # Scope to tickers our system placed (unless --all). Keeps the readout
    # about the canary, not the whole hand-traded account.
    if only_tickers is not None:
        if positions is not None and not positions.empty:
            positions = positions[positions["ticker"].astype(str).isin(only_tickers)]
        if settlements is not None and not settlements.empty:
            settlements = settlements[
                settlements["ticker"].astype(str).isin(only_tickers)]

    scope = ("our system's trades (from orders.log)" if only_tickers is not None
             else "ENTIRE Kalshi account")
    lines.append("=" * 70)
    lines.append("LIVE ACCOUNT STATUS (read-only)")
    lines.append(f"  settled cash: {_fmt_money(settled)}    "
                 f"pending payout: {_fmt_money(payout)}  (whole account)")
    lines.append(f"  positions/settlements shown: {scope}")
    lines.append("=" * 70)

    # ── Open positions ───────────────────────────────────────────────────
    lines.append("")
    if positions is None or positions.empty:
        lines.append("OPEN POSITIONS: none")
    else:
        lines.append(f"OPEN POSITIONS ({len(positions)}):")
        lines.append(f"  {'ticker':38s} {'side':4s} {'cnt':>4s} "
                     f"{'entry':>6s} {'value':>8s} {'uPnL':>8s}")
        tot_cost = tot_val = 0.0
        for _, p in positions.iterrows():
            tk = str(p.get("ticker", ""))
            pos = float(p.get("position", 0) or 0)
            side = "YES" if pos > 0 else "NO"
            cnt = abs(pos)
            value = float(p.get("market_exposure_cents", 0) or 0) / 100.0
            entry = cb.get(tk, {}).get("avg_price", 0.0)
            cost = entry * cnt
            upnl = value - cost
            tot_cost += cost
            tot_val += value
            lines.append(f"  {tk[:38]:38s} {side:4s} {cnt:>4.0f} "
                         f"{entry:>6.2f} {value:>8.2f} {upnl:>+8.2f}")
        lines.append(f"  {'':38s} {'':4s} {'':>4s} {'cost':>6s} "
                     f"{tot_val:>8.2f} {tot_val - tot_cost:>+8.2f}")
        lines.append(f"  deployed cost ${tot_cost:,.2f} | "
                     f"current value ${tot_val:,.2f} | "
                     f"unrealized ${tot_val - tot_cost:+,.2f}")

    # ── Recent settlements ───────────────────────────────────────────────
    lines.append("")
    if settlements is None or settlements.empty:
        lines.append("RECENT SETTLEMENTS: none yet")
    else:
        lines.append(f"RECENT SETTLEMENTS ({len(settlements)}):")
        lines.append(f"  {'ticker':38s} {'held':4s} {'won?':4s} "
                     f"{'cost':>7s} {'revenue':>8s} {'P&L':>8s}")
        realized = 0.0
        for _, s in settlements.iterrows():
            tk = str(s.get("ticker", ""))
            yes_c = float(s.get("yes_count", 0) or 0)
            held = "YES" if yes_c > 0 else "NO"
            revenue = float(s.get("revenue_cents", 0) or 0) / 100.0
            # Won iff the contract paid out (revenue > 0) — unambiguous for a
            # held-to-resolution binary, and avoids market_result parsing.
            won = "✓" if revenue > 0 else "✗"
            cost = cb.get(tk, {}).get("buy_cost", 0.0)
            pnl = revenue - cost
            realized += pnl
            lines.append(f"  {tk[:38]:38s} {held:4s} {won:^4s} "
                         f"{cost:>7.2f} {revenue:>8.2f} {pnl:>+8.2f}")
        lines.append(f"  realized P&L over these {len(settlements)} "
                     f"settlements: ${realized:+,.2f}  (approx; cost excl. fees)")

    lines.append("=" * 70)
    return "\n".join(lines)


def run(profile: str | None = None, *, show_all: bool = False) -> int:
    try:
        import dotenv
        dotenv.load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    if profile:
        os.environ["POLYMARKET_ACTIVE_PROFILE"] = profile

    from pm.venue import kalshi_auth as ka
    if not ka.is_configured():
        print("Not configured for the live account (kalshi_auth.is_configured() "
              "is False).\nCheck KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH and "
              "pass --profile <owner profile>.")
        return 1
    only = None if show_all else placed_tickers()
    print(build_report(only_tickers=only))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only live account status.")
    p.add_argument("--profile", default=None,
                   help="Resolve the owner lock to this profile (the live one).")
    p.add_argument("--all", action="store_true",
                   help="Show the whole account (default: only tickers our "
                        "system placed, from orders.log).")
    p.add_argument("--watch", type=float, default=0.0,
                   help="Refresh every N seconds (Ctrl-C to stop).")
    args = p.parse_args(argv)
    if args.watch and args.watch > 0:
        try:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                rc = run(profile=args.profile, show_all=args.all)
                if rc != 0:
                    return rc
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    return run(profile=args.profile, show_all=args.all)


if __name__ == "__main__":
    raise SystemExit(main())
