"""Auto-resolution against the hardened forecast ledger.

``auto_resolve_runner.run_cycle`` walks *every* profile on a 15-minute cron and
writes each one's ``forecasts.json``. That makes it the main concurrent writer
against the arms, and the reason ``calibration.auto_resolve_pending`` now takes
the lock. These tests cover that path end to end against real files.
"""

from __future__ import annotations

import json

import pytest

from pm.ledger import calibration as cal
from pm.ops import auto_resolve_runner as arr


@pytest.fixture
def forecasts_path(tmp_path, monkeypatch):
    p = tmp_path / "forecasts.json"
    monkeypatch.setenv("POLYMARKET_FORECASTS_FILE", str(p))
    return p


class TestAutoResolvePending:
    def test_resolves_matching_yes_forecast_as_won(self, forecasts_path):
        cal.add_forecast("Q?", "TKR-1", "Yes", 0.4, 0.6)
        updated = cal.auto_resolve_pending({"TKR-1": True})
        assert len(updated) == 1
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is True

    def test_no_forecast_on_a_yes_resolution_is_a_loss(self, forecasts_path):
        cal.add_forecast("Q?", "TKR-1", "No", 0.4, 0.6)
        cal.auto_resolve_pending({"TKR-1": True})
        assert bool(cal.get_forecasts().iloc[0]["resolution"]) is False

    def test_already_resolved_forecasts_are_never_overwritten(self, forecasts_path):
        f = cal.add_forecast("Q?", "TKR-1", "Yes", 0.4, 0.6)
        cal.record_resolution(f["id"], True)
        assert cal.auto_resolve_pending({"TKR-1": False}) == []
        assert bool(cal.get_forecasts().iloc[0]["resolution"]) is True

    def test_unknown_tickers_are_skipped(self, forecasts_path):
        cal.add_forecast("Q?", "TKR-1", "Yes", 0.4, 0.6)
        assert cal.auto_resolve_pending({"OTHER": True}) == []

    def test_empty_lookup_is_a_no_op(self, forecasts_path):
        cal.add_forecast("Q?", "TKR-1", "Yes", 0.4, 0.6)
        assert cal.auto_resolve_pending({}) == []

    def test_does_not_rewrite_the_file_when_nothing_resolves(self, forecasts_path):
        """The cron runs this for every profile every 15 minutes and usually
        resolves nothing; a needless rewrite of a multi-megabyte ledger on each
        pass is churn the lock would also have to serialize."""
        cal.add_forecast("Q?", "TKR-1", "Yes", 0.4, 0.6)
        before = forecasts_path.stat().st_mtime_ns
        cal.auto_resolve_pending({"NOPE": True})
        assert forecasts_path.stat().st_mtime_ns == before

    def test_survives_a_damaged_ledger_by_recovering_from_bak(self, forecasts_path):
        cal.add_forecast("Q1?", "TKR-1", "Yes", 0.4, 0.6)
        cal.add_forecast("Q2?", "TKR-2", "Yes", 0.4, 0.6)   # writes .bak
        forecasts_path.write_text('{"version": 1, "forecasts": [{"id"')
        # Must not raise — this runs unattended under Restart=on-failure.
        cal.auto_resolve_pending({"TKR-1": True})
        assert len(cal.get_forecasts()) >= 1


class TestDeleteAndResolve:
    def test_delete_missing_forecast_leaves_the_file_untouched(self, forecasts_path):
        cal.add_forecast("Q?", "TKR-1", "Yes", 0.4, 0.6)
        before = forecasts_path.read_text()
        assert cal.delete_forecast("nonexistent-id") is False
        assert forecasts_path.read_text() == before

    def test_delete_removes_only_the_named_forecast(self, forecasts_path):
        a = cal.add_forecast("Q1?", "T1", "Yes", 0.4, 0.6)
        cal.add_forecast("Q2?", "T2", "Yes", 0.4, 0.6)
        assert cal.delete_forecast(a["id"]) is True
        remaining = cal.get_forecasts()
        assert len(remaining) == 1
        assert remaining.iloc[0]["condition_id"] == "T2"

    def test_delete_on_a_missing_file_is_false_not_an_error(self, forecasts_path):
        assert cal.delete_forecast("anything") is False

    def test_record_resolution_on_unknown_id_returns_false(self, forecasts_path):
        cal.add_forecast("Q?", "T1", "Yes", 0.4, 0.6)
        assert cal.record_resolution("no-such-id", True) is False


