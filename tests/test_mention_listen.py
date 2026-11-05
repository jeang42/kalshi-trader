"""Unit tests for mention_listen.py — pure logic only (no whisper/ffmpeg/network)."""
import json

import pytest

from pm.mention import mention_listen as ml

# ─── normalize_text / compile_pattern ──────────────────────────────────────

def test_normalize_text_strips_punctuation_and_collapses_space():
    assert ml.normalize_text("Golden Dome's ready!") == "golden domes ready"
    assert ml.normalize_text("  multiple   spaces  ") == "multiple spaces"
    assert ml.normalize_text("") == ""


def test_compile_pattern_matches_case_insensitive_multiword():
    rx = ml.compile_pattern("Golden Dome")
    assert rx.search("we discussed the golden dome plan")
    assert not rx.search("golden domes are cool")  # word-boundary: "dome" != "domes"


def test_compile_pattern_apostrophe_matches_normalized_plural():
    rx = ml.compile_pattern("Tariff's")
    assert rx.search("he said tariffs today")


def test_compile_pattern_word_boundary_no_substring_match():
    rx = ml.compile_pattern("art")
    assert not rx.search("he drove a cart")
    assert rx.search("that is fine art indeed")


# ─── TranscriptWindow: multi-word match spanning a chunk boundary ──────────

def test_window_join_and_offset_lookup():
    w = ml.TranscriptWindow(window_seconds=15.0)
    # Simulate chunk 1 landing "golden", chunk 2 landing "dome" a moment later.
    w.add_words([ml.WordEntry("golden", 100.0)])
    w.add_words([ml.WordEntry("dome", 100.7)])
    assert w.text() == "golden dome"
    assert w.ts_for_offset(0) == 100.0    # "golden" starts at offset 0
    assert w.ts_for_offset(7) == 100.7    # "dome" starts right after the space


def test_window_matches_phrase_spanning_simulated_chunk_boundary():
    w = ml.TranscriptWindow(window_seconds=15.0)
    rx = ml.compile_pattern("Golden Dome")
    # First chunk only has "golden" — no match yet.
    w.add_words([ml.WordEntry("golden", 200.0)])
    assert not rx.search(w.text())
    # Second chunk (next ffmpeg read) completes the phrase.
    w.add_words([ml.WordEntry("dome", 200.6)])
    m = rx.search(w.text())
    assert m is not None
    assert w.ts_for_offset(m.start()) == 200.0


def test_window_prunes_old_words_outside_window():
    w = ml.TranscriptWindow(window_seconds=5.0)
    w.add_words([ml.WordEntry("old", 0.0)])
    w.add_words([ml.WordEntry("new", 10.0)])
    assert "old" not in w.text()
    assert w.text() == "new"


# ─── MentionDetector: once-per-ticker dedup ────────────────────────────────

def _config(tmp_path):
    return ml.EventConfig(
        event_ticker="KXFTNMENTION-26JUL19",
        source=None,
        markets=[
            ml.MarketWatch(ticker="KXFTNMENTION-26JUL19-GOLD", patterns=["Golden Dome"]),
            ml.MarketWatch(ticker="KXFTNMENTION-26JUL19-TAR", patterns=["Tariff"]),
        ],
    )


def test_detector_fires_once_per_ticker(tmp_path):
    cfg = _config(tmp_path)
    log_path = tmp_path / "detections.jsonl"
    seen = []
    det = ml.MentionDetector(
        cfg, model="small", source="test-stream",
        on_detection=seen.append, detections_path=log_path,
    )

    w = ml.TranscriptWindow()
    w.add_words([ml.WordEntry("golden", 10.0), ml.WordEntry("dome", 10.5)])

    fired1 = det.check(w)
    assert len(fired1) == 1
    assert fired1[0]["ticker"] == "KXFTNMENTION-26JUL19-GOLD"
    assert fired1[0]["matched_text"] == "golden dome"
    assert fired1[0]["event_ticker"] == "KXFTNMENTION-26JUL19"

    # Same window scanned again (e.g. next chunk still contains the phrase)
    # must NOT re-fire the same ticker.
    fired2 = det.check(w)
    assert fired2 == []

    # Callback + JSONL log both got exactly one record.
    assert len(seen) == 1
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["ticker"] == "KXFTNMENTION-26JUL19-GOLD"

    assert not det.all_detected  # TAR still pending


# ─── MentionDetector: COUNT markets (fire only at the Nth occurrence) ──────

def _count_config():
    return ml.EventConfig(
        event_ticker="KXFTNMENTION-26JUL19",
        source=None,
        markets=[
            ml.MarketWatch(ticker="KXFTNMENTION-26JUL19-TRUM", patterns=["trump"], count=3),
        ],
    )


