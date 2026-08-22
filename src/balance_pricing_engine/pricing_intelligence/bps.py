"""Exact basis-point arithmetic for advertised rates.

Advertised rates are quoted in percent (``4.79`` means 4.79% p.a.). Binary floats
cannot represent those decimals exactly, so naive subtraction produces artefacts
such as ``-19.999999999999996`` where the market moved exactly -20 bps. Every
basis-point figure that reaches a change event, a market snapshot, or an alert is
computed here on :class:`~decimal.Decimal` so the arithmetic is exact.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Union

Number = Union[int, float, str, Decimal]

BPS_PER_PERCENT = Decimal(100)

# Advertised rates are published to at most three decimal places of a percent
# (0.1 bps). Quantising to 1e-4 bps keeps exactness while collapsing any residue
# from an upstream float round-trip.
_BPS_QUANTUM = Decimal("0.0001")


def to_decimal(value: Number) -> Decimal:
    """Convert a rate-like value to :class:`Decimal` without inheriting float noise.

    Floats are routed through ``str`` so ``4.79`` becomes ``Decimal("4.79")``
    rather than ``Decimal("4.79000000000000003552713678800500929355621337890625")``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def quantize_bps(value: Decimal) -> float:
    """Round a basis-point Decimal to the canonical quantum and return a float."""
    return float(value.quantize(_BPS_QUANTUM, rounding=ROUND_HALF_UP))


def percent_to_bps(percent: Number) -> float:
    """Convert a percentage-point figure to basis points (``0.20`` -> ``20.0``)."""
    return quantize_bps(to_decimal(percent) * BPS_PER_PERCENT)


def bps_to_percent(basis_points: Number) -> float:
    """Convert basis points to percentage points (``20`` -> ``0.2``)."""
    return float(to_decimal(basis_points) / BPS_PER_PERCENT)


def rate_change_bps(previous_percent: Number, current_percent: Number) -> float:
    """Return the exact signed movement from ``previous`` to ``current`` in bps.

    ``rate_change_bps(4.99, 4.79) == -20.0`` exactly, with no float residue.
    """
    delta = to_decimal(current_percent) - to_decimal(previous_percent)
    return quantize_bps(delta * BPS_PER_PERCENT)


def spread_bps(low_percent: Number, high_percent: Number) -> float:
    """Return the non-negative dispersion between two advertised rates in bps."""
    return abs(rate_change_bps(low_percent, high_percent))
