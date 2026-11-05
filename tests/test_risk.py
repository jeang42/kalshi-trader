"""Tests for the risk module (Tier 1 of the recommendations roadmap)."""

import json

import pandas as pd
import pytest

from pm.ledger import risk


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("POLYMARKET_SETTINGS_FILE", str(p))
    return p


# ---------------------------------------------------------------------------
# Settings load/save
# ---------------------------------------------------------------------------

class TestSettings:
    def test_load_defaults_when_no_file(self, settings_path):
        s = risk.load_settings()
        assert s.bankroll == 1000.0
        assert s.max_kelly_fraction == 0.25
        assert s.drawdown_pause_pct == 0.15

    def test_save_then_load_roundtrip(self, settings_path):
        s = risk.Settings(bankroll=5000.0, max_kelly_fraction=0.10,
                          max_market_position_pct=0.03,
                          max_category_exposure_pct=0.40,
                          drawdown_pause_pct=0.10)
        risk.save_settings(s)
        s2 = risk.load_settings()
        assert s2.bankroll == 5000.0
        assert s2.max_kelly_fraction == 0.10
        assert s2.drawdown_pause_pct == 0.10

    def test_unknown_keys_ignored(self, settings_path):
        # Forward-compat: settings.json may have keys we don't recognise
        settings_path.write_text(json.dumps({
            "bankroll": 2000.0,
            "future_setting": "hello",
        }))
        s = risk.load_settings()
        assert s.bankroll == 2000.0
        # Defaults preserved for fields not in the file
        assert s.max_kelly_fraction == 0.25

    def test_malformed_json_falls_back_to_defaults(self, settings_path):
        settings_path.write_text("not valid json")
        s = risk.load_settings()
        # Should not raise; returns defaults
        assert s.bankroll == 1000.0


# ---------------------------------------------------------------------------
# Calibration-adjusted Kelly multiplier
# ---------------------------------------------------------------------------

class TestCalibrationAdjustedMultiplier:
    def test_sharp_calibration_full_kelly(self):
        mult, msg = risk.calibration_adjusted_kelly_multiplier(0.05)
        assert mult == 1.0
        assert "sharp" in msg.lower() or "full" in msg.lower()

    def test_at_full_kelly_threshold(self):
        # 0.10 is the upper edge of the full-Kelly band
        assert risk.calibration_adjusted_kelly_multiplier(0.10)[0] == 1.0

    def test_solid_calibration_half_kelly(self):
        mult, _ = risk.calibration_adjusted_kelly_multiplier(0.15)
        assert mult == 0.5

    def test_at_half_kelly_threshold(self):
        assert risk.calibration_adjusted_kelly_multiplier(0.20)[0] == 0.5

    def test_mediocre_calibration_quarter_kelly(self):
        mult, _ = risk.calibration_adjusted_kelly_multiplier(0.22)
        assert mult == 0.25

    def test_at_quarter_kelly_threshold(self):
        assert risk.calibration_adjusted_kelly_multiplier(0.25)[0] == 0.25

    def test_worse_than_random_zero_multiplier(self):
        """Brier > 0.25 means worse than uniform-random forecaster — don't bet."""
        mult, msg = risk.calibration_adjusted_kelly_multiplier(0.30)
        assert mult == 0.0
        assert "don't bet" in msg.lower() or "match the market" in msg.lower()

    def test_none_returns_half_kelly_default(self):
        """No calibration data → default to half Kelly with explanation."""
        mult, msg = risk.calibration_adjusted_kelly_multiplier(None)
        assert mult == 0.5
        assert "no calibration data" in msg.lower()


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

