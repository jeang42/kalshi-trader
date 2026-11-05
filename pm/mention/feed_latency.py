"""Relative latency measurement between two or more live audio feeds.

Why this exists
----------------
The live-mention-cascade thesis (``docs/edge-research-log.md`` 2026-07-19,
``mention_listen.py``) depends on our whisper stack hearing words as close to
"true live" as possible — the repricing window is only 7-60s wide. Whether we
land inside that window is decided almost entirely by which INPUT FEED we
point the transcriber at, not by whisper's decode speed. A web/HLS stream can
trail an OTA broadcast by anywhere from ~2s to 30+s depending on the CDN's
segment buffering; two "live" web feeds of the SAME event (e.g. a network's
own YouTube simulcast vs its website HLS player) routinely disagree by
several seconds too. This module answers, empirically: of the feeds we can
reach, which is fastest, and by how much?

It is READ-ONLY. It captures short audio clips via ffmpeg and yt-dlp, cross-
correlates them, and prints/writes a report. It never touches the market
(imports no ``kalshi_orders``/``execution``/``kalshi_auth``) and places no
orders.

Method
------
1. **Capture**: launch one ffmpeg process per source concurrently, each
   writing 16kHz mono s16le PCM to a temp file, for ``--seconds`` (default
   90). All sources are started back-to-back in a tight loop (no way to
   truly launch N subprocess.Popen calls at the exact same instant in
   Python, but the loop overhead is ~milliseconds, negligible next to the
   multi-second lags this tool measures). Each source's approximate
   "first-sample" wall-clock time is recorded as ``time.time()`` sampled
   immediately before that source's ``Popen`` call. **Caveat**: ffmpeg
   itself has connection/startup latency (DNS + TCP/TLS handshake + HLS
   manifest fetch) that is NOT part of the feed's true content latency —
   it is a few hundred ms to a couple seconds and, worse, may differ
   *between* sources (e.g. an HLS manifest fetch is slower than a plain
   HTTP stream open). This is not corrected for; the wall-clock start
   offset is a coarse alignment term, and the report leans on the
   audio-domain cross-correlation lag (which is immune to this) as the
   primary signal. For high-precision work, capture longer clips (more
   redundant signal for the correlation) rather than trusting the
   wall-clock offset alone.

2. **Lag estimation** (pure, unit-tested): ``estimate_lag`` computes an
   envelope (rectify + downsample to ~100Hz) of each signal so the
   comparison is robust to different codecs/bitrates/loudness between
   feeds, cross-correlates the two envelopes via
   ``scipy.signal.correlate(..., mode="full")``, and takes the argmax lag.
   Confidence is the normalized peak height (peak / sqrt(energy_a *
   energy_b)), a value in [0, 1] roughly analogous to correlation
   coefficient at the best lag.

3. **Total lag** = signal-domain lag (from step 2, converted to seconds)
   + (wall-clock start of A - wall-clock start of B). If A's capture
   started 1.0s after B's, and A's content lags B's by 4.0s in the audio
   domain, A's true real-time lag behind B is 4.0 + 1.0 = 5.0s (A started
   later AND still needed extra signal-domain shift to line up).

Usage — compare C-SPAN's YouTube live simulcast against C-SPAN.org's own
HLS stream this week (pick whichever is currently live on both; the
`yt:` source resolves via yt-dlp, the `spanhls:` source is a raw HLS URL
you grab from c-span.org's page source or dev-tools Network tab)::

    python feed_latency.py \\
        --source YOUTUBE=yt:https://www.youtube.com/watch?v=<CSPAN_LIVE_ID> \\
        --source CSPANORG=https://<cspan-hls-manifest>.m3u8 \\
        --seconds 90

Later — OTA tuner (HDHomeRun) as the low-latency reference vs a web feed::

    # python feed_latency.py \\
    #     --source OTA=hdhr:11.1:3 \\
    #     --source WEB=yt:https://www.youtube.com/watch?v=<LIVE_ID> \\
    #     --seconds 90 --hdhr-ip 192.168.0.50

Report is printed and also written to
``data/shared/feed_latency_report.txt``.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from pm.core import logctx, provenance
from pm.mention import hdhomerun

log = logging.getLogger("feed_latency")

SAMPLE_RATE = 16000
ENVELOPE_RATE_HZ = 100.0  # downsample target for the correlation envelope
LOW_CONFIDENCE_THRESHOLD = 0.4
DEFAULT_HDHR_TUNER = 3

REPORT_PATH = Path("data/shared/feed_latency_report.txt")


# ---------------------------------------------------------------------------
# Source-spec parsing
# ---------------------------------------------------------------------------


def resolve_source_url(spec: str, *, hdhr_ip: str, yt_dlp_bin: str = "yt-dlp") -> str:
    """Turn a ``--source`` SPEC into an ffmpeg-ingestable URL.

    Forms:

    - Raw URL/path (``http://...``, ``https://...``, a local file path, or
      any other ffmpeg-understood string): passed through unchanged.
    - ``hdhr:<channel>[:<tuner>]``: builds an HDHomeRun tuner URL
      ``http://<hdhr_ip>:5004/tuner<tuner>/v<channel>``. Tuner defaults to
      :data:`DEFAULT_HDHR_TUNER` (3) when omitted.
    - ``yt:<url>``: resolves to a direct media URL via ``yt-dlp -g``.

    >>> resolve_source_url("hdhr:11.1:3", hdhr_ip="192.168.0.51")
    'http://192.168.0.51:5004/tuner3/v11.1'
    >>> resolve_source_url("hdhr:11.1", hdhr_ip="192.168.0.51")
    'http://192.168.0.51:5004/tuner3/v11.1'
    >>> resolve_source_url("https://example.com/stream.m3u8", hdhr_ip="x")
    'https://example.com/stream.m3u8'
    """
    if spec.startswith("hdhr:"):
        rest = spec[len("hdhr:"):]
        parts = rest.split(":")
        channel = parts[0]
        tuner = parts[1] if len(parts) > 1 and parts[1] else str(DEFAULT_HDHR_TUNER)
        return f"http://{hdhr_ip}:5004/tuner{tuner}/v{channel}"

    if spec.startswith("yt:"):
        page_url = spec[len("yt:"):]
        return _resolve_yt_dlp_url(page_url, yt_dlp_bin=yt_dlp_bin)

    # Raw ffmpeg-ingestable URL or local path — pass through.
    return spec


def _resolve_yt_dlp_url(page_url: str, *, yt_dlp_bin: str = "yt-dlp") -> str:
    """Resolve a YouTube/webpage URL to a direct stream URL via ``yt-dlp -g``.

    ``yt-dlp -g`` can print more than one URL (e.g. separate video/audio
    streams for a DASH-muxed source). We only need audio, so we take the
    LAST line printed (yt-dlp orders video-then-audio for a two-line
    result) — falling back to the first (and only) line otherwise.
    """
    try:
        result = subprocess.run(
            [yt_dlp_bin, "-g", "-f", "bestaudio/best", page_url],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"yt-dlp failed to resolve {page_url!r}: {exc}") from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"yt-dlp could not resolve {page_url!r}: {result.stderr.strip()[:500]}"
        )

    lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"yt-dlp returned no URLs for {page_url!r}")
    return lines[-1]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


@dataclass
class CaptureResult:
    label: str
    path: Path | None            # PCM file on disk; None on failure
    wall_start: float            # time.time() sampled just before Popen
    error: str | None = None     # non-None means capture failed for this source


def _build_ffmpeg_capture_cmd(url: str, out_path: Path, seconds: float) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if url.startswith(("http://", "https://")):
        cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
    cmd.extend(["-i", url, "-t", str(seconds)])
    cmd.extend(["-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le"])
    cmd.append(str(out_path))
    return cmd


def capture_source(
    label: str, url: str, *, seconds: float, workdir: Path, timeout_slack: float = 30.0,
) -> CaptureResult:
    """Capture one source to a 16kHz mono s16le PCM file, blocking until done.

    Records ``wall_start`` as close to ffmpeg's first-sample as feasible: a
    ``time.time()`` snapshot taken immediately before ``Popen``. See the
    module docstring for the accuracy caveat (ffmpeg connection/startup
    latency is not subtracted out).
    """
    out_path = workdir / f"{label}.pcm"
    cmd = _build_ffmpeg_capture_cmd(url, out_path, seconds)
    wall_start = time.time()
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=seconds + timeout_slack, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CaptureResult(label, None, wall_start, error=f"ffmpeg timed out: {exc}")

    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[-500:] if proc.stderr else ""
        return CaptureResult(label, None, wall_start, error=f"ffmpeg failed (rc={proc.returncode}): {stderr}")

    return CaptureResult(label, out_path, wall_start)


def capture_all(
    sources: dict[str, str], *, seconds: float, workdir: Path, hdhr_ip: str, yt_dlp_bin: str = "yt-dlp",
) -> dict[str, CaptureResult]:
    """Launch all sources' captures concurrently (threaded) for ``seconds``.

    Resolution failures (bad yt-dlp URL, unresolvable spec) are captured
    per-source as a :class:`CaptureResult` with ``error`` set rather than
    raising — one bad source shouldn't abort the whole comparison.
    """
    import concurrent.futures as cf

    def _run_one(label: str, spec: str) -> CaptureResult:
        try:
            url = resolve_source_url(spec, hdhr_ip=hdhr_ip, yt_dlp_bin=yt_dlp_bin)
        except RuntimeError as exc:
            return CaptureResult(label, None, time.time(), error=str(exc))
        return capture_source(label, url, seconds=seconds, workdir=workdir)

    results: dict[str, CaptureResult] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, len(sources))) as ex:
        futs = {ex.submit(_run_one, label, spec): label for label, spec in sources.items()}
        for fut in cf.as_completed(futs):
            res = fut.result()
            results[res.label] = res
    return results


def load_pcm_s16le(path: Path) -> np.ndarray:
    """Read a raw s16le PCM file into a float32 array in [-1, 1]."""
    raw = np.fromfile(path, dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Lag estimation (pure, unit-tested)
# ---------------------------------------------------------------------------


def _envelope(signal: np.ndarray, sample_rate: int, envelope_rate_hz: float = ENVELOPE_RATE_HZ) -> np.ndarray:
    """Rectified, downsampled amplitude envelope — codec/volume-robust."""
    rectified = np.abs(signal.astype(np.float64))
    hop = max(1, int(round(sample_rate / envelope_rate_hz)))
    n_frames = len(rectified) // hop
    if n_frames < 2:
        return rectified
    trimmed = rectified[: n_frames * hop]
    env = trimmed.reshape(n_frames, hop).mean(axis=1)
    return env


def estimate_lag(
    sig_a: np.ndarray, sig_b: np.ndarray, sample_rate: int, *, envelope_rate_hz: float = ENVELOPE_RATE_HZ,
) -> tuple[float, float]:
    """Estimate how many seconds A leads B via envelope cross-correlation.

    Positive ``lag_seconds`` means A's content appears EARLIER in real
    time than B's (A leads B); negative means A lags B.

    ``confidence`` is the normalized cross-correlation peak, in [0, 1]:
    ``peak / sqrt(energy_a * energy_b)``. Values below roughly 0.4 suggest
    the two signals may not carry the same content (or one capture failed)
    — callers should NOT report a confident lag number in that case.

    >>> import numpy as np
    >>> rng = np.random.default_rng(0)
    >>> base = rng.standard_normal(16000 * 20).astype(np.float32) * 0.1
    >>> a = base.copy()
    >>> b = np.concatenate([np.zeros(1600, dtype=np.float32), base])[: len(base)]
    >>> lag, conf = estimate_lag(a, b, 16000)
    >>> lag > 0.05  # A leads B (B is delayed relative to A)
    True
    >>> conf > 0.4
    True
    """
    from scipy.signal import correlate

    env_a = _envelope(sig_a, sample_rate, envelope_rate_hz)
    env_b = _envelope(sig_b, sample_rate, envelope_rate_hz)

    if len(env_a) < 2 or len(env_b) < 2:
        return 0.0, 0.0

    env_a = env_a - env_a.mean()
    env_b = env_b - env_b.mean()

    energy_a = float(np.sum(env_a ** 2))
    energy_b = float(np.sum(env_b ** 2))
    if energy_a <= 0.0 or energy_b <= 0.0:
        return 0.0, 0.0

    corr = correlate(env_a, env_b, mode="full")
    lags = np.arange(-(len(env_b) - 1), len(env_a))  # lag[i] applies to corr[i]

    peak_idx = int(np.argmax(corr))
    peak_val = float(corr[peak_idx])
    lag_frames = int(lags[peak_idx])

    confidence = peak_val / np.sqrt(energy_a * energy_b)
    confidence = float(np.clip(confidence, 0.0, 1.0))

    # scipy.signal.correlate(a, b, 'full')'s peak lag is NEGATIVE when b is
    # delayed relative to a (i.e. a's content appears earlier) — verified
    # empirically against a hand-built shifted-impulse pair. Negate so our
    # documented convention (positive = a leads b) holds.
    lag_seconds = -lag_frames / envelope_rate_hz
    return lag_seconds, confidence


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class PairResult:
    label_a: str
    label_b: str
    signal_lag_seconds: float
    total_lag_seconds: float   # signal_lag + wallclock start offset
    confidence: float
    low_confidence: bool = field(init=False)

    def __post_init__(self) -> None:
        self.low_confidence = self.confidence < LOW_CONFIDENCE_THRESHOLD


def compare_captures(captures: dict[str, CaptureResult]) -> list[PairResult]:
    """Pairwise-compare every successfully-captured source.

    Skips any source whose capture failed (``CaptureResult.error`` set).
    """
    ok = {label: cap for label, cap in captures.items() if cap.error is None and cap.path is not None}
    labels = sorted(ok.keys())
    pairs: list[PairResult] = []

    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            label_a, label_b = labels[i], labels[j]
            cap_a, cap_b = ok[label_a], ok[label_b]
            sig_a = load_pcm_s16le(cap_a.path)  # type: ignore[arg-type]
            sig_b = load_pcm_s16le(cap_b.path)  # type: ignore[arg-type]

            signal_lag, confidence = estimate_lag(sig_a, sig_b, SAMPLE_RATE)
            wallclock_offset = cap_a.wall_start - cap_b.wall_start
            total_lag = signal_lag + wallclock_offset

            pairs.append(PairResult(label_a, label_b, signal_lag, total_lag, confidence))

    return pairs


def fastest_feed(pairs: list[PairResult]) -> str | None:
    """The label that leads every other label it was compared against.

    Only considers pairs with confidence >= threshold. Returns None if no
    single feed leads everything (ambiguous / insufficient confident data).
    """
    confident = [p for p in pairs if not p.low_confidence]
    if not confident:
        return None

    labels = set()
    for p in confident:
        labels.add(p.label_a)
        labels.add(p.label_b)

    for candidate in labels:
        leads_all = True
        for p in confident:
            if p.label_a == candidate and p.total_lag_seconds > 0:
                continue
            if p.label_b == candidate and p.total_lag_seconds < 0:
                continue
            if candidate not in (p.label_a, p.label_b):
                continue
            leads_all = False
            break
        if leads_all:
            return candidate
    return None


def format_report(captures: dict[str, CaptureResult], pairs: list[PairResult]) -> str:
    lines = ["=" * 70, "FEED LATENCY REPORT", "=" * 70, ""]

    failed = {label: cap for label, cap in captures.items() if cap.error is not None}
    if failed:
        lines.append("CAPTURE FAILURES:")
        for label, cap in failed.items():
            lines.append(f"  {label}: {cap.error}")
        lines.append("")

    if not pairs:
        lines.append("No pairs to compare (need >= 2 successful captures).")
        lines.append("=" * 70)
        return "\n".join(lines)

    lines.append("PAIRWISE COMPARISON:")
    for p in pairs:
        if p.total_lag_seconds > 0:
            leader, follower, mag = p.label_a, p.label_b, p.total_lag_seconds
        elif p.total_lag_seconds < 0:
            leader, follower, mag = p.label_b, p.label_a, -p.total_lag_seconds
        else:
            leader, follower, mag = p.label_a, p.label_b, 0.0

        flag = "  *** LOW CONFIDENCE — feeds may not match, or one failed ***" if p.low_confidence else ""
        lines.append(
            f"  {leader} leads {follower} by {mag:.2f}s (confidence {p.confidence:.2f}){flag}"
        )

    lines.append("")
    fastest = fastest_feed(pairs)
    if fastest:
        lines.append(f"FASTEST FEED: {fastest}")
    else:
        lines.append("FASTEST FEED: undetermined (no feed confidently led all others)")

    lines.append("=" * 70)
    return "\n".join(lines)


def write_report(text: str, path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(provenance.stamp_line() + "\n" + text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_source_arg(arg: str) -> tuple[str, str]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(f"--source must be LABEL=SPEC, got {arg!r}")
    label, spec = arg.split("=", 1)
    label, spec = label.strip(), spec.strip()
    if not label or not spec:
        raise argparse.ArgumentTypeError(f"--source must be LABEL=SPEC, got {arg!r}")
    return label, spec


def main() -> None:
    logctx.setup()

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--source", action="append", type=_parse_source_arg, dest="sources", required=True,
        metavar="LABEL=SPEC",
        help="Repeatable. SPEC is a raw URL, hdhr:<channel>[:<tuner>], or yt:<url>.",
    )
    ap.add_argument("--seconds", type=float, default=90.0, help="Capture duration per source (default 90).")
    ap.add_argument("--workdir", type=Path, default=None, help="Dir for temp PCM files (default: a tempdir).")
    ap.add_argument("--hdhr-ip", default=None,
                    help="tuner IP (default: HDHR_IP, else hdhomerun's fallback)")
    ap.add_argument("--yt-dlp-bin", default=".venv/bin/yt-dlp" if Path(".venv/bin/yt-dlp").exists() else "yt-dlp")
    ap.add_argument("--report-path", type=Path, default=REPORT_PATH)
    args = ap.parse_args()

    if len(args.sources) < 2:
        raise SystemExit("Need at least 2 --source args to compare.")

    sources = dict(args.sources)

    if args.workdir:
        args.workdir.mkdir(parents=True, exist_ok=True)
        workdir = args.workdir
        _run(sources, args, workdir)
    else:
        with tempfile.TemporaryDirectory(prefix="feed_latency_") as tmp:
            _run(sources, args, Path(tmp))


def _run(sources: dict[str, str], args: argparse.Namespace, workdir: Path) -> None:
    log.info("capturing %d sources for %.0fs into %s", len(sources), args.seconds, workdir)
    captures = capture_all(
        sources, seconds=args.seconds, workdir=workdir,
        hdhr_ip=args.hdhr_ip or hdhomerun.device_ip(), yt_dlp_bin=args.yt_dlp_bin,
    )
    pairs = compare_captures(captures)
    report = format_report(captures, pairs)
    print(report)
    write_report(report, args.report_path)
    log.info("report written to %s", args.report_path)


if __name__ == "__main__":
    main()
