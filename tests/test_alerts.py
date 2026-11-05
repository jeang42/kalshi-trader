"""Tests for the alerts module."""

import pandas as pd
import pytest

from pm.ledger import alerts


@pytest.fixture
def alerts_path(tmp_path, monkeypatch):
    """Redirect alerts storage to a temp file via the env-var override."""
    p = tmp_path / "alerts.json"
    monkeypatch.setenv("POLYMARKET_ALERTS_FILE", str(p))
    return p


def _markets_df(rows):
    """Build a minimal markets DataFrame: rows is list of (cid, outcomes, prices)."""
    return pd.DataFrame([
        {"condition_id": cid, "outcomes": list(outcomes),
         "outcome_prices": list(prices)}
        for cid, outcomes, prices in rows
    ])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestAddAlert:
    def test_basic(self, alerts_path):
        a = alerts.add_alert("Q", "0xaaa", "Yes", "above", 0.80)
        assert a["triggered"] is False
        assert a["threshold"] == 0.80

    def test_invalid_direction_raises(self, alerts_path):
        with pytest.raises(ValueError, match="direction"):
            alerts.add_alert("Q", "0xaaa", "Yes", "sideways", 0.5)

    def test_invalid_threshold_raises(self, alerts_path):
        with pytest.raises(ValueError, match="threshold"):
            alerts.add_alert("Q", "0xaaa", "Yes", "above", 1.5)

    def test_persists(self, alerts_path):
        alerts.add_alert("A", "0x1", "Yes", "above", 0.5)
        alerts.add_alert("B", "0x2", "No",  "below", 0.2)
        df = alerts.get_alerts()
        assert len(df) == 2


class TestDeleteAlert:
    def test_returns_true_when_deleted(self, alerts_path):
        a = alerts.add_alert("Q", "0x1", "Yes", "above", 0.5)
        assert alerts.delete_alert(a["id"]) is True
        assert alerts.get_alerts().empty

    def test_unknown_id_returns_false(self, alerts_path):
        assert alerts.delete_alert("nope") is False


class TestResetAlert:
    def test_clears_triggered_state(self, alerts_path):
        a = alerts.add_alert("Q", "0x1", "Yes", "above", 0.5)
        # Force-trigger via check
        df = _markets_df([("0x1", ["Yes", "No"], [0.7, 0.3])])
        alerts.check_alerts(df)

        assert alerts.get_alerts().iloc[0]["triggered"]

        assert alerts.reset_alert(a["id"]) is True
        row = alerts.get_alerts().iloc[0]
        assert bool(row["triggered"]) is False
        assert row["triggered_at"] is None

    def test_unknown_id_returns_false(self, alerts_path):
        assert alerts.reset_alert("nope") is False


# ---------------------------------------------------------------------------
# Threshold checking
# ---------------------------------------------------------------------------

class TestCheckAlerts:
    def test_above_triggers_when_price_crosses(self, alerts_path):
        alerts.add_alert("Q", "0xaaa", "Yes", "above", 0.80)
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.85, 0.15])])
        new = alerts.check_alerts(df)
        assert len(new) == 1
        assert new[0]["triggered_price"] == pytest.approx(0.85)

    def test_above_does_not_trigger_when_below(self, alerts_path):
        alerts.add_alert("Q", "0xaaa", "Yes", "above", 0.80)
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.75, 0.25])])
        new = alerts.check_alerts(df)
        assert new == []

    def test_below_triggers(self, alerts_path):
        alerts.add_alert("Q", "0xaaa", "Yes", "below", 0.30)
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.25, 0.75])])
        new = alerts.check_alerts(df)
        assert len(new) == 1

    def test_below_at_exact_threshold_triggers(self, alerts_path):
        """≤ comparison — equal to threshold counts."""
        alerts.add_alert("Q", "0xaaa", "Yes", "below", 0.30)
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.30, 0.70])])
        new = alerts.check_alerts(df)
        assert len(new) == 1

    def test_above_at_exact_threshold_triggers(self):
        """Sanity: ≥ comparison."""
        # No need for fixture — direct helper test
        from pm.ledger.alerts import _alert_should_trigger
        assert _alert_should_trigger("above", 0.80, 0.80) is True
        assert _alert_should_trigger("above", 0.79, 0.80) is False
        assert _alert_should_trigger("below", 0.30, 0.30) is True
        assert _alert_should_trigger("below", 0.31, 0.30) is False

    def test_already_triggered_not_re_triggered(self, alerts_path):
        """Once triggered, the alert is not returned again on subsequent checks."""
        alerts.add_alert("Q", "0xaaa", "Yes", "above", 0.80)
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.85, 0.15])])

        first = alerts.check_alerts(df)
        assert len(first) == 1
        second = alerts.check_alerts(df)
        assert second == []   # no double-fire

    def test_market_missing_from_data_no_trigger(self, alerts_path):
        """If the market isn't in df_markets, the alert simply isn't checked."""
        alerts.add_alert("Q", "0xmissing", "Yes", "above", 0.5)
        df = _markets_df([("0xother", ["Yes", "No"], [0.99, 0.01])])
        new = alerts.check_alerts(df)
        assert new == []
        # State unchanged
        assert bool(alerts.get_alerts().iloc[0]["triggered"]) is False

    def test_outcome_mismatch_no_trigger(self, alerts_path):
        """Alert on 'Yes' isn't triggered by the 'No' price."""
        alerts.add_alert("Q", "0xaaa", "Yes", "below", 0.20)
        # Yes is 0.85, No is 0.15. Alert on Yes should NOT trigger (Yes is high)
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.85, 0.15])])
        new = alerts.check_alerts(df)
        assert new == []

    def test_multiple_alerts_can_fire_same_check(self, alerts_path):
        alerts.add_alert("A", "0xaaa", "Yes", "above", 0.50)
        alerts.add_alert("B", "0xbbb", "Yes", "below", 0.20)
        df = _markets_df([
            ("0xaaa", ["Yes", "No"], [0.99, 0.01]),
            ("0xbbb", ["Yes", "No"], [0.10, 0.90]),
        ])
        new = alerts.check_alerts(df)
        assert len(new) == 2

    def test_empty_markets_df_no_trigger(self, alerts_path):
        alerts.add_alert("Q", "0xaaa", "Yes", "above", 0.5)
        new = alerts.check_alerts(pd.DataFrame())
        assert new == []


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummaryStats:
    def test_empty(self, alerts_path):
        s = alerts.summary_stats()
        assert s == {"total": 0, "active": 0, "triggered": 0}

    def test_counts(self, alerts_path):
        alerts.add_alert("A", "0xaaa", "Yes", "above", 0.50)
        alerts.add_alert("B", "0xbbb", "Yes", "above", 0.50)
        # Trigger one
        df = _markets_df([("0xaaa", ["Yes", "No"], [0.99, 0.01])])
        alerts.check_alerts(df)

        s = alerts.summary_stats()
        assert s["total"] == 2
        assert s["active"] == 1
        assert s["triggered"] == 1
