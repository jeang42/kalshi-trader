"""Operator alerting — the one place anything in this system tells you something.

Why this exists
---------------
Nothing used to. Services ran under ``Restart=on-failure`` with no
``OnFailure=`` hook, the control plane was pull-only on a 30-second refresh,
and there was no metric, no digest and no push. An arm could die at 03:00 and
the first you would know is noticing a flat P&L days later. For a fleet that
places real orders unattended, "you have to look" is not a monitoring story.

Design rules
------------
1. **Never raise.** Every public function swallows its own errors. An alerting
   path that can crash the thing it is watching is worse than no alerting —
   this is called from ``systemd OnFailure=`` handlers and from inside arm
   cycles, and it must be incapable of making a bad situation worse.
2. **Every channel is optional and independent.** A missing phone topic must
   not stop the health log being written; a broken network must not stop the
   desktop notification.
3. **Never put money or secrets in a message body.** See
   ``docs/push-notifications.md`` — the push topic is protected only by being
   unguessable, so bodies say *"arm X failed, go look"*, never a balance, a
   position size or a key.

Channels
--------
====================  =====================================  ==================
Channel               Enabled by                             Reaches you
====================  =====================================  ==================
health log (JSONL)    always                                 control plane, grep
desktop notification  a graphical session being present      at the machine
ntfy push             ``NTFY_TOPIC`` set                     on your phone
====================  =====================================  ==================

CLI::

    python -m notify --test                     # end-to-end check of every channel
    python -m notify --event unit-failed --detail "fade-nearclose.service"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pm.core import logctx

log = logging.getLogger(__name__)

#: Append-only operator-visible event log. Read by the control plane and by the
#: daily digest; the one channel that always works.
DEFAULT_HEALTH_LOG = "data/shared/health_events.jsonl"

#: ntfy priority levels, lowest to highest urgency.
_PRIORITIES = ("min", "low", "default", "high", "urgent")


def health_log_path() -> Path:
    return Path(os.getenv("HEALTH_LOG_FILE", DEFAULT_HEALTH_LOG))


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ─── Channel 1: the health log ────────────────────────────────────────────


def record(event: str, detail: str = "", *, severity: str = "error",
           extra: dict | None = None) -> bool:
    """Append one event to the health log. Returns True on success.

    JSONL and append-only so concurrent writers cannot corrupt each other's
    lines the way a rewritten JSON document can — a single ``write()`` of a
    line under the O_APPEND flag is atomic for reasonable line lengths, which
    is exactly the property the ledger stores lacked.
    """
    try:
        path = health_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": _now(), "event": event, "severity": severity,
               "detail": detail}
        if extra:
            row.update(extra)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as e:  # noqa: BLE001 - alerting must never raise
        log.error("notify: could not write the health log: %s", e)
        return False


def recent_events(limit: int = 50, *, since_hours: float | None = None) -> list[dict]:
    """Most recent events, newest first. Returns [] rather than raising."""
    try:
        path = health_log_path()
        if not path.exists():
            return []
        rows: list[dict] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # a torn line must not hide the rest
        if since_hours is not None:
            cutoff = datetime.now(UTC).timestamp() - since_hours * 3600
            kept = []
            for r in rows:
                try:
                    if datetime.fromisoformat(r["ts"]).timestamp() >= cutoff:
                        kept.append(r)
                except (KeyError, ValueError):
                    continue
            rows = kept
        return list(reversed(rows))[:limit]
    except Exception as e:  # noqa: BLE001
        log.error("notify: could not read the health log: %s", e)
        return []


# ─── Channel 2: desktop notification ──────────────────────────────────────


def _desktop(title: str, body: str, *, urgent: bool = False) -> bool:
    """notify-send, when a graphical session is actually present.

    Guarded on DISPLAY/WAYLAND_DISPLAY because from a systemd *user* unit or a
    cron job there may be no session to draw on, and notify-send would just
    fail noisily every time.
    """
    try:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        exe = shutil.which("notify-send")
        if not exe:
            return False
        subprocess.run(
            [exe, "-u", "critical" if urgent else "normal", "-a", "polymarket",
             title, body],
            check=False, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("notify: desktop notification failed: %s", e)
        return False


# ─── Channel 3: phone push (ntfy) ─────────────────────────────────────────


def ntfy_configured() -> bool:
    return bool(os.getenv("NTFY_TOPIC", "").strip())


def _ntfy(title: str, body: str, *, priority: str | None = None,
          tags: str = "warning", click: str | None = None) -> bool:
    """POST to an ntfy topic. See docs/push-notifications.md for the model and,
    importantly, its security tradeoff: a public topic is readable by anyone
    who knows the name, so callers must keep money and secrets out of ``body``.
    """
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    try:
        import requests
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        prio = (priority or os.getenv("NTFY_PRIORITY") or "high").strip().lower()
        if prio not in _PRIORITIES:
            prio = "high"
        # HTTP headers are latin-1; an emoji title raised UnicodeEncodeError
        # and the push silently became log-only (found 2026-08-24 when the
        # storm trigger's 🌀 title hit it). ntfy supports RFC 2047 for exactly
        # this — encode only when needed so plain titles stay readable in
        # curl/proxies.
        try:
            title.encode("latin-1")
            wire_title = title
        except UnicodeEncodeError:
            from base64 import b64encode
            wire_title = "=?UTF-8?B?" + b64encode(title.encode("utf-8")).decode("ascii") + "?="
        headers = {"Title": wire_title, "Priority": prio, "Tags": tags}
        if click:
            headers["Click"] = click
        token = os.getenv("NTFY_TOKEN", "").strip()
        if token:                       # set when the topic is access-controlled
            headers["Authorization"] = f"Bearer {token}"
        r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - a dead network must not raise here
        log.error("notify: ntfy push failed: %s", e)
        return False


# ─── The public entry point ───────────────────────────────────────────────


def alert(title: str, body: str, *, severity: str = "error",
          event: str = "alert", tags: str = "warning",
          extra: dict | None = None) -> dict[str, bool]:
    """Send to every configured channel. Returns which ones succeeded.

    Never raises. A caller can ignore the return value entirely; it exists so
    ``--test`` can report what actually worked.
    """
    urgent = severity in ("critical", "error")
    results = {
        "health_log": record(event, body, severity=severity, extra=extra),
        "desktop": _desktop(title, body, urgent=urgent),
        "ntfy": _ntfy(title, body, tags=tags),
    }
    log.info("notify: %s -> %s", title,
             ", ".join(k for k, v in results.items() if v) or "no channel reached")
    return results


def unit_failed(unit: str) -> dict[str, bool]:
    """Called by the systemd OnFailure= template unit."""
    return alert(
        title="Service failed",
        body=f"{unit} entered a failed state at {_now()}. "
             f"Check: systemctl --user status {unit}",
        severity="error", event="unit_failed", tags="rotating_light",
        extra={"unit": unit},
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test", action="store_true",
                    help="send a test alert through every configured channel")
    ap.add_argument("--event", default="alert")
    ap.add_argument("--detail", default="")
    ap.add_argument("--title", default="polymarket")
    ap.add_argument("--severity", default="error")
    ap.add_argument("--unit", help="report this systemd unit as failed")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logctx.setup()

    if args.unit:
        res = unit_failed(args.unit)
    elif args.test:
        print("channels configured:")
        print(f"  health log : {health_log_path()}")
        print(f"  desktop    : {'yes' if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY') else 'no graphical session'}")
        print(f"  ntfy       : {'topic set' if ntfy_configured() else 'NTFY_TOPIC not set — see docs/push-notifications.md'}")
        res = alert(title="polymarket test",
                    body="Test alert. If this reached your phone, push works.",
                    severity="info", event="test", tags="white_check_mark")
    else:
        res = alert(title=args.title, body=args.detail,
                    severity=args.severity, event=args.event)

    for channel, ok in res.items():
        print(f"  {channel:<11} {'sent' if ok else 'not sent'}")
    # Exit 0 as long as the durable channel worked: a missing phone topic or an
    # absent desktop session is a configuration choice, not a failure.
    return 0 if res.get("health_log") else 1


if __name__ == "__main__":
    sys.exit(main())