def test_count_market_fires_only_on_nth_occurrence(tmp_path):
    cfg = _count_config()
    det = ml.MentionDetector(cfg, model="small", source="t", detections_path=tmp_path / "d.jsonl")
    w = ml.TranscriptWindow(window_seconds=100.0)

    # Instance 1.
    w.add_words([ml.WordEntry("trump", 1.0)])
    assert det.check(w) == []
    # Instance 2.
    w.add_words([ml.WordEntry("said", 2.0), ml.WordEntry("trump", 3.0)])
    assert det.check(w) == []
    # Instance 3 — fires now, occurrence=3.
    w.add_words([ml.WordEntry("trump", 4.0)])
    fired = det.check(w)
    assert len(fired) == 1
    assert fired[0]["ticker"] == "KXFTNMENTION-26JUL19-TRUM"
    assert fired[0]["occurrence"] == 3
    assert det.all_detected


def test_count_market_sliding_window_does_not_double_count(tmp_path):
    """The same audio instance staying in the (unpruned) rolling window
    across multiple check() calls must not be recounted — only a genuinely
    NEW word-index match advances the occurrence count."""
    cfg = _count_config()
    det = ml.MentionDetector(cfg, model="small", source="t", detections_path=tmp_path / "d.jsonl")
    w = ml.TranscriptWindow(window_seconds=100.0)

    w.add_words([ml.WordEntry("trump", 1.0)])
    det.check(w)
    # Same window scanned again (e.g. next chunk overlaps, no new words) —
    # "trump" at ts=1.0 is still the same instance, must not count twice.
    det.check(w)
    det.check(w)
    w.add_words([ml.WordEntry("trump", 2.0)])
    det.check(w)
    det.check(w)  # re-scan again — still just 2 distinct instances
    assert not det.all_detected

    w.add_words([ml.WordEntry("trump", 3.0)])
    fired = det.check(w)
    assert len(fired) == 1
    assert fired[0]["occurrence"] == 3


def test_count_defaults_to_one():
    mw = ml.MarketWatch(ticker="T", patterns=["golden dome"])
    assert mw.count == 1


def test_load_event_config_reads_count(tmp_path):
    cfg_path = tmp_path / "event.json"
    cfg_path.write_text(json.dumps({
        "event_ticker": "E",
        "source": None,
        "markets": [{"ticker": "T-TRUM", "patterns": ["trump"], "count": 3}],
    }))
    cfg = ml.load_event_config(cfg_path)
    assert cfg.markets[0].count == 3


# ─── single_speaker guardrail ───────────────────────────────────────────────

def test_speaker_warning_none_when_single_speaker():
    cfg = ml.EventConfig(event_ticker="E", source=None, markets=[], single_speaker=True)
    assert ml._speaker_warning(cfg) is None


def test_speaker_warning_present_by_default():
    cfg = ml.EventConfig(event_ticker="E", source=None, markets=[])
    assert cfg.single_speaker is False
    warning = ml._speaker_warning(cfg)
    assert warning is not None
    assert "speaker-blind" in warning
    assert "do NOT trade" in warning


def test_load_event_config_reads_single_speaker(tmp_path):
    cfg_path = tmp_path / "event.json"
    cfg_path.write_text(json.dumps({
        "event_ticker": "E",
        "source": None,
        "single_speaker": True,
        "markets": [{"ticker": "T-A", "patterns": ["Golden Dome"]}],
    }))
    cfg = ml.load_event_config(cfg_path)
    assert cfg.single_speaker is True


def test_load_event_config_single_speaker_defaults_false(tmp_path):
    cfg_path = tmp_path / "event.json"
    cfg_path.write_text(json.dumps({
        "event_ticker": "E",
        "source": None,
        "markets": [{"ticker": "T-A", "patterns": ["Golden Dome"]}],
    }))
    cfg = ml.load_event_config(cfg_path)
    assert cfg.single_speaker is False


def test_detector_all_detected_flag(tmp_path):
    cfg = _config(tmp_path)
    det = ml.MentionDetector(cfg, model="small", source="t", detections_path=tmp_path / "d.jsonl")
    w = ml.TranscriptWindow()
    w.add_words([
        ml.WordEntry("golden", 1.0), ml.WordEntry("dome", 1.5),
        ml.WordEntry("tariff", 2.0),
    ])
    det.check(w)
    assert det.all_detected


# ─── extract_patterns / config builder question parsing ───────────────────

def test_extract_patterns_double_quoted():
    r = ml.extract_patterns('Will "Golden Dome" be said during the address?')
    assert r["patterns"] == ["Golden Dome", "Golden Domes", "Golden Dome's"]
    assert r["comment"] is None


def test_extract_patterns_single_quoted():
    r = ml.extract_patterns("Will Trump say 'Witch Hunt' tonight?")
    assert r["patterns"] == ["Witch Hunt", "Witch Hunts", "Witch Hunt's"]
    assert r["comment"] is None


