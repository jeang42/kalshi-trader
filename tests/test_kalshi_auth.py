"""Tests for kalshi_auth — signing, key loading, permission checks,
and the authed_get retry/error paths."""

from __future__ import annotations

import base64
import os
import stat
import time

import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pm.venue import kalshi_auth as ka

# ---------------------------------------------------------------------------
# Test key fixture — generated once per session to avoid the >1s
# overhead of RSA keygen on every test.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_keypair():
    """Generate a real RSA-2048 keypair for signing tests."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def configured_env(test_keypair, tmp_path, monkeypatch):
    """Write the test key to a properly-permissioned tmp file and set
    env vars to point at it. Returns the path for tests that need it."""
    priv, _ = test_keypair
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "test_key.pem"
    p.write_bytes(pem)
    os.chmod(p, 0o600)

    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id-1234")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(p))
    ka._reset_key_cache()
    yield p
    ka._reset_key_cache()


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_unset_env_returns_false(self, monkeypatch):
        monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
        assert ka.is_configured() is False

    def test_missing_file_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "x")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
        assert ka.is_configured() is False

    def test_configured_returns_true(self, configured_env, monkeypatch):
        # Force owner-profile check to pass so this test is robust to
        # whatever profile the dev machine is currently on.
        monkeypatch.setattr(ka, "is_owner_profile_active", lambda: True)
        assert ka.is_configured() is True

    def test_wrong_profile_blocks_configured(self, configured_env, monkeypatch):
        # Env + file are fine, but the active profile isn't the owner.
        monkeypatch.setattr(ka, "is_owner_profile_active", lambda: False)
        assert ka.is_configured() is False


class TestOwnerProfileGuard:
    def test_owner_profile_default_is_jeff(self, monkeypatch):
        monkeypatch.delenv("KALSHI_OWNER_PROFILE", raising=False)
        assert ka.owner_profile() == "jeff"

    def test_owner_profile_env_override(self, monkeypatch):
        monkeypatch.setenv("KALSHI_OWNER_PROFILE", "alice")
        assert ka.owner_profile() == "alice"

    def test_owner_check_against_active_profile(self, monkeypatch):
        from pm.core import profiles
        monkeypatch.delenv("LIVE_TRADING_PROFILES", raising=False)
        monkeypatch.setattr(profiles, "active_profile", lambda: "jeff")
        monkeypatch.setenv("KALSHI_OWNER_PROFILE", "jeff")
        assert ka.is_owner_profile_active() is True

        monkeypatch.setattr(profiles, "active_profile", lambda: "sandbox")
        assert ka.is_owner_profile_active() is False  # paper profile locked out

    def test_live_trading_profile_is_authorized(self, monkeypatch):
        # A profile in LIVE_TRADING_PROFILES is authorized even though it's
        # not the owner — this is what lets multiple live arms authenticate.
        from pm.core import profiles
        monkeypatch.setenv("KALSHI_OWNER_PROFILE", "jeff")
        monkeypatch.setenv("LIVE_TRADING_PROFILES",
                           "agentic_favorite_hold,agentic_closegame_scalp_stop")
        monkeypatch.setattr(profiles, "active_profile",
                            lambda: "agentic_closegame_scalp_stop")
        assert ka.is_owner_profile_active() is True
        # …but a profile NOT in the list (and not owner) is still locked out.
        monkeypatch.setattr(profiles, "active_profile", lambda: "sandbox")
        assert ka.is_owner_profile_active() is False

    def test_authorized_profiles_is_owner_plus_allowlist(self, monkeypatch):
        monkeypatch.setenv("KALSHI_OWNER_PROFILE", "jeff")
        monkeypatch.setenv("LIVE_TRADING_PROFILES", "arm_a, arm_b")
        assert ka.authorized_profiles() == {"jeff", "arm_a", "arm_b"}
        monkeypatch.delenv("LIVE_TRADING_PROFILES", raising=False)
        assert ka.authorized_profiles() == {"jeff"}

    def test_owner_check_allows_when_profiles_unavailable(self, monkeypatch):
        # CLI / tests without a profile system — fall through to True so
        # scripts that don't run inside Streamlit aren't blocked.
        from pm.core import profiles

        def boom():
            raise RuntimeError("no profiles here")

        monkeypatch.setattr(profiles, "active_profile", boom)
        assert ka.is_owner_profile_active() is True


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_group_readable_rejected(self, test_keypair, tmp_path, monkeypatch):
        priv, _ = test_keypair
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        p = tmp_path / "loose.pem"
        p.write_bytes(pem)
        os.chmod(p, 0o644)  # group + world readable

        monkeypatch.setenv("KALSHI_API_KEY_ID", "x")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(p))
        ka._reset_key_cache()

        with pytest.raises(ka.KalshiAuthError, match="unsafe permissions"):
            ka._get_key()

    def test_owner_only_accepted(self, configured_env):
        # configured_env already chmod'd to 0o600
        key_id, key = ka._get_key()
        assert key_id == "test-key-id-1234"
        assert isinstance(key, rsa.RSAPrivateKey)


# ---------------------------------------------------------------------------
# Missing-config errors are loud
# ---------------------------------------------------------------------------

class TestMissingConfig:
    def test_missing_key_id_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "x.pem"))
        ka._reset_key_cache()
        with pytest.raises(ka.KalshiAuthError, match="KALSHI_API_KEY_ID"):
            ka._get_key()

    def test_missing_pem_path_env_raises(self, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "x")
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
        ka._reset_key_cache()
        with pytest.raises(ka.KalshiAuthError, match="KALSHI_PRIVATE_KEY_PATH"):
            ka._get_key()

    def test_missing_pem_file_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "x")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
        ka._reset_key_cache()
        with pytest.raises(ka.KalshiAuthError, match="not found"):
            ka._get_key()


# ---------------------------------------------------------------------------
# Signing produces verifiable RSA-PSS signatures
# ---------------------------------------------------------------------------

class TestSigning:
    def test_signature_verifies_with_public_key(self, configured_env, test_keypair):
        _, pub = test_keypair
        ts = 1715000000000
        headers = ka._sign("GET", "/trade-api/v2/portfolio/balance", now_ms=ts)

        assert headers["KALSHI-ACCESS-KEY"] == "test-key-id-1234"
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == str(ts)
        sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        msg = f"{ts}GET/trade-api/v2/portfolio/balance".encode()
        # If this raises, the signature is malformed.
        pub.verify(
            sig, msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_method_in_signature_is_uppercased(self, configured_env, test_keypair):
        _, pub = test_keypair
        ts = 1715000000000
        headers = ka._sign("get", "/trade-api/v2/x", now_ms=ts)
        sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        # Must verify against the uppercase-method message
        pub.verify(
            sig, f"{ts}GET/trade-api/v2/x".encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_query_string_excluded_from_signature(self, configured_env, test_keypair):
        _, pub = test_keypair
        ts = 1715000000000
        headers = ka._sign("GET", "/trade-api/v2/x?cursor=abc", now_ms=ts)
        sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        # The signed message must be the path WITHOUT the query string
        pub.verify(
            sig, f"{ts}GET/trade-api/v2/x".encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_default_timestamp_is_current_time(self, configured_env):
        before = int(time.time() * 1000)
        headers = ka._sign("GET", "/x")
        after = int(time.time() * 1000)
        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestFullPath:
    def test_full_path_keeps_trade_api_prefix(self, monkeypatch):
        monkeypatch.setattr(ka, "KALSHI_BASE",
                             "https://api.elections.kalshi.com/trade-api/v2")
        signed, url = ka._full_path("portfolio/balance")
        assert signed == "/trade-api/v2/portfolio/balance"
        assert url == "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance"

    def test_full_path_tolerates_leading_slash(self, monkeypatch):
        monkeypatch.setattr(ka, "KALSHI_BASE",
                             "https://api.elections.kalshi.com/trade-api/v2")
        signed, _ = ka._full_path("/portfolio/balance")
        assert signed == "/trade-api/v2/portfolio/balance"


# ---------------------------------------------------------------------------
# authed_get behavior — happy path + 429 retry + auth-error logging
# ---------------------------------------------------------------------------

class _MockResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class TestAuthedGet:
    def test_happy_path_returns_payload_and_signs(self, configured_env, monkeypatch):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _MockResponse(200, {"balance": 1234, "payout": 0})

        monkeypatch.setattr(ka._session, "get", fake_get)
        result = ka.authed_get("portfolio/balance")
        assert result == {"balance": 1234, "payout": 0}
        # Signed with all three required headers
        h = captured["headers"]
        assert h["KALSHI-ACCESS-KEY"] == "test-key-id-1234"
        assert "KALSHI-ACCESS-TIMESTAMP" in h
        assert "KALSHI-ACCESS-SIGNATURE" in h

    def test_429_triggers_retry(self, configured_env, monkeypatch):
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _MockResponse(429, headers={"Retry-After": "0.1"})
            return _MockResponse(200, {"balance": 42})

        monkeypatch.setattr(ka._session, "get", fake_get)
        monkeypatch.setattr(ka.time, "sleep", lambda s: None)
        result = ka.authed_get("portfolio/balance")
        assert result == {"balance": 42}
        assert calls["n"] == 2

    def test_401_raises_httperror(self, configured_env, monkeypatch):
        def fake_get(url, params=None, headers=None, timeout=None):
            return _MockResponse(401, payload={"error": "bad signature"},
                                  text='{"error":"bad signature"}')

        monkeypatch.setattr(ka._session, "get", fake_get)
        with pytest.raises(requests.HTTPError):
            ka.authed_get("portfolio/balance")


class TestAuthedPost:
    def test_post_signs_method_post(self, configured_env, monkeypatch,
                                     test_keypair):
        _, pub = test_keypair
        captured = {}

        def fake_post(url, params=None, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers)
            return _MockResponse(200, {"order_id": "ok"})

        monkeypatch.setattr(ka._session, "post", fake_post)
        resp = ka.authed_post("portfolio/orders", {"ticker": "X"})
        assert resp == {"order_id": "ok"}
        # Verify signature matches POST method
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        h = captured["headers"]
        sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
        msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}POST/trade-api/v2/portfolio/orders".encode()
        pub.verify(sig, msg,
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
        # Body sent as JSON
        assert captured["json"] == {"ticker": "X"}

    def test_post_400_raises_with_logged_body(self, configured_env,
                                                monkeypatch):
        def fake_post(url, params=None, json=None, headers=None, timeout=None):
            return _MockResponse(400, payload={"error": "insufficient_balance"},
                                  text='{"error":"insufficient_balance"}')

        monkeypatch.setattr(ka._session, "post", fake_post)
        with pytest.raises(requests.HTTPError):
            ka.authed_post("portfolio/orders", {"x": 1})


class TestAuthedDelete:
    def test_delete_signs_method_delete(self, configured_env, monkeypatch,
                                          test_keypair):
        _, pub = test_keypair
        captured = {}

        def fake_delete(url, params=None, headers=None, timeout=None):
            captured.update(url=url, headers=headers)
            return _MockResponse(200, {"status": "cancelled"})

        monkeypatch.setattr(ka._session, "delete", fake_delete)
        resp = ka.authed_delete("portfolio/orders/abc")
        assert resp["status"] == "cancelled"
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        h = captured["headers"]
        sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
        msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}DELETE/trade-api/v2/portfolio/orders/abc".encode()
        pub.verify(sig, msg,
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
