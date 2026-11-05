"""Tests for the calibration module."""

import pandas as pd
import pytest

from pm.ledger import calibration as cal

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def forecasts_path(tmp_path, monkeypatch):
    """Redirect forecasts storage to a temp file via the env-var override."""
    p = tmp_path / "forecasts.json"
    monkeypatch.setenv("POLYMARKET_FORECASTS_FILE", str(p))
    return p


def _add(question="Q", cid="0xaaa", outcome="Yes", market=0.5, prob=0.5,
         *, source="user", notes=""):
    return cal.add_forecast(question, cid, outcome, market, prob,
                            source=source, notes=notes)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestAddForecast:
    def test_basic(self, forecasts_path):
        _add(prob=0.7)
        df = cal.get_forecasts()
        assert len(df) == 1
        assert df.iloc[0]["your_probability"] == pytest.approx(0.7)
        assert df.iloc[0]["resolution"] is None

    def test_invalid_market_price_raises(self, forecasts_path):
        with pytest.raises(ValueError, match="market_price"):
            _add(market=1.5)

    def test_invalid_probability_raises(self, forecasts_path):
        with pytest.raises(ValueError, match="your_probability"):
            _add(prob=-0.1)

    def test_uuid_assigned(self, forecasts_path):
        f = _add()
        assert "id" in f and f["id"]

    def test_multiple_forecasts_persisted(self, forecasts_path):
        _add(question="A")
        _add(question="B")
        _add(question="C")
        assert len(cal.get_forecasts()) == 3


class TestRecordResolution:
    def test_marks_resolved(self, forecasts_path):
        f = _add(prob=0.7)
        assert cal.record_resolution(f["id"], outcome_yes=True) is True
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is True
        assert df.iloc[0]["resolved_at"] is not None

    def test_unknown_id_returns_false(self, forecasts_path):
        _add()
        assert cal.record_resolution("nonexistent-uuid", outcome_yes=True) is False


class TestDeleteForecast:
    def test_deletes(self, forecasts_path):
        f = _add()
        assert cal.delete_forecast(f["id"]) is True
        assert cal.get_forecasts().empty

    def test_unknown_id_returns_false(self, forecasts_path):
        assert cal.delete_forecast("nope") is False


# ---------------------------------------------------------------------------
# Source tagging + per-source Brier
# ---------------------------------------------------------------------------

class TestSourceField:
    def test_default_source_is_user(self, forecasts_path):
        f = _add()
        assert f["source"] == "user"
        df = cal.get_forecasts()
        assert df.iloc[0]["source"] == "user"

    def test_custom_source_persisted(self, forecasts_path):
        _add(source="llm:llama3.1:latest", notes="anchored to market")
        df = cal.get_forecasts()
        assert df.iloc[0]["source"] == "llm:llama3.1:latest"
        assert df.iloc[0]["notes"] == "anchored to market"

    def test_legacy_records_default_to_user(self, forecasts_path, tmp_path):
        # Write a legacy file with no `source` field
        import json
        legacy = {
            "version": 1,
            "forecasts": [{
                "id": "abc", "timestamp": "2026-05-01T00:00:00+00:00",
                "question": "Q?", "condition_id": "0xa", "outcome": "Yes",
                "market_price": 0.5, "your_probability": 0.6,
                "resolution": None, "resolved_at": None,
            }],
        }
        with open(forecasts_path, "w") as f:
            json.dump(legacy, f)
        df = cal.get_forecasts()
        assert df.iloc[0]["source"] == "user"
        assert df.iloc[0]["notes"] == ""

    def test_brier_by_source_groups_correctly(self, forecasts_path):
        # User: 2 forecasts, perfectly calibrated
        _add(question="A", source="user", prob=1.0)
        _add(question="B", source="user", prob=0.0)
        # LLM: 1 forecast, terrible
        _add(question="C", source="llm:fake", prob=1.0)
        df = cal.get_forecasts()
        # Resolve them
        cal.record_resolution(df.iloc[0]["id"], True)
        cal.record_resolution(df.iloc[1]["id"], False)
        cal.record_resolution(df.iloc[2]["id"], False)

        by_src = cal.brier_by_source(cal.get_forecasts())
        assert set(by_src.keys()) == {"user", "llm:fake"}
        assert by_src["user"]["brier"] == pytest.approx(0.0)
        assert by_src["user"]["n"] == 2
        assert by_src["llm:fake"]["brier"] == pytest.approx(1.0)
        assert by_src["llm:fake"]["n"] == 1

    def test_brier_by_source_empty(self):
        assert cal.brier_by_source(pd.DataFrame()) == {}


