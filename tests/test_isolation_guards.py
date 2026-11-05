"""The two isolation invariants, proven rather than assumed (2026-08-17)."""

from __future__ import annotations

import pytest
import requests


def test_tests_cannot_reach_the_network():
    with pytest.raises((RuntimeError, requests.ConnectionError)) as ei:
        requests.get("http://127.0.0.1:9", timeout=2)
    assert "real network connection" in str(ei.value)


def test_orders_log_is_redirected_away_from_data(tmp_path):
    from pm.venue import kalshi_orders as ko
    p = ko._orders_log_path()
    assert "data/shared" not in str(p), p
    ko._audit("isolation_probe", {})
    assert p.exists()
