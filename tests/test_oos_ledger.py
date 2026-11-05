"""Tests for out-of-sample bookkeeping.

The property that matters is the one the module exists for: **changing a rule
must reset the evidence clock**, so trades fitted to a filter can never be
counted as though they tested it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pm.ledger import oos_ledger as oos


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("OOS_LEDGER_FILE", str(tmp_path / "oos.json"))
    return tmp_path / "oos.json"


class TestFingerprint:
    def test_key_order_does_not_matter(self):
        assert oos.fingerprint({"a": 1, "b": 2}) == oos.fingerprint({"b": 2, "a": 1})

    def test_float_formatting_does_not_matter(self):
        assert oos.fingerprint({"a": 0.10}) == oos.fingerprint({"a": 0.1})

    def test_value_change_changes_the_hash(self):
        assert oos.fingerprint({"a": 1}) != oos.fingerprint({"a": 2})

    def test_added_parameter_changes_the_hash(self):
        assert oos.fingerprint({"a": 1}) != oos.fingerprint({"a": 1, "b": 2})

    def test_list_order_matters(self):
        """An exclusion list is a set semantically, but callers are expected to
        sort it before passing. If they do not, order changes read as rule
        changes — noisy, but it fails safe (resets the clock) rather than
        silently treating different rules as the same."""
        assert oos.fingerprint({"x": [1, 2]}) != oos.fingerprint({"x": [2, 1]})


class TestRecordParams:
    def test_first_registration_starts_the_clock(self):
        r = oos.record_params("arm", {"band": 0.1})
        assert r.change_count == 0
        assert r.days_of_evidence() < 1

    def test_identical_params_do_not_move_the_boundary(self):
        first = oos.record_params("arm", {"band": 0.1})
        later = oos.record_params("arm", {"band": 0.1})
        assert later.frozen_at == first.frozen_at
        assert later.change_count == 0

    def test_changed_params_reset_the_clock(self):
        """The whole point. A re-fit must not inherit the old evidence."""
        old = datetime.now(UTC) - timedelta(days=30)
        oos.record_params("arm", {"band": 0.1}, now=old)
        oos.freeze_at("arm", old)
        assert oos.get_rules("arm").days_of_evidence() > 29

        after = oos.record_params("arm", {"band": 0.2})
        assert after.days_of_evidence() < 1, "changing a rule must restart the clock"
        assert after.change_count == 1
        assert after.previous_fingerprint == oos.fingerprint({"band": 0.1})

    def test_repeated_edits_are_counted(self):
        oos.record_params("arm", {"b": 1})
        oos.record_params("arm", {"b": 2})
        oos.record_params("arm", {"b": 3})
        assert oos.get_rules("arm").change_count == 2

    def test_arms_are_independent(self):
        oos.record_params("a", {"x": 1})
        oos.record_params("b", {"x": 1})
        oos.record_params("a", {"x": 2})
        assert oos.get_rules("a").change_count == 1
        assert oos.get_rules("b").change_count == 0

    def test_unknown_arm_reads_as_none(self):
        assert oos.get_rules("never-seen") is None


class TestFreezeAt:
    def test_backdates_the_boundary(self):
        oos.record_params("arm", {"x": 1})
        when = datetime(2026, 7, 19, tzinfo=UTC)
        r = oos.freeze_at("arm", when, reason="commit abc123")
        assert r.frozen_at.startswith("2026-07-19")

    def test_unknown_arm_returns_none(self):
        assert oos.freeze_at("nope", datetime.now(UTC)) is None

    def test_a_later_param_change_still_overrides_a_backdate(self):
        """Backdating records history; it must not pin the clock against
        future edits, or a re-fit would keep inherited evidence."""
        oos.record_params("arm", {"x": 1})
        oos.freeze_at("arm", datetime(2026, 1, 1, tzinfo=UTC))
        oos.record_params("arm", {"x": 2})
        assert oos.get_rules("arm").days_of_evidence() < 1


class TestSplitPositions:
    @pytest.fixture
    def frozen_arm(self):
        oos.record_params("arm", {"x": 1})
        oos.freeze_at("arm", datetime(2026, 6, 1, tzinfo=UTC))
        return "arm"

    def _pos(self, day, pnl, event=None):
        return {"opened_at": datetime(2026, day[0], day[1], tzinfo=UTC).timestamp(),
                "pnl": pnl, "event_id": event}

    def test_splits_at_the_boundary(self, frozen_arm):
        s = oos.split_positions(frozen_arm, [
            self._pos((5, 1), 10.0),      # before
            self._pos((7, 1), 5.0),       # after
        ])
        assert (s.in_sample_n, s.in_sample_pnl) == (1, 10.0)
        assert (s.out_of_sample_n, s.out_of_sample_pnl) == (1, 5.0)

    def test_unresolved_positions_are_excluded_entirely(self, frozen_arm):
        """An open position is not evidence either way."""
        s = oos.split_positions(frozen_arm, [
            {"opened_at": datetime(2026, 7, 1, tzinfo=UTC).timestamp(), "pnl": None},
        ])
        assert s.out_of_sample_n == 0

    def test_counts_independent_clusters(self, frozen_arm):
        s = oos.split_positions(frozen_arm, [
            self._pos((7, 1), 1.0, "EV1"),
            self._pos((7, 2), 1.0, "EV1"),   # same event — one observation
            self._pos((7, 3), 1.0, "EV2"),
        ])
        assert s.out_of_sample_n == 3
        assert s.out_of_sample_clusters == 2, "correlated positions are one observation"
        assert s.oos_mean_per_cluster == pytest.approx(1.5)

    def test_iso_timestamps_are_accepted(self, frozen_arm):
        s = oos.split_positions(frozen_arm, [
            {"opened_at": "2026-07-01T00:00:00+00:00", "pnl": 3.0},
        ])
        assert s.out_of_sample_n == 1

    def test_naive_timestamps_are_treated_as_utc(self, frozen_arm):
        s = oos.split_positions(frozen_arm, [
            {"opened_at": "2026-07-01T00:00:00", "pnl": 3.0},
        ])
        assert s.out_of_sample_n == 1

    def test_unparseable_timestamps_are_skipped_not_guessed(self, frozen_arm):
        s = oos.split_positions(frozen_arm, [{"opened_at": "not a date", "pnl": 1.0}])
        assert s.in_sample_n == s.out_of_sample_n == 0

    def test_unregistered_arm_returns_none(self):
        assert oos.split_positions("nope", []) is None


class TestVerdict:
    def _split(self, **kw):
        return oos.Split(arm="a", frozen_at="2026-06-01T00:00:00+00:00", **kw)

    def test_no_oos_is_stated_plainly(self):
        assert "NO OOS EVIDENCE" in self._split().verdict

    def test_small_sample_is_called_too_early(self):
        v = self._split(out_of_sample_n=10, out_of_sample_clusters=10,
                        out_of_sample_pnl=99.0).verdict
        assert "TOO EARLY" in v, "a big number on a tiny sample is not a result"

    def test_positive_result_is_hedged_not_declared(self):
        v = self._split(out_of_sample_n=500, out_of_sample_clusters=500,
                        out_of_sample_pnl=100.0).verdict
        assert "not yet a result" in v

    def test_negative_result_is_stated_bluntly(self):
        v = self._split(out_of_sample_n=500, out_of_sample_clusters=500,
                        out_of_sample_pnl=-100.0).verdict
        assert "NEGATIVE" in v

    def test_clusters_override_position_count_in_the_verdict(self):
        """400 positions across 5 events is 5 observations, and must read as
        too early rather than as a large sample."""
        v = self._split(out_of_sample_n=400, out_of_sample_clusters=5,
                        out_of_sample_pnl=50.0).verdict
        assert "TOO EARLY" in v
        assert "5 OOS events" in v


# TestArmsWiring moved to attic/tests/test_nearclose_fragments.py on
# 2026-08-24 — it pinned the retired arms' _decision_params() wiring. The
# ledger's own fingerprint/split logic keeps its full coverage above; the
# ledger FILE keeps both arms' frozen-rule history as evidence.

class TestFormatting:
    def test_status_with_no_arms_explains_how_to_register(self):
        out = oos.format_status([])
        assert "record_params" in out, "an empty report should say what to do next"

    def test_status_lists_each_arm_with_its_age(self):
        oos.record_params("arm_a", {"x": 1})
        oos.record_params("arm_b", {"y": 2})
        out = oos.format_status(oos.all_arms())
        assert "arm_a" in out and "arm_b" in out
        assert "fingerprint" in out

    def test_status_explains_that_an_edit_resets_the_clock(self):
        """The one thing a reader must not miss."""
        oos.record_params("arm", {"x": 1})
        assert "resets" in oos.format_status(oos.all_arms())


class TestCli:
    def test_status_exits_zero(self, capsys):
        oos.record_params("arm", {"x": 1})
        assert oos.main(["--status"]) == 0
        assert "arm" in capsys.readouterr().out

    def test_status_on_an_empty_ledger_still_exits_zero(self, capsys):
        assert oos.main(["--status"]) == 0

    def test_split_reports_the_boundary(self, capsys):
        oos.record_params("arm", {"x": 1})
        assert oos.main(["--split", "arm"]) == 0
        out = capsys.readouterr().out
        assert "rules frozen" in out and "fingerprint" in out

    def test_split_of_an_unknown_arm_exits_nonzero(self, capsys):
        assert oos.main(["--split", "ghost"]) == 1

    def test_freeze_at_backdates_from_the_cli(self, capsys):
        oos.record_params("arm", {"x": 1})
        rc = oos.main(["--freeze-at", "arm", "2026-07-19", "commit e0ff27b"])
        assert rc == 0
        assert oos.get_rules("arm").frozen_at.startswith("2026-07-19")
        assert "commit e0ff27b" in capsys.readouterr().out

    def test_freeze_at_on_an_unknown_arm_exits_nonzero(self, capsys):
        assert oos.main(["--freeze-at", "ghost", "2026-07-19", "reason"]) == 1

    def test_freeze_at_accepts_a_naive_date_as_utc(self):
        oos.record_params("arm", {"x": 1})
        oos.main(["--freeze-at", "arm", "2026-07-19T12:00:00", "reason"])
        assert oos.get_rules("arm").frozen_dt.tzinfo is not None


class TestDaysOfEvidence:
    def test_never_negative_for_a_future_boundary(self):
        """A clock-skewed or mistyped future date must read as zero, not as
        negative evidence."""
        oos.record_params("arm", {"x": 1})
        oos.freeze_at("arm", datetime.now(UTC) + timedelta(days=5))
        assert oos.get_rules("arm").days_of_evidence() == 0.0
