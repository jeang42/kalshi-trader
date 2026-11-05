"""Tests for mention_cascade_backfill.py — pure functions only, no network."""
from __future__ import annotations

from pm.mention import mention_cascade_backfill as mcb

# --------------------------------------------------------------------------
# characterize_cascade
# --------------------------------------------------------------------------


def test_characterize_cascade_empty_tape():
    c = mcb.characterize_cascade([])
    assert c.cascade_start_ts is None
    assert c.pre_jump_price is None
    assert c.seconds_to_95 is None
    assert c.cascade_volume == 0.0
    assert c.n_trades == 0
    assert c.shape == "no_sweep"


def test_characterize_cascade_no_sweep_already_high():
    tape = [(0, 0.96, 10), (10, 0.97, 10), (20, 0.98, 10)]
    c = mcb.characterize_cascade(tape)
    assert c.cascade_start_ts is None
    assert c.shape == "no_sweep"
    assert c.cascade_volume == 0.0
    assert c.n_trades == 0


def test_characterize_cascade_instant():
    # Jump completes within 2s of cascade start -> "instant".
    tape = [
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.35, 50), (21, 0.97, 300),
    ]
    c = mcb.characterize_cascade(tape)
    assert c.cascade_start_ts == 20
    assert c.seconds_to_95 == 1
    assert c.shape == "instant"
    assert c.cascade_volume == 350  # 50 + 300
    assert c.n_trades == 2
    assert round(c.pre_jump_price, 2) == 0.10


def test_characterize_cascade_fast():
    # Matches cascade_start's own doctest tape: 10s to 0.95 -> "fast".
    tape = [
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.35, 50), (25, 0.72, 80), (30, 0.97, 200),
    ]
    c = mcb.characterize_cascade(tape)
    assert c.cascade_start_ts == 20
    assert c.seconds_to_95 == 10
    assert c.shape == "fast"
    assert c.cascade_volume == 330  # 50 + 80 + 200
    assert c.n_trades == 3
    assert round(c.pre_jump_price, 2) == 0.10


def test_characterize_cascade_slow_cascade():
    # >15s to 0.95 -> "slow_cascade".
    tape = [
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.30, 20), (30, 0.50, 20), (40, 0.70, 20),
        (50, 0.85, 20), (60, 0.96, 50),
    ]
    c = mcb.characterize_cascade(tape)
    assert c.cascade_start_ts == 20
    assert c.seconds_to_95 == 40
    assert c.shape == "slow_cascade"
    assert c.cascade_volume == 20 + 20 + 20 + 20 + 50
    assert c.n_trades == 5


def test_characterize_cascade_pre_utterance_drift_trims_to_lookback():
    # 15 quiet trades before the jump; pre_jump_price should use only the
    # last 10 (median_lookback default), not all 15.
    quiet = [(i * 5, 0.05, 1) for i in range(15)]  # ts 0,5,...,70 at 0.05
    jump_ts = quiet[-1][0] + 5  # 75
    tape = [*quiet, (jump_ts, 0.40, 100), (jump_ts + 5, 0.97, 200)]
    c = mcb.characterize_cascade(tape)
    assert c.cascade_start_ts == jump_ts
    assert c.pre_jump_price == 0.05  # all quiet trades were 0.05 anyway
    assert c.seconds_to_95 == 5
    assert c.shape == "fast"
    assert c.cascade_volume == 300  # 100 + 200
    assert c.n_trades == 2


def test_characterize_cascade_no_sweep_within_window_never_reaches_95():
    # A jump that never reaches sweep_price within sweep_window_s should
    # not qualify as a cascade start at all (cascade_start returns None).
    tape = [
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.40, 50), (200, 0.60, 50),  # far outside the 60s sweep window
    ]
    c = mcb.characterize_cascade(tape)
    assert c.cascade_start_ts is None
    assert c.shape == "no_sweep"


# --------------------------------------------------------------------------
# detect_no_spike
# --------------------------------------------------------------------------


def test_detect_no_spike_empty():
    assert mcb.detect_no_spike([]) is None


def test_detect_no_spike_never_spikes():
    tape = [(0, 0.05, 10), (10, 0.10, 10), (20, 0.08, 5)]
    assert mcb.detect_no_spike(tape) is None


def test_detect_no_spike_spikes_and_falls_back():
    tape = [(0, 0.05, 10), (10, 0.60, 50), (20, 0.15, 10)]
    spike = mcb.detect_no_spike(tape)
    assert spike is not None
    assert spike.peak_price == 0.60
    assert spike.peak_ts == 10
    assert spike.final_price == 0.15


def test_detect_no_spike_spikes_and_stays_high_not_flagged():
    # Peaked >= threshold but the last print is still close to the peak
    # (within fallback_margin) -> not a "fell back" case.
    tape = [(0, 0.05, 10), (10, 0.90, 50), (20, 0.85, 10)]
    assert mcb.detect_no_spike(tape) is None


