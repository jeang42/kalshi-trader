"""Unit tests for feed_latency.py — pure logic only (no ffmpeg/yt-dlp/network)."""
from __future__ import annotations

import subprocess

import numpy as np
import pytest

from pm.mention import feed_latency as fl

SAMPLE_RATE = 16000


# ─── estimate_lag ───────────────────────────────────────────────────────────


def _synthetic_envelope_signal(n_frames: int, *, seed: int = 0) -> np.ndarray:
    """A random-ish, smoothed nonnegative 'envelope' signal at ENVELOPE_RATE_HZ."""
    rng = np.random.default_rng(seed)
    raw = rng.random(n_frames)
    # smooth so it isn't pure white noise (more realistic envelope shape)
    kernel = np.ones(5) / 5
    return np.convolve(raw, kernel, mode="same")


def _envelope_to_pcm(env: np.ndarray, hop: int) -> np.ndarray:
    """Upsample an envelope back to a fake PCM-rate signal (repeat each frame)."""
    return np.repeat(env, hop).astype(np.float32)


def test_estimate_lag_recovers_known_positive_shift():
    hop = int(round(SAMPLE_RATE / fl.ENVELOPE_RATE_HZ))
    env = _synthetic_envelope_signal(2000, seed=1) * 0.5
    shift_frames = 37  # known integer envelope-frame shift
    rng = np.random.default_rng(2)

    sig_a = _envelope_to_pcm(env, hop)
    # B lags A by shift_frames: B's content is a delayed copy of A's.
    shifted_env = np.concatenate([np.zeros(shift_frames), env])[: len(env)]
    sig_b = _envelope_to_pcm(shifted_env, hop)

    # mild noise on both
    sig_a = sig_a + rng.normal(0, 0.01, size=sig_a.shape).astype(np.float32)
    sig_b = sig_b + rng.normal(0, 0.01, size=sig_b.shape).astype(np.float32)

    lag, confidence = fl.estimate_lag(sig_a, sig_b, SAMPLE_RATE)

    expected_lag_seconds = shift_frames / fl.ENVELOPE_RATE_HZ
    assert lag == pytest.approx(expected_lag_seconds, abs=0.15)
    assert confidence > 0.4


def test_estimate_lag_sign_convention_a_leads_b():
    # A leads B (B is a delayed copy of A) -> positive lag.
    hop = int(round(SAMPLE_RATE / fl.ENVELOPE_RATE_HZ))
    env = _synthetic_envelope_signal(1500, seed=3) * 0.5
    sig_a = _envelope_to_pcm(env, hop)
    shifted = np.concatenate([np.zeros(20), env])[: len(env)]
    sig_b = _envelope_to_pcm(shifted, hop)

    lag, _ = fl.estimate_lag(sig_a, sig_b, SAMPLE_RATE)
    assert lag > 0

    # Swap roles: now B leads A -> negative lag from estimate_lag(A=old B, B=old A)
    lag2, _ = fl.estimate_lag(sig_b, sig_a, SAMPLE_RATE)
    assert lag2 < 0
    assert lag2 == pytest.approx(-lag, abs=0.05)


def test_estimate_lag_low_confidence_on_unrelated_signals():
    rng = np.random.default_rng(42)
    sig_a = rng.normal(0, 0.2, size=SAMPLE_RATE * 10).astype(np.float32)
    sig_b = rng.normal(0, 0.2, size=SAMPLE_RATE * 10).astype(np.float32)

    _, confidence = fl.estimate_lag(sig_a, sig_b, SAMPLE_RATE)
    assert confidence < fl.LOW_CONFIDENCE_THRESHOLD


def test_estimate_lag_handles_degenerate_short_signal():
    sig_a = np.zeros(1, dtype=np.float32)
    sig_b = np.zeros(1, dtype=np.float32)
    lag, confidence = fl.estimate_lag(sig_a, sig_b, SAMPLE_RATE)
    assert lag == 0.0
    assert confidence == 0.0


# ─── resolve_source_url (spec parser) ──────────────────────────────────────


def test_resolve_source_url_hdhr_with_tuner():
    url = fl.resolve_source_url("hdhr:11.1:3", hdhr_ip="192.0.2.51")
    assert url == "http://192.0.2.51:5004/tuner3/v11.1"


def test_resolve_source_url_hdhr_default_tuner():
    url = fl.resolve_source_url("hdhr:5.1", hdhr_ip="10.0.0.9")
    assert url == f"http://10.0.0.9:5004/tuner{fl.DEFAULT_HDHR_TUNER}/v5.1"


def test_resolve_source_url_raw_passthrough():
    raw = "https://example.com/live/stream.m3u8"
    assert fl.resolve_source_url(raw, hdhr_ip="ignored") == raw


def test_resolve_source_url_yt_calls_yt_dlp(monkeypatch):
    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="https://resolved.example/stream\n", stderr="")

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    url = fl.resolve_source_url("yt:https://www.youtube.com/watch?v=abc123", hdhr_ip="ignored")
    assert url == "https://resolved.example/stream"
    assert captured_cmd["cmd"][0] == "yt-dlp"
    assert "https://www.youtube.com/watch?v=abc123" in captured_cmd["cmd"]


def test_resolve_source_url_yt_takes_last_line_when_multiple(monkeypatch):
    def fake_run(cmd, **kwargs):
        stdout = "https://video-only.example/v\nhttps://audio-only.example/a\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    url = fl.resolve_source_url("yt:https://www.youtube.com/watch?v=abc123", hdhr_ip="ignored")
    assert url == "https://audio-only.example/a"


