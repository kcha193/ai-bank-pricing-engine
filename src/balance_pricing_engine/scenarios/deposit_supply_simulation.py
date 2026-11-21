"""Opt-in annual pricing simulation with bounded synthetic deposit supply."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal

import pandas as pd
from pydantic import ConfigDict, Field, field_serializer, model_validator

from balance_pricing_engine.config import EconomicConfig, EngineConfig, revalidation_payload
from balance_pricing_engine.config_identity import canonical_config_bytes
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.optimization.segmented import segment_deposit_customers
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.credit_loss import (
    ANNUAL_EXPECTED_LOSS_COLUMN,
    CreditLossProfile,
)
from balance_pricing_engine.pricing.deposit_supply import (
    ConcentrationMode,
    DepositMarketProfile,
    DepositSupply,
    DepositSupplyEstimate,
)
from balance_pricing_engine.pricing.profit import (
    expected_excess_deposit_income,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.scenarios.runner import (
    ScenarioDiagnostics,
    _evaluate_pricing_population,
    _generate_pricing_population,
)
from balance_pricing_engine.synthetic.customers import generate_deposit_customers
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import deposit_uptake_probability
from balance_pricing_engine.utils.random import make_rng


class DepositSupplySimulationConfig(EconomicConfig):
    """An explicitly synthetic request outside the governed pricing schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_config: EngineConfig
    profile: DepositMarketProfile
    max_boundary_expansions: int = Field(default=2, ge=0)
    deposit_sample_seed: int | None = Field(default=None, ge=0)
    fixed_rates: tuple[float, float] | None = None
    credit_loss_profile: CreditLossProfile | None = None
    response_model: Literal["synthetic_dual_elasticity_v1"] = "synthetic_dual_elasticity_v1"
    funding_basis: Literal["annual_opening_balance_and_disjoint_flows_v1"] = (
        "annual_opening_balance_and_disjoint_flows_v1"
    )

    @field_serializer("engine_config")
    def _serialize_engine_config(self, engine: EngineConfig) -> dict:
        return revalidation_payload(engine)

    @model_validator(mode="after")
    def validate_simulation_scope(self) -> DepositSupplySimulationConfig:
        engine = self.engine_config
        if engine.competitor_rates is not None and engine.competitor_rates.enabled:
            raise ValueError("Deposit supply simulation requires static market inputs.")
        if engine.multiperiod is not None:
            raise ValueError("Deposit supply simulation is single-horizon only.")
        if self.profile.horizon_months != 12 or any(
            segment.term_months != 12 for segment in self.profile.segments
        ):
            raise ValueError(
                "Annual pricing requires a 12-month horizon and comparable 12-month term."
            )
        if self.fixed_rates is not None:
            if engine.segmented_pricing is not None:
                raise ValueError("A fixed rate pair requires a joint pricing configuration.")
            if min(self.fixed_rates) < 0:
                raise ValueError("Fixed rates must be non-negative.")
        return self


@dataclass(frozen=True)
class ConcentrationComparison:
    """Compare the constrained selection with the same request without the cap."""

    selected_rates_changed: bool
    uncapped_loan_rate: float
    uncapped_deposit_rate: float
    uncapped_tier_rates: tuple[tuple[str, str, float], ...]
    uncapped_objective: float
    objective_delta: float
    matched_offer_objective_delta: float


@dataclass(frozen=True)
class FundingValue:
    """Annual effects against the no-bookings reference at the selected loan volume."""

    wholesale_cost_avoided: float
    additional_surplus_income: float
    shortfall_penalty_avoided: float

    @property
    def economic_funding_value(self) -> float:
        return self.wholesale_cost_avoided + self.additional_surplus_income


@dataclass(frozen=True)
class DepositSupplySimulation:
    diagnostics: ScenarioDiagnostics
    estimate: DepositSupplyEstimate
    funding_value: FundingValue
    effective_config_json: bytes
    config_hash: str
    governance_status: DecisionStatus
    concentration_comparison: ConcentrationComparison | None = None

    @property
    def effective_config(self) -> DepositSupplySimulationConfig:
        """Return a fresh view of the request bound to the simulation hash."""
        return DepositSupplySimulationConfig.model_validate_json(self.effective_config_json)