def test_extract_patterns_quoted_in_title_short_word():
    r = ml.extract_patterns('Will Trump say "Tariff" during the speech?')
    assert r["patterns"] == ["Tariff", "Tariffs", "Tariff's"]
    assert r["comment"] is None


def test_extract_patterns_no_quote_falls_back_to_sub_title_uncertain():
    r = ml.extract_patterns(
        "Will Zohran Mamdani win the mayoral primary?", "Zohran Mamdani",
    )
    assert r["patterns"] == ["Zohran Mamdani", "Zohran Mamdanis", "Zohran Mamdani's"]
    assert r["comment"] is not None
    assert "uncertain" in r["comment"].lower()


def test_extract_patterns_nothing_extractable_flags_for_human():
    r = ml.extract_patterns("Will the show mention a surprise guest?", "Yes")
    assert r["patterns"] == []
    assert r["comment"] is not None
    assert "fill in by hand" in r["comment"]


def test_variants_no_double_s_suffix():
    assert ml._variants("Chaos") == ["Chaos", "Chaos's"]
    assert ml._variants("Tariff") == ["Tariff", "Tariffs", "Tariff's"]


# ─── build_config: mocked kalshi_client, no network ────────────────────────

def test_build_config_mocked(monkeypatch):
    from pm.venue import kalshi_client as kc

    fake_response = {
        "markets": [
            {
                "ticker": "KXFTNMENTION-26JUL19-GOLD",
                "title": 'Will "Golden Dome" be said during the show?',
                "yes_sub_title": "Golden Dome",
            },
            {
                "ticker": "KXFTNMENTION-26JUL19-GUEST",
                "title": "Will there be a surprise guest mentioned?",
                "yes_sub_title": "Yes",
            },
        ]
    }

    def fake_get(path, params=None, retries=3):
        assert path == "markets"
        assert params["event_ticker"] == "KXFTNMENTION-26JUL19"
        return fake_response

    monkeypatch.setattr(kc, "_get", fake_get)

    cfg = ml.build_config("KXFTNMENTION-26JUL19")
    assert cfg["event_ticker"] == "KXFTNMENTION-26JUL19"
    assert cfg["source"] is None
    assert cfg["single_speaker"] is False
    assert isinstance(cfg["single_speaker_comment"], str) and cfg["single_speaker_comment"]
    assert len(cfg["markets"]) == 2

    gold = cfg["markets"][0]
    assert gold["ticker"] == "KXFTNMENTION-26JUL19-GOLD"
    assert gold["patterns"] == ["Golden Dome", "Golden Domes", "Golden Dome's"]
    assert "comment" not in gold

    guest = cfg["markets"][1]
    assert guest["patterns"] == []
    assert "comment" in guest


# ─── load_event_config round-trip ──────────────────────────────────────────

def test_load_event_config_round_trip(tmp_path):
    cfg_path = tmp_path / "event.json"
    cfg_path.write_text(json.dumps({
        "event_ticker": "KXFTNMENTION-26JUL19",
        "source": "https://example.com/stream.m3u8",
        "markets": [
            {"ticker": "T-A", "patterns": ["Golden Dome"]},
            {"ticker": "T-B", "patterns": ["Tariff"], "comment": "verify"},
        ],
    }))
    cfg = ml.load_event_config(cfg_path)
    assert cfg.event_ticker == "KXFTNMENTION-26JUL19"
    assert cfg.source == "https://example.com/stream.m3u8"
    assert len(cfg.markets) == 2
    assert cfg.markets[0].ticker == "T-A"
    assert len(cfg.markets[0].compiled) == 1
    assert cfg.markets[0].compiled[0].search("say golden dome now")


# ─── _words_to_entries: TranscriptSegment -> WordEntry with absolute ts ────

def test_words_to_entries_absolute_timestamp():
    from pm.mention.audio_transcribe import TranscriptSegment

    seg = TranscriptSegment(
        text="golden dome",
        start_time=3.0,
        end_time=6.0,
        chunk_index=1,
        confidence=0.9,
        is_speech=True,
        words=[
            {"word": " Golden", "start": 0.1, "end": 0.4},
            {"word": " Dome", "start": 0.5, "end": 0.8},
        ],
        stream_start_wall=1000.0,
    )
    entries = ml._words_to_entries(seg)
    assert [e.word for e in entries] == ["golden", "dome"]
    assert entries[0].ts == pytest.approx(1000.0 + 3.0 + 0.1)
    assert entries[1].ts == pytest.approx(1000.0 + 3.0 + 0.5)


def test_words_to_entries_empty_when_no_words():
    from pm.mention.audio_transcribe import TranscriptSegment

    seg = TranscriptSegment(
        text="hello", start_time=0.0, end_time=3.0, chunk_index=0,
        confidence=0.5, is_speech=True, words=None, stream_start_wall=0.0,
    )
    assert ml._words_to_entries(seg) == []
