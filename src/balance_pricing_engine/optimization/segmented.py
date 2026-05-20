"""Segmented portfolio-level product-tier pricing optimizer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from balance_pricing_engine.config import (
    DepositTierConfig,
    LoanTierConfig,
    ResponseCoefficientsConfig,
    SegmentedPricingConfig,
)
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_capital_charge,
    expected_credit_loss,
    expected_deposit_cost,
    expected_deposit_volume,
    expected_excess_deposit_income,
    expected_loan_interest_income,
    expected_loan_volume,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


@dataclass(frozen=True)
class TierResult:
    tier_name: str
    rate: float
    volume: float
    interest_income: float
    credit_loss: float
    interest_cost: float
    capital_charge: float = 0.0


@dataclass(frozen=True)
class SegmentedOptimizationResult:
    loan_tiers: list[TierResult]
    deposit_tiers: list[TierResult]
    total_loan_volume: float
    total_deposit_volume: float
    total_loan_interest_income: float
    total_credit_loss: float
    capital_charge: float
    total_deposit_interest_cost: float
    wholesale_funding_cost: float
    excess_deposit_income: float
    funding_shortfall_penalty: float
    funding_shortfall: float
    total_objective: float


def segment_loan_customers(
    customers: pd.DataFrame,
    tier_configs: list[LoanTierConfig],
) -> dict[str, pd.DataFrame]:
    """Split loan customers into named LVR tiers."""
    return _segment_customers(
        customers=customers,
        tier_configs=tier_configs,
        value_column="lvr",
        upper_bound_attribute="lvr_max",
    )


def segment_deposit_customers(
    customers: pd.DataFrame,
    tier_configs: list[DepositTierConfig],
) -> dict[str, pd.DataFrame]:
    """Split deposit customers into named balance tiers."""
    return _segment_customers(
        customers=customers,
        tier_configs=tier_configs,
        value_column="deposit_amount",
        upper_bound_attribute="balance_max",
    )


def optimize_segmented_rates(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    config: SegmentedPricingConfig,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
    loan_rwa_weight: float = 0.35,
    capital_hurdle_rate: float = 0.12,
    response_coefficients: ResponseCoefficientsConfig | None = None,
) -> SegmentedOptimizationResult:
    """Optimize tier-level rates, then apply funding economics at portfolio level."""
    response_coefficients = response_coefficients or ResponseCoefficientsConfig()
    loan_segments = segment_loan_customers(loan_customers, config.loan_tiers)
    deposit_segments = segment_deposit_customers(deposit_customers, config.deposit_tiers)

    loan_tier_results = [
        _optimize_loan_tier(
            customers=loan_segments[tier.name],
            tier=tier,
            market=market,
            loan_rate_elasticity=loan_rate_elasticity,
            loan_competitor_gap_elasticity=loan_competitor_gap_elasticity,
            loan_rwa_weight=loan_rwa_weight,
            capital_hurdle_rate=capital_hurdle_rate,
            response_coefficients=response_coefficients,
        )
        for tier in config.loan_tiers
    ]

    total_loan_volume = sum(tier.volume for tier in loan_tier_results)
    deposit_tier_results: list[TierResult] = []
    accumulated_deposit_volume = 0.0
    for tier in config.deposit_tiers:
        result = _optimize_deposit_tier(
            customers=deposit_segments[tier.name],
            tier=tier,
            market=market,
            deposit_rate_elasticity=deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=deposit_competitor_gap_elasticity,
            wholesale_funding_rate=market.wholesale_funding_rate,
            cash_rate=market.cash_rate,
            total_loan_volume=total_loan_volume,
            accumulated_deposit_volume=accumulated_deposit_volume,
            required_deposit_funding_ratio=required_deposit_funding_ratio,
            funding_shortfall_penalty_rate=funding_shortfall_penalty_rate,
            response_coefficients=response_coefficients,
        )
        deposit_tier_results.append(result)
        accumulated_deposit_volume += result.volume

    total_deposit_volume = sum(tier.volume for tier in deposit_tier_results)
    total_loan_interest_income = sum(tier.interest_income for tier in loan_tier_results)
    total_credit_loss = sum(tier.credit_loss for tier in loan_tier_results)
    capital_charge = expected_capital_charge(
        total_loan_volume,
        loan_rwa_weight,
        capital_hurdle_rate,
    )
    total_deposit_interest_cost = sum(tier.interest_cost for tier in deposit_tier_results)
    wholesale_funding_cost = expected_wholesale_funding_cost(
        total_loan_volume,
        total_deposit_volume,
        market.wholesale_funding_rate,
    )
    excess_deposit_income = expected_excess_deposit_income(
        total_loan_volume,
        total_deposit_volume,
        market.cash_rate,
    )
    funding = calculate_funding_result(
        total_loan_volume,
        total_deposit_volume,
        required_deposit_funding_ratio,
        funding_shortfall_penalty_rate,
    )
    total_objective = (
        total_loan_interest_income
        - total_credit_loss
        - capital_charge
        - total_deposit_interest_cost
        - wholesale_funding_cost
        - funding.funding_penalty
        + excess_deposit_income
    )

    return SegmentedOptimizationResult(
        loan_tiers=loan_tier_results,
        deposit_tiers=deposit_tier_results,
        total_loan_volume=total_loan_volume,
        total_deposit_volume=total_deposit_volume,
        total_loan_interest_income=total_loan_interest_income,
        total_credit_loss=total_credit_loss,
        capital_charge=capital_charge,
        total_deposit_interest_cost=total_deposit_interest_cost,
        wholesale_funding_cost=wholesale_funding_cost,
        excess_deposit_income=excess_deposit_income,
        funding_shortfall_penalty=funding.funding_penalty,
        funding_shortfall=funding.funding_shortfall,
        total_objective=total_objective,
    )


def _segment_customers(
    customers: pd.DataFrame,
    tier_configs: Iterable[LoanTierConfig | DepositTierConfig],
    value_column: str,
    upper_bound_attribute: str,
) -> dict[str, pd.DataFrame]:
    cohorts: dict[str, pd.DataFrame] = {}
    assigned_indexes: set[int] = set()
    previous_upper_bound = 0.0

    for tier in tier_configs:
        upper_bound = getattr(tier, upper_bound_attribute)
        values = customers[value_column]
        if upper_bound is None:
            mask = ~customers.index.isin(assigned_indexes)
        else:
            mask = (values >= previous_upper_bound) & (values < upper_bound)
            previous_upper_bound = float(upper_bound)

        cohort = customers.loc[mask].copy()
        if cohort.empty:
            raise ValueError(f"Tier {tier.name!r} has no customers.")

        cohorts[tier.name] = cohort
        assigned_indexes.update(cohort.index)

    if len(assigned_indexes) != len(customers):
        raise ValueError(f"Some customers are not covered by {value_column} tiers.")

    return cohorts


def _optimize_loan_tier(
    customers: pd.DataFrame,
    tier: LoanTierConfig,
    market: MarketState,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    loan_rwa_weight: float,
    capital_hurdle_rate: float,
    response_coefficients: ResponseCoefficientsConfig,
) -> TierResult:
    best_result: TierResult | None = None
    best_objective = float("-inf")

    for rate in _build_tier_rate_grid(tier.rate_grid.start, tier.rate_grid.stop, tier.rate_grid.step):
        probabilities = loan_acceptance_probability(
            customers,
            float(rate),
            market.competitor_loan_rate,
            loan_rate_elasticity,
            loan_competitor_gap_elasticity,
            coefficients=response_coefficients.loan,
        )
        volume = expected_loan_volume(customers, probabilities)
        interest_income = expected_loan_interest_income(customers, probabilities, float(rate))
        credit_loss = expected_credit_loss(customers, probabilities)
        capital_charge = expected_capital_charge(
            volume,
            loan_rwa_weight,
            capital_hurdle_rate,
        )
        # Wholesale funding cost is a real per-tier cost; the portfolio-level funding shortfall
        # penalty is excluded here because it is a portfolio constraint, not a per-tier charge.
        objective = (
            interest_income
            - credit_loss
            - capital_charge
            - expected_wholesale_funding_cost(volume, 0.0, market.wholesale_funding_rate)
        )
        if objective > best_objective:
            best_objective = objective
            best_result = TierResult(
                tier_name=tier.name,
                rate=float(rate),
                volume=volume,
                interest_income=interest_income,
                credit_loss=credit_loss,
                interest_cost=0.0,
                capital_charge=capital_charge,
            )

    if best_result is None:
        raise ValueError(f"Tier {tier.name!r} has no loan rates to evaluate.")
    return best_result


def _optimize_deposit_tier(
    customers: pd.DataFrame,
    tier: DepositTierConfig,
    market: MarketState,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    wholesale_funding_rate: float,
    cash_rate: float,
    total_loan_volume: float,
    accumulated_deposit_volume: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
    response_coefficients: ResponseCoefficientsConfig,
) -> TierResult:
    best_result: TierResult | None = None
    best_objective = float("-inf")

    for rate in _build_tier_rate_grid(tier.rate_grid.start, tier.rate_grid.stop, tier.rate_grid.step):
        probabilities = deposit_uptake_probability(
            customers,
            float(rate),
            market.competitor_deposit_rate,
            deposit_rate_elasticity,
            deposit_competitor_gap_elasticity,
            coefficients=response_coefficients.deposit,
        )
        volume = expected_deposit_volume(customers, probabilities)
        interest_cost = expected_deposit_cost(customers, probabilities, float(rate))
        # Deposits that displace wholesale borrowing save at wholesale_funding_rate; deposits
        # beyond total loan volume are excess and earn only cash_rate (via excess_deposit_income).
        shortfall_covered = min(volume, max(0.0, total_loan_volume - accumulated_deposit_volume))
        excess = volume - shortfall_covered
        funding_benefit = shortfall_covered * wholesale_funding_rate + excess * cash_rate
        remaining_required_deposits = max(
            0.0,
            total_loan_volume * required_deposit_funding_ratio - accumulated_deposit_volume,
        )
        penalty_relief = min(volume, remaining_required_deposits) * funding_shortfall_penalty_rate
        objective = -interest_cost + funding_benefit + penalty_relief
        if objective > best_objective:
            best_objective = objective
            best_result = TierResult(
                tier_name=tier.name,
                rate=float(rate),
                volume=volume,
                interest_income=0.0,
                credit_loss=0.0,
                interest_cost=interest_cost,
            )

    if best_result is None:
        raise ValueError(f"Tier {tier.name!r} has no deposit rates to evaluate.")
    return best_result


def _build_tier_rate_grid(start: float, stop: float, step: float) -> np.ndarray:
    """Build a bounded tier grid, appending stop when it is not reached by whole steps."""
    step_count = int(np.floor((stop - start) / step))
    rates = list(np.round(start + np.arange(step_count + 1) * step, 6))
    if not np.isclose(rates[-1], stop, rtol=1e-9, atol=1e-12):
        rates.append(round(stop, 6))
    return np.array(rates, dtype=float)
