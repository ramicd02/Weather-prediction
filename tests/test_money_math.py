"""Fees, bracket probabilities and event Kelly sizing. Written before the code."""

import math

import pytest

from weatherbot.kalshi.fees import fee_per_contract, maker_fee_e4, taker_fee_e4
from weatherbot.model.brackets import (
    add_settlement_noise,
    bracket_prob,
    pmf_from_cdf,
    yes_if,
)
from weatherbot.model.kelly import Candidate, edge_ok, kelly_fractions, size_event

# --- fees: ceil(0.07 x C x P x (1-P)) dollars, rounded up to the cent -----------------


@pytest.mark.parametrize(
    ("contracts", "price_e4", "fee_e4"),
    [
        (1, 5000, 200),  # 0.0175 -> $0.02
        (100, 5000, 17500),  # 1.75 exactly
        (10, 1000, 700),  # 0.063 -> $0.07
        (1, 100, 100),  # 0.000693 -> $0.01 (any fee rounds up to a cent)
        (5, 0, 0),
        (5, 10000, 0),
        (0, 5000, 0),
    ],
)
def test_taker_fee(contracts, price_e4, fee_e4):
    assert taker_fee_e4(contracts, price_e4) == fee_e4


def test_maker_fee_zero_by_default_and_standard_when_listed():
    assert maker_fee_e4(100, 5000) == 0
    assert maker_fee_e4(100, 5000, multiplier=1) == 4400  # 0.4375 -> $0.44


def test_fee_per_contract_is_the_unrounded_rate():
    assert fee_per_contract(0.5) == pytest.approx(0.0175)
    assert fee_per_contract(0.0) == 0


# --- brackets: whole degrees, inclusive both ends, direction from strike_type ---------

PMF = {73: 0.2, 74: 0.3, 75: 0.3, 76: 0.2}


def test_between_is_inclusive_both_ends():
    assert bracket_prob(PMF, "between", 74, 75) == pytest.approx(0.6)
    assert yes_if("between", 74, 75, 74) and yes_if("between", 74, 75, 75)


def test_tails_follow_strike_type():
    assert bracket_prob(PMF, "greater", 75, None) == pytest.approx(0.2)
    assert bracket_prob(PMF, "greater_or_equal", 75, None) == pytest.approx(0.5)
    assert bracket_prob(PMF, "less", None, 74) == pytest.approx(0.2)
    assert bracket_prob(PMF, "less_or_equal", None, 74) == pytest.approx(0.5)
    assert bracket_prob(PMF, "functional", 1, 2) is None


def test_event_brackets_sum_to_one():
    # A KXHIGH-style ladder: below 73, 73-74, 75-76, above 76.
    ladder = [("less", None, 73), ("between", 73, 74), ("between", 75, 76), ("greater", 76, None)]
    assert sum(bracket_prob(PMF, *b) for b in ladder) == pytest.approx(1.0)


def _normal_cdf(mu, sd):
    return lambda x: 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))


def test_pmf_from_cdf_uses_half_degree_edges():
    pmf = pmf_from_cdf(_normal_cdf(70.0, 2.0))
    assert sum(pmf.values()) == pytest.approx(1.0)
    # P(high == 70) is the mass between 69.5 and 70.5.
    assert pmf[70] == pytest.approx(0.1974, abs=1e-3)
    assert pmf[68] == pytest.approx(pmf[72])


def test_settlement_noise_moves_a_little_mass_to_neighbours():
    pmf = add_settlement_noise({70: 1.0}, 0.001)
    assert pmf[70] == pytest.approx(0.999)
    assert pmf[69] == pytest.approx(0.0005) and pmf[71] == pytest.approx(0.0005)
    assert sum(pmf.values()) == pytest.approx(1.0)


# --- edge rule -----------------------------------------------------------------------


def test_edge_rule_needs_absolute_and_relative_edge():
    assert edge_ok(prob=0.60, price=0.50, fee=0.0175, min_edge=0.05, min_rel=0.25) is False
    assert edge_ok(prob=0.70, price=0.50, fee=0.0175, min_edge=0.05, min_rel=0.25) is True
    # A 4c longshot "worth" 10c passes the 5-point test only if 25% relative also holds.
    assert edge_ok(prob=0.10, price=0.04, fee=0.003, min_edge=0.05, min_rel=0.25) is True
    assert edge_ok(prob=0.08, price=0.04, fee=0.003, min_edge=0.05, min_rel=0.25) is False


