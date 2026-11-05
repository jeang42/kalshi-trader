"""HDHomeRun FLEX QUATRO helper — pinned-tuner OTA stream URLs for the
mention-listening pipeline (``mention_listen.py``).

Context: we have one HDHomeRun FLEX QUATRO (4 tuners) at ``device_ip()``
shared with Plex Media Server on the same LAN. Plex tunes via
``/auto/v<CH>``, which the device resolves to whichever tuner is free —
almost always the lowest index (0 or 1) since Plex is usually first to
grab a tuner. If the mention pipeline also used ``/auto`` it could
collide with Plex (both landing on the same tuner) or get starved when
all 4 are busy.

**Design**: the pipeline PINS a specific tuner index (default
``PIPELINE_TUNER = 3``, the top of the range) via the explicit
``/tuner<N>/v<CHANNEL>`` URL form. Plex stays on ``/auto`` and will only
ever reach for tuners 0-2 in normal use, so the two consumers don't
fight over the same tuner. This is a *soft* convention (nothing stops
Plex from eventually reaching tuner3 if 0-2 are all busy) reinforced by
a *best-effort hard* reservation: ``acquire_lock`` sets a lockkey on the
pinned tuner via the ``hdhomerun_config`` CLI tool when it's installed.
When it isn't, the effective reservation is simply holding the HTTP
stream connection open (which the pipeline does anyway for the
duration of a listen session) — the device won't hand the same tuner
to a second HTTP client while a stream is being read from it in
practice, but a lockkey is the documented hard guarantee.

No trading imports. Pure/thin: URL building and channel-name resolution
are pure; ``discover``/``channel_present``/``acquire_lock``/
``release_lock`` are network/subprocess calls, kept separate so the
pure logic is unit-testable without a live device.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

import requests

log = logging.getLogger(__name__)

#: Where the tuner lives when HDHR_IP is unset. This is an RFC 5737
#: documentation address — a deliberate placeholder that will never route:
#: set HDHR_IP in .env to your tuner's real address (find it via
#: ``curl http://<tuner-ip>/discover.json``; a DHCP reservation on the
#: router keeps it stable). Resolved PER CALL via device_ip(), never frozen
#: at import (gotcha 1): a DHCP move once had the recorder dialing a dead
#: IP for weeks.
_FALLBACK_IP = "192.0.2.50"


def device_ip() -> str:
    """The tuner's IP: ``HDHR_IP`` if set, else the fallback."""
    return os.getenv("HDHR_IP") or _FALLBACK_IP

# The pipeline reserves the top tuner index, leaving 0-2 for Plex (which
# tunes via /auto and gravitates to the lowest free index) and any other
# ad-hoc consumer.
PIPELINE_TUNER = 3

# Dallas-Fort Worth ATSC virtual channel numbers for the broadcast
# networks we care about for mention-market rehearsal (debates, local
# news, awards broadcasts simulcast OTA). Virtual channels are
# market-specific — these are DFW-only; don't reuse elsewhere without
# re-checking /lineup.json for that market.
DFW_CHANNELS: dict[str, str] = {
    "CBS": "11.1",
    "FOX": "4.1",
    "NBC": "5.1",
    # WFAA's ATSC 1.0 feed moved off the 8.1-8.4 mux during the DFW
    # NextGen TV repack: 8.1 remains in /lineup.json as a stale entry with
    # NO SignalStrength/SignalQuality field at all and streams nothing (the
    # 10-23 kimmel capture died on a bare 5XX), while 8.8 "WFAA-HD" locks
    # at 100/100. A channel that vanishes from the air stays in the lineup,
    # so verify signal fields are PRESENT, not just that the entry exists.
    "ABC": "8.8",
}

_HTTP_TIMEOUT = 6.0


def _resolve_channel(channel: str) -> str:
    """Resolve a channel argument that may be a DFW network name
    (case-insensitive key into ``DFW_CHANNELS``) or an already-numeric
    virtual channel string, into the virtual channel string.

    >>> _resolve_channel("CBS")
    '11.1'
    >>> _resolve_channel("cbs")
    '11.1'
    >>> _resolve_channel("11.1")
    '11.1'
    """
    key = channel.strip().upper()
    return DFW_CHANNELS.get(key, channel.strip())


def stream_url(channel: str, *, tuner: int = PIPELINE_TUNER, ip: str | None = None) -> str:
    """Build the pinned-tuner HTTP stream URL for a channel.

    ``channel`` accepts either a known DFW network name (``"CBS"``,
    case-insensitive) resolved via ``DFW_CHANNELS``, or an explicit
    virtual channel string (``"11.1"``). ``tuner`` defaults to
    ``PIPELINE_TUNER`` (3) so the pipeline never needs to pass it
    explicitly in the common case.

    >>> stream_url("CBS")
    'http://192.0.2.50:5004/tuner3/v11.1'
    >>> stream_url("11.1", tuner=2)
    'http://192.0.2.50:5004/tuner2/v11.1'
    >>> stream_url("fox", ip="10.0.0.5")
    'http://10.0.0.5:5004/tuner3/v4.1'
    """
    ip = ip or device_ip()
    resolved = _resolve_channel(channel)
    return f"http://{ip}:5004/tuner{tuner}/v{resolved}"


