"""provenance — stamp results with the code that produced them (R-18)."""

from __future__ import annotations

import re
import subprocess

import pytest

from pm.core import provenance as pv


@pytest.fixture(autouse=True)
def _uncached():
    pv.git_commit.cache_clear()
    pv.is_dirty.cache_clear()
    yield
    pv.git_commit.cache_clear()
    pv.is_dirty.cache_clear()


def test_git_commit_matches_git_rev_parse(monkeypatch):
    monkeypatch.delenv("PROVENANCE_COMMIT", raising=False)
    want = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert pv.git_commit() == want[:7]
    assert pv.git_commit(short=False) == want


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("PROVENANCE_COMMIT", "abcdef0123456789")
    assert pv.git_commit() == "abcdef0"
    assert pv.is_dirty() is False


def test_stamp_and_line_shapes(monkeypatch):
    monkeypatch.delenv("PROVENANCE_COMMIT", raising=False)
    s = pv.stamp()
    assert set(s) == {"commit", "dirty", "python", "stamped_at"}
    assert re.fullmatch(r"[0-9a-f]{7}", s["commit"])
    assert s["dirty"] in (True, False)
    assert re.match(r"# produced by commit [0-9a-f]{7} \((clean|dirty)\) at \d{4}-", pv.stamp_line())
    assert pv.label() in (s["commit"], s["commit"] + "+dirty")


def test_no_repo_gives_none_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PROVENANCE_COMMIT", raising=False)
    monkeypatch.setattr(pv, "_repo_root", lambda start=None: None)
    assert pv.git_commit() is None
    assert pv.is_dirty() is None
    assert pv.label() == "unknown"
    assert "unknown (unknown-state)" in pv.stamp_line()


def test_read_head_sha_handles_symbolic_detached_and_packed(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    # detached
    (git / "HEAD").write_text("1111111111111111111111111111111111111111\n")
    assert pv._read_head_sha(tmp_path) == "1" * 40
    # symbolic + loose ref
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("2" * 40 + "\n")
    assert pv._read_head_sha(tmp_path) == "2" * 40
    # symbolic + packed only
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs with: peeled\n" + "3" * 40 + " refs/heads/main\n")
    assert pv._read_head_sha(tmp_path) == "3" * 40
    # missing ref entirely
    (git / "packed-refs").unlink()
    assert pv._read_head_sha(tmp_path) is None


class TestWritersAreStamped:
    """The stamp is only worth anything if the writers actually embed it."""

    def test_orders_audit_line_carries_commit(self, tmp_path, monkeypatch):
        import json

        from pm.venue import kalshi_orders as ko
        monkeypatch.setenv("ORDERS_LOG_PATH", str(tmp_path / "orders.log"))
        ko._audit("test_event", {"ticker": "KXT"})
        row = json.loads((tmp_path / "orders.log").read_text().splitlines()[-1])
        assert row["event"] == "test_event" and row["commit"] == pv.label()

    def test_exit_row_carries_commit(self, tmp_path, monkeypatch):
        import json

        from pm.arms import strategy_runner as sr
        from pm.core import profiles
        profiles.initialize_profiles()
        sr._log_exit(profile_name="agentic", condition_id="KXT", question="q",
                     outcome="Yes", shares=1.0, entry_price=0.4, exit_price=0.5,
                     peak_price=0.5, opened_at=None, exit_reason="test",
                     strategy_key="t")
        p = profiles.profile_dir("agentic") / "exits.jsonl"
        row = json.loads(p.read_text().splitlines()[-1])
        assert row["commit"] == pv.label()

    def test_backtest_run_carries_provenance(self, tmp_path, monkeypatch):
        import json

        from pm.research import backtest as bt
        from pm.research import backtest_history as bh
        monkeypatch.setenv("BACKTEST_HISTORY_DIR", str(tmp_path / "runs"))
        import dataclasses
        # build a minimal BacktestResult without depending on its full field list
        kw = {}
        for f in dataclasses.fields(bt.BacktestResult):
            if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                continue
            kw[f.name] = {"str": "s", "int": 0, "float": 0.0, "bool": False}.get(
                getattr(f.type, "__name__", str(f.type)).split(" ")[0], None)
        kw["strategy_name"] = "s"
        res = bt.BacktestResult(**kw)
        run_id = bh.save_run(res, params={"x": 1})
        payload = json.loads((tmp_path / "runs" / f"{run_id}.json").read_text())
        assert payload["provenance"]["commit"] == pv.git_commit()

    # test_arm_position_row_carries_commit_sha moved to
    # attic/tests/test_nearclose_fragments.py on 2026-08-24 — the last sqlite
    # arm that wrote stamped position rows retired. The stamping contract
    # still has live coverage where it matters most: kalshi_orders._audit
    # embeds provenance.label() into every orders.log row (tested in
    # test_kalshi_orders/test_logctx). Re-inline when a new arm ships.
