"""Opt-in term-matched FTP pricing simulation (FIN-003, Phase 4d).

The governed decision path is unchanged: ``EngineConfig`` has no FTP fields, and the
canonical market keeps ``cash_rate + wholesale_funding_spread``. These requests price the
aggregate residual deficit and investible surplus at external rates from a dated FTP
curve, attribute the result to loan, deposit and Treasury books with internal transfers
that cancel, and report a pre-steering economic ROE beside the unchanged model ROE.

Every result carries ``FTP_SIMULATION_ONLY`` (WARN) through the standard gate set, so it is
at least ``REVIEW_REQUIRED``. Existing BLOCK gates still produce ``BLOCKED``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import math
from typing import Any, Literal

from pydantic import AwareDatetime, ConfigDict, Field, field_serializer, model_validator

from balance_pricing_engine.config import EconomicConfig, EngineConfig, revalidation_payload
from balance_pricing_engine.config_identity import canonical_config_bytes
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    GateResult,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.pricing.credit_loss import (
    ANNUAL_EXPECTED_LOSS_COLUMN,
    CreditLossProfile,
)
from balance_pricing_engine.pricing.deposit_supply import (
    DepositMarketProfile,
    DepositSupply,
    DepositSupplyEstimate,
)
from balance_pricing_engine.pricing.economic_roe import EconomicROEBridge, economic_roe_bridge
from balance_pricing_engine.pricing.funds_transfer_pricing import (
    LEDGER_ABS_TOL,
    LEDGER_REL_TOL,
    FTPAllocationLedger,
    FTPCurve,
    FTPFundingProfile,
    FTPRateSet,
    amounts_reconcile,
    ftp_market,
    ledger_from_result,
)
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight
from balance_pricing_engine.scenarios.deposit_supply_simulation import _selected_supply
from balance_pricing_engine.scenarios.multiperiod import MultiPeriodDiagnostics, _roll_portfolio
from balance_pricing_engine.scenarios.runner import (
    ScenarioDiagnostics,
    _evaluate_pricing_population,
    _generate_pricing_population,
)
from balance_pricing_engine.synthetic.market import MarketState

LEGACY_FUNDING_BASIS = "gross_expected_deposit_volume"
SUPPLY_FUNDING_BASIS = "phase_4c_recognised_incremental_funding"


class _FTPSimulationInputs(EconomicConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_config: EngineConfig
    funding_profile: FTPFundingProfile
    max_boundary_expansions: int = Field(default=2, ge=0)
    response_model: Literal["synthetic_dual_elasticity_v1"] = "synthetic_dual_elasticity_v1"
    funding_aggregation: Literal["portfolio_aggregate_residual_v1"] = (
        "portfolio_aggregate_residual_v1"
    )
    economic_roe_basis: Literal["pre_tax_pre_opex_reporting_only_v1"] = (
        "pre_tax_pre_opex_reporting_only_v1"
    )

    @field_serializer("engine_config")
    def _serialize_engine_config(self, engine: EngineConfig) -> dict:
        return revalidation_payload(engine)

    @model_validator(mode="after")
    def _require_static_market(self) -> _FTPSimulationInputs:
        rates = self.engine_config.competitor_rates
        if rates is not None and rates.enabled:
            raise ValueError("FTP simulation requires static market inputs.")
        return self


class FTPSimulationConfig(_FTPSimulationInputs):
    """One annual horizon priced on one dated curve, outside the governed schema."""

    curve: FTPCurve
    valuation_time: AwareDatetime
    fixed_rates: tuple[float, float] | None = None
    deposit_supply_profile: DepositMarketProfile | None = None
    credit_loss_profile: CreditLossProfile | None = None

    @model_validator(mode="after")
    def _validate_scope(self) -> FTPSimulationConfig:
        engine = self.engine_config
        if engine.multiperiod is not None:
            raise ValueError(
                "FTPSimulationConfig is single-horizon; use FTPMultiPeriodSimulationConfig."
            )
        self.curve.check_valid_at(self.valuation_time)
        self.funding_profile.rates(self.curve)
        if self.fixed_rates is not None:
            if engine.segmented_pricing is not None:
                raise ValueError("A fixed rate pair requires a joint pricing configuration.")
            if min(self.fixed_rates) < 0 or not all(map(math.isfinite, self.fixed_rates)):
                raise ValueError("Fixed rates must be finite and non-negative.")
        supply = self.deposit_supply_profile
        if supply is not None:
            # Both schemas admit NZD only, so their currencies cannot mix.
            if supply.horizon_months != 12:
                raise ValueError("FTP with deposit supply requires the 12-month supply horizon.")
            if supply.term_months != self.funding_profile.deposit_contractual_tenor_months:
                raise ValueError(
                    f"Deposit supply term ({supply.term_months} months) differs from the FTP "
                    "deposit contractual tenor "
                    f"({self.funding_profile.deposit_contractual_tenor_months} months); the "
                    "funding bases are incompatible."
                )
        return self


@dataclass(frozen=True)
class FTPSimulation:
    """Synthetic FTP evidence bound to the canonical bytes of the effective request."""

    diagnostics: ScenarioDiagnostics
    rates: FTPRateSet
    ledger: FTPAllocationLedger
    roe_bridge: EconomicROEBridge
    eligible_funding_basis: str
    effective_config_json: bytes
    config_hash: str
    gates: tuple[GateResult, ...]
    governance_status: DecisionStatus
    deposit_supply_estimate: DepositSupplyEstimate | None = None

    @property
    def effective_config(self) -> FTPSimulationConfig:
        """Return a fresh view of the request bound to the reported hash."""
        return FTPSimulationConfig.model_validate_json(self.effective_config_json)

    def components(self) -> dict[str, float | None]:
        """The reconciliation view (A-Q). Mutually exclusive views are never summed."""
        best, ledger, bridge = self.diagnostics.best_result, self.ledger, self.roe_bridge
        estimate = self.deposit_supply_estimate
        customer_interest = (
            best.deposit_interest_cost if estimate is None else estimate.gross_interest_cost
        )
        return {
            "A_customer_loan_interest": best.loan_interest_income,
            "B_expected_credit_loss": best.expected_credit_loss,
            "C_customer_deposit_interest": customer_interest,
            "D_incremental_deposit_interest_in_objective": best.deposit_interest_cost,
            "E_deposit_acquisition_cost": bridge.deposit_acquisition_cost,
            "F_external_wholesale_funding_expense": ledger.external_wholesale_funding_cost,
            "G_surplus_cash_income": ledger.surplus_investment_income,
            "H_ftp_loan_allocation": -ledger.loan_ftp_charge,
            "I_ftp_deposit_allocation": ledger.deposit_ftp_credit,
            "J_treasury_offsetting_allocations": (
                ledger.treasury_ftp_receipts - ledger.treasury_ftp_payments
            ),
            "K_capital_charge": best.capital_charge,
            "L_funding_steering_penalty": best.funding_shortfall_penalty,
            "M_other_disclosed_costs_concentration_penalty": bridge.concentration_penalty,
            "N_total_consolidated_objective": best.total_objective,
            "O_model_roe": bridge.model_return_on_equity,
            "P_economic_roe": bridge.economic_return_on_equity,
            "Q_ftp_reconciliation_difference": ledger.reconciliation_difference,
        }

    def report(self) -> dict[str, Any]:
        """JSON-ready evidence: selected offers, decomposition, ledger and ROE bridge."""
        best = self.diagnostics.best_result
        estimate = self.deposit_supply_estimate
        loss_profile = self.diagnostics.credit_loss_profile
        return {
            "config_hash": self.config_hash,
            "selected_loan_rate": best.loan_rate,
            "selected_deposit_rate": best.deposit_rate,
            "segmented_tier_rates": self.diagnostics.segmented_tier_rates,
            "expected_loan_volume": best.expected_loan_volume,
            "expected_deposit_volume": best.expected_deposit_volume,
            "funding_shortfall": best.funding_shortfall,
            "eligible_funding_basis": self.eligible_funding_basis,
            "components": self.components(),
            "rates": self.rates.to_dict(),
            "ledger": self.ledger.to_dict(),
            "economic_roe": self.roe_bridge.to_dict(),
            "deposit_supply": None
            if estimate is None
            else {
                "profile_key": f"{self.diagnostics.deposit_supply_profile.profile_id}@"
                f"{self.diagnostics.deposit_supply_profile.version}",
                "gross_bookings": estimate.gross_bookings,
                "net_incremental_funding": estimate.net_incremental_funding,
                "usable_funding": estimate.usable_funding,
                "reference_funding": estimate.reference_funding,
                "constrained_funding": estimate.constrained_funding,
            },
            "credit_loss_profile_version": None if loss_profile is None else loss_profile.version,
            "policy_band_breached": self.diagnostics.policy_band_breached,
            "boundary_warning_flag": self.diagnostics.boundary_diagnostics.has_warning,
            "governance_status": self.governance_status.value,
            "gates": {gate.code: gate.status.value for gate in self.gates},
        }


def run_ftp_simulation(config: FTPSimulationConfig) -> FTPSimulation:
    """Price one horizon at term-matched external funding rates and attribute the result."""
    config = FTPSimulationConfig.model_validate(config)
    effective_config_json = canonical_config_bytes(config)
    engine = config.engine_config
    rates = config.funding_profile.rates(config.curve)
    market = ftp_market(MarketState(**engine.market.model_dump()), rates)

    loans, deposits = _generate_pricing_population(engine)
    if config.credit_loss_profile is not None:
        loss = config.credit_loss_profile.estimate(loans["loan_amount"], loans["lvr"])
        loans[ANNUAL_EXPECTED_LOSS_COLUMN] = loss.annual_expected_loss_nzd
    supply = (
        None
        if config.deposit_supply_profile is None
        else DepositSupply(config.deposit_supply_profile, deposits)
    )
    diagnostics = replace(
        _evaluate_pricing_population(
            engine,
            loans,
            deposits,
            max_boundary_expansions=config.max_boundary_expansions,
            deposit_supply=supply,
            fixed_rates=config.fixed_rates,
            market=market,
        ),
        credit_loss_profile=config.credit_loss_profile,
    )
    gates = tuple(
        evaluate_gates(config=engine, diagnostics=diagnostics, model_lineage={}, feature_lineage={})
    )
    best = diagnostics.best_result
    return FTPSimulation(
        diagnostics=diagnostics,
        rates=rates,
        ledger=ledger_from_result(best, rates),
        roe_bridge=economic_roe_bridge(best, rwa_weight=_effective_rwa_weight(engine)),
        eligible_funding_basis=LEGACY_FUNDING_BASIS if supply is None else SUPPLY_FUNDING_BASIS,
        effective_config_json=effective_config_json,
        config_hash=sha256(effective_config_json).hexdigest(),
        gates=gates,
        governance_status=decision_status_from_gates(list(gates)),
        deposit_supply_estimate=(
            None if supply is None else _selected_supply(engine, diagnostics, deposits, supply)
        ),
    )


def _effective_rwa_weight(engine: EngineConfig) -> float:
    return maturity_adjusted_rwa_weight(
        engine.balance_sheet.loan_rwa_weight, engine.balance_sheet.average_loan_tenor_years
    )


# -- Multi-period roll -------------------------------------------------------------------


class FTPMultiPeriodSimulationConfig(_FTPSimulationInputs):
    """A dated curve per annual period on the Phase 3b cohort roll.

    Booked cohorts keep their contractual coupon and their FTP rates locked at
    origination. The period's curve prices only that period's new business and the
    aggregate external funding of the whole active book. Deposit supply has no authorised
    multi-period stock-flow schedule, so it is rejected.
    """

    curve_schedule: tuple[FTPCurve, ...] = Field(min_length=1)
    valuation_times: tuple[AwareDatetime, ...] = Field(min_length=1)
    deposit_supply_profile: DepositMarketProfile | None = None

    @model_validator(mode="after")
    def _validate_schedule(self) -> FTPMultiPeriodSimulationConfig:
        mp = self.engine_config.multiperiod
        if mp is None:
            raise ValueError("A multi-period FTP simulation requires engine_config.multiperiod.")
        if self.deposit_supply_profile is not None:
            raise ValueError(
                "Deposit supply has no authorised multi-period stock-flow schedule (Phase 4c "
                "is single-horizon); FTP cannot roll it."
            )
        if not len(self.curve_schedule) == len(self.valuation_times) == mp.n_periods:
            raise ValueError("Provide one curve and one valuation time per period.")
        if any(b <= a for a, b in zip(self.valuation_times, self.valuation_times[1:])):
            raise ValueError("Period valuation times must be strictly increasing.")
        versions: dict[tuple[str, str], FTPCurve] = {}
        for curve, valuation_time in zip(self.curve_schedule, self.valuation_times):
            if versions.setdefault((curve.curve_id, curve.version), curve) != curve:
                raise ValueError(
                    f"FTP curve {curve.curve_id} version {curve.version} is ambiguous: give "
                    "each dated curve its own version."
                )
            curve.check_valid_at(valuation_time)
            self.funding_profile.rates(curve)
        return self


@dataclass(frozen=True)
class CohortFTPAllocation:
    """One surviving cohort's internal transfers at its origination (locked) FTP rates."""

    origination_period: int
    loan_volume: float
    deposit_volume: float
    loan_ftp_rate: float
    deposit_ftp_rate: float

    @property
    def loan_ftp_charge(self) -> float:
        return self.loan_volume * self.loan_ftp_rate

    @property
    def deposit_ftp_credit(self) -> float:
        return self.deposit_volume * self.deposit_ftp_rate


