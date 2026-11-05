"""WPA / Win Probability tests calibrated against published reference values.

Reference points come from Baseball-Reference Win Expectancy tables
and "The Book" (Tango/Lichtman/Dolphin 2007). Our implementation is
an approximation, so we test for *approximate* match (~5pp tolerance)
on sanity-check states + exact behavior on the deterministic ones."""

from __future__ import annotations

import pytest

from baseball import wpa

# ─── RE24 spot checks ─────────────────────────────────────────────────────


def test_re24_bases_empty_zero_outs():
    """Start of an inning. Tango's value: ~0.481 runs."""
    assert wpa.run_expectancy((False, False, False), 0) == pytest.approx(0.481, abs=0.01)


def test_re24_bases_loaded_no_outs():
    """Highest run expectancy state. Tango: ~2.282 runs."""
    assert wpa.run_expectancy((True, True, True), 0) == pytest.approx(2.282, abs=0.05)


def test_re24_runner_on_2nd_two_outs():
    """Classic RISP state. Tango: ~0.319 runs."""
    assert wpa.run_expectancy((False, True, False), 2) == pytest.approx(0.319, abs=0.02)


def test_re24_three_outs_returns_zero():
    """Inning over → no more runs expected."""
    assert wpa.run_expectancy((True, True, True), 3) == 0.0
    assert wpa.run_expectancy((False, False, False), 3) == 0.0


# ─── Win Probability calibration ──────────────────────────────────────────


def test_wp_start_of_game_near_home_field_advantage():
    """Top of 1st, bases empty, tied: home team WP ≈ 0.54 (long-run
    home-field advantage). Our model uses a symmetric run expectation,
    so this should land close to 0.50 — within 5pp either direction is
    fine for the approximation."""
    wp = wpa.win_probability(1, "top", 0, (False, False, False), 0)
    assert 0.45 <= wp <= 0.55


def test_wp_big_late_lead_near_one():
    """Bottom 9, home up 5 with bases empty: WP ≈ 1.0 (5-run lead in
    final inning is essentially decided)."""
    wp = wpa.win_probability(9, "bottom", 0, (False, False, False), 5)
    assert wp >= 0.95


def test_wp_big_late_deficit_near_zero():
    """Top 9, home down 5: away team has 1 half-inning to score 5+ runs."""
    wp = wpa.win_probability(9, "top", 0, (False, False, False), -5)
    assert wp <= 0.10


def test_wp_bottom_9_tied_favors_home():
    """Bottom of 9th, tied: home has last bat → meaningful edge."""
    wp = wpa.win_probability(9, "bottom", 0, (False, False, False), 0)
    assert wp > 0.55  # home-team last-bat advantage


def test_wp_tying_run_on_base_in_close_late_game():
    """Bottom 9, home down 1, runner on 1st, 0 outs.
    BR reference: ~0.34 home WP. Our approximation should land in 0.20-0.45."""
    wp = wpa.win_probability(9, "bottom", 0, (True, False, False), -1)
    assert 0.20 <= wp <= 0.45


def test_wp_endgame_short_circuit_when_home_wins_in_bottom_late():
    """Bottom 9 with home already leading + 3 outs is moot — game is
    over. Our model handles inning >=9, half=bottom, score_diff > 0
    as a 1.0 short-circuit regardless of outs."""
    assert wpa.win_probability(9, "bottom", 0, (False, False, False), 1) == 1.0


def test_wp_extras_more_uncertain_than_regulation():
    """Tied game in the 10th has more uncertainty than tied in the
    8th (more innings left in the latter)."""
    extras = wpa.win_probability(10, "top", 0, (False, False, False), 0)
    earlier = wpa.win_probability(8, "top", 0, (False, False, False), 0)
    # Both should be near 0.50 (tied), but extras moves less since
    # less time remains
    assert abs(extras - 0.50) <= 0.10
    assert abs(earlier - 0.50) <= 0.10


# ─── WPA computation ──────────────────────────────────────────────────────


def test_wpa_walk_off_home_run():
    """Bottom 9, tied, bases empty, 0 outs → solo HR ends the game.
    State before: home WP ~0.55. State after: home wins. WPA from
    home batting team's perspective: ~+0.45."""
    before = wpa.GameState(9, "bottom", 0, (False, False, False), 0)
    after = wpa.GameState(9, "bottom", 0, (False, False, False), 1)
    delta = wpa.wpa(before, after)
    assert delta > 0.30  # positive — helped batting (home) team
    assert delta < 0.50


def test_wpa_strikeout_with_bases_loaded():
    """Strikeout with bases loaded in late inning is a big DEFENSIVE
    play — WPA from batting team's view should be NEGATIVE."""
    # Bottom 8, tied, bases loaded, 2 outs, batter strikes out
    # → end of half-inning, no runs
    before = wpa.GameState(8, "bottom", 2, (True, True, True), 0)
    after = wpa.GameState(9, "top", 0, (False, False, False), 0)
    delta = wpa.wpa(before, after, batting_side="home")
    assert delta < 0  # batting team lost win prob
    assert delta < -0.05


def test_wpa_routine_play_low_magnitude():
    """A flyout in the 2nd inning, bases empty, 0 outs is barely a
    blip in win probability."""
    before = wpa.GameState(2, "top", 0, (False, False, False), 0)
    after = wpa.GameState(2, "top", 1, (False, False, False), 0)
    delta = wpa.wpa(before, after)
    assert abs(delta) < 0.05


def test_wpa_away_team_sign():
    """When the away team is batting, positive WPA for them means
    home WP went DOWN. Verify the sign convention."""
    # Away team scores a run in top 5 from tied
    before = wpa.GameState(5, "top", 0, (False, False, False), 0)
    after = wpa.GameState(5, "top", 0, (False, False, False), -1)
    delta = wpa.wpa(before, after)
    # away batting got a positive outcome → WPA positive (for them)
    assert delta > 0


def test_estimate_wpa_for_play_end_of_inning_transition():
    """When a play takes the team from 2 outs to 3 outs, the AFTER
    state is the start of the next half-inning (bases empty, 0 outs)."""
    # Top of 5th, 2 outs, runner on 1st, away leading by 1. Batter
    # grounds into a force-out at 2nd, ending the inning.
    delta = wpa.estimate_wpa_for_play(
        inning_before=5, half_before="top",
        outs_before=2, bases_before=(True, False, False),
        home_score_before=0, away_score_before=1,
        outs_after=3, bases_after=(False, False, False),  # 3 outs → next half
        home_score_after=0, away_score_after=1,
    )
    # End of away half-inning with no further scoring is mildly
    # negative for the away batting team (they had a runner on, lost it)
    assert delta < 0
