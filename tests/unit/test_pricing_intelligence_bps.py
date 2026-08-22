"""Basis-point arithmetic must be exact: these figures reach pricing decisions."""

import pytest

from balance_pricing_engine.pricing_intelligence.bps import (
    bps_to_percent,
    percent_to_bps,
    rate_change_bps,
    spread_bps,
)


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (4.99, 4.79, -20.0),
        (4.79, 4.89, 10.0),
        (4.79, 4.79, 0.0),
        (5.0, 4.0, -100.0),
        (4.89, 8.49, 360.0),
        (4.795, 4.79, -0.5),
        (0.05, 0.10, 5.0),
    ],
)
def test_rate_change_bps_is_exact(previous, current, expected):
    assert rate_change_bps(previous, current) == expected


def test_rate_change_bps_has_no_float_residue():
    """The naive float subtraction this replaces produces -19.999999999999996."""
    naive = (4.79 - 4.99) * 100
    assert naive != -20.0

    exact = rate_change_bps(4.99, 4.79)
    assert exact == -20.0
    assert repr(exact) == "-20.0"


def test_rate_change_bps_accumulates_without_drift():
    """A long chain of moves must sum to the same figure as the endpoints."""
    steps = [5.00, 4.95, 4.89, 4.79, 4.74, 4.69]
    total = sum(rate_change_bps(a, b) for a, b in zip(steps, steps[1:]))
    assert total == rate_change_bps(steps[0], steps[-1]) == -31.0


@pytest.mark.parametrize(
    ("percent", "expected"), [(0.20, 20.0), (1.0, 100.0), (-0.05, -5.0), (0.0, 0.0)]
)
def test_percent_to_bps(percent, expected):
    assert percent_to_bps(percent) == expected


@pytest.mark.parametrize(("basis_points", "expected"), [(20, 0.2), (-5, -0.05), (100, 1.0)])
def test_bps_to_percent(basis_points, expected):
    assert bps_to_percent(basis_points) == expected


def test_spread_bps_is_non_negative_regardless_of_argument_order():
    assert spread_bps(4.79, 4.89) == 10.0
    assert spread_bps(4.89, 4.79) == 10.0


def test_bps_round_trips_through_percent():
    for basis_points in (5, 10, 20, 37, 125):
        assert percent_to_bps(bps_to_percent(basis_points)) == float(basis_points)
