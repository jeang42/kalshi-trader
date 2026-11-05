"""Mention-market DETECTION layer — streaming audio → per-market word/phrase
detection. Rehearsal infrastructure for the mention-sniping thesis
(``docs/edge-research-log.md`` 2026-07-09 → 2026-07-19 entries,
``mention_capture.py``, memory note ``project_mention_sniping_thesis``).

Context: ``mention_capture.py`` established the OPEN QUESTION (does the book
reprice instantly or grind over seconds?) by watching Kalshi quotes. The
2026-07-19 trade-tape read answered it: mention resolutions are a 7-60s
HUMAN CASCADE, not an instant tick — there's a real window, and a richer
"context anticipation" tier (buy cheap words as the speech approaches them,
30-90s of leisure) doesn't even require winning a race. This module is the
listening half of testing that: given a live audio feed and a set of target
words/phrases per market, how fast and how reliably can we detect that a
word was SAID?

**This module places NO orders.** It must never import ``kalshi_orders`` or
``execution`` — it is pure detection/measurement, wired to a JSONL log (and
an in-process callback hook) so a future harness can consume detections
without this module knowing anything about trading.

Pipeline
--------
1. **Capture** — reuses ``audio_transcribe.AudioTranscriber`` (ffmpeg → 16kHz
   mono PCM chunks) for ingest. Don't duplicate that plumbing here.
2. **Transcribe** — faster-whisper with ``word_timestamps=True`` (added to
   ``AudioTranscriber`` for this consumer) so every word carries an offset
   we can turn into an absolute wall-clock timestamp via
   ``TranscriptSegment.stream_start_wall + start_time + word_offset``.
3. **Normalize + window** — lowercase/strip-punctuation each word into a
   rolling ~15s window (``TranscriptWindow``) so a target phrase that spans
   a chunk boundary (e.g. "golden" in chunk N, "dome" in chunk N+1) still
   matches.
4. **Match** — each market has one or more regex patterns (case-insensitive,
   word-boundary-wrapped) checked against the window text. First match per
   ticker fires ONCE per run (``MentionDetector`` dedups).
5. **Log** — every detection is appended to
   ``data/shared/mention_detections.jsonl`` and handed to an optional
   ``on_detection`` callback.

Config builder
--------------
``--build-config EVENT_TICKER`` pulls the event's markets from Kalshi and
emits a starter JSON config, extracting the target phrase from the quoted
text in the question (the common "Will X say \"WORD\"..." shape) with naive
plural/possessive variants. Uncertain extractions get a ``comment`` field
instead of a guessed pattern so a human fixes them by hand — never silently
ships a wrong pattern.

Future upgrade path (v0 is intentionally simple)
-------------------------------------------------
- **KWS / wake-word models** (e.g. openWakeWord, Porcupine) for sub-second
  per-word detection instead of whisper's ~1-2s-per-chunk decode latency —
  the natural next step once this measures whether detection speed is even
  the binding constraint (the 07-19 cascade read suggests it may not be:
  the GOLD pattern shows 30-90s of pre-utterance context, which a topic
  model could exploit without racing word-level ASR at all).
- **Streaming/incremental whisper** (rolling partial decodes instead of
  fixed non-overlapping-content chunks) to cut the v0 chunk-boundary
  latency further.

Read-only w.r.t. Kalshi (the config builder does one GET). No trades.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pm.core import logctx
from pm.mention import hdhomerun
from pm.mention.audio_transcribe import AudioTranscriber, TranscriptSegment

log = logging.getLogger(__name__)

DETECTIONS_PATH = "data/shared/mention_detections.jsonl"


# ─── Text normalization + pattern matching (pure) ──────────────────────────

def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation (keep alnum + spaces), collapse whitespace.

    Applied identically to the live transcript and to configured patterns
    so ASR output like "tariff's" and a hand-written pattern "Tariff's"
    land on the same normalized form.

    >>> normalize_text("Golden Dome's ready!")
    'golden domes ready'
    >>> normalize_text("  multiple   spaces  ")
    'multiple spaces'
    >>> normalize_text("")
    ''
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compile_pattern(pattern: str) -> re.Pattern[str]:
    r"""Compile a mention pattern into a regex matched against normalized text.

    Normalizes the pattern the same way as the transcript, then joins
    words with ``\s+`` (tolerates ASR spacing variance for multi-word
    phrases) and wraps the whole thing in word boundaries (so "art" never
    matches inside "cart").

    >>> bool(compile_pattern("Golden Dome").search("say golden dome now"))
    True
    >>> bool(compile_pattern("Tariff's").search("he said tariffs today"))
    True
    >>> bool(compile_pattern("art").search("he drove a cart"))
    False
    >>> bool(compile_pattern("golden dome").search("golden   dome"))
    True
    """
    norm = normalize_text(pattern)
    words = [w for w in norm.split(" ") if w]
    body = r"\s+".join(re.escape(w) for w in words) if words else re.escape(norm)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


# ─── Rolling transcript window (pure, spans chunk boundaries) ──────────────

@dataclass
class WordEntry:
    word: str   # already normalized
    ts: float   # absolute unix seconds


class TranscriptWindow:
    """Rolling window of recent normalized words, each with an absolute
    wall-clock timestamp. Backs both the matcher (join into one string so
    multi-word patterns can span a chunk boundary) and the audio-timestamp
    lookup for a match.
    """

    def __init__(self, window_seconds: float = 15.0):
        self.window_seconds = window_seconds
        self._words: deque[WordEntry] = deque()

    def add_words(self, words: list[WordEntry]) -> None:
        self._words.extend(words)
        self._prune()

    def _prune(self) -> None:
        if not self._words:
            return
        cutoff = self._words[-1].ts - self.window_seconds
        while self._words and self._words[0].ts < cutoff:
            self._words.popleft()

    def text(self) -> str:
        """Joined normalized words, single-space separated — what patterns
        are matched against.

        >>> w = TranscriptWindow()
        >>> w.add_words([WordEntry("golden", 100.0)])
        >>> w.add_words([WordEntry("dome", 100.5)])
        >>> w.text()
        'golden dome'
        """
        return " ".join(w.word for w in self._words)

    def ts_for_offset(self, char_offset: int) -> float | None:
        """Absolute timestamp of the word covering ``char_offset`` in
        ``text()``. Used to attribute a match's ``ts_word_audio``.

        >>> w = TranscriptWindow()
        >>> w.add_words([WordEntry("golden", 100.0), WordEntry("dome", 100.5)])
        >>> w.ts_for_offset(0)      # "golden" starts at 0
        100.0
        >>> w.ts_for_offset(7)      # "dome" starts at 7
        100.5
        """
        pos = 0
        for w in self._words:
            end = pos + len(w.word)
            if char_offset <= end:
                return w.ts
            pos = end + 1  # +1 for the joining space
        return self._words[-1].ts if self._words else None


# ─── Event config ───────────────────────────────────────────────────────────

@dataclass
class MarketWatch:
    ticker: str
    patterns: list[str]
    count: int = 1
    compiled: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.compiled:
            self.compiled = [compile_pattern(p) for p in self.patterns]
        if self.count < 1:
            self.count = 1


@dataclass
class EventConfig:
    event_ticker: str
    source: str | None
    markets: list[MarketWatch]
    single_speaker: bool = False


def load_event_config(path: str | Path) -> EventConfig:
    """Load an event config JSON (see module docstring / --build-config for
    the shape) and compile every market's patterns."""
    data = json.loads(Path(path).read_text())
    markets = [
        MarketWatch(
            ticker=str(m["ticker"]),
            patterns=list(m.get("patterns", [])),
            count=int(m.get("count", 1)),
        )
        for m in data.get("markets", [])
    ]
    return EventConfig(
        event_ticker=str(data.get("event_ticker", "")),
        source=data.get("source"),
        markets=markets,
        single_speaker=bool(data.get("single_speaker", False)),
    )


