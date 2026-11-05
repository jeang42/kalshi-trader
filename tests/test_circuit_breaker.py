"""circuit_breaker — fail fast on a dead dependency, probe, recover (R-21)."""

from __future__ import annotations

import requests

from pm.core import circuit_breaker as cb
from pm.core.circuit_breaker import CircuitOpenError


def _boom(*_a, **_k):
    raise requests.ConnectionError("down")


class TestStateMachine:
    def test_opens_after_threshold_and_fails_fast(self, monkeypatch, caplog):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
        b = cb.get("dep")
        for _ in range(3):
            try:
                b.call(_boom)
            except requests.ConnectionError:
                pass
        assert b.state == "open"
        calls = []
        try:
            b.call(lambda: calls.append(1))
        except CircuitOpenError as e:
            assert "circuit open after 3" in str(e)
        assert calls == [], "an open circuit must not attempt the call"
        # a CircuitOpenError IS a RequestException — existing handlers catch it
        assert issubclass(CircuitOpenError, requests.RequestException)

    def test_probe_after_cooldown_closes_on_success(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
        monkeypatch.setenv("CIRCUIT_COOLDOWN_S", "1")
        b = cb.get("dep")
        try:
            b.call(_boom)
        except requests.ConnectionError:
            pass
        assert b.state == "open"
        b._opened_at -= 5          # pretend the cooldown elapsed
        assert b.call(lambda: "ok") == "ok"
        assert b.state == "closed" and b.snapshot()["consecutive_failures"] == 0

    def test_probe_failure_reopens(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
        b = cb.get("dep")
        try:
            b.call(_boom)
        except requests.ConnectionError:
            pass
        b._opened_at -= 10_000
        try:
            b.call(_boom)             # the probe
        except requests.ConnectionError:
            pass
        assert b.state == "open" and b.snapshot()["opens"] == 2

    def test_4xx_counts_as_alive_and_resets(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
        b = cb.get("dep")
        try:
            b.call(_boom)
        except requests.ConnectionError:
            pass
        assert b.snapshot()["consecutive_failures"] == 1

        def notfound():
            r = requests.Response()
            r.status_code = 404
            raise requests.HTTPError(response=r)
        try:
            b.call(notfound)
        except requests.HTTPError:
            pass
        assert b.snapshot()["consecutive_failures"] == 0 and b.state == "closed"

    def test_success_resets_count(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
        b = cb.get("dep")
        for _ in range(2):
            try:
                b.call(_boom)
            except requests.ConnectionError:
                pass
        b.call(lambda: 1)
        try:
            b.call(_boom)
        except requests.ConnectionError:
            pass
        assert b.state == "closed"      # 2, reset, 1 — never reached 3

    def test_keyboard_interrupt_is_not_a_dependency_failure(self):
        b = cb.get("dep")

        def ki():
            raise KeyboardInterrupt
        try:
            b.call(ki)
        except KeyboardInterrupt:
            pass
        assert b.snapshot()["consecutive_failures"] == 0


class TestVisibility:
    def test_transitions_go_to_the_health_log_once(self, monkeypatch, tmp_path):
        import json

        from pm.core import notify
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path / "h.jsonl"))
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
        b = cb.get("kalshi")
        for _ in range(3):                # 3 failures, but only ONE open event
            try:
                b.call(_boom)
            except (requests.ConnectionError, CircuitOpenError):
                pass
        b._opened_at -= 10_000
        b.call(lambda: 1)                 # probe succeeds → closed
        rows = [json.loads(line) for line in (tmp_path / "h.jsonl").read_text().splitlines()]
        events = [r["event"] for r in rows]
        assert events.count("circuit_open") == 1 and events.count("circuit_closed") == 1
        assert notify.health_log_path() == tmp_path / "h.jsonl"

    def test_status_lists_touched_breakers(self):
        cb.get("a")
        cb.get("b")
        assert sorted(s["name"] for s in cb.status()) == ["a", "b"]
        assert all(s["state"] == "closed" for s in cb.status())

    def test_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "lots")
        monkeypatch.setenv("CIRCUIT_COOLDOWN_S", "soon")
        assert cb._threshold() == cb.FAILURE_THRESHOLD
        assert cb._cooldown() == cb.COOLDOWN_S


class TestHealthDigestSeesOpenCircuits:
    def test_open_without_close_is_a_problem(self, monkeypatch, tmp_path):
        from pm.core import notify
        from pm.ops import health_digest
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path / "h.jsonl"))
        notify.record("circuit_open", "kalshi: 5 consecutive failures", severity="warning",
                      extra={"breaker": "kalshi"})
        notify.record("circuit_open", "espn: 5 consecutive failures", severity="warning",
                      extra={"breaker": "espn"})
        notify.record("circuit_closed", "espn recovered", severity="info",
                      extra={"breaker": "espn"})
        problems, notes = health_digest.check_recent_events()
        assert any("'kalshi' has been OPEN" in p for p in problems)
        assert not any("'espn'" in p for p in problems)


class TestConnectorsAreWired:
    """Each connector's HTTP entry point really goes through its breaker: with
    the circuit forced open, the call fails fast and NO request is attempted
    (the session is patched to explode if touched)."""

    def _open(self, name):
        b = cb.get(name)
        b._state, b._opened_at, b._failures = "open", 1e12, 5   # far-future open
        return b

    def test_kalshi_client_get(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        self._open("kalshi")
        monkeypatch.setattr(kc._SESSION, "get", _boom)
        try:
            kc._get("markets")
        except CircuitOpenError:
            return
        raise AssertionError("expected CircuitOpenError")

    def test_kalshi_auth_get_and_request(self, monkeypatch):
        from pm.venue import kalshi_auth as ka
        self._open("kalshi")
        monkeypatch.setattr(ka, "_sign", lambda *a, **k: {})
        for fn, args in ((ka.authed_get, ("portfolio/balance",)),
                         (ka._authed_request, ("POST", "portfolio/orders"))):
            try:
                fn(*args)
            except CircuitOpenError:
                continue
            raise AssertionError(f"{fn.__name__}: expected CircuitOpenError")

    def test_polymarket_espn_coinbase_manifold(self, monkeypatch):
        from pm.feeds import crypto_data, manifold_data, sports_data
        from pm.venue import polymarket_client
        for mod, name, args in ((polymarket_client, "polymarket", ("http://x/y",)),
                                (sports_data, "espn", ("scoreboard",)),
                                (crypto_data, "coinbase", ("products",)),
                                (manifold_data, "manifold", ("markets",))):
            self._open(name)
            try:
                mod._get(*args)
            except CircuitOpenError:
                continue
            raise AssertionError(f"{mod.__name__}: expected CircuitOpenError")

    def test_soft_connectors_degrade_to_none_or_empty(self, monkeypatch):
        from pm.feeds import llm_data, macro_data, underlying_data, web_context
        for name in ("ollama", "fred", "yahoo", "google_news", "wikipedia"):
            self._open(name)
        assert llm_data.get_forecast(question="q?", model="m", bypass_cache=True) is None
        assert macro_data._get_csv("UNRATE") is None
        assert underlying_data._chart("GC=F", interval="60m", range_="5d") is None
        assert web_context.search_news("x", bypass_cache=True) == []
        assert web_context.wikipedia_summary("X", bypass_cache=True) is None
