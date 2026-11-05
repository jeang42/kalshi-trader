"""Nightly late-night monologue recorder — OTA tuner → audio → transcript corpus.

WHY (docs/edge-research-log.md 2026-08-07 collection plan, item 3). The mention
detector's binding constraint is precision/recall (71%/53% at WHCD, 0 of 14
cascades beaten), and tuning it needs ground-truth pairs of (broadcast audio,
known Kalshi settlement). Late-night monologues are the ideal nightly corpus:
near-single-speaker (attribution ≈ free, tier 1-2 of the attribution ladder),
`KXLATENIGHTMENTION` settles against them, and the 4-tuner HDHomeRun receives
them free every weeknight. This module RECORDS and TRANSCRIBES; it never
trades, never places orders, and imports no trading modules.

DESIGN — capture first, transcribe after, by intention:
- The corpus does not race anything, so there is no reason to pay live-decode
  complexity. ffmpeg writes a 16 kHz mono WAV (whisper-native; ~2 MB/min);
  transcription runs after capture completes. Two shows recording in the same
  time slot = two cheap ffmpeg processes on different pinned tuners; the GPU
  work happens afterwards.
- Wall-clock anchoring: `meta.json` stores capture start (epoch seconds), and
  every transcript row carries `wall_ts = started + segment/word offset`, so a
  later scoring pass can align words against Kalshi settlement times without
  guessing.
- Shows are a data table. Slots (America/Chicago, DFW OTA): the 22:30 weeknight
  slot covers Tonight Show (NBC 5.1) and Kimmel (ABC 8.8) monologues that start
  ~22:35. CBS's Late Show ended in May 2026 — no CBS entry. `snl` (NBC,
  Saturday) is in the table for the fall season but its timer ships DISABLED:
  recording summer reruns is waste.
- Tuner allocation avoids both Plex (`/auto` gravitates to tuners 0-1) and the
  mention pipeline's pinned tuner 3: tonight=3 (the pipeline tuner, idle at
  22:30 unless a live event rehearsal is running — the lock is best-effort
  and capture survives lock failure), kimmel=2.

Outputs per run, under data/shared/monologue_corpus/<YYYY-MM-DD>_<show>/:
    audio.wav         16 kHz mono capture (kept — improved detectors can be
                      re-run against raw audio later; ~80 MB per 40 min)
    transcript.jsonl  one row per whisper segment: start/end/text/words +
                      wall_ts (absolute)
    transcript.txt    human-readable
    meta.json         show, channel, tuner, started (epoch), seconds, model

Scheduling: systemd user timers `monologue-record@<show>.timer` → oneshot
`monologue-record@.service` (`%i` = show key). Timers use the machine TZ
(America/Chicago); `Persistent=false` — a missed broadcast cannot be made up.
CLI: `python monologue_recorder.py --show tonight [--minutes 40]`
   | `--transcribe-only <run_dir>` | `--list`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pm.core import logctx
from pm.mention import hdhomerun

log = logging.getLogger(__name__)

CORPUS_DIR = Path(os.getenv("MONOLOGUE_CORPUS_DIR",
                            "data/shared/monologue_corpus"))
WHISPER_MODEL = os.getenv("MONOLOGUE_WHISPER_MODEL", "small")


@dataclass(frozen=True)
class Show:
    key: str
    label: str
    channel: str        # DFW_CHANNELS key or explicit virtual channel
    tuner: int
    minutes: int        # default capture length
    note: str = ""


SHOWS: dict[str, Show] = {
    "tonight": Show("tonight", "The Tonight Show (NBC)", "NBC", 3, 40,
                    "Mon-Fri 22:34 CT; monologue in first ~15 min"),
    "kimmel": Show("kimmel", "Jimmy Kimmel Live (ABC)", "ABC", 2, 40,
                   "Mon-Fri 22:35 CT"),
    "snl": Show("snl", "Saturday Night Live (NBC)", "NBC", 3, 100,
                "Sat 22:29 CT, IN SEASON ONLY — timer ships disabled; "
                "enable when the fall season starts"),
}


def run_dir(show_key: str, *, when: datetime | None = None) -> Path:
    """Corpus directory for one recording run.

    >>> str(run_dir("tonight", when=datetime(2026, 8, 10, 3, 30, tzinfo=UTC)))
    'data/shared/monologue_corpus/2026-08-10_tonight'
    """
    when = when or datetime.now(UTC)
    return CORPUS_DIR / f"{when.date().isoformat()}_{show_key}"


def capture(show: Show, *, minutes: int | None = None,
            out: Path | None = None) -> Path | None:
    """Record the show's channel to a 16 kHz mono WAV. Returns the run dir,
    or None if ffmpeg failed outright. Best-effort tuner lock; a lock failure
    logs and proceeds (holding the HTTP stream is itself the reservation)."""
    minutes = minutes or show.minutes
    out = out or run_dir(show.key)
    out.mkdir(parents=True, exist_ok=True)
    url = hdhomerun.stream_url(show.channel, tuner=show.tuner)
    if not hdhomerun.channel_present(show.channel):
        log.warning("channel %s not in tuner lineup — attempting anyway",
                    show.channel)
    # Defense in depth: hdhomerun.acquire_lock's contract is "never crash a
    # capture", but a broadcast is unrepeatable — enforce that here too.
    try:
        lock = hdhomerun.acquire_lock(show.tuner)
    except Exception as e:  # noqa: BLE001
        log.warning("tuner lock failed (%s) — capturing without it", e)
        lock = None
    started = time.time()
    wav = out / "audio.wav"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-t", str(minutes * 60), "-i", url,
           "-vn", "-ac", "1", "-ar", "16000", str(wav)]
    log.info("capturing %s -> %s (%d min, tuner %d)",
             show.label, wav, minutes, show.tuner)
    try:
        proc = subprocess.run(cmd, timeout=minutes * 60 + 120,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("ffmpeg exited %d: %s", proc.returncode,
                      (proc.stderr or "")[-400:])
    except subprocess.TimeoutExpired:
        log.error("ffmpeg hung past capture window — killed")
    finally:
        if lock:
            hdhomerun.release_lock(show.tuner, lock)
    if not wav.exists() or wav.stat().st_size < 1_000_000:
        # < ~30s of audio = the capture failed; loud, per the silent-null rule
        log.error("capture produced no usable audio (%s bytes) — check tuner",
                  wav.stat().st_size if wav.exists() else 0)
        return None
    meta = {"show": show.key, "label": show.label, "channel": show.channel,
            "tuner": show.tuner, "started": started,
            "seconds": round(time.time() - started, 1),
            "whisper_model": WHISPER_MODEL,
            "recorded_at_utc": datetime.now(UTC).isoformat()}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    log.info("capture complete: %.1f MB", wav.stat().st_size / 1e6)
    return out


def transcribe(run: Path) -> int:
    """Whisper the run's WAV into transcript.jsonl/.txt. Returns segment count.
    Requires meta.json (for the wall-clock anchor)."""
    meta = json.loads((run / "meta.json").read_text())
    started = float(meta["started"])
    from pm.mention.audio_transcribe import AudioTranscriber
    tr = AudioTranscriber(model_size=meta.get("whisper_model", WHISPER_MODEL),
                          word_timestamps=True, chunk_seconds=10.0,
                          overlap_seconds=1.0)
    n = 0
    with (run / "transcript.jsonl").open("w") as jf, \
         (run / "transcript.txt").open("w") as tf:
        for seg in tr.transcribe_file(str(run / "audio.wav")):
            if not seg.is_speech or not seg.text.strip():
                continue
            words = [{"word": w.get("word"),
                      "wall_ts": round(started + seg.start_time
                                       + float(w.get("start") or 0), 2)}
                     for w in (seg.words or [])]
            jf.write(json.dumps({
                "start": round(seg.start_time, 2),
                "end": round(seg.end_time, 2),
                "wall_ts": round(started + seg.start_time, 2),
                "text": seg.text.strip(),
                "confidence": round(seg.confidence, 3),
                "words": words}) + "\n")
            tf.write(f"[{seg.start_time:7.1f}s] {seg.text.strip()}\n")
            n += 1
    log.info("transcribed %s: %d segments", run.name, n)
    return n


def record_and_transcribe(show_key: str, *, minutes: int | None = None,
                          do_transcribe: bool = True) -> Path | None:
    show = SHOWS.get(show_key)
    if show is None:
        log.error("unknown show %r (have: %s)", show_key, sorted(SHOWS))
        return None
    run = capture(show, minutes=minutes)
    if run is None:
        return None
    if do_transcribe:
        try:
            transcribe(run)
        except Exception as e:  # noqa: BLE001 — audio is safe on disk;
            # transcription can be re-run via --transcribe-only
            log.error("transcription failed (audio kept, re-run with "
                      "--transcribe-only %s): %s", run, e)
    return run


def main() -> int:
    # Gotcha 4: nothing loads .env unless it says so (HDHR_IP lives there).
    from dotenv import load_dotenv
    load_dotenv()
    logctx.setup()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", choices=sorted(SHOWS))
    ap.add_argument("--minutes", type=int, default=None)
    ap.add_argument("--no-transcribe", action="store_true")
    ap.add_argument("--transcribe-only", metavar="RUN_DIR",
                    help="(re)transcribe an existing capture directory")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for s in SHOWS.values():
            print(f"{s.key:<8} {s.label:<28} ch={s.channel} tuner={s.tuner} "
                  f"{s.minutes}min  {s.note}")
        return 0
    if args.transcribe_only:
        transcribe(Path(args.transcribe_only))
        return 0
    if not args.show:
        ap.error("--show required (or --list / --transcribe-only)")
    run = record_and_transcribe(args.show, minutes=args.minutes,
                                do_transcribe=not args.no_transcribe)
    # Nonzero on a failed capture so systemd marks the unit failed and the
    # OnFailure hook pages. Exiting 0 here hid a dead tuner IP from
    # 2026-08-16 to 10-13: 79 empty run dirs, every unit "Finished".
    return 0 if run is not None else 1


if __name__ == "__main__":
    sys.exit(main())