# ---------------------------------------------------------------------------
# Auto-resolution against a closed-markets lookup
# ---------------------------------------------------------------------------

class TestAutoResolvePending:
    def test_yes_forecast_with_yes_outcome_marks_correct(self, forecasts_path):
        f = _add(cid="m1", outcome="Yes", prob=0.7)
        updated = cal.auto_resolve_pending({"m1": True})
        assert updated == [f["id"]]
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is True

    def test_yes_forecast_with_no_outcome_marks_incorrect(self, forecasts_path):
        _add(cid="m1", outcome="Yes", prob=0.7)
        cal.auto_resolve_pending({"m1": False})
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is False

    def test_no_forecast_with_no_outcome_marks_correct(self, forecasts_path):
        _add(cid="m1", outcome="No", prob=0.6)
        cal.auto_resolve_pending({"m1": False})
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is True

    def test_no_forecast_with_yes_outcome_marks_incorrect(self, forecasts_path):
        _add(cid="m1", outcome="No", prob=0.6)
        cal.auto_resolve_pending({"m1": True})
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is False

    def test_already_resolved_forecasts_are_skipped(self, forecasts_path):
        f = _add(cid="m1", outcome="Yes")
        cal.record_resolution(f["id"], outcome_yes=True)
        original_resolved_at = cal.get_forecasts().iloc[0]["resolved_at"]
        # Re-run auto-resolve with conflicting data — must not overwrite
        updated = cal.auto_resolve_pending({"m1": False})
        assert updated == []
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is True   # unchanged
        assert df.iloc[0]["resolved_at"] == original_resolved_at

    def test_unmatched_condition_id_left_pending(self, forecasts_path):
        _add(cid="m1", outcome="Yes")
        cal.auto_resolve_pending({"m2": True})   # different market
        df = cal.get_forecasts()
        assert df.iloc[0]["resolution"] is None

    def test_empty_lookup_is_noop(self, forecasts_path):
        _add(cid="m1", outcome="Yes")
        assert cal.auto_resolve_pending({}) == []
        df = cal.get_forecasts()
        assert df.iloc[0]["resolution"] is None

    def test_persists_across_loads(self, forecasts_path):
        _add(cid="m1", outcome="Yes")
        cal.auto_resolve_pending({"m1": True})
        # Re-load from disk
        df = cal.get_forecasts()
        assert bool(df.iloc[0]["resolution"]) is True
        assert df.iloc[0]["resolved_at"] is not None

    def test_bulk_resolution_across_many_forecasts(self, forecasts_path):
        _add(question="A", cid="m1", outcome="Yes")
        _add(question="B", cid="m2", outcome="No")
        _add(question="C", cid="m3", outcome="Yes")   # no match
        _add(question="D", cid="m4", outcome="No")    # already resolved
        df = cal.get_forecasts()
        cal.record_resolution(df[df["question"] == "D"].iloc[0]["id"],
                              outcome_yes=False)
        updated = cal.auto_resolve_pending({"m1": True, "m2": False, "m4": True})
        # 2 newly resolved, 1 unmatched, 1 skipped-because-already-resolved
        assert len(updated) == 2
        df = cal.get_forecasts()
        assert bool(df[df["question"] == "A"].iloc[0]["resolution"]) is True
        assert bool(df[df["question"] == "B"].iloc[0]["resolution"]) is True
        assert df[df["question"] == "C"].iloc[0]["resolution"] is None
        # D was already False — must NOT be flipped despite m4=True in lookup
        assert bool(df[df["question"] == "D"].iloc[0]["resolution"]) is False


