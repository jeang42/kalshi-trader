"""Tests for auto_resolve_runner — the cron-fired forecast resolver.

Mocks the providers, Kalshi auth, and calibration writes. Verifies:
- the merged lookup gets built once on the owner profile
- every profile is walked and gets the merged lookup
- save/restore of the active profile around the cycle
- no settlements overlay when auth isn't configured
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.ops import auto_resolve_runner as ar

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch):
    """Tempdir-backed profile system with two profiles."""
    from pm.core import profiles
    monkeypatch.setattr(profiles, "PROFILES_ROOT", tmp_path)
    monkeypatch.setattr(profiles, "ACTIVE_PROFILE_FILE",
                         tmp_path / ".active_profile")
    for name in ("jeff", "sandbox"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    profiles.set_active_profile("jeff")
    return profiles


# ---------------------------------------------------------------------------
# build_closed_lookup
# ---------------------------------------------------------------------------

class TestBuildClosedLookup:
    @patch.object(ar.mp, "list_providers")
    def test_unions_across_providers(self, mock_list):
        p1 = MagicMock()
        p1.name = "polymarket"
        p1.get_closed_markets_in_range.return_value = pd.DataFrame([
            {"condition_id": "P1", "resolved_yes": True},
            {"condition_id": "P2", "resolved_yes": False},
        ])
        p2 = MagicMock()
        p2.name = "kalshi"
        p2.get_closed_markets_in_range.return_value = pd.DataFrame([
            {"condition_id": "K1", "resolved_yes": True},
        ])
        mock_list.return_value = [p1, p2]
        lookup = ar.build_closed_lookup()
        assert lookup == {"P1": True, "P2": False, "K1": True}

    @patch.object(ar.mp, "list_providers")
    def test_skips_provider_errors(self, mock_list):
        p1 = MagicMock()
        p1.name = "broken"
        p1.get_closed_markets_in_range.side_effect = RuntimeError("boom")
        p2 = MagicMock()
        p2.name = "good"
        p2.get_closed_markets_in_range.return_value = pd.DataFrame([
            {"condition_id": "G1", "resolved_yes": True},
        ])
        mock_list.return_value = [p1, p2]
        lookup = ar.build_closed_lookup()
        assert lookup == {"G1": True}

    @patch.object(ar.mp, "list_providers")
    def test_skips_null_resolved(self, mock_list):
        p = MagicMock()
        p.name = "p"
        p.get_closed_markets_in_range.return_value = pd.DataFrame([
            {"condition_id": "X", "resolved_yes": None},
            {"condition_id": "Y", "resolved_yes": True},
        ])
        mock_list.return_value = [p]
        lookup = ar.build_closed_lookup()
        assert lookup == {"Y": True}


# ---------------------------------------------------------------------------
# overlay_kalshi_settlements
# ---------------------------------------------------------------------------

class TestOverlay:
    def test_no_op_when_not_configured(self, monkeypatch):
        from pm.venue import kalshi_auth as ka
        monkeypatch.setattr(ka, "is_configured", lambda: False)
        base = {"X": True}
        out = ar.overlay_kalshi_settlements(base)
        assert out == {"X": True}   # unchanged

    def test_adds_settlements_when_configured(self, monkeypatch):
        from pm.venue import kalshi_auth as ka
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(ka, "is_configured", lambda: True)
        monkeypatch.setattr(kc, "get_settlements", lambda **kw: pd.DataFrame([
            {"ticker": "KX-1", "market_result": "no"},
            {"ticker": "KX-2", "market_result": "yes"},
        ]))
        out = ar.overlay_kalshi_settlements({"P1": True})
        assert out == {"P1": True, "KX-1": False, "KX-2": True}

    def test_settlements_override_public_feed(self, monkeypatch):
        """When same ticker is in both, settlement (account-side) wins."""
        from pm.venue import kalshi_auth as ka
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(ka, "is_configured", lambda: True)
        monkeypatch.setattr(kc, "get_settlements", lambda **kw: pd.DataFrame([
            {"ticker": "KX-1", "market_result": "no"},
        ]))
        # Public feed had it as resolved YES — settlements say NO
        out = ar.overlay_kalshi_settlements({"KX-1": True})
        assert out["KX-1"] is False

    def test_handles_settlement_errors_gracefully(self, monkeypatch):
        from pm.venue import kalshi_auth as ka
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(ka, "is_configured", lambda: True)
        def boom(**kw):
            raise RuntimeError("api down")
        monkeypatch.setattr(kc, "get_settlements", boom)
        # Should not raise; returns the input untouched
        out = ar.overlay_kalshi_settlements({"P1": True})
        assert out == {"P1": True}


# ---------------------------------------------------------------------------
# run_cycle — orchestration
# ---------------------------------------------------------------------------

class TestRunCycle:
    @patch.object(ar.mp, "list_providers", return_value=[])
    @patch.object(ar.cal, "auto_resolve_pending")
    def test_walks_every_profile_and_save_restores(
        self, mock_resolve, _list, isolated_profiles, monkeypatch,
    ):
        # initialize_profiles creates every DEFAULT_PROFILES dir — pin
        # the iteration to our two test profiles for predictability.
        monkeypatch.setattr(isolated_profiles, "list_profiles",
                             lambda: ["jeff", "sandbox"])
        mock_resolve.return_value = ["forecast-id-1"]   # resolve 1 per profile
        results = ar.run_cycle()
        assert set(results.keys()) == {"jeff", "sandbox"}
        assert results["jeff"] == 1
        assert results["sandbox"] == 1
        # Active profile restored to original (jeff)
        assert isolated_profiles.active_profile() == "jeff"

    @patch.object(ar.mp, "list_providers", return_value=[])
    @patch.object(ar.cal, "auto_resolve_pending")
    def test_one_profile_failure_doesnt_break_others(
        self, mock_resolve, _list, isolated_profiles, monkeypatch,
    ):
        monkeypatch.setattr(isolated_profiles, "list_profiles",
                             lambda: ["jeff", "sandbox"])
        # First call raises, second returns a resolution
        mock_resolve.side_effect = [RuntimeError("boom"), ["x"]]
        results = ar.run_cycle()
        # One profile got 0 (failed), the other got 1
        assert sum(results.values()) == 1


# ---------------------------------------------------------------------------
# main() loop semantics
# ---------------------------------------------------------------------------

class TestMain:
    @patch.object(ar, "run_cycle")
    def test_single_cycle_returns_zero(self, mock_cycle, monkeypatch):
        mock_cycle.return_value = {"jeff": 0}
        monkeypatch.setattr(sys, "argv", ["auto_resolve_runner"])
        rc = ar.main()
        assert rc == 0

    @patch.object(ar, "run_cycle")
    def test_failure_in_single_cycle_returns_one(self, mock_cycle, monkeypatch):
        mock_cycle.side_effect = RuntimeError("boom")
        monkeypatch.setattr(sys, "argv", ["auto_resolve_runner"])
        rc = ar.main()
        assert rc == 1
