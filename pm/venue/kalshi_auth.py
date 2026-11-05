"""Kalshi authenticated HTTP layer.

Loads a Kalshi API key ID + RSA private key from env / disk and signs
every request per the Kalshi auth spec:

    KALSHI-ACCESS-KEY:        the key UUID
    KALSHI-ACCESS-TIMESTAMP:  current epoch ms (as string)
    KALSHI-ACCESS-SIGNATURE:  base64(RSA-PSS-SHA256(privkey,
                                  f"{timestamp}{METHOD}{path}"))

The signed path is the request path *including* the API base prefix
(``/trade-api/v2/...``), not just the per-endpoint suffix.

Phase 1 is read-only on purpose: this module exposes ``authed_get``
(and a paginator) but no POST/DELETE helpers. Order placement will land
in a separate ``kalshi_orders.py`` once Phase 1 has been exercised
against the live venue and reconciliation against ``portfolio.json``
checks out.

Safety
------
- The PEM path's permissions are checked at load time. If the file is
  group- or world-readable, loading raises — refusing to start beats
  silently leaking a key.
- The key never enters log output: log messages only ever reference
  the key ID prefix.
- The module is silent if ``KALSHI_API_KEY_ID`` is unset, so importing
  it on a machine without credentials configured is harmless and lets
  the Live Portfolio page render a "configure your key" prompt instead
  of crashing the app.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

# Mirrors kalshi_client — same base + timeout knobs.
KALSHI_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
HTTP_TIMEOUT = float(os.getenv("KALSHI_HTTP_TIMEOUT", "15"))

_API_KEY_ID_ENV = "KALSHI_API_KEY_ID"
_PRIVATE_KEY_PATH_ENV = "KALSHI_PRIVATE_KEY_PATH"

# Profiles allowed to use the authed Kalshi API. The OWNER (default "jeff")
# is the primary human account; additionally, any profile in
# LIVE_TRADING_PROFILES (the agentic-arm allow-list) is authorized — this is
# what lets multiple live daemons each authenticate against the one account
# while paper-trading profiles (sandbox, dex, un-listed agentic_*) stay
# locked out. The real Kalshi account belongs to one human, so authorization
# is deliberately an explicit allow-list, never "any profile".
_OWNER_PROFILE_ENV = "KALSHI_OWNER_PROFILE"
_DEFAULT_OWNER_PROFILE = "jeff"
_LIVE_PROFILES_ENV = "LIVE_TRADING_PROFILES"  # mirrors execution.LIVE_PROFILES_ENV

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

class KalshiAuthError(RuntimeError):
    """Raised when auth is requested but credentials are unavailable or
    unsafe (missing env vars, missing key file, or loose permissions)."""


def _check_perms(p: Path) -> None:
    """Refuse to load a key file that anyone other than the owner can
    read. This is the only line of defence between a misconfigured
    deployment and a leaked key — better to fail loudly."""
    try:
        mode = p.stat().st_mode
    except OSError as e:
        raise KalshiAuthError(f"private key file not accessible: {e}") from e
    # Reject any 'group' or 'other' read/write/exec bits.
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KalshiAuthError(
            f"private key {p} has unsafe permissions {oct(mode & 0o777)}; "
            "tighten with `chmod 600`."
        )


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    _check_perms(path)
    data = path.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiAuthError(
            f"expected RSA private key at {path}, got {type(key).__name__}"
        )
    return key


def owner_profile() -> str:
    """The primary profile name allowed to use the authed Kalshi API.
    Defaults to ``jeff``; override via ``KALSHI_OWNER_PROFILE`` env. (Other
    profiles can also be authorized via ``LIVE_TRADING_PROFILES`` — see
    :func:`authorized_profiles`.)"""
    return os.getenv(_OWNER_PROFILE_ENV, _DEFAULT_OWNER_PROFILE).strip()


def authorized_profiles() -> set[str]:
    """Profiles allowed to drive the live account: the owner plus every
    profile in ``LIVE_TRADING_PROFILES`` (the agentic-arm allow-list). This
    is what lets several live daemons each authenticate against the one
    account; profiles outside this set (paper profiles) stay locked out."""
    profs = {owner_profile()}
    raw = os.getenv(_LIVE_PROFILES_ENV, "") or ""
    profs |= {p.strip() for p in raw.split(",") if p.strip()}
    return profs


def is_owner_profile_active() -> bool:
    """True iff the currently-active profile is authorized for the live
    account — the owner OR an allow-listed live-trading profile (see
    :func:`authorized_profiles`). Imports profiles lazily so this module
    stays usable in scripts / tests without a profile system."""
    try:
        from pm.core import profiles
        return profiles.active_profile() in authorized_profiles()
    except Exception as e:
        # Fail CLOSED. This gate is part of is_configured(), which every
        # authed call — including order placement — depends on. Until
        # 2026-11-04 it returned True here ("CLI users have shell access
        # already"), but the except also swallows runtime failures such as
        # an unreadable active-profile file, which silently authorized
        # whatever profile happened to be active. A CLI that legitimately
        # has no profile system can set POLYMARKET_ACTIVE_PROFILE to an
        # authorized profile instead.
        log.warning("profile authorization check failed (%s) — treating the "
                    "active profile as NOT authorized (fail-closed)", e)
        return False


@contextlib.contextmanager
def owner_scope():
    """Temporarily pin the active profile to the live-account owner so
    READ-ONLY authed calls (balance / positions / fills / settlements)
    authenticate even when a non-authorized paper profile is the active one
    (e.g. the Streamlit default ``agentic_favorites``). Restores the prior
    ``POLYMARKET_ACTIVE_PROFILE`` on exit.

    This is a read convenience, NOT a trade authorization: placing orders
    still requires ``KALSHI_LIVE_ENABLED`` AND the acting profile being in
    ``LIVE_TRADING_PROFILES`` (see ``kalshi_orders.is_trading_enabled``), so
    scoping to the owner here cannot cause an order to fire.

    >>> with owner_scope():                     # doctest: +SKIP
    ...     bal = kalshi_client.get_balance()
    """
    key = "POLYMARKET_ACTIVE_PROFILE"
    prev = os.environ.get(key)
    os.environ[key] = owner_profile()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def is_configured() -> bool:
    """Cheap precondition check — does the env have what auth needs AND is
    the active profile authorized for the live account?

    True iff both env vars are present, the PEM file exists, AND the active
    profile is authorized (the owner, or a profile in
    ``LIVE_TRADING_PROFILES``). Use this before touching authenticated
    endpoints so the UI can render a "configure your key" / "switch profile"
    prompt instead of leaking the wrong profile's view onto a live account.
    """
    key_id = os.getenv(_API_KEY_ID_ENV, "").strip()
    pem_path = os.getenv(_PRIVATE_KEY_PATH_ENV, "").strip()
    if not key_id or not pem_path:
        return False
    if not Path(pem_path).exists():
        return False
    return is_owner_profile_active()


# Lazy singleton: we load the key on first signed request, not at import.
# This keeps the module harmless to import on un-configured machines.
_cached_key: rsa.RSAPrivateKey | None = None
_cached_key_path: str | None = None


def _get_key() -> tuple[str, rsa.RSAPrivateKey]:
    """Return (key_id, loaded_private_key). Raises KalshiAuthError on
    any misconfiguration."""
    global _cached_key, _cached_key_path
    key_id = os.getenv(_API_KEY_ID_ENV, "").strip()
    pem_path = os.getenv(_PRIVATE_KEY_PATH_ENV, "").strip()
    if not key_id:
        raise KalshiAuthError(f"{_API_KEY_ID_ENV} env var is not set.")
    if not pem_path:
        raise KalshiAuthError(f"{_PRIVATE_KEY_PATH_ENV} env var is not set.")
    p = Path(pem_path)
    if not p.exists():
        raise KalshiAuthError(f"private key file not found: {pem_path}")
    if _cached_key is None or _cached_key_path != pem_path:
        _cached_key = _load_private_key(p)
        _cached_key_path = pem_path
        log.info("Loaded Kalshi private key (id prefix=%s)", key_id[:8])
    return key_id, _cached_key


def _reset_key_cache() -> None:
    """Test hook — forces the next _get_key() to re-load from disk."""
    global _cached_key, _cached_key_path
    _cached_key = None
    _cached_key_path = None


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def _sign(method: str, path: str, *, now_ms: int | None = None) -> dict[str, str]:
    """Build the three Kalshi auth headers for ``METHOD path``.

    ``path`` must be the request path including the ``/trade-api/v2``
    prefix — Kalshi signs the full path, not the per-endpoint suffix.
    Trailing query string is *excluded* from the signed message.
    """
    key_id, priv = _get_key()
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    # Strip any query string before signing — Kalshi's signature spec
    # signs only "{timestamp}{METHOD}{path}".
    sig_path = path.split("?", 1)[0]
    msg = f"{ts}{method.upper()}{sig_path}".encode()
    sig = priv.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def _full_path(suffix: str) -> tuple[str, str]:
    """Map a suffix like ``/portfolio/balance`` to (signed_path, full_url).

    ``KALSHI_BASE`` carries the ``/trade-api/v2`` prefix; the signed
    path is everything from ``/trade-api/v2`` onward.
    """
    base = KALSHI_BASE.rstrip("/")
    suf = "/" + suffix.lstrip("/")
    # base looks like https://host/trade-api/v2
    # signed path needs to be /trade-api/v2/<suffix>
    # Robust to alt hosts: just split off the scheme://host
    if "://" in base:
        host_end = base.index("/", base.index("://") + 3)
        signed = base[host_end:] + suf
    else:
        signed = base + suf
    return signed, base + suf


# ---------------------------------------------------------------------------
# Authenticated GET
# ---------------------------------------------------------------------------

def authed_get(suffix: str, params: dict[str, Any] | None = None,
                retries: int = 3) -> dict:
    """GET an authenticated Kalshi endpoint — under the shared ``kalshi``
    circuit breaker (R-21; same host as the public client)."""
    return circuit_breaker.get("kalshi").call(_authed_get_unguarded, suffix, params, retries)


def _authed_get_unguarded(suffix: str, params: dict[str, Any] | None = None,
                          retries: int = 3) -> dict:
    """GET an authenticated Kalshi endpoint.

    Mirrors ``kalshi_client._get`` — same retry-on-transient + 429
    backoff semantics — with auth headers stamped per attempt (each
    retry gets a fresh timestamp + signature; some venues reject stale
    signatures even if the original request never reached them).

    ``suffix`` is the per-endpoint path under ``/trade-api/v2``
    (e.g. ``portfolio/balance``). Leading slash optional.
    """
    signed, url = _full_path(suffix)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            headers = _sign("GET", signed)
            r = _session.get(url, params=params, headers=headers,
                              timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
            continue

        if r.status_code == 429:
            if attempt == retries - 1:
                r.raise_for_status()
                return {}
            sleep_s = float(r.headers.get("Retry-After", "1") or 1.0)
            sleep_s = min(max(sleep_s, 0.1), 30.0)
            log.debug("Kalshi rate-limited (auth GET %s); retrying in %.1fs",
                        suffix, sleep_s)
            time.sleep(sleep_s)
            continue

        try:
            r.raise_for_status()
        except requests.HTTPError:
            # Surface 401/403 distinctly — they almost always mean a
            # signing problem and the user needs actionable feedback.
            if r.status_code in (401, 403):
                log.warning("Kalshi auth %s on %s: %s",
                            r.status_code, suffix, r.text[:200])
            raise
        return r.json() if r.content else {}

    if last_exc:
        raise last_exc
    return {}


def _authed_request(method: str, suffix: str, *,
                     params: dict[str, Any] | None = None,
                     json_body: dict[str, Any] | None = None,
                     retries: int = 1) -> dict:
    """POST / DELETE / PUT under the shared ``kalshi`` breaker (R-21). An
    open circuit means an order is NOT placed — the money path fails closed
    (``LiveBroker`` records an error result; nothing rests unrecorded)."""
    return circuit_breaker.get("kalshi").call(
        _authed_request_unguarded, method, suffix,
        params=params, json_body=json_body, retries=retries)


def _authed_request_unguarded(method: str, suffix: str, *,
                               params: dict[str, Any] | None = None,
                               json_body: dict[str, Any] | None = None,
                               retries: int = 1) -> dict:
    """POST / DELETE / PUT helper. Single retry by default — write
    requests with the same client_order_id are safe to retry on 429
    or transient network failure (Kalshi de-dupes by client_order_id),
    but we don't want long retry loops blocking the UI.

    ``method`` is upper-cased before signing. ``json_body`` is sent as
    the request body for POST/PUT; ignored for DELETE.
    """
    method = method.upper()
    signed, url = _full_path(suffix)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            headers = _sign(method, signed)
            if method == "POST":
                r = _session.post(url, params=params, json=json_body,
                                   headers=headers, timeout=HTTP_TIMEOUT)
            elif method == "DELETE":
                r = _session.delete(url, params=params, headers=headers,
                                     timeout=HTTP_TIMEOUT)
            elif method == "PUT":
                r = _session.put(url, params=params, json=json_body,
                                  headers=headers, timeout=HTTP_TIMEOUT)
            else:
                raise ValueError(f"unsupported method: {method}")
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
            continue

        if r.status_code == 429:
            if attempt == retries - 1:
                r.raise_for_status()
                return {}
            sleep_s = float(r.headers.get("Retry-After", "1") or 1.0)
            sleep_s = min(max(sleep_s, 0.1), 30.0)
            log.debug("Kalshi rate-limited (auth %s %s); retrying in %.1fs",
                        method, suffix, sleep_s)
            time.sleep(sleep_s)
            continue

        try:
            r.raise_for_status()
        except requests.HTTPError:
            if r.status_code in (401, 403):
                log.warning("Kalshi auth %s on %s: %s",
                            r.status_code, suffix, r.text[:200])
            elif r.status_code >= 400:
                # Surface 4xx body for orders — Kalshi returns helpful
                # rejection reasons (e.g. "insufficient_balance") that
                # the UI needs to display.
                log.warning("Kalshi %s %s rejected with %s: %s",
                            method, suffix, r.status_code, r.text[:300])
            raise
        return r.json() if r.content else {}

    if last_exc:
        raise last_exc
    return {}


def authed_post(suffix: str, json_body: dict[str, Any]) -> dict:
    """POST a JSON body to an authed endpoint. See ``_authed_request``."""
    return _authed_request("POST", suffix, json_body=json_body)


def authed_delete(suffix: str) -> dict:
    """DELETE an authed endpoint (e.g. cancel order)."""
    return _authed_request("DELETE", suffix)


def authed_paginate(suffix: str, params: dict | None, items_key: str,
                     max_items: int) -> list[dict]:
    """Cursor-based pagination for authenticated endpoints. Mirrors
    ``kalshi_client._paginate``."""
    out: list[dict] = []
    p: dict[str, Any] = dict(params or {})
    while len(out) < max_items:
        data = authed_get(suffix, params=p)
        items = data.get(items_key) or []
        if not items:
            break
        out.extend(items)
        cursor = data.get("cursor")
        if not cursor:
            break
        p["cursor"] = cursor
    return out[:max_items]


__all__ = [
    "KalshiAuthError",
    "is_configured",
    "authed_get",
    "authed_post",
    "authed_delete",
    "authed_paginate",
    "owner_profile",
    "authorized_profiles",
    "is_owner_profile_active",
]