# ─── Detector (pure decision logic; logging is a side effect) ─────────────

class MentionDetector:
    """Scans a rolling ``TranscriptWindow`` for each market's target
    pattern(s). Fires exactly once per ticker per instance (dedup lives in
    ``_detected``) — construct a fresh instance per run.

    A market's ``count`` (default 1) gates COUNT markets ("said 3+ times"):
    the detection event only fires on the Nth *distinct* occurrence. Each
    match is deduped by the audio timestamp of its first word (via
    ``TranscriptWindow.ts_for_offset``) so the same instance re-appearing in
    a later window (the rolling window hasn't pruned it yet) is never
    counted twice — only a genuinely NEW word-index match advances the
    count.
    """

    def __init__(
        self,
        config: EventConfig,
        *,
        model: str,
        source: str,
        on_detection: Callable[[dict], None] | None = None,
        detections_path: str | Path = DETECTIONS_PATH,
    ):
        self.config = config
        self.model = model
        self.source = source
        self.on_detection = on_detection
        self.detections_path = Path(detections_path)
        self._detected: set[str] = set()
        # ticker -> {dedup_key: (matched_text, pattern_src, ts)} of every
        # distinct occurrence counted so far (across all of that market's
        # patterns — they're alternative spellings of the same target word).
        self._occurrences: dict[str, dict[float | int, tuple[str, str, float | None]]] = {}

    @property
    def all_detected(self) -> bool:
        return len(self._detected) >= len(self.config.markets)

    def check(self, window: TranscriptWindow) -> list[dict]:
        """Scan for new matches. Returns newly-fired detections (empty when
        nothing new fired this call). Logs + calls the callback for each."""
        text = window.text()
        fired = []
        for mw in self.config.markets:
            if mw.ticker in self._detected:
                continue
            seen = self._occurrences.setdefault(mw.ticker, {})
            newly_added: list[float | int] = []
            for pattern_src, rx in zip(mw.patterns, mw.compiled, strict=False):
                for m in rx.finditer(text):
                    ts = window.ts_for_offset(m.start())
                    dedup_key: float | int = ts if ts is not None else m.start()
                    if dedup_key in seen:
                        continue
                    seen[dedup_key] = (m.group(0), pattern_src, ts)
                    newly_added.append(dedup_key)
            if not newly_added or len(seen) < mw.count:
                continue

            matched_text, pattern_src, ts = seen[newly_added[-1]]
            det = {
                "ts_detected": int(time.time() * 1000),
                "ts_word_audio": ts,
                "event_ticker": self.config.event_ticker,
                "ticker": mw.ticker,
                "matched_text": matched_text,
                "pattern": pattern_src,
                "occurrence": len(seen),
                "model": self.model,
                "source": self.source,
            }
            self._detected.add(mw.ticker)
            fired.append(det)
            self._log(det)
            if self.on_detection:
                try:
                    self.on_detection(det)
                except Exception:
                    log.exception("on_detection callback failed for %s", mw.ticker)
        return fired

    def _log(self, det: dict) -> None:
        self.detections_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.detections_path, "a") as f:
            f.write(json.dumps(det) + "\n")
        log.info(
            "DETECTED %s (%s) — matched %r via pattern %r",
            det["ticker"], det["event_ticker"], det["matched_text"], det["pattern"],
        )