# ---------------------------------------------------------------------------
# Brier score
# ---------------------------------------------------------------------------

class TestBrierScore:
    def test_empty_returns_none(self):
        assert cal.brier_score(pd.DataFrame()) is None

    def test_no_resolved_returns_none(self):
        df = pd.DataFrame([
            {"your_probability": 0.5, "resolution": None, "outcome": "Yes"}
        ])
        assert cal.brier_score(df) is None

    def test_perfect_forecaster_scores_zero(self):
        df = pd.DataFrame([
            {"your_probability": 1.0, "resolution": True,  "outcome": "Yes"},
            {"your_probability": 0.0, "resolution": False, "outcome": "Yes"},
            {"your_probability": 1.0, "resolution": False, "outcome": "No"},
        ])
        assert cal.brier_score(df) == pytest.approx(0.0)

    def test_worst_forecaster_scores_one(self):
        df = pd.DataFrame([
            {"your_probability": 1.0, "resolution": False, "outcome": "Yes"},
            {"your_probability": 0.0, "resolution": True,  "outcome": "Yes"},
        ])
        assert cal.brier_score(df) == pytest.approx(1.0)

    def test_random_forecaster_scores_quarter(self):
        df = pd.DataFrame([
            {"your_probability": 0.5, "resolution": True,  "outcome": "Yes"},
            {"your_probability": 0.5, "resolution": False, "outcome": "Yes"},
            {"your_probability": 0.5, "resolution": True,  "outcome": "Yes"},
            {"your_probability": 0.5, "resolution": False, "outcome": "Yes"},
        ])
        assert cal.brier_score(df) == pytest.approx(0.25)

    def test_no_outcome_correctly_handled(self):
        """Forecasting 80% on NO that resolves NO is just as good as 80% on
        YES that resolves YES."""
        df = pd.DataFrame([
            {"your_probability": 0.8, "resolution": True,  "outcome": "Yes"},
            {"your_probability": 0.8, "resolution": False, "outcome": "No"},
        ])
        # Both are "correct outcome predicted with 80% confidence" → same error
        # Per-row error = (0.8 - 1.0)^2 = 0.04
        assert cal.brier_score(df) == pytest.approx(0.04)

    def test_ignores_pending_forecasts(self):
        df = pd.DataFrame([
            {"your_probability": 1.0, "resolution": True, "outcome": "Yes"},
            {"your_probability": 0.5, "resolution": None, "outcome": "Yes"},
        ])
        # Only the resolved one (perfect) contributes
        assert cal.brier_score(df) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Calibration bins
# ---------------------------------------------------------------------------

