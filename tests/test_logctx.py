"""Tests for pm/core/logctx.py (P3/R-23) — correlation contract.

The load-bearing claims: bindings nest and always restore; contexts are
isolated across threads; text lines outside any bind() are byte-identical to
the pre-R-23 format (no noise on quiet paths); JSON mode emits parseable
envelopes; and an orders.log audit row written inside a bind() carries the
correlation fields (the single most valuable consumer — it ties a real order
to the cycle that placed it).
"""
from __future__ import annotations

import io
import json
import logging
import threading

import pytest

from pm.core import logctx


@pytest.fixture()
def fresh_root():
    """Restore root-logger handler state after the test; logctx.setup mutates
    it. The test must call ``_strip(root)`` ITSELF, in its body: pytest's
    logging plugin re-attaches its capture handlers between fixture setup and
    the call phase, so handlers removed here would be back by the time the
    test runs — and ``setup()`` would (correctly) take its idempotent branch
    and add nothing. That cost an hour; hence this comment."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield root
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)


def _strip(root: logging.Logger) -> None:
    for h in root.handlers[:]:
        root.removeHandler(h)


class TestBind:
    def test_nests_additively_and_restores(self):
        assert logctx.current() == {}
        with logctx.bind(a=1):
            with logctx.bind(b="x"):
                assert logctx.current() == {"a": "1", "b": "x"}
            assert logctx.current() == {"a": "1"}
        assert logctx.current() == {}

    def test_restores_on_exception(self):
        with pytest.raises(RuntimeError):
            with logctx.bind(a=1):
                raise RuntimeError("boom")
        assert logctx.current() == {}

    def test_inner_bind_shadows_outer(self):
        with logctx.bind(a="outer"):
            with logctx.bind(a="inner"):
                assert logctx.current()["a"] == "inner"
            assert logctx.current()["a"] == "outer"

    def test_values_are_stringified_once(self):
        from pathlib import Path
        with logctx.bind(n=3, p=Path("/x")):
            assert logctx.current() == {"n": "3", "p": "/x"}

    def test_threads_are_isolated(self):
        seen = {}

        def worker():
            seen["worker"] = logctx.current()

        with logctx.bind(main="only"):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        assert seen["worker"] == {}, "a sibling thread must not inherit bindings"


class TestScope:
    def test_fresh_cycle_id_per_call(self):
        ids = []

        @logctx.scope(arm="test_arm")
        def cycle():
            ids.append(logctx.current()["cycle"])
            assert logctx.current()["arm"] == "test_arm"

        cycle()
        cycle()
        assert len(set(ids)) == 2 and all(i.startswith("cyc-") for i in ids)


class TestSetupAndFormat:
    def test_text_lines_unchanged_outside_bind(self, fresh_root):
        _strip(fresh_root)
        buf = io.StringIO()
        logctx.setup("INFO", stream=buf)
        logging.getLogger("t").info("quiet path")
        line = buf.getvalue().strip()
        assert line.endswith("[INFO] t: quiet path"), line
        assert "[" not in line.split("quiet path")[1] if "quiet path" in line else True

    def test_text_lines_carry_ctx_inside_bind(self, fresh_root):
        _strip(fresh_root)
        buf = io.StringIO()
        logctx.setup("INFO", stream=buf)
        with logctx.bind(arm="a", cycle="cyc-1"):
            logging.getLogger("t").info("hot path")
        assert buf.getvalue().strip().endswith("hot path [arm=a cycle=cyc-1]")

    def test_json_mode(self, fresh_root, monkeypatch):
        monkeypatch.setenv("POLYMARKET_LOG_FORMAT", "json")
        _strip(fresh_root)
        buf = io.StringIO()
        logctx.setup("INFO", stream=buf)
        with logctx.bind(order="ord-1"):
            logging.getLogger("t").warning("w")
        rec = json.loads(buf.getvalue().strip())
        assert (rec["level"], rec["logger"], rec["msg"]) == ("WARNING", "t", "w")
        assert rec["ctx"] == {"order": "ord-1"}

    def test_idempotent_like_basicconfig(self, fresh_root):
        _strip(fresh_root)
        buf = io.StringIO()
        logctx.setup("INFO", stream=buf)
        logctx.setup("DEBUG", stream=io.StringIO())  # second call: level only
        assert len(fresh_root.handlers) == 1
        assert fresh_root.level == logging.DEBUG


class TestAuditRowsCarryCtx:
    def test_audit_embeds_bound_context(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORDERS_LOG_PATH", str(tmp_path / "orders.log"))
        from pm.venue import kalshi_orders as ko
        with logctx.bind(arm="fade_nearclose", cycle="cyc-42"):
            ko._audit("test_event", {"ticker": "KXTEST-1"})
        row = json.loads((tmp_path / "orders.log").read_text().strip())
        assert row["event"] == "test_event"
        assert row["ctx"] == {"arm": "fade_nearclose", "cycle": "cyc-42"}

    def test_audit_omits_ctx_when_none_bound(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORDERS_LOG_PATH", str(tmp_path / "orders.log"))
        from pm.venue import kalshi_orders as ko
        ko._audit("test_event", {})
        row = json.loads((tmp_path / "orders.log").read_text().strip())
        assert "ctx" not in row, "quiet rows must not grow an empty ctx field"
