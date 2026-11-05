"""Shared-secret gate for the sidecar and control-plane web UIs.

The problem
-----------
Four FastAPI apps bind ``0.0.0.0`` with no authentication of any kind. Three
serve sport data and an LLM chat endpoint that will drive the local GPU on
request; the fourth is the control plane, which renders a best-effort read of
the real account balance. Anyone who can reach the host — another device on
the LAN, or anyone arriving through a misconfigured router, VPN or port
forward — can read all of it and trigger the mutating ``POST /refresh`` and
``POST /chat/send`` routes.

What this is, and what it is not
--------------------------------
This is a **shared secret**, not authentication. There are no users, no
sessions, no per-device revocation. One token grants full access; leaking it
grants full access to whoever has it. That is a deliberate trade: the
alternative that closes the hole completely is binding to loopback, which
would end casual access from a phone on the same network, and real auth
(accounts, hashed passwords, sessions) is a disproportionate build for a
single-operator LAN tool.

So it is a large improvement over nothing and should not be mistaken for more.
If these apps ever face an untrusted network, replace this, do not extend it.

Behaviour
---------
* ``WEB_UI_TOKEN`` unset  → every request is allowed, a loud warning is
  logged once at startup, **and (since 2026-11-04) the apps bind loopback
  only** — :func:`default_bind_host` returns ``127.0.0.1`` when no token is
  configured, so "no auth" no longer means "open to the whole LAN". Unset
  must not silently break a running fleet, and a service that refuses to
  boot because a new variable is missing is its own outage; narrowing the
  bind keeps both properties while closing the network hole. To expose an
  unauthenticated app anyway, set ``WEB_BIND_HOST=0.0.0.0`` explicitly.
* ``WEB_UI_TOKEN`` set    → a request must present it, by any of:

  1. ``?token=…`` in the query string — how you get in from a phone the first
     time. On success the token is stored in a cookie so the rest of the
     session works normally and the secret leaves the URL bar.
  2. the ``polymarket_ui`` cookie — set automatically by (1).
  3. ``Authorization: Bearer …`` — for scripts and curl.

Comparison is constant-time (``hmac.compare_digest``): a naive ``==`` leaks
the token a character at a time to anyone willing to measure, which is exactly
the mistake a hand-rolled gate is expected to make.

``/health.json`` stays open by design so an external monitor can check
liveness without holding the secret; it exposes no account data.
"""

from __future__ import annotations

import hmac
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse

log = logging.getLogger(__name__)

#: Cookie the gate sets once a valid token arrives by query string.
COOKIE_NAME = "polymarket_ui"

#: Paths reachable without the token. Deliberately tiny.
OPEN_PATHS = frozenset({"/health.json", "/favicon.ico"})

#: Cookie lifetime. Long, because re-entering a token on a phone is friction
#: that leads to the gate being turned off.
COOKIE_MAX_AGE = 60 * 60 * 24 * 90


def configured_token() -> str:
    """The expected token, or "" when the gate is disabled."""
    return os.getenv("WEB_UI_TOKEN", "").strip()


def default_bind_host() -> str:
    """The address a LAN-facing app should bind, given the auth config.

    * ``WEB_BIND_HOST`` set → that address, verbatim. The operator said so.
    * token configured      → ``0.0.0.0`` — the gate is on, LAN access is
      the point of these apps.
    * neither               → ``127.0.0.1``. An unauthenticated service must
      not listen on the network by default; a deployment that wants the old
      open-LAN behaviour has to say ``WEB_BIND_HOST=0.0.0.0`` explicitly.

    (Before 2026-11-04 every app bound ``0.0.0.0`` unconditionally, so "no
    token configured" meant "open to the whole LAN" — fail-open. This is
    the release-audit P0-B fix; see docs/release-audit/04-security.md.
    Unit-tested in tests/test_web_auth.py rather than doctested: the cases
    are env mutations, which doctests would leak into the session.)
    """
    host = os.getenv("WEB_BIND_HOST", "").strip()
    if host:
        return host
    return "0.0.0.0" if configured_token() else "127.0.0.1"


def _bearer(header: str | None) -> str:
    if not header:
        return ""
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def token_is_valid(supplied: str, expected: str) -> bool:
    """Constant-time comparison of a supplied token against the expected one.

    >>> token_is_valid("abc", "abc")
    True
    >>> token_is_valid("abc", "abd")
    False
    >>> token_is_valid("", "abc")
    False
    >>> token_is_valid("abc", "")
    False
    """
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


_DENIED_HTML = """<!doctype html>
<title>Locked</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#161b21;color:#e7ecf2;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .box{{max-width:34rem;padding:2rem;border:1px solid #27303a;background:#0e1216}}
 h1{{margin:0 0 .5rem;font-size:1.25rem}}
 p{{color:#9ca7b5;line-height:1.6;margin:.6rem 0}}
 code{{background:#1d242c;padding:.15rem .4rem;border-radius:2px;font-size:.9em}}
</style>
<div class="box">
  <h1>{title}</h1>
  <p>This interface requires the access token configured in
     <code>WEB_UI_TOKEN</code>.</p>
  <p>Open it once as <code>{path}?token=YOUR_TOKEN</code> — the token is then
     stored in a cookie and you will not need it again on this device.</p>
</div>
"""


class TokenGateMiddleware(BaseHTTPMiddleware):
    """Require the shared secret on every request except :data:`OPEN_PATHS`."""

    def __init__(self, app, *, app_name: str = "UI"):
        super().__init__(app)
        self.app_name = app_name

    async def dispatch(self, request, call_next):
        expected = configured_token()
        if not expected or request.url.path in OPEN_PATHS:
            return await call_next(request)

        from_query = request.query_params.get("token", "")
        supplied = (from_query
                    or request.cookies.get(COOKIE_NAME, "")
                    or _bearer(request.headers.get("authorization")))

        if not token_is_valid(supplied, expected):
            # 401 with no hint about what was wrong: distinguishing "no token"
            # from "wrong token" tells a prober they found a live gate.
            log.warning("%s: rejected %s %s from %s", self.app_name,
                        request.method, request.url.path,
                        getattr(request.client, "host", "?"))
            return HTMLResponse(
                _DENIED_HTML.format(title=f"{self.app_name} — access token required",
                                    path=request.url.path),
                status_code=401)

        response = await call_next(request)
        if from_query:
            # Move the secret out of the URL and into a cookie. httponly so
            # page scripts cannot read it; lax so normal navigation carries it.
            response.set_cookie(
                COOKIE_NAME, expected, max_age=COOKIE_MAX_AGE,
                httponly=True, samesite="lax")
        return response


def protect(app, *, app_name: str = "UI"):
    """Attach the gate to a FastAPI/Starlette app. Returns the app for chaining.

    Call this on every LAN-facing app. It is a no-op when WEB_UI_TOKEN is unset,
    so adding it cannot break a deployment that has not configured a token yet.

    The "you are unprotected" warning is emitted HERE rather than in the
    middleware's constructor: Starlette does not instantiate middleware until
    it builds the stack at startup, so a warning in ``__init__`` fires late and
    — for an app that is imported but never served — not at all. This runs at
    wire time, which is the moment someone can still act on it.
    """
    if not configured_token():
        log.warning(
            "%s: WEB_UI_TOKEN is not set — this interface is OPEN to anyone who "
            "can reach this host on the network. Set WEB_UI_TOKEN in .env to "
            "require a token. See web_auth.py.", app_name)
    app.add_middleware(TokenGateMiddleware, app_name=app_name)
    return app
