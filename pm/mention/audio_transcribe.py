"""Real-time audio stream transcription via faster-whisper.

A reusable module that captures any audio stream (internet radio,
local audio device, file) via ffmpeg, chunks it, and runs Whisper
on the GPU for continuous text output. Designed for low-latency
live transcription — each chunk produces text within ~2 seconds.

Architecture
------------
Three-stage async pipeline:

1. **Capture** — ffmpeg reads the audio source (URL, device, file),
   resamples to 16kHz mono PCM, and pipes raw bytes to stdout.

2. **Chunk** — A background thread reads ffmpeg's output in fixed-
   duration windows (default 5s). Each window is a numpy array ready
   for Whisper. Windows overlap by ``overlap_seconds`` to catch words
   that span chunk boundaries.

3. **Transcribe** — faster-whisper processes each chunk on the GPU.
   Results are yielded to the caller with timestamps and confidence.

Usage::

    from pm.mention.audio_transcribe import AudioTranscriber

    t = AudioTranscriber(model_size="small", device="cuda")

    # Stream from a URL
    for segment in t.transcribe_stream("https://stream.example.com/live"):
        print(f"[{segment.start:.1f}s] {segment.text}")

    # Stream from a local audio device (e.g., PulseAudio)
    for segment in t.transcribe_stream("pulse", input_format="pulse"):
        print(segment.text)

    # Transcribe a file
    for segment in t.transcribe_file("recording.mp3"):
        print(segment.text)
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from pm.core import logctx

log = logging.getLogger("audio_transcribe")


@dataclass
class TranscriptSegment:
    """One chunk of transcribed text."""
    text: str
    start_time: float      # seconds since stream started
    end_time: float
    chunk_index: int
    confidence: float      # average log probability
    is_speech: bool        # False if Whisper detected no speech
    # Populated only when the transcriber was constructed with
    # word_timestamps=True. Each entry: {"word": str, "start": float,
    # "end": float} with start/end relative to the START OF THIS CHUNK
    # (add stream_start_wall + start_time to get an absolute wall-clock
    # timestamp — see mention_listen.py for a consumer).
    words: list[dict] = None  # type: ignore[assignment]
    # time.time() at the moment this stream's capture loop began. Same
    # value across every segment of one transcribe_stream()/transcribe_file()
    # call; lets a caller reconstruct absolute audio timestamps without
    # threading its own wall-clock bookkeeping through the generator.
    stream_start_wall: float = 0.0


class AudioTranscriber:
    """Continuous audio transcription engine.

    Parameters
    ----------
    model_size : str
        Whisper model: "tiny", "base", "small", "medium", "large-v3".
        "small" is the sweet spot for speed vs accuracy on an RTX 5000.
    device : str
        "cuda" or "cpu".
    language : str
        ISO language code. "en" skips language detection (faster).
    chunk_seconds : float
        Duration of each audio chunk sent to Whisper. Shorter = lower
        latency but less context for the model. 5s is a good default.
    overlap_seconds : float
        Overlap between consecutive chunks. Catches words split across
        chunk boundaries. 0.5s is usually enough.
    word_timestamps : bool
        Ask Whisper for per-word start/end offsets (populates
        ``TranscriptSegment.words``). Off by default — it's extra
        decode cost the plain transcription CLI doesn't need; consumers
        that need word-level timing (e.g. mention_listen.py) turn it on.
    vad_filter : bool
        Whether to run Silero VAD before decoding each chunk (drops
        silent spans). On by default. A caller chasing recall on quiet
        speech (e.g. mention_listen.py's ``--no-vad``) can disable it —
        every chunk gets decoded even if VAD would call it silence.
    vad_min_silence_ms : int
        ``min_silence_duration_ms`` passed to the VAD when
        ``vad_filter`` is on. Higher = less aggressive (VAD needs a
        longer silent gap before it trims audio) — reduces the risk of
        clipping a quiet trailing word right before a pause. Default
        500 matches the original hardcoded behavior.
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cuda",
        language: str = "en",
        chunk_seconds: float = 5.0,
        overlap_seconds: float = 0.5,
        word_timestamps: bool = False,
        vad_filter: bool = True,
        vad_min_silence_ms: int = 500,
    ):
        self.model_size = model_size
        self.device = device
        self.language = language
        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds
        self.word_timestamps = word_timestamps
        self.vad_filter = vad_filter
        self.vad_min_silence_ms = vad_min_silence_ms
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info("loading Whisper %s on %s...", self.model_size, self.device)
            compute = "float16" if self.device == "cuda" else "int8"
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=compute,
            )
            log.info("model loaded")
        return self._model

    def transcribe_stream(
        self,
        source: str,
        *,
        input_format: str | None = None,
        on_chunk: Any = None,
        max_seconds: float | None = None,
    ) -> Iterator[TranscriptSegment]:
        """Transcribe a live audio stream. Yields TranscriptSegments
        as they're produced.

        Parameters
        ----------
        source : str
            URL (http/https/rtmp), local file path, or audio device name.
        input_format : str | None
            ffmpeg input format override (e.g., "pulse" for PulseAudio,
            "alsa" for ALSA). None = auto-detect from source.
        on_chunk : callable | None
            Called with each raw audio chunk (numpy array) before
            transcription. Useful for saving audio alongside text.
        max_seconds : float | None
            Stop after this many seconds. None = run until stream ends
            or KeyboardInterrupt.
        """
        model = self._get_model()
        sample_rate = 16000
        chunk_samples = int(self.chunk_seconds * sample_rate)
        overlap_samples = int(self.overlap_seconds * sample_rate)
        bytes_per_sample = 2  # 16-bit PCM

        # Build ffmpeg command
        cmd = self._build_ffmpeg_cmd(source, input_format, sample_rate)
        log.info("starting ffmpeg: %s", " ".join(cmd[:6]) + "...")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        try:
            yield from self._process_stream(
                proc, model, sample_rate, chunk_samples,
                overlap_samples, bytes_per_sample, on_chunk, max_seconds,
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def transcribe_file(self, path: str) -> Iterator[TranscriptSegment]:
        """Transcribe a local audio file. Convenience wrapper."""
        yield from self.transcribe_stream(path)

    def _build_ffmpeg_cmd(
        self, source: str, input_format: str | None, sample_rate: int,
    ) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        if input_format:
            cmd.extend(["-f", input_format])

        # Reconnect options for HTTP streams
        if source.startswith(("http://", "https://")):
            cmd.extend([
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
            ])

        cmd.extend(["-i", source])

        # Output: 16kHz mono 16-bit PCM to stdout
        cmd.extend([
            "-ar", str(sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "pipe:1",
        ])
        return cmd

    def _process_stream(
        self, proc, model, sample_rate, chunk_samples,
        overlap_samples, bytes_per_sample, on_chunk, max_seconds,
    ) -> Iterator[TranscriptSegment]:
        buffer = np.array([], dtype=np.int16)
        chunk_index = 0
        stream_start = time.time()
        chunk_bytes = chunk_samples * bytes_per_sample

        while True:
            if max_seconds and (time.time() - stream_start) > max_seconds:
                break

            raw = proc.stdout.read(chunk_bytes)
            if not raw:
                break

            # Convert raw PCM to numpy
            samples = np.frombuffer(raw, dtype=np.int16)
            buffer = np.concatenate([buffer, samples])

            if len(buffer) < chunk_samples:
                continue

            # Extract chunk with overlap
            chunk = buffer[:chunk_samples].copy()
            buffer = buffer[chunk_samples - overlap_samples:]

            # Normalize to float32 [-1, 1]
            audio = chunk.astype(np.float32) / 32768.0

            if on_chunk:
                try:
                    on_chunk(audio, chunk_index)
                except Exception:
                    pass

            # Transcribe
            t0 = time.time()
            segments, info = model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                best_of=1,
                vad_filter=self.vad_filter,
                vad_parameters=dict(
                    min_silence_duration_ms=self.vad_min_silence_ms,
                    speech_pad_ms=200,
                ),
                word_timestamps=self.word_timestamps,
            )

            elapsed_since_start = time.time() - stream_start
            text_parts = []
            word_parts: list[dict] = []
            avg_logprob = -1.0
            is_speech = False

            for seg in segments:
                text_parts.append(seg.text.strip())
                avg_logprob = max(avg_logprob, getattr(seg, 'avg_logprob', getattr(seg, 'avg_log_prob', -1.0)))
                is_speech = True
                if self.word_timestamps:
                    for w in (getattr(seg, "words", None) or []):
                        word_parts.append({
                            "word": w.word.strip(),
                            "start": w.start,
                            "end": w.end,
                        })

            text = " ".join(text_parts).strip()
            transcribe_time = time.time() - t0

            if text:
                segment = TranscriptSegment(
                    text=text,
                    start_time=elapsed_since_start - self.chunk_seconds,
                    end_time=elapsed_since_start,
                    chunk_index=chunk_index,
                    confidence=min(1.0, max(0.0, 1.0 + avg_logprob)),
                    is_speech=is_speech,
                    words=word_parts,
                    stream_start_wall=stream_start,
                )
                log.debug("chunk %d (%.1fs): [%.2fs] %s",
                          chunk_index, transcribe_time, segment.start_time, text[:80])
                yield segment

            chunk_index += 1


# ─── Convenience functions ───────────────────────────────────────────────

def transcribe_url(
    url: str,
    *,
    model_size: str = "small",
    device: str = "cuda",
    language: str = "en",
    chunk_seconds: float = 5.0,
    max_seconds: float | None = None,
) -> Iterator[TranscriptSegment]:
    """One-liner for transcribing a URL stream."""
    t = AudioTranscriber(
        model_size=model_size, device=device,
        language=language, chunk_seconds=chunk_seconds,
    )
    yield from t.transcribe_stream(url, max_seconds=max_seconds)


# ─── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Real-time audio transcription via Whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("source", help="Audio source: URL, file path, or device name")
    ap.add_argument("--model", default="small", help="Whisper model size (default: small)")
    ap.add_argument("--device", default="cuda", help="cuda or cpu (default: cuda)")
    ap.add_argument("--language", default="en", help="Language code (default: en)")
    ap.add_argument("--chunk", type=float, default=5.0, help="Chunk duration in seconds (default: 5)")
    ap.add_argument("--max-seconds", type=float, default=None, help="Stop after N seconds")
    ap.add_argument("--input-format", default=None, help="ffmpeg input format (e.g., pulse, alsa)")
    ap.add_argument("--output", default=None, help="Write transcript to file (JSONL)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logctx.setup(args.log_level.upper())

    t = AudioTranscriber(
        model_size=args.model, device=args.device,
        language=args.language, chunk_seconds=args.chunk,
    )

    out_file = None
    if args.output:
        import json
        out_file = open(args.output, "a")

    try:
        for seg in t.transcribe_stream(
            args.source,
            input_format=args.input_format,
            max_seconds=args.max_seconds,
        ):
            timestamp = f"[{seg.start_time:6.1f}s]"
            print(f"{timestamp} {seg.text}")
            sys.stdout.flush()

            if out_file:
                import json
                out_file.write(json.dumps({
                    "text": seg.text,
                    "start": seg.start_time,
                    "end": seg.end_time,
                    "confidence": seg.confidence,
                    "chunk": seg.chunk_index,
                }) + "\n")
                out_file.flush()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if out_file:
            out_file.close()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
