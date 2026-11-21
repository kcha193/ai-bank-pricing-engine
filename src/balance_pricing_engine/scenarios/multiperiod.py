"""Multi-period scenario roll with explicit cohort accounting (FIN-002, Phase 3b).

Each period prices new business myopically with the standard single-period engine at that
period's market, books the winning result as a cohort, and evaluates the full active book:
every surviving earlier cohort at its booked contractual terms plus the new cohort, with the
funding terms calculated once on the aggregate balances. The result is a rolling annual
scenario projection, not a globally optimised lifetime NPV and not a calibrated ALM forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
import warnings

import pandas as pd
from pydantic import model_validator

from balance_pricing_engine.config import EngineConfig, revalidation_payload
from balance_pricing_engine.config_identity import effective_config_hash
from balance_pricing_engine.pricing.credit_loss import CreditLossProfile
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight
from balance_pricing_engine.scenarios.cohort_accounting import (
    ActiveCohort,
    BookedCohort,
    PortfolioEconomics,
    evaluate_active_portfolio,
    position_at,
    reconciles,
)
from balance_pricing_engine.scenarios.credit_loss_simulation import (
    CreditLossSimulationConfig,
    _CreditLossSimulationInputs,
    run_credit_loss_simulation,
)
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics, run_baseline_diagnostics
from balance_pricing_engine.synthetic.market import MarketState

# Market fields a period's drift may move, in the order they are checked.
_DRIFTED_MARKET_FIELDS = (
    "cash_rate",
    "competitor_loan_rate",
    "competitor_deposit_rate",
    "home_loan_reference_rate",
    "term_deposit_reference_rate",
)


@dataclass(frozen=True)
class PeriodEconomics:
    """One period's new-business, back-book and full active-book economics.

    ``back_book_incremental_contribution`` is a portfolio attribution
    (``active_portfolio_objective - new_business_objective``): it includes the change in the
    nonlinear portfolio funding terms caused by the surviving book, so it is not a standalone
    contractual profit of the earlier cohorts.
    """

    period: int
    cash_rate: float
    wholesale_funding_rate: float
    n_active_cohorts: int
    new_loan_volume: float
    surviving_loan_volume: float
    total_active_loan_volume: float
    new_deposit_volume: float
    surviving_deposit_volume: float
    total_active_deposit_volume: float
    new_business_objective: float
    active_portfolio_objective: float
    back_book_incremental_contribution: float
    loan_interest_income: float
    expected_credit_loss: float
    capital_charge: float
    deposit_interest_cost: float
    wholesale_funding_cost: float
    excess_deposit_income: float
    funding_shortfall: float
    funding_shortfall_penalty: float
    discount_factor: float
    discounted_contribution: float
    active_cohorts: tuple[ActiveCohort, ...] = ()


@dataclass(frozen=True)
class MultiPeriodDiagnostics:
    period_diagnostics: list[ScenarioDiagnostics]
    period_discounted_contributions: list[float]
    npv: float
    n_periods: int
    loan_runoff_rate: float
    deposit_runoff_rate: float
    discount_rate: float
    reinvestment_spread_bps: float = 0.0
    period_economics: tuple[PeriodEconomics, ...] = ()
    booked_cohorts: tuple[BookedCohort, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def period_summary(self) -> pd.DataFrame:
        """Return one row per period.

        The first five columns keep their names and definitions: ``total_objective`` is the
        period's optimised new-business objective; ``discounted_contribution`` is the
        discounted period contribution, now of the full active book at booked terms. Its
        values (and ``npv``) differ from pre-Phase-3b output, whose back book was misstated
        (FIN-002). The remaining columns decompose it.
        """
        rows = []
        for period, (diagnostics, discounted_contribution) in enumerate(
            zip(self.period_diagnostics, self.period_discounted_contributions)
        ):
            row: dict[str, Any] = {
                "period": period,
                "best_loan_rate": diagnostics.best_result.loan_rate,
                "best_deposit_rate": diagnostics.best_result.deposit_rate,
                "total_objective": diagnostics.best_result.total_objective,
                "discounted_contribution": discounted_contribution,
            }
            if period < len(self.period_economics):
                economics = self.period_economics[period]
                row.update(
                    {
                        name: getattr(economics, name)
                        for name in _PERIOD_SUMMARY_FIELDS
                    }
                )
            rows.append(row)
        return pd.DataFrame(rows)

    def cohort_summary(self) -> pd.DataFrame:
        """Return one row per active cohort per period, at its booked contractual terms."""
        summary = pd.DataFrame(
            [
                {
                    "period": economics.period,
                    "origination_period": position.cohort.origination_period,
                    "booked_loan_rate": position.cohort.loan_rate,
                    "booked_deposit_rate": position.cohort.deposit_rate,
                    "loan_survival": position.loan_survival,
                    "deposit_survival": position.deposit_survival,
                    "loan_volume": position.loan_volume,
                    "deposit_volume": position.deposit_volume,
                    "loan_interest_income": position.loan_interest_income,
                    "expected_credit_loss": position.credit_loss,
                    "capital_charge": position.capital_charge,
                    "deposit_interest_cost": position.deposit_interest_cost,
                }
                for economics in self.period_economics
                for position in economics.active_cohorts
            ],
            columns=_COHORT_SUMMARY_COLUMNS,
        )
        if "credit_loss_profiles" in self.provenance:
            summary["credit_loss_profile_version"] = [
                position.cohort.credit_loss_profile.version
                for economics in self.period_economics
                for position in economics.active_cohorts
            ]
        return summary


_PERIOD_SUMMARY_FIELDS = (
    "new_business_objective",
    "back_book_incremental_contribution",
    "active_portfolio_objective",
    "discount_factor",
    "new_loan_volume",
    "surviving_loan_volume",
    "total_active_loan_volume",
    "new_deposit_volume",
    "surviving_deposit_volume",
    "total_active_deposit_volume",
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "excess_deposit_income",
    "funding_shortfall",
    "funding_shortfall_penalty",
    "n_active_cohorts",
    "cash_rate",
    "wholesale_funding_rate",
)

_COHORT_SUMMARY_COLUMNS = [
    "period",
    "origination_period",
    "booked_loan_rate",
    "booked_deposit_rate",
    "loan_survival",
    "deposit_survival",
    "loan_volume",
    "deposit_volume",
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
]


def run_multiperiod_diagnostics(
    config: EngineConfig,
    max_boundary_expansions: int = 2,
) -> MultiPeriodDiagnostics:
    """Roll the portfolio forward ``n_periods`` annual periods with cohort accounting."""
    config = EngineConfig.model_validate(revalidation_payload(config))
    return _roll_portfolio(config, max_boundary_expansions=max_boundary_expansions)


class CreditLossMultiPeriodSimulationConfig(_CreditLossSimulationInputs):
    """Explicitly select an illustrative profile for each annual booking period."""

    profiles: tuple[CreditLossProfile, ...]

    @model_validator(mode="after")
    def validate_profile_schedule(self) -> CreditLossMultiPeriodSimulationConfig:
        mp = self.engine_config.multiperiod
        if mp is None or len(self.profiles) != mp.n_periods:
            raise ValueError("A multi-period credit loss simulation requires one profile per period.")
        versions: dict[str, CreditLossProfile] = {}
        for profile in self.profiles:
            if versions.setdefault(profile.version, profile) != profile:
                raise ValueError(f"Credit loss profile version {profile.version!r} is ambiguous.")
        return self


def run_multiperiod_credit_loss_simulation(
    config: CreditLossMultiPeriodSimulationConfig,
) -> MultiPeriodDiagnostics:
    """Project synthetic bookings using the assigned period profile and booked cohort loss."""
    config = CreditLossMultiPeriodSimulationConfig.model_validate(config)
    result = _roll_portfolio(
        config.engine_config,
        max_boundary_expansions=config.max_boundary_expansions,
        profiles=config.profiles,
    )
    return replace(
        result,
        provenance={
            **result.provenance,
            "config_hash": effective_config_hash(config),
            "loss_model": config.loss_model,
            "governance_status": "REVIEW_REQUIRED",
            "credit_loss_profiles": [profile.model_dump(mode="json") for profile in config.profiles],
        },
    )


def _roll_portfolio(
    config: EngineConfig,
    *,
    max_boundary_expansions: int,
    profiles: tuple[CreditLossProfile, ...] | None = None,
) -> MultiPeriodDiagnostics:
    mp = config.multiperiod
    if mp is None:
        raise ValueError("config.multiperiod is required for multi-period diagnostics.")

    # Effective per-period loan runoff: the tenor override is a 1/tenor approximation.
    effective_loan_runoff = (
        min(1.0, 1.0 / mp.loan_tenor_years)
        if mp.loan_tenor_years is not None
        else mp.loan_runoff_rate
    )
    _warn_for_drift_and_reinvestment(config)

    # Same capital basis as the single-period engine (runner.run_baseline_diagnostics).
    loan_rwa_weight = maturity_adjusted_rwa_weight(
        config.balance_sheet.loan_rwa_weight,
        config.balance_sheet.average_loan_tenor_years,
    )

    period_diagnostics: list[ScenarioDiagnostics] = []
    period_economics: list[PeriodEconomics] = []
    booked_cohorts: list[BookedCohort] = []

    for period in range(mp.n_periods):
        scenario_config = _build_period_config(config, period)
        market = MarketState(**scenario_config.market.model_dump())
        if profiles is None:
            diagnostics = run_baseline_diagnostics(
                scenario_config, max_boundary_expansions=max_boundary_expansions
            )
        else:
            diagnostics = run_credit_loss_simulation(
                CreditLossSimulationConfig(
                    engine_config=scenario_config,
                    profile=profiles[period],
                    max_boundary_expansions=max_boundary_expansions,
                )
            ).diagnostics
        best = diagnostics.best_result
        new_cohort = BookedCohort.from_result(
            best,
            origination_period=period,
            loan_rwa_weight=loan_rwa_weight,
            capital_hurdle_rate=scenario_config.balance_sheet.capital_hurdle_rate,
            credit_loss_profile=diagnostics.credit_loss_profile,
        )
        new_position = position_at(
            new_cohort, period, effective_loan_runoff, mp.deposit_runoff_rate
        )
        new_only = evaluate_active_portfolio([new_position], market, scenario_config.balance_sheet)
        if not reconciles(new_only.total_objective, best.total_objective):
            raise ValueError(
                f"Period {period} new-business objective {best.total_objective!r} does not "
                f"reconcile to its booked cohort ({new_only.total_objective!r})."
            )

        surviving = [
            position
            for position in (
                position_at(cohort, period, effective_loan_runoff, mp.deposit_runoff_rate)
                for cohort in booked_cohorts
            )
            if not position.is_run_off
        ]
        active = evaluate_active_portfolio(
            [*surviving, new_position], market, scenario_config.balance_sheet
        )
        discount_factor = 1.0 / (1.0 + mp.discount_rate) ** (period + 1)

        period_diagnostics.append(diagnostics)
        period_economics.append(
            _period_economics(
                period=period,
                market=market,
                new_business=best.total_objective,
                new_only=new_only,
                active=active,
                active_cohorts=(*surviving, new_position),
                discount_factor=discount_factor,
            )
        )
        booked_cohorts.append(new_cohort)

    contributions = [economics.discounted_contribution for economics in period_economics]
    return MultiPeriodDiagnostics(
        period_diagnostics=period_diagnostics,
        period_discounted_contributions=contributions,
        npv=sum(contributions),
        n_periods=mp.n_periods,
        loan_runoff_rate=effective_loan_runoff,
        deposit_runoff_rate=mp.deposit_runoff_rate,
        discount_rate=mp.discount_rate,
        reinvestment_spread_bps=mp.reinvestment_spread_bps,
        period_economics=tuple(period_economics),
        booked_cohorts=tuple(booked_cohorts),
        provenance=_provenance(config),
    )


def _period_economics(
    *,
    period: int,
    market: MarketState,
    new_business: float,
    new_only: PortfolioEconomics,
    active: PortfolioEconomics,
    active_cohorts: tuple[ActiveCohort, ...],
    discount_factor: float,
) -> PeriodEconomics:
    return PeriodEconomics(
        period=period,
        cash_rate=market.cash_rate,
        wholesale_funding_rate=market.wholesale_funding_rate,
        n_active_cohorts=len(active_cohorts),
        new_loan_volume=new_only.loan_volume,
        surviving_loan_volume=active.loan_volume - new_only.loan_volume,
        total_active_loan_volume=active.loan_volume,
        new_deposit_volume=new_only.deposit_volume,
        surviving_deposit_volume=active.deposit_volume - new_only.deposit_volume,
        total_active_deposit_volume=active.deposit_volume,
        new_business_objective=new_business,
        active_portfolio_objective=active.total_objective,
        back_book_incremental_contribution=active.total_objective - new_business,
        loan_interest_income=active.loan_interest_income,
        expected_credit_loss=active.expected_credit_loss,
        capital_charge=active.capital_charge,
        deposit_interest_cost=active.deposit_interest_cost,
        wholesale_funding_cost=active.wholesale_funding_cost,
        excess_deposit_income=active.excess_deposit_income,
        funding_shortfall=active.funding_shortfall,
        funding_shortfall_penalty=active.funding_shortfall_penalty,
        discount_factor=discount_factor,
        discounted_contribution=active.total_objective * discount_factor,
        active_cohorts=active_cohorts,
    )


def _warn_for_drift_and_reinvestment(config: EngineConfig) -> None:
    mp = config.multiperiod
    assert mp is not None  # checked by the caller
    if mp.market_cash_rate_drift_bps != 0 and (
        mp.competitor_loan_rate_drift_bps == 0 or mp.competitor_deposit_rate_drift_bps == 0
    ):
        warnings.warn(
            "market_cash_rate_drift_bps is set but competitor rates are not drifted. "
            "Competitor gaps will shift mechanically, distorting elasticity calculations.",
            stacklevel=3,
        )
    if mp.reinvestment_spread_bps != 0:
        warnings.warn(
            "reinvestment_spread_bps is retained for compatibility but not applied: surviving "
            "loans earn their booked contractual rate and the roll has no maturity, rollover "
            "or reinvestment event to price.",
            stacklevel=3,
        )


def _provenance(config: EngineConfig) -> dict[str, Any]:
    mp = config.multiperiod
    assert mp is not None  # checked by the caller
    if config.segmented_pricing is not None:
        pricing_mode = "segmented_pricing"
    elif config.pricing_strategy is not None:
        pricing_mode = "pricing_strategy"
    else:
        pricing_mode = "products"
    return {
        "projection": "rolling_annual_scenario_projection",
        # run_baseline_diagnostics is called without a model registry: the synthetic
        # response functions price every period. No ML champion drives these results.
        "response_model": "synthetic",
        "pricing_mode": pricing_mode,
        "config_hash": effective_config_hash(config),
        "synthetic_population": (
            "stationary: every period re-simulates the same seeded prospect population "
            "(random_seed); cohorts are identified by origination period, not by customer"
        ),
        "loan_runoff_basis": "loan_tenor_years" if mp.loan_tenor_years is not None else (
            "loan_runoff_rate"
        ),
        "reference_rate_drift": mp.reference_rate_drift,
        "discount_convention": "end_of_period",
        "reinvestment_spread_applied": False,
    }


def _build_period_config(config: EngineConfig, period: int) -> EngineConfig:
    """Return the validated config for ``period``: drift applied once, from the period-0 base."""
    mp = config.multiperiod
    if mp is None:
        raise ValueError("config.multiperiod is required for multi-period diagnostics.")
    market = config.market
    loan_drift = period * mp.competitor_loan_rate_drift_bps / 10_000
    deposit_drift = period * mp.competitor_deposit_rate_drift_bps / 10_000
    update: dict[str, float] = {
        "cash_rate": market.cash_rate + period * mp.market_cash_rate_drift_bps / 10_000,
        "competitor_loan_rate": market.competitor_loan_rate + loan_drift,
        "competitor_deposit_rate": market.competitor_deposit_rate + deposit_drift,
    }
    if mp.reference_rate_drift == "track_competitor":
        # The reference rates are the market benchmarks the spread grids are quoted against;
        # they move with the competitor benchmark so spreads keep their meaning.
        if market.home_loan_reference_rate is not None:
            update["home_loan_reference_rate"] = market.home_loan_reference_rate + loan_drift
        if market.term_deposit_reference_rate is not None:
            update["term_deposit_reference_rate"] = (
                market.term_deposit_reference_rate + deposit_drift
            )
    for name in _DRIFTED_MARKET_FIELDS:
        value = update.get(name)
        if value is not None and value < 0 and value != getattr(market, name):
            raise ValueError(
                f"Multi-period drift makes period {period} market.{name} negative "
                f"({value:.6f}); shorten the horizon or reduce the drift."
            )
    return config.model_copy(update={"market": market.model_copy(update=update)})
