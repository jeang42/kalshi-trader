"""Tests for mention_rehearsal.py — cascade detection, fill sim, JSONL tail."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from pm.mention import mention_rehearsal as mr

# --------------------------------------------------------------------------
# cascade_start
# --------------------------------------------------------------------------


class TestCascadeStart:
    def test_empty_tape(self):
        assert mr.cascade_start([]) is None

    def test_clean_sweep(self):
        tape = [
            (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
            (20, 0.35, 50), (25, 0.72, 80), (30, 0.97, 200),
        ]
        assert mr.cascade_start(tape) == 20

    def test_pre_utterance_drift_then_sweep(self):
        # Slow decay/drift for a while (no jump), then the real sweep.
        tape = [
            (0, 0.60, 5), (600, 0.45, 5), (1200, 0.32, 5), (2400, 0.18, 5),
            (2410, 0.20, 10),  # small move, not a >=0.05 jump vs recent median
            (2420, 0.60, 100),  # the real jump
            (2425, 0.90, 150),
            (2440, 0.99, 300),
        ]
        start = mr.cascade_start(tape)
        assert start == 2420

    def test_already_high_no_sweep(self):
        tape = [(0, 0.96, 10), (10, 0.97, 10), (20, 0.98, 10)]
        assert mr.cascade_start(tape) is None

    def test_jump_but_never_completes_sweep(self):
        # Jumps up but never reaches sweep_price within the window -> None.
        tape = [(0, 0.10, 10), (5, 0.10, 10), (10, 0.40, 50), (20, 0.45, 10)]
        assert mr.cascade_start(tape) is None

    def test_unordered_input_still_works(self):
        tape = [
            (30, 0.97, 200), (0, 0.10, 10), (25, 0.72, 80),
            (10, 0.09, 10), (5, 0.11, 10), (20, 0.35, 50),
        ]
        assert mr.cascade_start(tape) == 20

    def test_single_trade_no_baseline(self):
        # One trade, however low, can never be a cascade start (no prior).
        tape = [(0, 0.10, 10)]
        assert mr.cascade_start(tape) is None


# --------------------------------------------------------------------------
# simulated_fill
# --------------------------------------------------------------------------


class TestSimulatedFill:
    def test_vwap_primary_window(self):
        tape = [
            (100, 0.30, 10), (100.5, 0.50, 20), (101.5, 0.70, 5),
            (105, 0.99, 50),
        ]
        fill = mr.simulated_fill(tape, t_land=100.0)
        assert fill == pytest.approx(0.4714, abs=1e-3)

    def test_no_fill_anywhere(self):
        tape = [(100, 0.30, 10)]
        assert mr.simulated_fill(tape, t_land=1000.0) is None

    def test_fallback_first_print_after_landing(self):
        tape = [(90, 0.20, 10), (112, 0.90, 5)]
        fill = mr.simulated_fill(tape, t_land=100.0)
        assert fill == 0.9

    def test_fallback_beyond_window_is_none(self):
        tape = [(90, 0.20, 10), (140, 0.90, 5)]
        assert mr.simulated_fill(tape, t_land=100.0, fallback_window_s=30.0) is None

    def test_empty_tape(self):
        assert mr.simulated_fill([], t_land=100.0) is None


# --------------------------------------------------------------------------
# would_be_pnl
# --------------------------------------------------------------------------


class TestWouldBePnl:
    def test_resolved_yes(self):
        pnl = mr.would_be_pnl(0.60, resolved_yes=True, contracts=100)
        assert pnl == pytest.approx(38.32, abs=0.01)

    def test_resolved_no(self):
        pnl = mr.would_be_pnl(0.60, resolved_yes=False, contracts=100)
        assert pnl == pytest.approx(-61.68, abs=0.01)

    def test_pending(self):
        assert mr.would_be_pnl(0.5, resolved_yes=None) is None


# --------------------------------------------------------------------------
# JsonlTailer
# --------------------------------------------------------------------------


class TestJsonlTailer:
    def test_processes_only_new_rows_in_order(self, tmp_path):
        path = tmp_path / "detections.jsonl"
        tailer = mr.JsonlTailer(path)

        # Nothing written yet.
        assert tailer.poll() == []

        with path.open("a") as fh:
            fh.write(json.dumps({"ticker": "A"}) + "\n")
            fh.write(json.dumps({"ticker": "B"}) + "\n")

        rows = tailer.poll()
        assert [r["ticker"] for r in rows] == ["A", "B"]

        # No new rows -> no reprocessing.
        assert tailer.poll() == []

        with path.open("a") as fh:
            fh.write(json.dumps({"ticker": "C"}) + "\n")

        rows2 = tailer.poll()
        assert [r["ticker"] for r in rows2] == ["C"]

    def test_missing_file_returns_empty(self, tmp_path):
        tailer = mr.JsonlTailer(tmp_path / "nope.jsonl")
        assert tailer.poll() == []

    def test_truncated_file_restarts(self, tmp_path):
        path = tmp_path / "detections.jsonl"
        tailer = mr.JsonlTailer(path)
        with path.open("a") as fh:
            fh.write(json.dumps({"ticker": "KXVERYLONGTICKERNAME"}) + "\n")
            fh.write(json.dumps({"ticker": "KXANOTHERLONGONE"}) + "\n")
        assert [r["ticker"] for r in tailer.poll()] == [
            "KXVERYLONGTICKERNAME", "KXANOTHERLONGONE",
        ]

        # Truncate to a shorter file + rewrite (logrotate copytruncate-style).
        with path.open("w") as fh:
            fh.write(json.dumps({"ticker": "Z"}) + "\n")
        assert [r["ticker"] for r in tailer.poll()] == ["Z"]

    def test_rotated_same_size_detected_via_inode(self, tmp_path):
        path = tmp_path / "detections.jsonl"
        tailer = mr.JsonlTailer(path)
        with path.open("a") as fh:
            fh.write(json.dumps({"ticker": "A"}) + "\n")
        assert [r["ticker"] for r in tailer.poll()] == ["A"]

        # Replace with a brand-new file of coincidentally the same size
        # (e.g. atomic rename-based rotation) -> different inode.
        tmp = tmp_path / "detections.jsonl.new"
        with tmp.open("w") as fh:
            fh.write(json.dumps({"ticker": "Z"}) + "\n")
        tmp.replace(path)
        assert [r["ticker"] for r in tailer.poll()] == ["Z"]

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "detections.jsonl"
        with path.open("w") as fh:
            fh.write(json.dumps({"ticker": "A"}) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps({"ticker": "B"}) + "\n")
        tailer = mr.JsonlTailer(path)
        rows = tailer.poll()
        assert [r["ticker"] for r in rows] == ["A", "B"]


# --------------------------------------------------------------------------
# rehearse_detection — network mocked
# --------------------------------------------------------------------------


class TestRehearseDetection:
    def test_builds_intended_order_from_quote(self):
        detection = {
            "ts_detected": 1000.0,
            "ticker": "KXFTNMENTION-26JUL19-WORD",
            "event_ticker": "KXFTNMENTION-26JUL19",
            "matched_text": "word",
        }

        def fake_get_market(ticker):
            assert ticker == detection["ticker"]
            return {"yes_bid_dollars": "0.20", "yes_ask_dollars": "0.25"}

        record = mr.rehearse_detection(
            detection, stake_dollars=50.0,
            get_market_fn=fake_get_market,
            now_fn=lambda: 1000.5,
        )
        assert record["yes_bid"] == 0.20
        assert record["yes_ask"] == 0.25
        assert record["intended"]["price"] == pytest.approx(0.27)
        assert record["intended"]["count"] == int(50.0 // 0.27)
        assert record["quote_latency_s"] == pytest.approx(0.5)
        assert record["ticker"] == detection["ticker"]
        assert record["event_ticker"] == detection["event_ticker"]

    def test_caps_price_at_99_cents(self):
        detection = {"ts_detected": 0.0, "ticker": "T", "event_ticker": "E"}

        def fake_get_market(ticker):
            return {"yes_bid_dollars": "0.99", "yes_ask_dollars": "0.98"}

        record = mr.rehearse_detection(
            detection, get_market_fn=fake_get_market, now_fn=lambda: 0.1,
        )
        assert record["intended"]["price"] == 0.99


# --------------------------------------------------------------------------
# score_event — network mocked
# --------------------------------------------------------------------------


class TestScoreEvent:
    def test_scores_a_single_ticker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "DETECTIONS_PATH", tmp_path / "detections.jsonl")
        monkeypatch.setattr(mr, "REHEARSAL_PATH", tmp_path / "rehearsal.jsonl")

        detection = {
            "ts_detected": 1000.0,
            "ticker": "KXEVT-26JUL19-WORD",
            "event_ticker": "KXEVT-26JUL19",
            "matched_text": "word",
        }
        mr._append_jsonl(mr.DETECTIONS_PATH, detection)
        mr._append_jsonl(mr.REHEARSAL_PATH, {**detection, "stake_dollars": 50.0})

        tape_rows = [
            {"ts_unix": 990, "yes_price": 0.10, "count": 10},
            {"ts_unix": 995, "yes_price": 0.11, "count": 10},
            {"ts_unix": 1002, "yes_price": 0.40, "count": 50},
            {"ts_unix": 1005, "yes_price": 0.99, "count": 200},
        ]
        fake_df = pd.DataFrame(tape_rows)

        def fake_get_all_trades(ticker, *, since_ts=None, until_ts=None):
            return fake_df

        def fake_get_market(ticker):
            return {"result": "yes"}

        scores = mr.score_event(
            "KXEVT-26JUL19",
            get_all_trades_fn=fake_get_all_trades,
            get_market_fn=fake_get_market,
        )
        assert len(scores) == 1
        s = scores[0]
        assert s.resolved_yes is True
        assert s.cascade_start_ts == 1002
        assert s.our_lag_s == pytest.approx(1000.0 - 1002)
        assert s.fill is not None
        assert s.pnl_per_contract is not None

    def test_missing_quote_row_still_scored_from_detection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "DETECTIONS_PATH", tmp_path / "detections.jsonl")
        monkeypatch.setattr(mr, "REHEARSAL_PATH", tmp_path / "rehearsal.jsonl")

        detection = {
            "ts_detected": None,
            "ticker": "KXEVT-26JUL19-NOQ",
            "event_ticker": "KXEVT-26JUL19",
            "matched_text": "noquote",
        }
        mr._append_jsonl(mr.DETECTIONS_PATH, detection)

        scores = mr.score_event(
            "KXEVT-26JUL19",
            get_all_trades_fn=lambda *a, **k: pd.DataFrame(),
            get_market_fn=lambda t: {},
        )
        assert len(scores) == 1
        assert scores[0].ts_detected is None
        assert scores[0].cascade_start_ts is None
        assert scores[0].pnl_at_stake is None
