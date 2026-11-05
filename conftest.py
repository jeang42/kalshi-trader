"""Project-wide pytest invariants: data isolation + the live-data tripwire.

These live at the repository ROOT, not in ``tests/``, on purpose. A conftest
only governs items collected at or below its own directory, and since
2026-08-17 the suite also collects the doctests in the root modules
(``testpaths = ["."]`` + ``--doctest-modules``; see the note in
``pyproject.toml`` on why it is ``"."`` and not a ``"*.py"`` glob). Before that,
``--doctest-modules`` had been in ``addopts`` for months while ``testpaths``
pointed only at ``tests/`` — so the "doctested" pure functions across the
arms had never actually been executed by pytest (three stale expectations
surfaced the day they first ran). Root doctests must run under exactly the
same redirects and the same tripwire as everything else, so the fixtures
moved here. Test-data fixtures (``portfolio_path``, ``sample_markets_df``)
stay in ``tests/conftest.py`` where the tests that use them live.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_live_execution_state(tmp_path, monkeypatch):
    """Keep the live-execution guardrails off the network and out of data/.

    Wiring the drawdown circuit breaker into ``LiveBroker._place`` gave every
    existing LiveBroker order test a new dependency: the breaker resolves
    account equity, which calls the AUTHENTICATED Kalshi endpoints and
    persists a high-water mark. Without this fixture the suite made real API
    calls and wrote ``data/shared/live_equity.json`` in the working tree —
    caught when a local run recorded the real account balance.

    So, autouse and cheap:
      * the breaker's state file goes to tmp_path;
      * the two authed reads it needs return benign fixed values.

    A test that wants the real behaviour patches over these, and its patch
    wins because it is applied later — ``TestAccountEquity`` and the
    ``_in_drawdown_pause`` tests both do exactly that.
    """
    monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(tmp_path / "live_equity.json"))
    try:
        from pm.trading import execution
    except Exception:  # pragma: no cover - import failure surfaces elsewhere
        return
    # Patch the equity helper, NOT kalshi_client.get_balance/get_authed_positions:
    # those have their own tests, and stubbing them here would shadow the very
    # functions under test. A test needing the real helper binds it at import
    # time (`from execution import account_equity_dollars`), which captures the
    # original object before this fixture rebinds the module attribute.
    monkeypatch.setattr(execution, "account_equity_dollars",
                        lambda: 1000.0, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may open a network socket. Full stop.

    Added 2026-08-17 after finding that a LiveBroker test had been issuing a
    REAL authenticated DELETE to Kalshi on every run (the phantom order it
    adopted was then "cancelled" for real — 404, but the request went out
    with live credentials). Isolation of data/ had a tripwire; isolation of
    the venue had nothing but convention. A test that needs a connector
    mocks it above the socket; anything that reaches this raises.
    """
    import socket

    def _refuse(self, *a, **k):
        raise RuntimeError(
            "test attempted a real network connection — mock the connector "
            f"(socket.connect{a[:1]})")
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    # create_connection() is a thin wrapper, but patch it too so the error
    # names the destination host rather than a raw address tuple.
    monkeypatch.setattr(socket, "create_connection",
                        lambda address, *a, **k: _refuse(None, address))


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Breaker state is per-process and would otherwise leak between tests:
    a retry test that drives a connector to five failures would open the
    circuit for every test after it (R-21)."""
    from pm.core import circuit_breaker
    circuit_breaker.reset_all()
    yield
    circuit_breaker.reset_all()


@pytest.fixture(autouse=True)
def _isolate_profile_data_root(tmp_path, monkeypatch):
    """Point the per-profile data tree at tmp_path for every test.

    Until 2026-08-12 ``profiles.PROFILES_ROOT`` was a module-level constant
    frozen at import, so nothing could redirect it. Tests that passed a profile
    name (``profile_name="test_profile"``) therefore wrote into the REAL
    ``data/`` tree: a full run appended synthetic closures to a live exits log,
    and the same defect class had previously put a synthetic row into a
    production database.

    The root is now resolved per call from ``POLYMARKET_DATA_DIR``, which makes
    this one line possible. Tests that want the real tree can override the env
    var themselves; nothing in the suite legitimately needs to.
    """
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _isolate_shared_logs(tmp_path, monkeypatch):
    """Redirect shared (non-profile-scoped) audit logs away from the live tree."""
    monkeypatch.setenv("REACTIVE_LOG_FILE", str(tmp_path / "reactive_log.jsonl"))
    monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path / "health_events.jsonl"))
    monkeypatch.setenv("OOS_LEDGER_FILE", str(tmp_path / "oos_ledger.json"))
    # The real-order audit log. Missed until 2026-08-17: a test that exercised
    # LiveBroker's orphan-order adoption wrote 70 cancel rows into the LIVE
    # orders.log (and hit the venue — see _no_network below).
    monkeypatch.setenv("ORDERS_LOG_PATH", str(tmp_path / "orders.log"))
    # Every arm's own store. These default to paths under data/shared, and a
    # test that forgets to redirect one writes to the live arm's record.
    #
    # EVERY store with an env override belongs here. The first version listed
    # only the trading arms, and CI caught the gap that a developer machine
    # hides: locally these files already exist, so a test touching one changes
    # nothing detectable, while on a clean checkout it CREATES them inside
    # data/. The tripwire below reported exactly that on the first green-CI
    # run — which is the argument for the tripwire.
    for var, name in (
        # trading arms
        ("FADE_NEARCLOSE_DB", "fade_nearclose.sqlite"),
        ("BACK_NEARCLOSE_DB", "back_nearclose.sqlite"),
        # fade_props / fade_retail_longshot were retired to attic/ on
        # 2026-08-23, but their stores are RETAINED as evidence and the
        # attic tests can still be run by hand from the repo root (where
        # this conftest applies). Keep redirecting them: the cost is two
        # lines, the failure mode is synthetic rows in a real ledger.
        ("FADE_PROPS_DB", "fade_props.sqlite"),
        ("FADE_RETAIL_DB", "fade_retail.sqlite"),
        # capture streams
        ("ECON_CAPTURE_DB", "econ_capture.sqlite"),
        ("MENTION_CAPTURE_DB", "mention_capture.sqlite"),
        ("NEAR_CLOSE_DB", "near_close_capture.sqlite"),
        ("CANDLE_PANEL_DB", "candle_panel.sqlite"),
        ("CRYPTO_CAPTURE_DB", "crypto_capture.sqlite"),
        ("NEWS_CAPTURE_DB", "news_capture.sqlite"),
        ("TENNIS_CAPTURE_DB", "tennis_capture.sqlite"),
        ("WNBA_CAPTURE_DB", "wnba_capture.sqlite"),
        # sidecar state + shared archives
        ("BASEBALL_STATE_PATH", "baseball_state.sqlite"),
        ("TENNIS_STATE_PATH", "tennis_state.sqlite"),
        ("WNBA_STATE_PATH", "wnba_state.sqlite"),
        ("BOOK_ARCHIVE_PATH", "book_archive.sqlite"),
        ("TRADE_ARCHIVE_PATH", "trade_archive.sqlite"),
        ("LLM_CACHE_PATH", "llm_cache.sqlite"),
        ("CAPTURED_EDGE_DB", "captured_edge.sqlite"),
        ("STORM_WATCH_STATE_FILE", "storm_watch.json"),
        ("ADVISORY_CAPTURE_DB", "advisory_capture.sqlite"),
    ):
        monkeypatch.setenv(var, str(tmp_path / name))
    monkeypatch.setenv("BACKTEST_HISTORY_DIR", str(tmp_path / "backtest_runs"))


def _live_data_fingerprint() -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) for every JSON/JSONL file in the REAL data tree."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent / "data"
    out: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        # .sqlite included deliberately: the first version of this tripwire
        # watched only JSON, and a test with a frozen DB constant wrote three
        # synthetic rows straight into a production SQLite store without it
        # firing. Watch everything a test could plausibly corrupt.
        if p.is_file() and (p.suffix in (".json", ".jsonl", ".sqlite", ".txt")
                            or p.name == "orders.log"):
            try:
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return out


@pytest.fixture(scope="session", autouse=True)
def _fail_if_tests_touch_live_data():
    """Tripwire: the suite must not write into the real ``data/`` tree.

    Isolation is enforced by fixtures above, but isolation is only as good as
    the next path someone adds. This turns the rule into an invariant that
    fails visibly instead of a convention that quietly erodes — which is what
    happened before: a frozen module-level path meant tests appended real rows
    to a live audit log and a production database on every run, for weeks,
    with nothing to notice it.

    Files a running daemon writes concurrently would be false positives, so
    this reports rather than asserts when it cannot tell the difference —
    see the message.
    """
    before = _live_data_fingerprint()
    yield
    after = _live_data_fingerprint()
    changed = sorted(
        k for k in set(before) & set(after) if before[k] != after[k])
    added = sorted(set(after) - set(before))
    if changed or added:
        detail = "\n".join(
            [f"  modified: {p}" for p in changed] + [f"  created:  {p}" for p in added])
        pytest.fail(
            "The test suite wrote into the live data/ tree:\n" + detail +
            "\n\nIf a background daemon was running it may be a false positive — "
            "re-run with the fleet stopped to confirm. Otherwise a path is "
            "escaping isolation: make it lazy (env-overridable) and redirect it "
            "in conftest, as with POLYMARKET_DATA_DIR / REACTIVE_LOG_FILE.")