def discover(ip: str | None = None, *, timeout: float = _HTTP_TIMEOUT) -> dict | None:
    """GET ``discover.json`` and return the parsed dict (keys include
    ``FriendlyName``, ``TunerCount``, ``FirmwareVersion``, ``DeviceID``),
    or ``None`` on any failure (unreachable device, bad JSON, non-200).
    Never raises — this is a best-effort preflight check, not a
    correctness dependency.
    """
    ip = ip or device_ip()
    url = f"http://{ip}/discover.json"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.warning("hdhomerun discover() failed against %s", url, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _lineup(ip: str | None = None, *, timeout: float = _HTTP_TIMEOUT) -> list[dict] | None:
    ip = ip or device_ip()
    url = f"http://{ip}/lineup.json"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.warning("hdhomerun lineup fetch failed against %s", url, exc_info=True)
        return None
    if not isinstance(data, list):
        return None
    return data


def channel_present(channel: str, ip: str | None = None) -> bool:
    """Check whether ``channel`` (network name or virtual channel
    string) appears in the device's ``lineup.json``. Returns ``False``
    (not an exception) if the lineup can't be fetched or the channel
    isn't found.
    """
    ip = ip or device_ip()
    resolved = _resolve_channel(channel)
    lineup = _lineup(ip)
    if lineup is None:
        return False
    for entry in lineup:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("GuideNumber", "")).strip()
        if number == resolved:
            return True
    return False


def _device_id(ip: str) -> str | None:
    info = discover(ip)
    if not info:
        return None
    device_id = info.get("DeviceID")
    return str(device_id) if device_id else None


def acquire_lock(tuner: int = PIPELINE_TUNER, ip: str | None = None) -> str | None:
    """Best-effort HARD reservation of ``tuner`` via the ``hdhomerun_config``
    CLI tool, if it's installed. Sets a lockkey so the device refuses to
    hand this tuner to another client's tuning request.

    Returns the lockkey (str) used on success, ``None`` if the tool
    isn't installed or the lock could not be set. Never raises — a lock
    failure must never crash a capture session; the fallback protection
    is simply holding the stream connection open, which the pipeline
    does anyway for the duration of a listen run.
    """
    ip = ip or device_ip()
    if not shutil.which("hdhomerun_config"):
        log.info(
            "hdhomerun_config not installed — skipping hard tuner lock for "
            "tuner%d. Falling back to the soft reservation: holding the "
            "stream connection open for the duration of the listen run. "
            "Install hdhomerun_config (libhdhomerun) for a hard lockkey "
            "reservation.",
            tuner,
        )
        return None

    device_id = _device_id(ip)
    if not device_id:
        log.warning(
            "acquire_lock: could not resolve device id via discover() at "
            "%s — skipping tuner lock", ip,
        )
        return None

    lockkey = f"{os.getpid()}"
    try:
        result = subprocess.run(
            ["hdhomerun_config", device_id, "set", f"/tuner{tuner}/lockkey", lockkey],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        log.warning("acquire_lock: hdhomerun_config invocation failed", exc_info=True)
        return None

    if result.returncode != 0:
        log.warning(
            "acquire_lock: hdhomerun_config set lockkey failed (rc=%d): %s",
            result.returncode, (result.stderr or result.stdout or "").strip(),
        )
        return None

    log.info("acquired lockkey %s on tuner%d (device %s)", lockkey, tuner, device_id)
    return lockkey


def release_lock(lockkey: str, tuner: int = PIPELINE_TUNER, ip: str | None = None) -> None:
    """Release a lockkey previously set by ``acquire_lock``. Best-effort,
    never raises. No-op if ``hdhomerun_config`` isn't installed.
    """
    ip = ip or device_ip()
    if not shutil.which("hdhomerun_config"):
        return
    device_id = _device_id(ip)
    if not device_id:
        return
    try:
        result = subprocess.run(
            ["hdhomerun_config", device_id, "set", f"/tuner{tuner}/lockkey", "none"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning(
                "release_lock: hdhomerun_config failed (rc=%d): %s",
                result.returncode, (result.stderr or result.stdout or "").strip(),
            )
        else:
            log.info("released lockkey %s on tuner%d", lockkey, tuner)
    except Exception:
        log.warning("release_lock: hdhomerun_config invocation failed", exc_info=True)
