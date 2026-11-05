"""Tests for kelly_sizing() in polymarket_client.

All tests are pure math — no API calls, no file I/O.
"""

import pytest

from pm.venue.polymarket_client import kelly_sizing

# ---------------------------------------------------------------------------
# Direction and edge
# ---------------------------------------------------------------------------

def test_yes_edge_direction():
    r = kelly_sizing(market_price=0.30, your_probability=0.50)
    assert r["direction"] == "YES"
    assert pytest.approx(r["edge"], abs=1e-9) == 0.20

def test_no_edge_direction():
    r = kelly_sizing(market_price=0.70, your_probability=0.50)
    assert r["direction"] == "NO"
    assert pytest.approx(r["edge"], abs=1e-9) == -0.20

def test_zero_edge():
    r = kelly_sizing(market_price=0.50, your_probability=0.50)
    assert r["edge"] == pytest.approx(0.0)
    assert r["kelly_fraction"] == 0.0
    assert r["half_kelly_fraction"] == 0.0

def test_edge_sign_matches_direction():
    """Positive edge ↔ YES, negative edge ↔ NO."""
    yes = kelly_sizing(0.20, 0.60)
    assert yes["edge"] > 0 and yes["direction"] == "YES"

    no = kelly_sizing(0.80, 0.40)
    assert no["edge"] < 0 and no["direction"] == "NO"


# ---------------------------------------------------------------------------
# Kelly fraction values
# ---------------------------------------------------------------------------

def test_yes_kelly_formula():
    # market=0.30, p=0.50  →  fraction = (0.50 - 0.30) / (1 - 0.30) = 0.20/0.70
    r = kelly_sizing(0.30, 0.50)
    assert r["kelly_fraction"] == pytest.approx(0.20 / 0.70, rel=1e-6)

def test_no_kelly_formula():
    # market=0.70, p=0.50  →  fraction = (0.70 - 0.50) / 0.70 = 0.20/0.70
    r = kelly_sizing(0.70, 0.50)
    assert r["kelly_fraction"] == pytest.approx(0.20 / 0.70, rel=1e-6)

def test_symmetry():
    """Betting YES on m=0.30, p=0.50 gives the same fraction as betting NO on m=0.70, p=0.50."""
    yes = kelly_sizing(0.30, 0.50)
    no = kelly_sizing(0.70, 0.50)
    assert yes["kelly_fraction"] == pytest.approx(no["kelly_fraction"], rel=1e-6)

def test_half_kelly_is_half():
    r = kelly_sizing(0.30, 0.50)
    assert r["half_kelly_fraction"] == pytest.approx(r["kelly_fraction"] / 2, rel=1e-9)


# ---------------------------------------------------------------------------
# JSON-serializability of the result (no lambdas)
# ---------------------------------------------------------------------------

def test_result_is_json_serializable():
    """The dict returned by kelly_sizing must be JSON-serializable so callers
    can persist, log, or transmit it without special handling."""
    import json
    r = kelly_sizing(0.30, 0.50)
    blob = json.dumps(r)
    decoded = json.loads(blob)
    assert decoded["direction"] == "YES"
    assert decoded["kelly_fraction"] == pytest.approx(r["kelly_fraction"])

def test_no_callables_in_result():
    r = kelly_sizing(0.30, 0.50)
    for k, v in r.items():
        assert not callable(v), f"{k} should be a plain value, got callable"

def test_dollar_amounts_via_simple_multiplication():
    """The recommended pattern: bankroll * fraction. Verify linearity."""
    r = kelly_sizing(0.30, 0.50)
    assert 1000 * r["kelly_fraction"] == pytest.approx(285.71, rel=1e-3)
    assert 2000 * r["kelly_fraction"] == pytest.approx(2 * 1000 * r["kelly_fraction"], rel=1e-9)
    assert 1000 * r["half_kelly_fraction"] == pytest.approx(
        500 * r["kelly_fraction"], rel=1e-9
    )


# ---------------------------------------------------------------------------
# Expected value
# ---------------------------------------------------------------------------

def test_positive_ev_when_yes_edge():
    r = kelly_sizing(0.30, 0.50)
    assert r["expected_value"] > 0

def test_positive_ev_when_no_edge():
    r = kelly_sizing(0.70, 0.50)
    assert r["expected_value"] > 0

def test_zero_ev_at_zero_edge():
    r = kelly_sizing(0.50, 0.50)
    assert r["expected_value"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Boundary / clamping
# ---------------------------------------------------------------------------

def test_fraction_never_negative():
    """Even if raw formula would go negative, clamped to 0."""
    r = kelly_sizing(0.90, 0.50)
    assert r["kelly_fraction"] >= 0.0

def test_fraction_never_exceeds_one():
    """Full bankroll is the absolute ceiling."""
    r = kelly_sizing(0.01, 0.99)
    assert r["kelly_fraction"] <= 1.0

def test_near_zero_market_price():
    """Market at 1% — large YES bet but doesn't blow up."""
    r = kelly_sizing(0.01, 0.50)
    assert r["direction"] == "YES"
    assert 0.0 < r["kelly_fraction"] <= 1.0

def test_near_one_market_price():
    """Market at 99% — large NO bet but doesn't blow up."""
    r = kelly_sizing(0.99, 0.50)
    assert r["direction"] == "NO"
    assert 0.0 < r["kelly_fraction"] <= 1.0

@pytest.mark.parametrize("market,yours", [
    (0.10, 0.20),
    (0.25, 0.60),
    (0.50, 0.80),
    (0.75, 0.40),
    (0.90, 0.10),
])
def test_fraction_in_unit_interval(market, yours):
    r = kelly_sizing(market, yours)
    assert 0.0 <= r["kelly_fraction"] <= 1.0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_price", [-0.1, 1.1, 2.0, -1.0])
def test_market_price_out_of_range_raises(bad_price):
    with pytest.raises(ValueError, match="market_price"):
        kelly_sizing(market_price=bad_price, your_probability=0.5)

@pytest.mark.parametrize("bad_p", [-0.1, 1.1, 2.0, -1.0])
def test_your_probability_out_of_range_raises(bad_p):
    with pytest.raises(ValueError, match="your_probability"):
        kelly_sizing(market_price=0.5, your_probability=bad_p)

def test_boundary_zero_market_accepted():
    """m=0 is on the boundary and should not raise."""
    r = kelly_sizing(market_price=0.0, your_probability=0.5)
    assert r["direction"] == "YES"

def test_boundary_one_market_accepted():
    """m=1 is on the boundary and should not raise."""
    r = kelly_sizing(market_price=1.0, your_probability=0.5)
    assert r["direction"] == "NO"
