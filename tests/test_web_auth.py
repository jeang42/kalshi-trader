"""Tests for the shared-secret gate on the LAN-facing web UIs.

A gate is only worth having if it actually refuses. These drive a real
Starlette app through the middleware rather than testing the helpers alone,
because the interesting failure modes (a cookie not being set, an open path
being closed, a 200 leaking through on a bad token) live in the wiring.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pm.core import web_auth

TOKEN = "s3cr3t-token-value"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_UI_TOKEN", TOKEN)
    app = FastAPI()
    web_auth.protect(app, app_name="Test UI")

    @app.get("/")
    def index():
        return {"ok": True}

    @app.get("/health.json")
    def health():
        return {"status": "ok"}

    @app.post("/refresh")
    def refresh():
        return {"refreshed": True}

    return TestClient(app)


@pytest.fixture
def open_client(monkeypatch):
    """No token configured — the gate must be a no-op."""
    monkeypatch.delenv("WEB_UI_TOKEN", raising=False)
    app = FastAPI()
    web_auth.protect(app, app_name="Test UI")

    @app.get("/")
    def index():
        return {"ok": True}

    return TestClient(app)


class TestTokenIsValid:
    def test_match(self):
        assert web_auth.token_is_valid("abc", "abc") is True

    def test_mismatch(self):
        assert web_auth.token_is_valid("abc", "abd") is False

    def test_empty_supplied_never_matches(self):
        assert web_auth.token_is_valid("", "abc") is False

    def test_empty_expected_never_matches(self):
        """Guards the worst failure: an unset token accidentally matching an
        empty string and opening everything."""
        assert web_auth.token_is_valid("abc", "") is False
        assert web_auth.token_is_valid("", "") is False

    def test_length_difference_does_not_raise(self):
        assert web_auth.token_is_valid("short", "a-much-longer-token") is False


class TestGateClosed:
    def test_no_token_is_refused(self, client):
        assert client.get("/").status_code == 401

    def test_wrong_token_is_refused(self, client):
        assert client.get("/?token=nope").status_code == 401

    def test_wrong_cookie_is_refused(self, client):
        client.cookies.set(web_auth.COOKIE_NAME, "nope")
        assert client.get("/").status_code == 401

    def test_mutating_route_is_refused(self, client):
        """The routes that actually do something — refresh, chat — matter most."""
        assert client.post("/refresh").status_code == 401

    def test_refusal_does_not_leak_the_token(self, client):
        body = client.get("/?token=wrong").text
        assert TOKEN not in body

    def test_refusal_does_not_distinguish_missing_from_wrong(self, client):
        """Telling a prober which one they got wrong confirms a live gate."""
        assert client.get("/").text == client.get("/?token=wrong").text


class TestGateOpen:
    def test_query_token_is_accepted(self, client):
        assert client.get(f"/?token={TOKEN}").status_code == 200

    def test_query_token_sets_a_cookie_so_it_leaves_the_url(self, client):
        r = client.get(f"/?token={TOKEN}")
        assert r.cookies.get(web_auth.COOKIE_NAME) == TOKEN

    def test_cookie_alone_works_on_later_requests(self, client):
        client.get(f"/?token={TOKEN}")          # first visit sets the cookie
        assert client.get("/").status_code == 200

    def test_bearer_header_is_accepted(self, client):
        r = client.get("/", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200

    def test_bearer_is_case_insensitive_on_the_scheme(self, client):
        r = client.get("/", headers={"Authorization": f"bearer {TOKEN}"})
        assert r.status_code == 200

    def test_malformed_authorization_header_is_refused(self, client):
        r = client.get("/", headers={"Authorization": TOKEN})   # no scheme
        assert r.status_code == 401


class TestOpenPaths:
    def test_health_is_reachable_without_a_token(self, client):
        """An external uptime check must not need the secret, and health
        exposes no account data."""
        r = client.get("/health.json")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_open_paths_are_a_short_list(self):
        assert web_auth.OPEN_PATHS == frozenset({"/health.json", "/favicon.ico"})


class TestDisabledByDefault:
    def test_unset_token_allows_everything(self, open_client):
        """Adding the middleware must not break a deployment that has not set
        a token yet — a service refusing to boot is its own outage."""
        assert open_client.get("/").status_code == 200

    def test_unset_token_logs_a_warning(self, monkeypatch, caplog):
        monkeypatch.delenv("WEB_UI_TOKEN", raising=False)
        app = FastAPI()
        with caplog.at_level("WARNING"):
            web_auth.protect(app, app_name="Loud UI")
        assert "OPEN" in caplog.text

    def test_whitespace_only_token_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("WEB_UI_TOKEN", "   ")
        assert web_auth.configured_token() == ""


class TestDefaultBindHost:
    """Release audit P0-B: an unauthenticated app must not listen on the
    network by default. Loopback unless a token is configured or the
    operator overrides explicitly."""

    def test_no_token_binds_loopback(self, monkeypatch):
        monkeypatch.delenv("WEB_UI_TOKEN", raising=False)
        monkeypatch.delenv("WEB_BIND_HOST", raising=False)
        assert web_auth.default_bind_host() == "127.0.0.1"

    def test_token_configured_binds_lan(self, monkeypatch):
        monkeypatch.setenv("WEB_UI_TOKEN", TOKEN)
        monkeypatch.delenv("WEB_BIND_HOST", raising=False)
        assert web_auth.default_bind_host() == "0.0.0.0"

    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.delenv("WEB_UI_TOKEN", raising=False)
        monkeypatch.setenv("WEB_BIND_HOST", "192.0.2.7")
        assert web_auth.default_bind_host() == "192.0.2.7"

    def test_whitespace_token_still_means_loopback(self, monkeypatch):
        monkeypatch.setenv("WEB_UI_TOKEN", "   ")
        monkeypatch.delenv("WEB_BIND_HOST", raising=False)
        assert web_auth.default_bind_host() == "127.0.0.1"


class TestControlPlaneHealthLeak:
    """Release audit P0-B follow-on: /health.json is in OPEN_PATHS (never
    authenticated), so it must expose liveness ONLY. Until 2026-11-04 it
    returned the full fleet snapshot — real Kalshi balance included — to
    anyone who could reach the port, token or not."""

    def test_health_json_is_open_but_carries_no_account_data(self, monkeypatch):
        monkeypatch.setenv("WEB_UI_TOKEN", TOKEN)
        from control_plane import web as cp_web
        c = TestClient(cp_web.app)
        r = c.get("/health.json")  # no token supplied — must still answer
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        forbidden = {"live_account", "balance", "balance_dollars", "payout",
                     "services", "arms", "panels", "sidecars"}
        assert not (set(data) & forbidden)

    def test_dashboard_still_requires_the_token(self, monkeypatch):
        monkeypatch.setenv("WEB_UI_TOKEN", TOKEN)
        from control_plane import web as cp_web
        c = TestClient(cp_web.app)
        assert c.get("/").status_code == 401


class TestRealApps:
    """The gate must actually be attached to the shipped apps — a middleware
    that exists but was never wired protects nothing."""

    @pytest.mark.parametrize("module_path,attr", [
        ("control_plane.web", "app"),
    ])
    def test_control_plane_is_gated(self, monkeypatch, module_path, attr):
        monkeypatch.setenv("WEB_UI_TOKEN", TOKEN)
        import importlib
        mod = importlib.import_module(module_path)
        app = getattr(mod, attr)
        names = [m.cls.__name__ for m in app.user_middleware]
        assert "TokenGateMiddleware" in names