@dataclass(frozen=True)
class PeriodFTPLedger:
    """One period's attribution for the whole active book (locked transfers, current
    external funding). The capital charge and shortfall penalty stay below the line."""

    period: int
    curve_key: str
    wholesale_funding_rate: float
    surplus_reinvestment_rate: float
    allocations: tuple[CohortFTPAllocation, ...]
    loan_interest_income: float
    expected_credit_loss: float
    deposit_interest_cost: float
    external_wholesale_funding_cost: float
    surplus_investment_income: float
    capital_charge: float
    funding_shortfall_penalty: float
    discount_factor: float

    @property
    def loan_ftp_charge(self) -> float:
        return math.fsum(item.loan_ftp_charge for item in self.allocations)

    @property
    def deposit_ftp_credit(self) -> float:
        return math.fsum(item.deposit_ftp_credit for item in self.allocations)

    @property
    def loan_business_contribution(self) -> float:
        return self.loan_interest_income - self.expected_credit_loss - self.loan_ftp_charge

    @property
    def deposit_business_contribution(self) -> float:
        return self.deposit_ftp_credit - self.deposit_interest_cost

    @property
    def treasury_contribution(self) -> float:
        return (
            self.loan_ftp_charge
            - self.deposit_ftp_credit
            - self.external_wholesale_funding_cost
            + self.surplus_investment_income
        )

    @property
    def internal_transfer_net(self) -> float:
        charge, credit = self.loan_ftp_charge, self.deposit_ftp_credit
        return math.fsum((-charge, charge, credit, -credit))

    @property
    def consolidated_contribution(self) -> float:
        return math.fsum(
            (
                self.loan_business_contribution,
                self.deposit_business_contribution,
                self.treasury_contribution,
            )
        )

    @property
    def external_economic_contribution(self) -> float:
        return math.fsum(
            (
                self.loan_interest_income,
                -self.expected_credit_loss,
                -self.deposit_interest_cost,
                -self.external_wholesale_funding_cost,
                self.surplus_investment_income,
            )
        )

    @property
    def model_objective(self) -> float:
        return (
            self.external_economic_contribution
            - self.capital_charge
            - self.funding_shortfall_penalty
        )

    @property
    def discounted_model_objective(self) -> float:
        return self.model_objective * self.discount_factor

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "curve_key": self.curve_key,
            "wholesale_funding_rate": self.wholesale_funding_rate,
            "surplus_reinvestment_rate": self.surplus_reinvestment_rate,
            "locked_allocations": [
                {
                    "origination_period": item.origination_period,
                    "loan_volume": item.loan_volume,
                    "deposit_volume": item.deposit_volume,
                    "loan_ftp_rate": item.loan_ftp_rate,
                    "deposit_ftp_rate": item.deposit_ftp_rate,
                }
                for item in self.allocations
            ],
            "loan_business_contribution": self.loan_business_contribution,
            "deposit_business_contribution": self.deposit_business_contribution,
            "treasury_contribution": self.treasury_contribution,
            "internal_transfer_net": self.internal_transfer_net,
            "consolidated_contribution": self.consolidated_contribution,
            "external_economic_contribution": self.external_economic_contribution,
            "capital_charge": self.capital_charge,
            "funding_shortfall_penalty": self.funding_shortfall_penalty,
            "model_objective": self.model_objective,
            "discount_factor": self.discount_factor,
            "discounted_model_objective": self.discounted_model_objective,
        }