def test_resolve_source_url_yt_raises_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: Unsupported URL")

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="yt-dlp could not resolve"):
        fl.resolve_source_url("yt:https://bad.example/notavideo", hdhr_ip="ignored")


def test_resolve_source_url_yt_raises_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="yt-dlp failed to resolve"):
        fl.resolve_source_url("yt:https://example.com/live", hdhr_ip="ignored")


# ─── capture_source / capture_all (mocked subprocess, no real ffmpeg) ─────


def test_capture_source_success(monkeypatch, tmp_path):
    out_path = tmp_path / "LABEL.pcm"

    def fake_run(cmd, **kwargs):
        # Simulate ffmpeg writing output before returning.
        out_path.write_bytes(b"\x00\x01" * 100)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    result = fl.capture_source("LABEL", "https://example.com/stream", seconds=1.0, workdir=tmp_path)
    assert result.error is None
    assert result.path == out_path
    assert result.path.exists()


def test_capture_source_ffmpeg_failure(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"connection refused")

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    result = fl.capture_source("LABEL", "https://example.com/stream", seconds=1.0, workdir=tmp_path)
    assert result.error is not None
    assert "ffmpeg failed" in result.error


def test_capture_source_timeout(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(fl.subprocess, "run", fake_run)

    result = fl.capture_source("LABEL", "https://example.com/stream", seconds=1.0, workdir=tmp_path)
    assert result.error is not None
    assert "timed out" in result.error


def test_capture_all_isolates_per_source_failure(monkeypatch, tmp_path):
    def fake_resolve(spec, *, hdhr_ip, yt_dlp_bin="yt-dlp"):
        if spec.startswith("bad:"):
            raise RuntimeError("cannot resolve")
        return spec

    def fake_capture(label, url, *, seconds, workdir, timeout_slack=30.0):
        path = workdir / f"{label}.pcm"
        path.write_bytes(b"\x00\x01" * 50)
        return fl.CaptureResult(label, path, 0.0)

    monkeypatch.setattr(fl, "resolve_source_url", fake_resolve)
    monkeypatch.setattr(fl, "capture_source", fake_capture)

    sources = {"GOOD": "https://good.example/stream", "BAD": "bad:spec"}
    results = fl.capture_all(sources, seconds=1.0, workdir=tmp_path, hdhr_ip="ignored")

    assert results["GOOD"].error is None
    assert results["BAD"].error is not None


# ─── compare_captures / fastest_feed / format_report ───────────────────────


def _write_pcm(path, samples: np.ndarray) -> None:
    (samples.astype(np.int16)).tofile(path)


def test_compare_captures_and_fastest_feed(tmp_path):
    hop = int(round(SAMPLE_RATE / fl.ENVELOPE_RATE_HZ))
    env = _synthetic_envelope_signal(1000, seed=7) * 5000
    pcm_a = _envelope_to_pcm(env, hop)

    # B lags A by 30 envelope frames (0.3s) in signal domain, no wallclock offset.
    shifted = np.concatenate([np.zeros(30), env])[: len(env)]
    pcm_b = _envelope_to_pcm(shifted, hop)

    path_a = tmp_path / "A.pcm"
    path_b = tmp_path / "B.pcm"
    _write_pcm(path_a, pcm_a)
    _write_pcm(path_b, pcm_b)

    captures = {
        "A": fl.CaptureResult("A", path_a, wall_start=100.0),
        "B": fl.CaptureResult("B", path_b, wall_start=100.0),
    }

    pairs = fl.compare_captures(captures)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.label_a == "A" and p.label_b == "B"
    assert p.total_lag_seconds > 0  # A leads B
    assert not p.low_confidence

    assert fl.fastest_feed(pairs) == "A"

    report = fl.format_report(captures, pairs)
    assert "A leads B" in report
    assert "FASTEST FEED: A" in report


def test_compare_captures_skips_failed_sources(tmp_path):
    path_a = tmp_path / "A.pcm"
    _write_pcm(path_a, np.zeros(1000))

    captures = {
        "A": fl.CaptureResult("A", path_a, wall_start=0.0),
        "B": fl.CaptureResult("B", None, wall_start=0.0, error="ffmpeg failed"),
    }
    pairs = fl.compare_captures(captures)
    assert pairs == []

    report = fl.format_report(captures, pairs)
    assert "CAPTURE FAILURES" in report
    assert "B: ffmpeg failed" in report


def test_format_report_flags_low_confidence():
    captures = {
        "A": fl.CaptureResult("A", None, wall_start=0.0),
        "B": fl.CaptureResult("B", None, wall_start=0.0),
    }
    pairs = [fl.PairResult("A", "B", signal_lag_seconds=1.0, total_lag_seconds=1.0, confidence=0.1)]
    report = fl.format_report(captures, pairs)
    assert "LOW CONFIDENCE" in report
    assert "FASTEST FEED: undetermined" in report


def test_fastest_feed_none_when_all_low_confidence():
    pairs = [fl.PairResult("A", "B", signal_lag_seconds=1.0, total_lag_seconds=1.0, confidence=0.1)]
    assert fl.fastest_feed(pairs) is None


# ─── source arg parsing ─────────────────────────────────────────────────


def test_parse_source_arg_valid():
    assert fl._parse_source_arg("YOUTUBE=yt:https://x/y") == ("YOUTUBE", "yt:https://x/y")


def test_parse_source_arg_rejects_missing_equals():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        fl._parse_source_arg("no-equals-here")