# ─── Config builder ─────────────────────────────────────────────────────────

_QUOTE_RE = re.compile(r'"([^"]+)"|“([^”]+)”|\'([^\']+)\'')


def _find_quoted(text: str) -> str | None:
    m = _QUOTE_RE.search(text or "")
    if not m:
        return None
    for g in m.groups():
        if g:
            return g.strip()
    return None


def _variants(phrase: str) -> list[str]:
    """Naive plural/possessive variants of a phrase for pattern matching.

    >>> _variants("Tariff")
    ['Tariff', 'Tariffs', "Tariff's"]
    >>> _variants("Chaos")
    ['Chaos', "Chaos's"]
    """
    phrase = phrase.strip()
    out = [phrase]
    if not phrase.endswith("s"):
        out.append(phrase + "s")
    out.append(phrase + "'s")
    return out


def extract_patterns(question: str, yes_sub_title: str = "") -> dict:
    """Extract mention pattern candidates from a Kalshi mention-market
    question (+ optional yes_sub_title). Looks for quoted text first (the
    common ``Will X say "WORD"...`` shape); falls back to a short
    yes_sub_title; otherwise returns no patterns and a comment flagging
    manual review — never guesses silently.

    >>> extract_patterns('Will "Golden Dome" be said during the address?')['patterns']
    ['Golden Dome', 'Golden Domes', "Golden Dome's"]
    >>> extract_patterns('Will Trump say "Tariff" during the speech?')['patterns']
    ['Tariff', 'Tariffs', "Tariff's"]
    >>> extract_patterns("Will Trump say 'Witch Hunt' tonight?")['patterns']
    ['Witch Hunt', 'Witch Hunts', "Witch Hunt's"]
    >>> r = extract_patterns('Will Zohran Mamdani win the mayoral primary?', 'Zohran Mamdani')
    >>> r['patterns']
    ['Zohran Mamdani', 'Zohran Mamdanis', "Zohran Mamdani's"]
    >>> r['comment'] is not None
    True
    >>> r2 = extract_patterns('Will the show mention a surprise guest?', 'Yes')
    >>> r2['patterns']
    []
    >>> r2['comment'] is not None
    True
    """
    quoted = _find_quoted(question) or _find_quoted(yes_sub_title)
    if quoted:
        return {"patterns": _variants(quoted), "comment": None}

    sub = (yes_sub_title or "").strip()
    if sub and sub.lower() not in ("yes", "no") and len(sub.split()) <= 4:
        return {
            "patterns": _variants(sub),
            "comment": (
                f"uncertain extraction from yes_sub_title — verify against "
                f"question: {question!r}"
            ),
        }
    return {
        "patterns": [],
        "comment": f"could not extract a pattern — fill in by hand. question: {question!r}",
    }


