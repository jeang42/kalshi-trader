"""Did the live orders we placed actually settle — and does the venue agree?

Built 2026-09-09, the day the plumbing canary placed this system's first live
order since August. The canary exercised submit → fill → position → audit, but
**settlement is the one leg a session cannot watch**: the market closes hours
later, long after anyone is looking. Without this, "it settled fine" is an
assumption, and this project's rule is that unwatched money paths are exactly
where silent breakage lives (see the nightly-backup failure of 2026-08-25..28,
invisible for four nights because nothing checked the outcome).

Answers one question per ticker: **settled or still pending, and what did the
venue actually pay us.** Read-only — places nothing, cancels nothing.

Scope defaults to tickers OUR system placed (`live_status.placed_tickers()`
reads `orders.log`), so a shared account's manual trades are never reported on.

CLI:
    python -m pm.ops.settlement_check --profile jeff              # print
    python -m pm.ops.settlement_check --profile jeff --notify     # + phone push
    python -m pm.ops.settlement_check --ticker KXFOO-26SEP09-B1   # one market
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger(__name__)


def check(tickers: set[str] | None = None, *, limit: int = 200) -> dict:
    """Settlement state for ``tickers`` (default: everything we placed).

    Returns ``{"settled": [...], "pending": [...], "realized_cents": int}``.
    Each settled row carries ticker / result / revenue_cents / settled_time.
    """
    from pm.trading import live_status
    from pm.venue import kalshi_client as kc

    targets = tickers if tickers is not None else live_status.placed_tickers()
    if not targets:
        return {"settled": [], "pending": [], "realized_cents": 0}

    s = kc.get_settlements(limit=limit)
    settled_map: dict[str, dict] = {}
    if s is not None and not s.empty:
        for _, r in s.iterrows():
            tk = str(r.get("ticker") or "")
            if tk in targets:
                settled_map[tk] = {
                    "ticker": tk,
                    "result": str(r.get("market_result") or ""),
                    "revenue_cents": int(r.get("revenue_cents") or 0),
                    "settled_time": str(r.get("settled_time") or ""),
                }
    # Anything we placed that has NOT appeared in settlements yet is pending —
    # which is the normal state before close, and the alarming state long after.
    pending = sorted(t for t in targets if t not in settled_map)
    settled = [settled_map[t] for t in sorted(settled_map)]
    return {"settled": settled, "pending": pending,
            "realized_cents": sum(x["revenue_cents"] for x in settled)}


def format_report(res: dict) -> str:
    lines = []
    if res["settled"]:
        lines.append(f"settled ({len(res['settled'])}):")
        for x in res["settled"]:
            lines.append(f"  {x['ticker']} → {x['result'] or '?'}  "
                         f"paid ${x['revenue_cents'] / 100:.2f}  "
                         f"{x['settled_time'][:19]}")
        lines.append(f"  total paid out: ${res['realized_cents'] / 100:.2f}")
    if res["pending"]:
        lines.append(f"pending ({len(res['pending'])}):")
        lines.extend(f"  {t}" for t in res["pending"])
    return "\n".join(lines) or "nothing placed by this system yet"


def main(argv: list[str] | None = None) -> int:
    # gotcha #4: nothing loads .env unless it says so — and under systemd there
    # is no EnvironmentFile, so without this NTFY_TOPIC is absent and the push
    # silently goes nowhere. That already happened once, to storm_watch.
    try:
        from dotenv import load_dotenv

        from pm import REPO_ROOT
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:  # pragma: no cover
        pass

    from pm.core import logctx
    logctx.setup()

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default=os.getenv("KALSHI_OWNER_PROFILE", "jeff"),
                    help="Profile the owner-lock resolves to (gotcha 5).")
    ap.add_argument("--ticker", action="append", default=None,
                    help="Check only this ticker (repeatable).")
    ap.add_argument("--notify", action="store_true",
                    help="Push the result through pm.core.notify.")
    ap.add_argument("--wait-hours", type=float, default=0.0,
                    help="Poll until nothing is pending, up to this many hours, "
                         "then notify once. Beats guessing a settlement time: the "
                         "2026-09-09 canary settled 9 minutes AFTER a one-shot "
                         "timer fired, so the push said 'pending' and was useless.")
    ap.add_argument("--poll-minutes", type=float, default=10.0,
                    help="Interval for --wait-hours.")
    args = ap.parse_args(argv)

    # The authed reads only work when the ACTIVE profile is the owner — pin it
    # per-process the way a daemon does, never by writing the profile file.
    os.environ["POLYMARKET_ACTIVE_PROFILE"] = args.profile

    from pm.venue import kalshi_auth as ka
    if not ka.is_configured():
        print("not configured for the live account — check KALSHI_API_KEY_ID / "
              "KALSHI_PRIVATE_KEY_PATH and --profile")
        return 1

    targets = set(args.ticker) if args.ticker else None
    res = check(targets)
    if args.wait_hours > 0 and res["pending"]:
        import time

        from pm.core import shutdown
        shutdown.install()
        deadline = time.monotonic() + args.wait_hours * 3600
        while res["pending"] and time.monotonic() < deadline:
            if shutdown.sleep(max(1.0, args.poll_minutes * 60)):
                break
            res = check(targets)
        if res["pending"]:
            log.warning("still pending after %.1fh: %s", args.wait_hours,
                        ", ".join(res["pending"]))
    body = format_report(res)
    print(body)

    if args.notify:
        from pm.core import notify
        n_s, n_p = len(res["settled"]), len(res["pending"])
        notify.alert(
            title=f"Settlement: {n_s} settled, {n_p} pending",
            body=body[:1500],
            severity="info", event="settlement_check", tags="moneybag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