class TestCalibrationBins:
    def test_empty_returns_empty_frame(self):
        result = cal.calibration_bins(pd.DataFrame())
        assert result.empty
        assert list(result.columns) == ["bin_center", "n", "mean_predicted", "observed_rate"]

    def test_basic_binning(self):
        # All forecasts at 0.7, half resolve correctly
        df = pd.DataFrame([
            {"your_probability": 0.7, "resolution": True,  "outcome": "Yes"},
            {"your_probability": 0.7, "resolution": False, "outcome": "Yes"},
        ])
        result = cal.calibration_bins(df, n_bins=10)
        # 0.7 lands in the 0.6-0.7 bin (right edge inclusive) → center 0.65
        # Or 0.7-0.8 depending on right-inclusivity — just check we have 1 bin
        assert len(result) == 1
        assert result.iloc[0]["n"] == 2
        assert result.iloc[0]["observed_rate"] == pytest.approx(0.5)

    def test_well_calibrated_forecaster_on_diagonal(self):
        # Forecaster says 0.8 ten times, 8 resolve correctly
        df = pd.DataFrame([
            {"your_probability": 0.8, "resolution": (i < 8), "outcome": "Yes"}
            for i in range(10)
        ])
        result = cal.calibration_bins(df, n_bins=10)
        assert len(result) == 1
        # observed_rate should be 0.8 (8 out of 10 correct)
        assert result.iloc[0]["observed_rate"] == pytest.approx(0.8)
        assert result.iloc[0]["mean_predicted"] == pytest.approx(0.8)

    def test_overconfident_forecaster_below_diagonal(self):
        # Always says 0.9, only 50% correct → severely overconfident
        df = pd.DataFrame([
            {"your_probability": 0.9, "resolution": (i < 5), "outcome": "Yes"}
            for i in range(10)
        ])
        result = cal.calibration_bins(df, n_bins=10)
        assert result.iloc[0]["mean_predicted"] == pytest.approx(0.9)
        assert result.iloc[0]["observed_rate"] == pytest.approx(0.5)

    def test_ignores_pending(self):
        df = pd.DataFrame([
            {"your_probability": 0.7, "resolution": True, "outcome": "Yes"},
            {"your_probability": 0.7, "resolution": None, "outcome": "Yes"},
        ])
        result = cal.calibration_bins(df)
        assert result.iloc[0]["n"] == 1


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------

class TestBrierScoreByCategory:
    def _markets(self):
        return pd.DataFrame([
            {"condition_id": "0xc1", "category": "Crypto"},
            {"condition_id": "0xc2", "category": "Crypto"},
            {"condition_id": "0xs1", "category": "Sports"},
        ])

    def _forecast(self, cid, prob, resolution, outcome="Yes"):
        return {
            "your_probability": prob,
            "resolution": resolution,
            "outcome": outcome,
            "condition_id": cid,
        }

    def test_empty_inputs(self):
        assert cal.brier_score_by_category(pd.DataFrame(), pd.DataFrame()) == {}
        assert cal.brier_score_by_category(pd.DataFrame(), self._markets()) == {}

    def test_below_min_sample_returns_none(self):
        # Only 3 Crypto forecasts — below the 5-min threshold
        df_fc = pd.DataFrame([self._forecast("0xc1", 0.5, True) for _ in range(3)])
        out = cal.brier_score_by_category(df_fc, self._markets())
        assert out.get("Crypto") is None

    def test_at_min_sample_computes(self):
        # 5 Crypto forecasts: 4 perfect, 1 wrong. Brier = (4*0 + 1*1)/5 = 0.2
        df_fc = pd.DataFrame([
            self._forecast("0xc1", 1.0, True),
            self._forecast("0xc1", 1.0, True),
            self._forecast("0xc2", 1.0, True),
            self._forecast("0xc2", 1.0, True),
            self._forecast("0xc2", 1.0, False),    # wrong
        ])
        out = cal.brier_score_by_category(df_fc, self._markets())
        assert out["Crypto"] == pytest.approx(0.2)

    def test_only_resolved_forecasts_count(self):
        # 5 Crypto entries but 3 are unresolved → below threshold for Crypto
        df_fc = pd.DataFrame([
            self._forecast("0xc1", 1.0, True),
            self._forecast("0xc1", 1.0, True),
            self._forecast("0xc1", 0.5, None),   # unresolved
            self._forecast("0xc2", 0.5, None),
            self._forecast("0xc2", 0.5, None),
        ])
        out = cal.brier_score_by_category(df_fc, self._markets())
        assert out.get("Crypto") is None

    def test_multiple_categories(self):
        df_fc = pd.DataFrame(
            [self._forecast("0xc1", 1.0, True) for _ in range(5)] +
            [self._forecast("0xs1", 0.0, False) for _ in range(5)]
        )
        out = cal.brier_score_by_category(df_fc, self._markets())
        assert out["Crypto"] == pytest.approx(0.0)   # all perfect
        assert out["Sports"] == pytest.approx(0.0)   # 0.0 prob, resolved NO on a YES forecast → correct

    def test_unknown_condition_id_bucketed_as_unknown(self):
        df_fc = pd.DataFrame([
            self._forecast("0xunknown", 1.0, True) for _ in range(5)
        ])
        out = cal.brier_score_by_category(df_fc, self._markets())
        assert "Unknown" in out