class TestCurrentDrawdown:
    def test_empty(self):
        r = risk.current_drawdown(pd.DataFrame())
        assert r["peak"] is None
        assert r["pct"] is None
        assert r["in_drawdown"] is False

    def test_single_snapshot_no_drawdown(self):
        df = pd.DataFrame([{"net_value": 1000.0}])
        r = risk.current_drawdown(df)
        assert r["peak"] == 1000.0
        assert r["current"] == 1000.0
        assert r["pct"] == 0.0
        assert r["in_drawdown"] is False

    def test_monotone_rising_no_drawdown(self):
        df = pd.DataFrame([{"net_value": v} for v in [1000, 1100, 1200, 1300]])
        r = risk.current_drawdown(df)
        assert r["pct"] == 0.0
        assert r["in_drawdown"] is False

    def test_falling_after_peak(self):
        df = pd.DataFrame([{"net_value": v} for v in [1000, 1500, 1200]])
        r = risk.current_drawdown(df)
        assert r["peak"] == 1500.0
        assert r["current"] == 1200.0
        assert r["pct"] == pytest.approx(-0.20)
        assert r["in_drawdown"] is True

    def test_recovered_to_peak(self):
        # Went down then back up to the peak: no current drawdown
        df = pd.DataFrame([{"net_value": v} for v in [1000, 1500, 1200, 1500]])
        r = risk.current_drawdown(df)
        assert r["pct"] == 0.0
        assert r["in_drawdown"] is False


class TestShouldPauseForDrawdown:
    def test_below_threshold_no_pause(self):
        dd = {"pct": -0.05, "in_drawdown": True}
        assert risk.should_pause_for_drawdown(dd, 0.15) is False

    def test_above_threshold_pause(self):
        dd = {"pct": -0.20, "in_drawdown": True}
        assert risk.should_pause_for_drawdown(dd, 0.15) is True

    def test_at_threshold_pause(self):
        """Exactly at the threshold: pause (the user crossed it)."""
        dd = {"pct": -0.15, "in_drawdown": True}
        assert risk.should_pause_for_drawdown(dd, 0.15) is True

    def test_none_pct_no_pause(self):
        """No drawdown data → don't gate the UI."""
        dd = {"pct": None, "in_drawdown": False}
        assert risk.should_pause_for_drawdown(dd, 0.15) is False


# ---------------------------------------------------------------------------
# Category exposure
# ---------------------------------------------------------------------------

class TestCategoryExposurePct:
    def test_empty(self):
        assert risk.category_exposure_pct(pd.DataFrame()) == {}

    def test_single_category(self):
        df = pd.DataFrame([{"category": "Crypto", "cost_basis": 100.0}])
        assert risk.category_exposure_pct(df) == {"Crypto": 1.0}

    def test_multiple_categories(self):
        df = pd.DataFrame([
            {"category": "Crypto", "cost_basis": 300.0},
            {"category": "Sports", "cost_basis": 100.0},
            {"category": "Crypto", "cost_basis": 100.0},
        ])
        out = risk.category_exposure_pct(df)
        assert out["Crypto"] == pytest.approx(0.8)
        assert out["Sports"] == pytest.approx(0.2)

    def test_nan_category_bucketed_as_unknown(self):
        df = pd.DataFrame([
            {"category": None, "cost_basis": 50.0},
            {"category": "Sports", "cost_basis": 50.0},
        ])
        out = risk.category_exposure_pct(df)
        assert "Unknown" in out
        assert out["Unknown"] == pytest.approx(0.5)

    def test_empty_string_bucketed_as_unknown(self):
        df = pd.DataFrame([
            {"category": "", "cost_basis": 50.0},
            {"category": "Sports", "cost_basis": 50.0},
        ])
        out = risk.category_exposure_pct(df)
        assert "Unknown" in out


class TestConcentratedCategories:
    def test_no_concentration(self):
        assert risk.concentrated_categories({"A": 0.25, "B": 0.25}, 0.30) == []

    def test_one_over(self):
        assert risk.concentrated_categories({"A": 0.40, "B": 0.30}, 0.30) == [("A", 0.40)]

    def test_sorted_descending(self):
        out = risk.concentrated_categories({"A": 0.40, "B": 0.50, "C": 0.10}, 0.30)
        assert out == [("B", 0.50), ("A", 0.40)]


# ---------------------------------------------------------------------------
# Position-size limit
# ---------------------------------------------------------------------------

class TestPositionExceedsLimit:
    def test_under_limit(self):
        assert risk.position_exceeds_limit(40, 1000, 0.05) is False

    def test_at_limit(self):
        assert risk.position_exceeds_limit(50, 1000, 0.05) is False

    def test_over_limit(self):
        assert risk.position_exceeds_limit(51, 1000, 0.05) is True

    def test_zero_bankroll(self):
        """Zero bankroll: any positive cost exceeds the limit."""
        assert risk.position_exceeds_limit(10, 0, 0.05) is True
        assert risk.position_exceeds_limit(0, 0, 0.05) is False