# --- Kelly ---------------------------------------------------------------------------

OUTCOMES = {60: 0.3, 61: 0.3, 62: 0.4}


def test_single_yes_bet_matches_closed_form():
    # p = 0.6 to win at price 0.5, no fee: f* = (p - a) / (1 - a) = 0.2.
    c = Candidate("A", "yes", 0.5, 0.0, frozenset({61, 62}))
    [f] = kelly_fractions([c], {60: 0.4, 61: 0.3, 62: 0.3})
    assert f == pytest.approx(0.2, abs=1e-4)


def test_single_no_bet_matches_closed_form():
    # YES wins only at 60 (p = 0.3), so NO wins with 0.7 at price 0.5: f* = 0.4.
    c = Candidate("A", "no", 0.5, 0.0, frozenset({60}))  # wins = where YES pays
    [f] = kelly_fractions([c], {60: 0.3, 61: 0.4, 62: 0.3})
    assert f == pytest.approx(0.4, abs=1e-4)


def test_no_edge_means_no_bet():
    c = Candidate("A", "yes", 0.7, 0.0, frozenset({61, 62}))
    assert kelly_fractions([c], OUTCOMES) == [0.0]


def test_exclusive_brackets_are_sized_together():
    # Two brackets at 20c, each 30% likely. Alone, each gets (0.3-0.2)/0.8 = 1/8.
    # Together one partly hedges the other, and the joint optimum is 1/6 each:
    # maximise 0.6*log(1+3f) + 0.4*log(1-2f)  ->  f = 1/6.
    a = Candidate("A", "yes", 0.2, 0.0, frozenset({60}))
    b = Candidate("B", "yes", 0.2, 0.0, frozenset({61}))
    assert kelly_fractions([a], OUTCOMES)[0] == pytest.approx(0.125, abs=1e-4)
    together = kelly_fractions([a, b], OUTCOMES)
    assert together == pytest.approx([1 / 6, 1 / 6], abs=1e-4)


def test_yes_and_no_on_the_same_event_never_lose_everything():
    a = Candidate("A", "yes", 0.2, 0.0, frozenset({60}))
    c = Candidate("C", "no", 0.3, 0.0, frozenset({62}))  # NO on 62 wins at 60 or 61
    fs = kelly_fractions([a, c], OUTCOMES)
    for k in OUTCOMES:
        wealth = 1 + sum(
            f * ((k in x.wins) == (x.side == "yes")) / x.price - f
            for f, x in zip(fs, [a, c], strict=True)
        )
        assert wealth > 0


def test_fees_shrink_the_bet():
    no_fee = Candidate("A", "yes", 0.5, 0.0, frozenset({61, 62}))
    fee = Candidate("A", "yes", 0.5, 0.0175, frozenset({61, 62}))
    assert kelly_fractions([fee], OUTCOMES)[0] < kelly_fractions([no_fee], OUTCOMES)[0]


def test_size_event_applies_fraction_cap_and_whole_contracts():
    c = Candidate("A", "yes", 0.5, 0.0, frozenset({61, 62}))  # full Kelly 0.2 when p=0.6
    probs = {60: 0.4, 61: 0.3, 62: 0.3}
    # Quarter-Kelly: 5% of $1,000 = $50 -> 100 contracts at 50c, but the 2% city cap
    # limits cost to $20 -> 40 contracts; max 50 per trade doesn't bind.
    [n] = size_event([c], probs, bankroll=1000, kelly_fraction=0.25, cap=0.02, max_contracts=50)
    assert n == 40
    [n] = size_event([c], probs, bankroll=1000, kelly_fraction=0.25, cap=0.10, max_contracts=50)
    assert n == 50  # 100 contracts wanted, capped at 50
    [n] = size_event([c], probs, bankroll=10, kelly_fraction=0.25, cap=0.02, max_contracts=50)
    assert n == 0  # $0.20 budget can't buy one 50c contract