class TestMarketBrierScore:
    def test_empty_returns_none(self):
        assert cal.market_brier_score(pd.DataFrame()) is None

    def test_perfect_predictions(self):
        df = pd.DataFrame([
            {"predicted_prob": 1.0, "actual": 1.0},
            {"predicted_prob": 0.0, "actual": 0.0},
        ])
        assert cal.market_brier_score(df) == pytest.approx(0.0)

    def test_worst_predictions(self):
        df = pd.DataFrame([
            {"predicted_prob": 1.0, "actual": 0.0},
            {"predicted_prob": 0.0, "actual": 1.0},
        ])
        assert cal.market_brier_score(df) == pytest.approx(1.0)

    def test_random_predictions(self):
        df = pd.DataFrame([
            {"predicted_prob": 0.5, "actual": 1.0},
            {"predicted_prob": 0.5, "actual": 0.0},
            {"predicted_prob": 0.5, "actual": 1.0},
            {"predicted_prob": 0.5, "actual": 0.0},
        ])
        assert cal.market_brier_score(df) == pytest.approx(0.25)

    def test_known_value(self):
        # predicted 0.7, actual 1 → (0.7-1)^2 = 0.09; predicted 0.3, actual 0 → 0.09
        df = pd.DataFrame([
            {"predicted_prob": 0.7, "actual": 1.0},
            {"predicted_prob": 0.3, "actual": 0.0},
        ])
        assert cal.market_brier_score(df) == pytest.approx(0.09)


class TestMarketBrierByCategory:
    def test_empty(self):
        assert cal.market_brier_by_category(pd.DataFrame()) == {}

    def test_below_threshold_returns_none(self):
        df = pd.DataFrame([
            {"predicted_prob": 0.5, "actual": 1.0, "category": "Sports"}
            for _ in range(3)
        ])
        out = cal.market_brier_by_category(df)
        assert out["Sports"] is None

    def test_above_threshold_computes(self):
        df = pd.DataFrame([
            {"predicted_prob": 1.0, "actual": 1.0, "category": "Sports"}
            for _ in range(5)
        ])
        out = cal.market_brier_by_category(df)
        assert out["Sports"] == pytest.approx(0.0)


class TestMarketCalibrationBins:
    def test_empty(self):
        assert cal.market_calibration_bins(pd.DataFrame()).empty

    def test_bins_correctly(self):
        # All predictions at 0.7, mix of YES/NO outcomes
        df = pd.DataFrame([
            {"predicted_prob": 0.7, "actual": 1.0},
            {"predicted_prob": 0.7, "actual": 1.0},
            {"predicted_prob": 0.7, "actual": 0.0},
        ])
        result = cal.market_calibration_bins(df, n_bins=10)
        assert len(result) == 1
        # 2 of 3 resolved YES → observed_rate ≈ 0.667
        assert result.iloc[0]["observed_rate"] == pytest.approx(2/3, rel=1e-3)
        assert result.iloc[0]["n"] == 3


class TestSummaryStats:
    def test_empty(self, forecasts_path):
        s = cal.summary_stats(cal.get_forecasts())
        assert s == {"total": 0, "resolved": 0, "pending": 0, "brier": None}

    def test_counts(self, forecasts_path):
        f1 = _add()
        _add()
        cal.record_resolution(f1["id"], outcome_yes=True)
        s = cal.summary_stats(cal.get_forecasts())
        assert s["total"] == 2
        assert s["resolved"] == 1
        assert s["pending"] == 1
        assert s["brier"] is not None