def build_config(event_ticker: str) -> dict:
    """Fetch an event's markets from Kalshi and emit a starter event-config
    dict (JSON-serializable) — see module docstring for the shape. The
    only network call this module makes."""
    from pm.venue import kalshi_client as kc

    resp = kc._get("markets", params={"event_ticker": event_ticker, "limit": 100})
    items = resp.get("markets", []) if isinstance(resp, dict) else []
    markets = []
    for m in items:
        ticker = str(m.get("ticker", ""))
        if not ticker:
            continue
        question = (m.get("title") or "").strip()
        sub = (m.get("yes_sub_title") or "").strip()
        extraction = extract_patterns(question, sub)
        entry: dict = {"ticker": ticker, "patterns": extraction["patterns"]}
        if extraction["comment"]:
            entry["comment"] = extraction["comment"]
        markets.append(entry)
    return {
        "event_ticker": event_ticker,
        "source": None,
        "single_speaker": False,
        "single_speaker_comment": (
            "set true only for genuinely single-voice content (e.g. a "
            "speech/rally with no moderator/interviewer); leave false for "
            "interviews/panels — detections are speaker-blind (no "
            "diarization yet) and the 2026-07-19 FTN rehearsal measured "
            "net-negative paper P&L on multi-speaker content"
        ),
        "markets": markets,
    }


# ─── Speaker-attribution guardrail ─────────────────────────────────────────

def _speaker_warning(config: EventConfig) -> str | None:
    """Loud startup warning for multi-speaker (``single_speaker=False``)
    configs — pure so it's unit-testable without the whisper pipeline.

    Guardrail only, not diarization (phase 2): detections on multi-speaker
    content are speaker-blind (a moderator's stray word fires the same as
    the resolving voice saying it), and the 2026-07-19 FTN rehearsal
    measured net-negative paper P&L on exactly that failure mode
    (MAIN/MINN said by the moderator, not the interview subject).

    >>> _speaker_warning(EventConfig("E", None, [], single_speaker=True)) is None
    True
    >>> "speaker-blind" in _speaker_warning(EventConfig("E", None, [], single_speaker=False))
    True
    """
    if config.single_speaker:
        return None
    return (
        "MULTI-SPEAKER event (single_speaker=false): detections are "
        "speaker-blind — a moderator/interviewer's stray word fires a "
        "market exactly like the resolving voice saying it. The "
        "2026-07-19 FTN rehearsal measured NET-NEGATIVE paper P&L on this "
        "failure mode. Rehearsal-only — do NOT trade on these detections "
        "until diarization (phase 2) lands."
    )


