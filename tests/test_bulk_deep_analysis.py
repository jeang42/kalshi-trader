"""Tests for the bulk Deep Analysis runner.

analyze_market and the provider are mocked — these don't hit the
network. Live coverage comes from the smoke test (existing checks
already cover the underlying analyze_market path).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.ops import bulk_deep_analysis as bda


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    """Tempdir-backed profile system + isolated forecast file."""
    from pm.core import profiles
    monkeypatch.setattr(profiles, "PROFILES_ROOT", tmp_path)
    monkeypatch.setattr(profiles, "ACTIVE_PROFILE_FILE",
                         tmp_path / ".active_profile")
    for name in ("jeff", "agentic_deep"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    profiles.set_active_profile("jeff")
    monkeypatch.setenv("POLYMARKET_FORECASTS_FILE",
                       str(tmp_path / "_forecasts.json"))
    return profiles


def _markets_df(n: int = 5, category: str = "Politics") -> pd.DataFrame:
    return pd.DataFrame([
        {
            "condition_id": f"KX-{i}",
            "question": f"Question {i}",
            "category": category,
            "outcome_prices": [0.4, 0.6],
            "liquidity": 1000.0 - i,
            "days_to_resolution": 5.0,
        }
        for i in range(n)
    ])


def _mixed_df(category_counts: dict[str, int]) -> pd.DataFrame:
    """Build a DataFrame with markets across multiple categories.
    Liquidity is descending across the whole frame so the natural sort
    biases toward whichever category appears first in this iteration."""
    rows = []
    liq = 10000.0
    for cat, n in category_counts.items():
        for i in range(n):
            rows.append({
                "condition_id": f"{cat[:4]}-{i}",
                "question": f"{cat} Q{i}",
                "category": cat,
                "outcome_prices": [0.4, 0.6],
                "liquidity": liq,
                "days_to_resolution": 5.0,
            })
            liq -= 1
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# run_cycle — happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_analyzes_and_logs_each_market(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        # Provider returns 3 markets
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _markets_df(3)
        mock_get_provider.return_value = prov

        # Mock analyze returns a fresh DeepAnalysis per call
        from pm.feeds import llm_deep_analysis as lda
        def _fake_analysis(*, ticker, **kw):
            return lda.DeepAnalysis(
                model="qwen3.5:35b", market_id=ticker,
                question="Q", as_of="now",
                probability=0.55, confidence="medium",
                key_drivers=["x"], key_risks=["y"],
                evidence_used=["z"],
                recommended_action="BUY_YES",
                reasoning="r", context_summary="ctx",
            )
        mock_analyze.side_effect = _fake_analysis

        stats = bda.run_cycle(
            model="qwen3.5:35b", profile="agentic_deep",
            max_markets=10, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        assert stats["considered"] == 3
        assert stats["analyzed"] == 3
        assert stats["logged"] == 3

        # Forecasts landed in the calibration log
        from pm.ledger import calibration as cal
        df = cal.get_forecasts()
        assert len(df) == 3
        assert (df["source"] == "llm_deep:qwen3.5:35b").all()
        assert (df["outcome"] == "Yes").all()
        assert (df["your_probability"] == 0.55).all()

    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_save_restores_active_profile(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _markets_df(1)
        mock_get_provider.return_value = prov
        mock_analyze.return_value = None  # treated as failure

        # User is on jeff before the run
        assert isolated_profile.active_profile() == "jeff"
        bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=5, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        # After the run, we're back on jeff
        assert isolated_profile.active_profile() == "jeff"

    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_max_markets_caps_the_batch(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _markets_df(20)
        mock_get_provider.return_value = prov

        from pm.feeds import llm_deep_analysis as lda
        mock_analyze.return_value = lda.DeepAnalysis(
            model="m", market_id="x", question="q", as_of="now",
            probability=0.5, confidence="low",
            recommended_action="WAIT", reasoning="",
            context_summary="",
        )
        stats = bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=5, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        assert stats["considered"] == 5  # capped


# ---------------------------------------------------------------------------
# Category diversity
# ---------------------------------------------------------------------------

class TestCategoryDiversity:
    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_picks_across_categories_not_just_top_liquidity(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        # Sports dominates by liquidity (50 markets at the top). Without
        # diversity logic the batch would be 100% sports. With it, all
        # 4 categories should be represented.
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _mixed_df({
            "Sports": 50, "Politics": 10,
            "Finance": 5, "Geopolitics": 3,
        })
        mock_get_provider.return_value = prov

        from pm.feeds import llm_deep_analysis as lda
        mock_analyze.return_value = lda.DeepAnalysis(
            model="m", market_id="x", question="q", as_of="now",
            probability=0.5, confidence="low",
            recommended_action="WAIT", reasoning="",
            context_summary="",
        )
        bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=20, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        # Inspect the calibration log for category mix
        from pm.ledger import calibration as cal
        df = cal.get_forecasts()
        # The questions are "<Category> Q<i>" so we can recover
        # category by splitting on space.
        df["cat"] = df["question"].str.split(" ", n=1).str[0]
        cats = set(df["cat"].unique())
        assert len(cats) >= 3, (
            f"only {len(cats)} categories in batch: {cats} — "
            "diversity logic isn't firing")


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailures:
    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_none_result_counted_as_skipped(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _markets_df(2)
        mock_get_provider.return_value = prov
        mock_analyze.return_value = None
        stats = bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=5, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        assert stats["skipped_failed"] == 2
        assert stats["logged"] == 0

    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_one_failure_doesnt_stop_the_run(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _markets_df(3)
        mock_get_provider.return_value = prov

        from pm.feeds import llm_deep_analysis as lda
        # First call raises, second returns None, third succeeds
        mock_analyze.side_effect = [
            RuntimeError("boom"),
            None,
            lda.DeepAnalysis(
                model="m", market_id="KX-2", question="Q2", as_of="now",
                probability=0.4, confidence="low",
                recommended_action="WAIT", reasoning="",
                context_summary="",
            ),
        ]
        stats = bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=5, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        assert stats["skipped_failed"] == 2
        assert stats["logged"] == 1


# ---------------------------------------------------------------------------
# Recency skip
# ---------------------------------------------------------------------------

class TestRecencySkip:
    @patch("pm.ops.bulk_deep_analysis.lda.analyze_market")
    @patch("pm.ops.bulk_deep_analysis.mp.get_provider")
    def test_recently_analyzed_market_is_skipped(
        self, mock_get_provider, mock_analyze, isolated_profile,
    ):
        prov = MagicMock()
        prov.get_markets_closing_within.return_value = _markets_df(2)
        mock_get_provider.return_value = prov

        from pm.feeds import llm_deep_analysis as lda
        mock_analyze.return_value = lda.DeepAnalysis(
            model="m", market_id="x", question="q", as_of="now",
            probability=0.5, confidence="low",
            recommended_action="WAIT", reasoning="",
            context_summary="",
        )
        # First run — both markets analyzed
        bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=10, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        assert mock_analyze.call_count == 2

        # Second run within the same window — both skipped
        mock_analyze.reset_mock()
        stats = bda.run_cycle(
            model="m", profile="agentic_deep",
            max_markets=10, max_days=30,
            include_web_context=False, skip_age_hours=12.0,
        )
        assert stats["skipped_recent"] == 2
        assert mock_analyze.call_count == 0


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

class TestMain:
    @patch.object(bda, "run_cycle")
    def test_main_runs_one_cycle(self, mock_cycle, monkeypatch):
        mock_cycle.return_value = {
            "model": "m", "profile": "agentic_deep",
            "considered": 0, "analyzed": 0, "logged": 0,
            "skipped_recent": 0, "skipped_failed": 0,
        }
        monkeypatch.setattr(sys, "argv",
                             ["bulk_deep_analysis", "--model", "m"])
        assert bda.main() == 0
        mock_cycle.assert_called_once()