@dataclass(frozen=True)
class FTPMultiPeriodSimulation:
    multiperiod: MultiPeriodDiagnostics
    period_rates: tuple[FTPRateSet, ...]
    period_ledgers: tuple[PeriodFTPLedger, ...]
    effective_config_json: bytes
    config_hash: str
    period_governance_status: tuple[DecisionStatus, ...]

    @property
    def effective_config(self) -> FTPMultiPeriodSimulationConfig:
        return FTPMultiPeriodSimulationConfig.model_validate_json(self.effective_config_json)

    @property
    def governance_status(self) -> DecisionStatus:
        if DecisionStatus.BLOCKED in self.period_governance_status:
            return DecisionStatus.BLOCKED
        return DecisionStatus.REVIEW_REQUIRED


def run_ftp_multiperiod_simulation(
    config: FTPMultiPeriodSimulationConfig,
) -> FTPMultiPeriodSimulation:
    """Roll cohorts on dated curves; never rebook earlier cohorts at a later curve."""
    config = FTPMultiPeriodSimulationConfig.model_validate(config)
    effective_config_json = canonical_config_bytes(config)
    period_rates = tuple(config.funding_profile.rates(curve) for curve in config.curve_schedule)

    def period_market(scenario_config: EngineConfig, period: int) -> MarketState:
        return ftp_market(MarketState(**scenario_config.market.model_dump()), period_rates[period])

    def evaluate_period(scenario_config: EngineConfig, period: int) -> ScenarioDiagnostics:
        loans, deposits = _generate_pricing_population(scenario_config)
        return _evaluate_pricing_population(
            scenario_config,
            loans,
            deposits,
            max_boundary_expansions=config.max_boundary_expansions,
            market=period_market(scenario_config, period),
        )

    roll = _roll_portfolio(
        config.engine_config,
        max_boundary_expansions=config.max_boundary_expansions,
        evaluate_period=evaluate_period,
        period_market=period_market,
    )
    config_hash = sha256(effective_config_json).hexdigest()
    roll = replace(
        roll,
        provenance={
            **roll.provenance,
            "config_hash": config_hash,
            "ftp_curve_keys": [rates.curve_key for rates in period_rates],
            "ftp_profile_key": config.funding_profile.key,
            "ftp_allocation_policy": config.funding_profile.allocation_policy,
            "governance_status": "REVIEW_REQUIRED",
        },
    )
    ledgers = tuple(_period_ledger(economics, period_rates) for economics in roll.period_economics)
    statuses = tuple(
        decision_status_from_gates(
            evaluate_gates(
                config=config.engine_config,
                diagnostics=diagnostics,
                model_lineage={},
                feature_lineage={},
            )
        )
        for diagnostics in roll.period_diagnostics
    )
    return FTPMultiPeriodSimulation(
        multiperiod=roll,
        period_rates=period_rates,
        period_ledgers=ledgers,
        effective_config_json=effective_config_json,
        config_hash=config_hash,
        period_governance_status=statuses,
    )