# ─── Live wiring ────────────────────────────────────────────────────────────

def _words_to_entries(segment: TranscriptSegment) -> list[WordEntry]:
    """Convert one TranscriptSegment's word list into normalized,
    absolute-timestamped WordEntry objects for the rolling window."""
    out = []
    for w in (segment.words or []):
        text = normalize_text(w.get("word", ""))
        if not text:
            continue
        abs_ts = segment.stream_start_wall + segment.start_time + float(w.get("start", 0.0))
        out.append(WordEntry(word=text, ts=abs_ts))
    return out


def run_listen(
    config: EventConfig,
    *,
    source: str,
    model: str = "small",
    chunk_seconds: float = 5.0,
    overlap_seconds: float = 1.0,
    window_seconds: float = 15.0,
    input_format: str | None = None,
    max_seconds: float | None = None,
    replay: bool = False,
    vad_filter: bool = True,
    vad_min_silence_ms: int = 700,
    on_detection: Callable[[dict], None] | None = None,
    detections_path: str | Path = DETECTIONS_PATH,
) -> list[dict]:
    """Run the capture → transcribe → match loop against a live or replayed
    audio source until it ends, ``max_seconds`` elapses, or every market has
    fired. Returns every detection that fired (also streamed to the JSONL
    log + ``on_detection`` as they happen).

    ``chunk_seconds``/``overlap_seconds`` default to 5.0/1.0 (v0 used
    3.0/0.5) — the recall fix from the 2026-07-19 FTN rehearsal (a word
    ("sanctuary") straddling a chunk boundary got whisper-hallucinated into
    unrelated text on a short, low-overlap chunk; a wider window with more
    overlapping coverage decoded it cleanly — see docs/edge-research-log.md
    for the empirical comparison). ``vad_min_silence_ms`` defaults to 700
    (up from faster_whisper's 500) — less aggressive silence trimming, so a
    quiet word right before a pause is less likely to get clipped.
    """
    warning = _speaker_warning(config)
    if warning:
        log.warning(warning)

    transcriber = AudioTranscriber(
        model_size=model, chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds, word_timestamps=True,
        vad_filter=vad_filter, vad_min_silence_ms=vad_min_silence_ms,
    )
    window = TranscriptWindow(window_seconds=window_seconds)
    detector = MentionDetector(
        config, model=model, source=source,
        on_detection=on_detection, detections_path=detections_path,
    )

    stream = (
        transcriber.transcribe_file(source)
        if replay
        else transcriber.transcribe_stream(source, input_format=input_format, max_seconds=max_seconds)
    )

    detections: list[dict] = []
    for seg in stream:
        log.info("[%6.1fs] %s", seg.start_time, seg.text)
        window.add_words(_words_to_entries(seg))
        fired = detector.check(window)
        detections.extend(fired)
        if detector.all_detected:
            log.info("all %d market(s) detected — stopping", len(config.markets))
            break

    return detections


