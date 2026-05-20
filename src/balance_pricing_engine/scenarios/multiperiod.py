"""Multi-period scenario roll orchestration."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics, run_baseline_diagnostics


@dataclass(frozen=True)
class MultiPeriodDiagnostics:
    period_diagnostics: list[ScenarioDiagnostics]
    period_discounted_contributions: list[float]
    npv: float
    n_periods: int
    loan_runoff_rate: float
    deposit_runoff_rate: float
    discount_rate: float

    def period_summary(self) -> pd.DataFrame:
        """Return one row per period with headline pricing and objective values."""
        return pd.DataFrame(
            [
                {
                    "period": period,
                    "best_loan_rate": diagnostics.best_result.loan_rate,
                    "best_deposit_rate": diagnostics.best_result.deposit_rate,
                    "total_objective": diagnostics.best_result.total_objective,
                    "discounted_contribution": discounted_contribution,
                }
                for period, (diagnostics, discounted_contribution) in enumerate(
                    zip(self.period_diagnostics, self.period_discounted_contributions)
                )
            ]
        )


def run_multiperiod_diagnostics(
    config: EngineConfig,
    max_boundary_expansions: int = 2,
) -> MultiPeriodDiagnostics:
    mp = config.multiperiod
    if mp is None:
        raise ValueError("config.multiperiod is required for multi-period diagnostics.")

    period_diagnostics: list[ScenarioDiagnostics] = []
    period_discounted_contributions: list[float] = []
    back_book_loan_income = 0.0
    back_book_deposit_cost = 0.0

    for period in range(mp.n_periods):
        scenario_config = _build_period_config(config, period)
        diagnostics = run_baseline_diagnostics(
            scenario_config,
            max_boundary_expansions=max_boundary_expansions,
        )
        back_book_income = back_book_loan_income - back_book_deposit_cost
        period_net = diagnostics.best_result.total_objective + back_book_income
        discounted_contribution = period_net / (1 + mp.discount_rate) ** period

        period_diagnostics.append(diagnostics)
        period_discounted_contributions.append(discounted_contribution)

        surviving_loan_fraction = (1 - mp.loan_runoff_rate) ** (period + 1)
        surviving_deposit_fraction = (1 - mp.deposit_runoff_rate) ** (period + 1)
        back_book_loan_income += (
            diagnostics.best_result.loan_income_after_losses * surviving_loan_fraction
        )
        back_book_deposit_cost += diagnostics.best_result.deposit_cost * surviving_deposit_fraction

    return MultiPeriodDiagnostics(
        period_diagnostics=period_diagnostics,
        period_discounted_contributions=period_discounted_contributions,
        npv=sum(period_discounted_contributions),
        n_periods=mp.n_periods,
        loan_runoff_rate=mp.loan_runoff_rate,
        deposit_runoff_rate=mp.deposit_runoff_rate,
        discount_rate=mp.discount_rate,
    )


def _build_period_config(config: EngineConfig, period: int) -> EngineConfig:
    mp = config.multiperiod
    if mp is None:
        raise ValueError("config.multiperiod is required for multi-period diagnostics.")
    cash_rate = config.market.cash_rate + period * mp.market_cash_rate_drift_bps / 10_000
    updated_market = config.market.model_copy(update={"cash_rate": cash_rate})
    return config.model_copy(update={"market": updated_market})
