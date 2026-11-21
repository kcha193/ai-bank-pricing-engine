"""Balance sheet constraints linking lending and deposits."""

from __future__ import annotations

from dataclasses import dataclass

from balance_pricing_engine.pricing.profit import FloatOrArray, non_negative_part


@dataclass(frozen=True)
class FundingResult:
    required_deposits: FloatOrArray
    funding_shortfall: FloatOrArray
    funding_penalty: FloatOrArray


def calculate_funding_result(
    loan_volume: FloatOrArray,
    deposit_volume: FloatOrArray,
    required_deposit_funding_ratio: float,
    shortfall_penalty_rate: float,
) -> FundingResult:
    """Calculate deposit funding requirement and any shortfall penalty.

    Scalar volumes give scalar fields; arrays of candidate portfolios give arrays.
    """
    required_deposits = loan_volume * required_deposit_funding_ratio
    funding_shortfall = non_negative_part(required_deposits - deposit_volume)
    funding_penalty = funding_shortfall * shortfall_penalty_rate
    return FundingResult(
        required_deposits=required_deposits,
        funding_shortfall=funding_shortfall,
        funding_penalty=funding_penalty,
    )

