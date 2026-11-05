"""Control-plane data collection — aggregate the fleet's state for the dashboard.

Pulls from existing surfaces (no new state): systemd for service health, each
capture module's ``stats()`` for panel growth, ``exits_analysis`` for paper-arm
P&L, the ``sidecars`` registry for sidecar health, and a best-effort read-only
Kalshi balance. Every collector is wrapped so one failing source degrades to a
placeholder rather than taking the whole dashboard down.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

# Curated fleet, grouped for the dashboard. Names are systemd --user units
# (without the .service suffix). Retired arms intentionally omitted.
# Fleet cleanup 2026-07-11: stopped everything serving a measured-dead thesis —
# MLB feeds (kalshi-ws, baseball-live, mlb-book-poller, pre-position: reaction
# edges falsified), reactive arms, tennis/wnba/news captures (gates read
# dead/too-slow), arb-scan (null over 3k markets). Re-enable any via
# `systemctl --user enable --now <svc>`; state DBs retained.
SERVICES: dict[str, list[str]] = {
    "Research idlers": ["near-close-capture", "mention-capture", "candle-panel",
                        ],
    "Sidecar UIs": ["baseball-web", "wnba-web", "tennis-web", "control-plane"],
}

# Paper-arm profiles to show P&L for (profile, display label).
ARMS: list[tuple[str, str]] = [
    ("agentic_reactive_hold", "Reactive-hold (retired — reaction falsified)"),
    ("agentic_reactive", "Reactive scalp (retired)"),
    ("agentic_favorite_hold", "Favorite-hold (retired)"),
]


# ─── systemd (pure parser + thin subprocess) ───────────────────────────────

def _parse_systemctl_show(text: str) -> dict[str, dict[str, str]]:
    """Parse `systemctl show <units> -p ...` output (blank-line-separated
    blocks of KEY=VALUE) into {unit_id: {key: value}}."""
    out: dict[str, dict[str, str]] = {}
    block: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if block.get("Id"):
                out[block["Id"]] = block
            block = {}
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            block[k] = v
    if block.get("Id"):
        out[block["Id"]] = block
    return out


def service_states() -> list[dict]:
    """Per-service status, grouped. active/enabled + a short uptime/detail."""
    units = [f"{n}.service" for grp in SERVICES.values() for n in grp]
    parsed: dict[str, dict[str, str]] = {}
    try:
        res = subprocess.run(
            ["systemctl", "--user", "show", *units,
             "-p", "Id", "-p", "ActiveState", "-p", "SubState",
             "-p", "UnitFileState", "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10)
        parsed = _parse_systemctl_show(res.stdout)
    except Exception as e:  # noqa: BLE001
        log.warning("systemctl show failed: %s", e)

    rows: list[dict] = []
    for group, names in SERVICES.items():
        for n in names:
            info = parsed.get(f"{n}.service", {})
            active = info.get("ActiveState", "unknown")
            rows.append({
                "group": group, "name": n,
                "active": active,
                "ok": active == "active",
                "enabled": info.get("UnitFileState", ""),
                "since": info.get("ActiveEnterTimestamp", ""),
                "sub": info.get("SubState", ""),
            })
    return rows


# ─── data panels (reuse each capture's stats()) ────────────────────────────

def panel_stats() -> list[dict]:
    """Row counts + freshness for each accruing research panel."""
    out: list[dict] = []

    def _safe(label: str, fn, count_key: str, detail_keys: list[str]):
        try:
            s = fn()
            detail = "  ".join(f"{k}={s.get(k)}" for k in detail_keys if k in s)
            out.append({"name": label, "count": s.get(count_key, 0),
                        "detail": detail, "ok": True})
        except Exception as e:  # noqa: BLE001
            out.append({"name": label, "count": "—", "detail": str(e)[:50],
                        "ok": False})

    # (fade-nearclose + back-nearclose panels removed 2026-08-24 with the
    # arms' retirement — the candle_panel retrospective closed the thesis.
    # No paper arms remain; the board shows the capture instruments, which
    # are also the re-entry detector.)
    from pm.capture import mention_capture, near_close_capture, news_capture, tennis_capture
    _safe("Mention repricing capture", mention_capture.stats, "total",
          ["tickers"])
    _safe("Near-close panel", near_close_capture.stats, "total",
          ["resolved", "pending"])
    _safe("News catalysts", news_capture.stats, "total",
          ["markets", "avg_lag_minutes"])
    _safe("Tennis events", tennis_capture.stats, "total",
          ["matched_to_market"])
    return out


# ─── paper-arm P&L (reuse exits_analysis) ──────────────────────────────────

def arm_pnl() -> list[dict]:
    from pm.ledger import exits_analysis as ea
    out: list[dict] = []
    for profile, label in ARMS:
        try:
            df = ea.load_exits(profile)
            if df is None or df.empty or "pnl_dollars" not in df:
                out.append({"label": label, "n": 0, "net": 0.0, "win": None})
                continue
            out.append({
                "label": label, "n": int(len(df)),
                "net": round(float(df["pnl_dollars"].sum()), 2),
                "win": round(100 * float((df["pnl_dollars"] > 0).mean())),
            })
        except Exception as e:  # noqa: BLE001
            out.append({"label": label, "n": "—", "net": "—",
                        "win": None, "error": str(e)[:50]})
    return out


# ─── sidecars + live account ───────────────────────────────────────────────

def sidecar_health() -> list[dict]:
    out: list[dict] = []
    try:
        from pm.core import sidecars
        for sc in sidecars.list_sidecars():
            try:
                h = sidecars.safe_health(sc.health)
                out.append({"name": getattr(sc, "display_name", sc.name),
                            "status": h.status, "detail": h.detail[:60],
                            "errors_24h": h.errors_24h})
            except Exception as e:  # noqa: BLE001
                out.append({"name": getattr(sc, "name", "?"),
                            "status": "down", "detail": str(e)[:60],
                            "errors_24h": 0})
    except Exception as e:  # noqa: BLE001
        log.debug("sidecar health unavailable: %s", e)
    return out


def live_account() -> dict | None:
    """Best-effort read-only Kalshi balance. None if not configured/auth'd."""
    try:
        from pm.venue import kalshi_auth, kalshi_client
        # Read as the live-account owner so the panel works regardless of
        # which paper profile is active (read-only; does not enable trading).
        with kalshi_auth.owner_scope():
            bal = kalshi_client.get_balance()
        return {"balance": bal.get("balance_cents", 0) / 100.0,
                "payout": bal.get("payout_cents", 0) / 100.0}
    except Exception as e:  # noqa: BLE001
        log.debug("live account unavailable: %s", e)
        return None


def snapshot() -> dict:
    """Everything the dashboard renders, in one call."""
    from datetime import UTC, datetime
    services = service_states()
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "services": services,
        "service_groups": SERVICES,
        "services_up": sum(1 for s in services if s["ok"]),
        "services_total": len(services),
        "panels": panel_stats(),
        "arms": arm_pnl(),
        "sidecars": sidecar_health(),
        "account": live_account(),
    }
