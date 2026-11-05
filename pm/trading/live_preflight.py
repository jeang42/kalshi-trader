"""Read-only live-API preflight — verify the Kalshi reconciliation reads
work against the REAL account BEFORE any live order is placed.

Calls only authenticated **read** endpoints (balance, positions, fills,
resting orders) plus the `execution.LiveBroker` read paths that wrap them.
These need ``kalshi_auth.is_configured()`` but NOT ``KALSHI_LIVE_ENABLED``,
so this places nothing and *cannot* trade. Run it the moment credentials
are installed — before flipping ``LIVE_TRADING_PROFILES`` — to confirm:

* the owner-profile lock resolves (``is_configured()`` is True),
* our field-name assumptions match live response shapes,
* the LiveBroker normalizes real positions/balance without error.

It turns "the first real order is a blind integration test" into "the read
paths are already confirmed; only the write path is new."

CLI: ``python live_preflight.py``. Exit code 0 = all read checks passed.
"""

from __future__ import annotations

import argparse
import os


def _line(ok: bool | None, label: str, detail: str = "") -> str:
    mark = {True: "✓", False: "✗", None: "•"}[ok]
    return f"  {mark} {label}" + (f" — {detail}" if detail else "")


def run(profile: str | None = None) -> int:
    # Load .env so we see the same creds the UI does (app.py loads it; a bare
    # `python live_preflight.py` otherwise wouldn't).
    try:
        import dotenv
        dotenv.load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    # Simulate the daemon's process-local identity so the owner-profile lock
    # resolves the way it will at runtime (the live daemon pins this same env).
    if profile:
        os.environ["POLYMARKET_ACTIVE_PROFILE"] = profile

    from pm.venue import kalshi_auth as ka

    out: list[str] = []
    out.append("=" * 64)
    out.append("LIVE-API PREFLIGHT (read-only — places nothing)")
    if profile:
        out.append(f"simulating active profile = {profile!r}")
    out.append("=" * 64)

    # ── 1. Configuration / owner-profile lock ────────────────────────────
    key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    pem = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
    owner = ka.owner_profile()
    try:
        from pm.core import profiles
        active = profiles.active_profile()
    except Exception:  # noqa: BLE001
        active = "(profiles unavailable)"
    out.append("")
    out.append("Configuration:")
    out.append(_line(bool(key_id), "KALSHI_API_KEY_ID set",
                     f"prefix={key_id[:8]}" if key_id else "MISSING"))
    out.append(_line(bool(pem) and os.path.exists(pem),
                     "KALSHI_PRIVATE_KEY_PATH exists", pem or "MISSING"))
    out.append(_line(active == owner, "active profile == KALSHI_OWNER_PROFILE",
                     f"active={active!r} owner={owner!r}"))
    configured = ka.is_configured()
    out.append(_line(configured, "kalshi_auth.is_configured()",
                     "ready" if configured else
                     "FALSE — read calls will fail until fixed"))

    # KALSHI_LIVE_ENABLED is expected OFF during preflight (we place nothing).
    live_enabled = os.getenv("KALSHI_LIVE_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")
    out.append(_line(None, "KALSHI_LIVE_ENABLED",
                     f"{'ON' if live_enabled else 'off'} "
                     "(off is fine for preflight — orders stay disabled)"))

    if not configured:
        out.append("")
        out.append("STOP: not configured. Fix the above, then re-run. No "
                   "endpoints were called.")
        print("\n".join(out))
        return 1

    # ── 2. Authenticated READ endpoints (no order placement) ─────────────
    from pm.venue import kalshi_client as kc

    out.append("")
    out.append("Authenticated reads:")
    all_ok = True

    def _check(label: str, fn):
        nonlocal all_ok
        try:
            val = fn()
            out.append(_line(True, label, val))
        except Exception as e:  # noqa: BLE001
            all_ok = False
            out.append(_line(False, label, f"{type(e).__name__}: {e}"))

    _check("get_balance()",
           lambda: f"${kc.get_balance().get('balance_cents', 0) / 100:.2f} settled")
    _check("get_authed_positions()",
           lambda: f"{len(kc.get_authed_positions())} open position(s)")
    _check("get_fills()",
           lambda: f"{len(kc.get_fills(limit=10))} recent fill(s)")

    def _shard_funding() -> str:
        """Which exchange shards hold money.

        Kalshi split the exchange into `exchange_index` venues; an order for
        a market on an unfunded shard fails with a bare 404 `user_not_found`
        that names nothing. Surfacing the split here means that is caught
        BEFORE an order rather than by a failed placement (2026-09-09).
        """
        from pm.venue import kalshi_auth as ka
        names = {e.get("exchange_index"): e.get("description", "?")
                 for e in (ka.authed_get("exchange/status") or {})
                 .get("exchange_index_statuses", [])}
        funded = []
        for b in (ka.authed_get("portfolio/balance") or {}).get("balance_breakdown", []):
            i = b.get("exchange_index")
            amt = float(b.get("balance") or 0)
            if amt > 0:
                funded.append(f"{i}:{names.get(i, '?')} ${amt:.2f}")
        unfunded = [f"{i}:{n}" for i, n in sorted(names.items())
                    if i not in {int(f.split(":")[0]) for f in funded}]
        return (f"funded [{', '.join(funded) or 'NONE'}]"
                + (f" | unfunded (untradeable) [{', '.join(unfunded)}]" if unfunded else ""))

    _check("exchange shard funding", _shard_funding)

    from pm.venue import kalshi_orders as ko
    _check("get_resting_orders()",
           lambda: f"{len(ko.get_resting_orders(limit=10))} resting order(s)")

    # ── 3. LiveBroker read paths (our adapter against real responses) ────
    out.append("")
    out.append("LiveBroker reconciliation reads:")
    from pm.trading import execution
    lb = execution.LiveBroker()
    _check("LiveBroker.get_positions() normalizes",
           lambda: f"{len(lb.get_positions())} row(s), shape OK")
    _check("LiveBroker.available_cash(1.00)",
           lambda: f"${lb.available_cash(1.00):.2f} deployable at $1 bankroll")

    out.append("")
    out.append("=" * 64)
    if all_ok:
        out.append("PREFLIGHT PASSED — read paths verified against live Kalshi.")
        out.append("Next: set a tiny per-order cap, add the profile to "
                   "LIVE_TRADING_PROFILES, set KALSHI_LIVE_ENABLED=true, "
                   "restart the service, watch data/shared/orders.log.")
    else:
        out.append("PREFLIGHT FAILED — a read path errored above. Do NOT go "
                   "live until every read is ✓ (the reconciliation logic "
                   "depends on these exact responses).")
    out.append("=" * 64)
    print("\n".join(out))
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only live-API preflight.")
    p.add_argument("--profile", default=None,
                   help="Simulate this profile as the active/owner identity "
                        "(use the profile you intend to take live, e.g. "
                        "agentic_favorite_hold). Defaults to the global "
                        "active profile.")
    args = p.parse_args(argv)
    return run(profile=args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
