"""Offline tests for monologue_recorder. ffmpeg/whisper/tuner all mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pm.mention import monologue_recorder as mr


class TestShowTable:
    def test_shows_use_known_channels_and_safe_tuners(self):
        for s in mr.SHOWS.values():
            assert s.channel in ("NBC", "ABC", "CBS", "FOX")
            assert s.tuner in (2, 3)          # never Plex's 0/1
            assert s.minutes >= 30

    def test_no_cbs_late_show(self):
        """The Late Show ended May 2026 — a CBS 22:30 slot would record news."""
        assert not any(s.channel == "CBS" for s in mr.SHOWS.values())

    def test_run_dir_dated(self):
        d = mr.run_dir("tonight", when=datetime(2026, 8, 10, 4, tzinfo=UTC))
        assert d.name == "2026-08-10_tonight"


class TestCapture:
    @pytest.fixture
    def quiet_tuner(self, monkeypatch):
        monkeypatch.setattr(mr.hdhomerun, "channel_present", lambda ch: True)
        monkeypatch.setattr(mr.hdhomerun, "acquire_lock", lambda t: None)
        monkeypatch.setattr(mr.hdhomerun, "stream_url",
                            lambda ch, tuner: f"http://x/tuner{tuner}/v{ch}")

    def test_capture_writes_meta(self, tmp_path, monkeypatch, quiet_tuner):
        def fake_run(cmd, **k):
            Path(cmd[-1]).write_bytes(b"\0" * 2_000_000)   # plausible wav
            class R:
                returncode = 0
                stderr = ""
            return R()
        monkeypatch.setattr(mr.subprocess, "run", fake_run)
        out = mr.capture(mr.SHOWS["tonight"], minutes=1, out=tmp_path / "r")
        assert out is not None
        meta = json.loads((out / "meta.json").read_text())
        assert meta["show"] == "tonight" and meta["tuner"] == 3
        assert meta["started"] > 0

    def test_tiny_wav_is_a_loud_failure(self, tmp_path, monkeypatch,
                                        quiet_tuner, caplog):
        def fake_run(cmd, **k):
            Path(cmd[-1]).write_bytes(b"\0" * 100)         # ~nothing captured
            class R:
                returncode = 0
                stderr = ""
            return R()
        monkeypatch.setattr(mr.subprocess, "run", fake_run)
        import logging
        with caplog.at_level(logging.ERROR):
            out = mr.capture(mr.SHOWS["kimmel"], minutes=1, out=tmp_path / "r")
        assert out is None
        assert any("no usable audio" in m for m in caplog.messages)

    def test_lock_failure_does_not_block_capture(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr.hdhomerun, "channel_present", lambda ch: True)
        monkeypatch.setattr(mr.hdhomerun, "acquire_lock",
                            lambda t: (_ for _ in ()).throw(RuntimeError))
        monkeypatch.setattr(mr.hdhomerun, "stream_url",
                            lambda ch, tuner: "http://x")
        def fake_run(cmd, **k):
            Path(cmd[-1]).write_bytes(b"\0" * 2_000_000)
            class R:
                returncode = 0
                stderr = ""
            return R()
        monkeypatch.setattr(mr.subprocess, "run", fake_run)
        # acquire_lock raising violates its own contract, but a broadcast is
        # unrepeatable — capture must survive it and record anyway
        out = mr.capture(mr.SHOWS["tonight"], minutes=1, out=tmp_path / "r")
        assert out is not None
        assert (out / "meta.json").exists()


class TestTranscribe:
    def test_transcript_rows_carry_wall_ts(self, tmp_path, monkeypatch):
        run = tmp_path / "r"
        run.mkdir()
        (run / "meta.json").write_text(json.dumps(
            {"started": 1_000_000.0, "whisper_model": "small"}))
        (run / "audio.wav").write_bytes(b"\0" * 2_000_000)

        class Seg:
            text = "welcome to the show"
            start_time, end_time = 12.0, 15.0
            chunk_index, confidence, is_speech = 0, -0.2, True
            words = [{"word": "welcome", "start": 0.1, "end": 0.4}]

        class FakeTr:
            def __init__(self, **k): pass
            def transcribe_file(self, p): return iter([Seg()])

        from pm.mention import audio_transcribe
        monkeypatch.setattr(audio_transcribe, "AudioTranscriber", FakeTr)
        assert mr.transcribe(run) == 1
        row = json.loads((run / "transcript.jsonl").read_text())
        assert row["wall_ts"] == 1_000_012.0
        assert row["words"][0]["wall_ts"] == pytest.approx(1_000_012.1)


def test_main_exits_nonzero_when_capture_fails(monkeypatch):
    # A dead tuner must fail the unit so OnFailure pages; exiting 0 hid
    # 2026-08-16..10-13 (79 empty runs, every unit "Finished").
    monkeypatch.setattr(mr, "record_and_transcribe", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["monologue_recorder", "--show", "tonight"])
    assert mr.main() == 1


def test_device_ip_is_resolved_per_call(monkeypatch):
    from pm.mention import hdhomerun
    monkeypatch.setenv("HDHR_IP", "10.9.8.7")
    assert hdhomerun.stream_url("11.1").startswith("http://10.9.8.7:5004/")
    monkeypatch.delenv("HDHR_IP")
    assert hdhomerun.device_ip() == hdhomerun._FALLBACK_IP