# ─── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mention-market DETECTION layer: streaming audio -> per-market word/phrase detection. Places no orders.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--build-config", metavar="EVENT_TICKER",
                     help="Fetch an event's markets from Kalshi and emit a starter event-config JSON to stdout")
    ap.add_argument("--event-config", help="Path to an event-config JSON (required unless --build-config)")
    ap.add_argument("--source", default=None, help="Audio source: URL/file/device. Overrides the config's source field.")
    ap.add_argument("--replay", default=None, help="Path to a recorded audio file to replay instead of a live source")
    ap.add_argument(
        "--hdhr-channel", default=None, metavar="CH",
        help=(
            "Use a pinned HDHomeRun OTA tuner as the source instead of --source "
            "(mutually exclusive with --source). CH is a DFW network name "
            "(CBS/FOX/NBC/ABC) or an explicit virtual channel like 11.1."
        ),
    )
    ap.add_argument("--hdhr-tuner", type=int, default=hdhomerun.PIPELINE_TUNER,
                     help=f"HDHomeRun tuner index to pin (default: {hdhomerun.PIPELINE_TUNER})")
    ap.add_argument("--hdhr-ip", default=None,
                     help="HDHomeRun device IP (default: HDHR_IP, else hdhomerun's fallback)")
    ap.add_argument("--model", default="small", help="Whisper model size (default: small — latency over accuracy)")
    ap.add_argument("--chunk", type=float, default=5.0, help="Chunk duration in seconds (default: 5 — v0.1 recall fix)")
    ap.add_argument("--overlap", type=float, default=1.0, help="Chunk overlap in seconds (default: 1.0 — v0.1 recall fix)")
    ap.add_argument("--window-seconds", type=float, default=15.0, help="Rolling match window in seconds (default: 15)")
    ap.add_argument("--input-format", default=None, help="ffmpeg input format override (e.g. pulse, alsa)")
    ap.add_argument("--max-seconds", type=float, default=None, help="Stop after N seconds")
    vad_group = ap.add_mutually_exclusive_group()
    vad_group.add_argument("--vad", dest="vad", action="store_true", default=True,
                            help="Enable VAD silence-trimming before decode (default: on)")
    vad_group.add_argument("--no-vad", dest="vad", action="store_false",
                            help="Disable VAD — decode every chunk even if it looks silent (chasing recall)")
    ap.add_argument("--vad-min-silence-ms", type=int, default=700,
                     help="VAD min_silence_duration_ms — higher is less aggressive trimming (default: 700)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logctx.setup(args.log_level.upper())

    if args.build_config:
        print(json.dumps(build_config(args.build_config), indent=2))
        return 0

    if not args.event_config:
        ap.error("--event-config is required (or use --build-config EVENT_TICKER)")

    if args.source and args.hdhr_channel:
        ap.error("--source and --hdhr-channel are mutually exclusive")

    config = load_event_config(args.event_config)
    replay_source = args.replay
    live_source = args.source or config.source
    lockkey: str | None = None

    if args.hdhr_channel:
        info = hdhomerun.discover(args.hdhr_ip)
        if info is None:
            log.warning(
                "HDHomeRun device unreachable at %s — attempting the stream "
                "anyway (discover() is a preflight check only)", args.hdhr_ip,
            )
        else:
            log.info(
                "HDHomeRun device found: %s (tuners=%s, firmware=%s)",
                info.get("FriendlyName"), info.get("TunerCount"), info.get("FirmwareVersion"),
            )
        live_source = hdhomerun.stream_url(args.hdhr_channel, tuner=args.hdhr_tuner, ip=args.hdhr_ip)
        log.info("pinned HDHomeRun source: %s", live_source)
        try:
            lockkey = hdhomerun.acquire_lock(tuner=args.hdhr_tuner, ip=args.hdhr_ip)
        except Exception:
            log.warning("hdhomerun.acquire_lock raised — continuing without a hard lock", exc_info=True)

    if not replay_source and not live_source:
        ap.error("no audio source: pass --source, --hdhr-channel, --replay, or set 'source' in the event config")

    try:
        if replay_source:
            dets = run_listen(
                config, source=replay_source, model=args.model,
                chunk_seconds=args.chunk, overlap_seconds=args.overlap,
                window_seconds=args.window_seconds, replay=True,
                max_seconds=args.max_seconds,
                vad_filter=args.vad, vad_min_silence_ms=args.vad_min_silence_ms,
            )
        else:
            dets = run_listen(
                config, source=live_source, model=args.model,
                chunk_seconds=args.chunk, overlap_seconds=args.overlap,
                window_seconds=args.window_seconds,
                input_format=args.input_format, max_seconds=args.max_seconds,
                vad_filter=args.vad, vad_min_silence_ms=args.vad_min_silence_ms,
            )
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        if args.hdhr_channel and lockkey:
            try:
                hdhomerun.release_lock(lockkey, tuner=args.hdhr_tuner, ip=args.hdhr_ip)
            except Exception:
                log.warning("hdhomerun.release_lock raised — ignoring", exc_info=True)

    print(f"{len(dets)} detection(s) — see {DETECTIONS_PATH}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