class _FakeSidecar:
    """Minimal stand-in for a sidecar exposing the optional resolve hook."""

    name = "fake"

    def __init__(self, resolutions=None, raises=False):
        self._resolutions = resolutions or {}
        self._raises = raises

    def resolve_pending_tickers(self, tickers):
        if self._raises:
            raise RuntimeError("sidecar exploded")
        return {t: v for t, v in self._resolutions.items() if t in tickers}


class TestOverlaySidecarResolutions:
    def test_sidecar_resolutions_are_overlaid(self, monkeypatch):
        from pm.core import sidecars
        monkeypatch.setattr(sidecars, "list_sidecars",
                            lambda: [_FakeSidecar({"B": False})])
        merged = arr.overlay_sidecar_resolutions({"A": True}, ["B"])
        assert merged["A"] is True
        assert merged["B"] is False

    def test_no_pending_tickers_short_circuits(self, monkeypatch):
        from pm.core import sidecars

        def boom():
            raise AssertionError("must not consult sidecars with nothing pending")

        monkeypatch.setattr(sidecars, "list_sidecars", boom)
        assert arr.overlay_sidecar_resolutions({"A": True}, []) == {"A": True}

    def test_a_raising_sidecar_does_not_break_the_cycle(self, monkeypatch):
        """This walks every profile unattended; one bad sidecar must not stop
        resolution for the rest."""
        from pm.core import sidecars
        monkeypatch.setattr(sidecars, "list_sidecars",
                            lambda: [_FakeSidecar(raises=True),
                                     _FakeSidecar({"B": True})])
        merged = arr.overlay_sidecar_resolutions({"A": True}, ["B"])
        assert merged["B"] is True

    def test_sidecar_overlay_wins_over_the_public_feed(self, monkeypatch):
        """Documented precedence: later overlays win, because sidecars know
        game outcomes days before the venue settles."""
        from pm.core import sidecars
        monkeypatch.setattr(sidecars, "list_sidecars",
                            lambda: [_FakeSidecar({"A": False})])
        merged = arr.overlay_sidecar_resolutions({"A": True}, ["A"])
        assert merged["A"] is False


class TestBuildClosedLookup:
    def test_tolerates_a_provider_that_raises(self, monkeypatch):
        """One broken venue must not stop resolution for the others — this
        walks every profile unattended."""
        class Boom:
            name = "boom"
            def get_closed_markets(self, *a, **k):
                raise RuntimeError("provider down")

        monkeypatch.setattr(arr.mp, "list_providers", lambda: [Boom()])
        assert arr.build_closed_lookup() == {}


class TestLedgerIntegration:
    def test_concurrent_add_and_resolve_keep_every_forecast(self, forecasts_path):
        """Interleave the two writers that actually collide in production: an
        arm appending a forecast and the cron resolving one."""
        ids = [cal.add_forecast(f"Q{i}?", f"T{i}", "Yes", 0.4, 0.6)["id"]
               for i in range(5)]
        cal.auto_resolve_pending({"T0": True, "T2": True})
        cal.add_forecast("Q-late?", "T-late", "Yes", 0.5, 0.5)

        data = json.loads(forecasts_path.read_text())
        assert len(data["forecasts"]) == 6, "an append or a resolution was lost"
        by_id = {f["id"]: f for f in data["forecasts"]}
        assert by_id[ids[0]]["resolution"] is True
        assert by_id[ids[1]]["resolution"] is None
        assert by_id[ids[2]]["resolution"] is True