def _period_ledger(economics: Any, period_rates: tuple[FTPRateSet, ...]) -> PeriodFTPLedger:
    rates = period_rates[economics.period]
    ledger = PeriodFTPLedger(
        period=economics.period,
        curve_key=rates.curve_key,
        wholesale_funding_rate=rates.wholesale_funding_rate,
        surplus_reinvestment_rate=rates.surplus_reinvestment_rate,
        allocations=tuple(
            CohortFTPAllocation(
                origination_period=position.cohort.origination_period,
                loan_volume=position.loan_volume,
                deposit_volume=position.deposit_volume,
                loan_ftp_rate=period_rates[position.cohort.origination_period].loan_ftp_rate,
                deposit_ftp_rate=period_rates[position.cohort.origination_period].deposit_ftp_rate,
            )
            for position in economics.active_cohorts
        ),
        loan_interest_income=economics.loan_interest_income,
        expected_credit_loss=economics.expected_credit_loss,
        deposit_interest_cost=economics.deposit_interest_cost,
        external_wholesale_funding_cost=economics.wholesale_funding_cost,
        surplus_investment_income=economics.excess_deposit_income,
        capital_charge=economics.capital_charge,
        funding_shortfall_penalty=economics.funding_shortfall_penalty,
        discount_factor=economics.discount_factor,
    )
    deficit = max(economics.total_active_loan_volume - economics.total_active_deposit_volume, 0.0)
    surplus = max(economics.total_active_deposit_volume - economics.total_active_loan_volume, 0.0)
    for name, actual, expected in (
        (
            "wholesale cost",
            economics.wholesale_funding_cost,
            deficit * rates.wholesale_funding_rate,
        ),
        (
            "surplus income",
            economics.excess_deposit_income,
            surplus * rates.surplus_reinvestment_rate,
        ),
        ("objective", economics.active_portfolio_objective, ledger.model_objective),
    ):
        if not math.isclose(actual, expected, rel_tol=LEDGER_REL_TOL, abs_tol=LEDGER_ABS_TOL):
            raise ValueError(f"Period {economics.period} {name} does not reconcile.")
    if not amounts_reconcile(
        ledger.consolidated_contribution, ledger.external_economic_contribution
    ):
        raise ValueError(f"Period {economics.period} FTP transfers do not eliminate.")
    return ledger


__all__ = [
    "CohortFTPAllocation",
    "FTPMultiPeriodSimulation",
    "FTPMultiPeriodSimulationConfig",
    "FTPSimulation",
    "FTPSimulationConfig",
    "PeriodFTPLedger",
    "run_ftp_multiperiod_simulation",
    "run_ftp_simulation",
]
