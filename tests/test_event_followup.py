"""Offline tests for the shared forward-price follow-up helper."""

from __future__ import annotations

from pm.live import event_followup as ef


def _prices(d):
    return lambda tk: d.get(tk)


class TestCollectSamples:
    def test_emits_one_sample_per_active_follow(self):
        follows = {
            "k1": ef.make_follow("KX-A", 1000.0, beneficiary="NY",
                                 kind="wp_swing", detection_mid=0.6),
        }
        rows = ef.collect_samples(
            follows, now_ts=1030.0,
            price_of=_prices({"KX-A": (0.62, 0.66, 0.64)}))
        assert len(rows) == 1
        r = rows[0]
        assert r["ticker"] == "KX-A" and r["seconds_since"] == 30.0
        assert r["yes_mid"] == 0.64 and r["detection_mid"] == 0.6
        assert "k1" in follows  # follow still active

    def test_expires_old_follow(self):
        follows = {"k1": ef.make_follow("KX-A", 1000.0)}
        rows = ef.collect_samples(
            follows, now_ts=1000.0 + 400, price_of=_prices({"KX-A": (0.5, 0.5, 0.5)}),
            max_follow_s=300.0)
        assert rows == [] and "k1" not in follows   # pruned

    def test_skips_when_no_quote(self):
        follows = {"k1": ef.make_follow("KX-A", 1000.0)}
        rows = ef.collect_samples(follows, now_ts=1030.0,
                                  price_of=_prices({}))   # not quoted
        assert rows == [] and "k1" in follows           # kept, just no sample

    def test_skips_when_mid_none(self):
        follows = {"k1": ef.make_follow("KX-A", 1000.0)}
        rows = ef.collect_samples(
            follows, now_ts=1030.0,
            price_of=_prices({"KX-A": (None, None, None)}))
        assert rows == [] and "k1" in follows

    def test_multiple_samples_over_time(self):
        follows = {"k1": ef.make_follow("KX-A", 1000.0, detection_mid=0.5)}
        px = _prices({"KX-A": (0.5, 0.54, 0.52)})
        s1 = ef.collect_samples(follows, now_ts=1030.0, price_of=px)
        s2 = ef.collect_samples(follows, now_ts=1060.0, price_of=px)
        assert s1[0]["seconds_since"] == 30.0
        assert s2[0]["seconds_since"] == 60.0   # same follow, later sample
