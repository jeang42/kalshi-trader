"""Direct tests for position_meta's lifecycle API.

Until 2026-08-17 these paths (``extra`` fields on entry, ``set_field``,
``clear_entry``, ``all_entries``) were exercised only incidentally by the
longshot-scalper daemon's test suite. That daemon was retired to ``attic/``,
and coverage of a live money-path module must not depend on dead code.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pm.ledger import position_meta as pm


@pytest.fixture
def meta_file(tmp_path, monkeypatch):
    p = tmp_path / "position_meta.json"
    monkeypatch.setenv("POLYMARKET_POSITION_META_FILE", str(p))
    return p


class TestRecordEntryExtra:
    def test_extra_fields_are_stored(self, meta_file):
        e = pm.record_entry("T1", "Yes", entry_price=0.30,
                            extra={"from_flip": True, "note": "x"})
        assert e["from_flip"] is True and e["note"] == "x"
        assert pm.get_entry("T1", "Yes")["from_flip"] is True

    def test_extra_cannot_clobber_reserved_lifecycle_keys(self, meta_file):
        opened = datetime(2026, 1, 1, tzinfo=UTC)
        e = pm.record_entry("T1", "Yes", entry_price=0.30, opened_at=opened,
                            extra={"entry_price": 0.99, "peak_price": 0.99,
                                   "opened_at": "bogus", "peak_at": "bogus"})
        assert e["entry_price"] == 0.30
        assert e["peak_price"] == 0.30
        assert e["opened_at"] == opened.isoformat()
        assert e["peak_at"] == opened.isoformat()

    def test_record_entry_is_idempotent_on_key(self, meta_file):
        pm.record_entry("T1", "Yes", entry_price=0.30)
        again = pm.record_entry("T1", "Yes", entry_price=0.50)
        assert again["entry_price"] == 0.30  # original entry preserved


class TestSetField:
    def test_sets_and_updates_a_free_field(self, meta_file):
        pm.record_entry("T1", "Yes", entry_price=0.30)
        e = pm.set_field("T1", "Yes", "tp_order_id", "abc")
        assert e["tp_order_id"] == "abc"
        e = pm.set_field("T1", "Yes", "tp_order_id", "def")
        assert pm.get_entry("T1", "Yes")["tp_order_id"] == "def"

    def test_returns_none_for_unknown_position(self, meta_file):
        assert pm.set_field("NOPE", "Yes", "k", 1) is None

    @pytest.mark.parametrize("key", ["entry_price", "opened_at",
                                     "peak_price", "peak_at"])
    def test_reserved_keys_raise(self, meta_file, key):
        pm.record_entry("T1", "Yes", entry_price=0.30)
        with pytest.raises(ValueError):
            pm.set_field("T1", "Yes", key, 0.99)


class TestClearAndAll:
    def test_clear_entry_removes_and_reports(self, meta_file):
        pm.record_entry("T1", "Yes", entry_price=0.30)
        pm.record_entry("T2", "No", entry_price=0.60)
        assert pm.clear_entry("T1", "Yes") is True
        assert pm.get_entry("T1", "Yes") is None
        assert pm.get_entry("T2", "No") is not None
        assert pm.clear_entry("T1", "Yes") is False  # already gone

    def test_all_entries_is_a_detached_copy(self, meta_file):
        pm.record_entry("T1", "Yes", entry_price=0.30)
        pm.record_entry("T2", "No", entry_price=0.60)
        allm = pm.all_entries()
        assert set(allm) == {pm._key("T1", "Yes"), pm._key("T2", "No")}
        allm.clear()  # mutating the returned dict must not write back
        assert len(pm.all_entries()) == 2

    def test_all_entries_empty_store(self, meta_file):
        assert pm.all_entries() == {}


class TestPeak:
    def test_peak_ratchets_up_only(self, meta_file):
        pm.record_entry("T1", "Yes", entry_price=0.30)
        assert pm.update_peak("T1", "Yes", current_price=0.45)["peak_price"] == 0.45
        assert pm.update_peak("T1", "Yes", current_price=0.35)["peak_price"] == 0.45
        assert pm.update_peak("NOPE", "Yes", current_price=0.9) is None
