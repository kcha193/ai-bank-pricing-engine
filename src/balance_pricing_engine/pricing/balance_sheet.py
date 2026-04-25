"""Balance sheet constraints linking lending and deposits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FundingResult:
    required_deposits: float
    funding_shortfall: float
    funding_penalty: float


def calculate_funding_result(
    loan_volume: float,
    deposit_volume: float,
    required_deposit_funding_ratio: float,
    shortfall_penalty_rate: float,
) -> FundingResult:
    """Calculate deposit funding requirement and any shortfall penalty."""
    required_deposits = loan_volume * required_deposit_funding_ratio
    funding_shortfall = max(required_deposits - deposit_volume, 0.0)
    funding_penalty = funding_shortfall * shortfall_penalty_rate
    return FundingResult(
        required_deposits=required_deposits,
        funding_shortfall=funding_shortfall,
        funding_penalty=funding_penalty,
    )