def test_detect_no_spike_custom_thresholds():
    tape = [(0, 0.05, 10), (10, 0.35, 50), (20, 0.05, 10)]
    # Default spike_threshold=0.5 -> no spike detected.
    assert mcb.detect_no_spike(tape) is None
    # Lowered threshold -> now qualifies.
    spike = mcb.detect_no_spike(tape, spike_threshold=0.3)
    assert spike is not None
    assert spike.peak_price == 0.35


# --------------------------------------------------------------------------
# _word_label
# --------------------------------------------------------------------------


def test_word_label_prefers_yes_sub_title():
    m = {"yes_sub_title": "NATO", "custom_strike": {"Word": "NATO2"}, "ticker": "T-NATO"}
    assert mcb._word_label(m) == "NATO"


def test_word_label_falls_back_to_custom_strike():
    m = {"yes_sub_title": "", "custom_strike": {"Word": "Shipbuilding"}, "ticker": "T-SHIP"}
    assert mcb._word_label(m) == "Shipbuilding"


def test_word_label_falls_back_to_ticker():
    m = {"yes_sub_title": None, "custom_strike": {}, "ticker": "T-XYZ"}
    assert mcb._word_label(m) == "T-XYZ"


# --------------------------------------------------------------------------
# process_event (mocked kc calls — no network)
# --------------------------------------------------------------------------


def _fake_market(ticker, word, result):
    return {
        "ticker": ticker,
        "yes_sub_title": word,
        "result": result,
        "close_time": "2099-01-01T00:00:00Z",
    }


def test_process_event_mocked():
    markets = [
        _fake_market("EV-AAA", "AAA", "yes"),
        _fake_market("EV-BBB", "BBB", "no"),
        _fake_market("EV-CCC", "CCC", ""),  # unresolved
    ]

    yes_tape = [
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.35, 50), (25, 0.72, 80), (30, 0.97, 200),
    ]
    no_spike_tape = [(0, 0.05, 10), (10, 0.60, 50), (20, 0.15, 10)]

    class _FakeDF:
        """Mimics the get_all_trades DataFrame shape via itertuples()."""

        def __init__(self, tape):
            self._tape = tape

        def __len__(self):
            return len(self._tape)

        def itertuples(self):
            class Row:
                def __init__(self, ts_unix, yes_price, count):
                    self.ts_unix = ts_unix
                    self.yes_price = yes_price
                    self.count = count

            return [Row(*t) for t in self._tape]

    def fake_get_markets(event_ticker):
        return markets

    def fake_get_all_trades(ticker, **kwargs):
        if ticker == "EV-AAA":
            return _FakeDF(yes_tape)
        if ticker == "EV-BBB":
            return _FakeDF(no_spike_tape)
        return _FakeDF([])

    report = mcb.process_event(
        "EV", get_markets_fn=fake_get_markets, get_all_trades_fn=fake_get_all_trades,
    )

    assert report.n_markets == 3
    assert report.n_unresolved == 1
    assert len(report.yes_rows) == 1
    assert report.yes_rows[0].ticker == "EV-AAA"
    assert report.yes_rows[0].characterization.shape == "fast"
    assert report.n_no_words == 1
    assert len(report.no_spike_rows) == 1
    assert report.no_spike_rows[0].ticker == "EV-BBB"


def test_process_event_tape_fetch_failure_is_isolated():
    markets = [_fake_market("EV-AAA", "AAA", "yes")]

    def fake_get_markets(event_ticker):
        return markets

    def fake_get_all_trades(ticker, **kwargs):
        raise RuntimeError("network down")

    report = mcb.process_event(
        "EV", get_markets_fn=fake_get_markets, get_all_trades_fn=fake_get_all_trades,
    )
    assert report.n_fetch_failed == 1
    assert report.yes_rows == []


# --------------------------------------------------------------------------
# format_report — smoke test (doesn't crash, sorts by cascade duration)
# --------------------------------------------------------------------------


def test_format_report_smoke():
    fast_char = mcb.characterize_cascade([
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.35, 50), (21, 0.97, 300),
    ])
    slow_char = mcb.characterize_cascade([
        (0, 0.10, 10), (5, 0.11, 10), (10, 0.09, 10),
        (20, 0.30, 20), (30, 0.50, 20), (40, 0.70, 20),
        (50, 0.85, 20), (60, 0.96, 50),
    ])
    report = mcb.EventCascadeReport(
        event_ticker="EV-TEST",
        yes_rows=[
            mcb.YesWordRow(ticker="EV-SLOW", word="SLOW", characterization=slow_char),
            mcb.YesWordRow(ticker="EV-FAST", word="FAST", characterization=fast_char),
        ],
        no_spike_rows=[
            mcb.NoSpikeRow(
                ticker="EV-SPIKE", word="SPIKE",
                spike=mcb.NoWordSpike(peak_price=0.6, peak_ts=10, final_price=0.15),
            ),
        ],
        n_markets=3, n_no_words=1,
    )
    text = mcb.format_report(report)
    assert "EV-TEST" in text
    # Fastest cascade (FAST) should be listed before the slower one.
    assert text.index("FAST") < text.index("SLOW")
    assert "SPIKE" in text
    assert "median seconds_to_95" in text