def run_deposit_supply_simulation(config: DepositSupplySimulationConfig) -> DepositSupplySimulation:
    """Evaluate an uncalibrated supply profile through the existing pricing solvers."""
    config = DepositSupplySimulationConfig.model_validate(config)
    effective_config_json = canonical_config_bytes(config)
    engine = config.engine_config
    loans, deposits = _generate_pricing_population(engine)
    if config.deposit_sample_seed is not None:
        deposits = generate_deposit_customers(
            engine.simulation.n_deposit_customers, make_rng(config.deposit_sample_seed)
        )
    if config.credit_loss_profile is not None:
        loss = config.credit_loss_profile.estimate(loans["loan_amount"], loans["lvr"])
        loans[ANNUAL_EXPECTED_LOSS_COLUMN] = loss.annual_expected_loss_nzd

    supply = DepositSupply(config.profile, deposits)
    diagnostics = _evaluate(config, loans, deposits, supply)
    estimate = _selected_supply(engine, diagnostics, deposits, supply)
    gates = evaluate_gates(
        config=engine, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    comparison = None
    if config.profile.concentration is not None and (
        config.profile.concentration.mode is not ConcentrationMode.DIAGNOSTIC_ONLY
    ):
        uncapped = DepositSupply(
            config.profile.model_copy(update={"concentration": None}), deposits
        )
        alternative = _evaluate(config, loans, deposits, uncapped)
        comparison = _compare_concentration(config, diagnostics, alternative, estimate)
    return DepositSupplySimulation(
        diagnostics=diagnostics,
        estimate=estimate,
        funding_value=_funding_value(engine, diagnostics, estimate),
        effective_config_json=effective_config_json,
        config_hash=sha256(effective_config_json).hexdigest(),
        governance_status=decision_status_from_gates(gates),
        concentration_comparison=comparison,
    )


def _evaluate(
    config: DepositSupplySimulationConfig,
    loans: pd.DataFrame,
    deposits: pd.DataFrame,
    supply: DepositSupply,
) -> ScenarioDiagnostics:
    return replace(
        _evaluate_pricing_population(
            config.engine_config,
            loans,
            deposits,
            max_boundary_expansions=config.max_boundary_expansions,
            deposit_supply=supply,
            fixed_rates=config.fixed_rates,
        ),
        deposit_supply_profile=config.profile,
        credit_loss_profile=config.credit_loss_profile,
    )


def _selected_supply(
    engine: EngineConfig,
    diagnostics: ScenarioDiagnostics,
    deposits: pd.DataFrame,
    supply: DepositSupply,
) -> DepositSupplyEstimate:
    if engine.segmented_pricing is None:
        cohorts = {"portfolio": deposits}
        rates = {"portfolio": diagnostics.best_result.deposit_rate}
    else:
        cohorts = segment_deposit_customers(deposits, engine.segmented_pricing.deposit_tiers)
        rates = diagnostics.segmented_tier_rates["deposit_tiers"]
    response = engine.response
    offers = []
    for name, cohort in cohorts.items():
        rate = rates[name]
        probabilities = deposit_uptake_probability(
            cohort,
            rate,
            engine.market.competitor_deposit_rate,
            response.deposit_rate_elasticity,
            response.deposit_competitor_gap_elasticity,
            coefficients=response.coefficients.deposit,
        )
        offers.append(supply.offer(cohort, probabilities, rate))
    return supply.estimate(offers)


def _funding_value(
    config: EngineConfig, diagnostics: ScenarioDiagnostics, estimate: DepositSupplyEstimate
) -> FundingValue:
    result = diagnostics.best_result
    market = MarketState(**config.market.model_dump())
    loans = result.expected_loan_volume
    reference = estimate.reference_funding
    policy = config.balance_sheet
    reference_penalty = calculate_funding_result(
        loans,
        reference,
        policy.required_deposit_funding_ratio,
        policy.funding_shortfall_penalty_rate,
    ).funding_penalty
    return FundingValue(
        wholesale_cost_avoided=(
            expected_wholesale_funding_cost(loans, reference, market.wholesale_funding_rate)
            - result.wholesale_funding_cost
        ),
        additional_surplus_income=(
            result.excess_deposit_income
            - expected_excess_deposit_income(loans, reference, market.cash_rate)
        ),
        shortfall_penalty_avoided=reference_penalty - result.funding_shortfall_penalty,
    )


def _compare_concentration(
    config: DepositSupplySimulationConfig,
    diagnostics: ScenarioDiagnostics,
    uncapped: ScenarioDiagnostics,
    estimate: DepositSupplyEstimate,
) -> ConcentrationComparison:
    selected, alternative = diagnostics.best_result, uncapped.best_result
    market = MarketState(**config.engine_config.market.model_dump())
    policy = config.engine_config.balance_sheet
    uncapped_funding = estimate.usable_funding + estimate.constrained_funding
    loans = selected.expected_loan_volume
    wholesale = expected_wholesale_funding_cost(
        loans, uncapped_funding, market.wholesale_funding_rate
    )
    surplus = expected_excess_deposit_income(loans, uncapped_funding, market.cash_rate)
    penalty = calculate_funding_result(
        loans,
        uncapped_funding,
        policy.required_deposit_funding_ratio,
        policy.funding_shortfall_penalty_rate,
    ).funding_penalty
    return ConcentrationComparison(
        selected_rates_changed=(
            (selected.loan_rate, selected.deposit_rate)
            != (alternative.loan_rate, alternative.deposit_rate)
            if diagnostics.segmented_tier_rates is None
            else diagnostics.segmented_tier_rates != uncapped.segmented_tier_rates
        ),
        uncapped_loan_rate=alternative.loan_rate,
        uncapped_deposit_rate=alternative.deposit_rate,
        uncapped_tier_rates=tuple(
            (side, tier, rate)
            for side, rates in (uncapped.segmented_tier_rates or {}).items()
            for tier, rate in rates.items()
        ),
        uncapped_objective=alternative.total_objective,
        objective_delta=selected.total_objective - alternative.total_objective,
        matched_offer_objective_delta=(
            wholesale
            - selected.wholesale_funding_cost
            + selected.excess_deposit_income
            - surplus
            + penalty
            - selected.funding_shortfall_penalty
            - estimate.concentration_penalty
        ),
    )
