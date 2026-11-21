"""Run a multi-period pricing scenario roll (rolling annual synthetic projection)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from balance_pricing_engine.config import MultiPeriodConfig, load_config
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics


CONFIG_PATH = Path("configs/base.yaml")

SUMMARY_COLUMNS = [
    "period",
    "best_loan_rate",
    "best_deposit_rate",
    "new_business_objective",
    "back_book_incremental_contribution",
    "active_portfolio_objective",
    "total_active_loan_volume",
    "total_active_deposit_volume",
    "discount_factor",
    "discounted_contribution",
]

COMPONENT_COLUMNS = [
    "period",
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "funding_shortfall_penalty",
    "excess_deposit_income",
]


def main() -> None:
    config = load_config(CONFIG_PATH)
    config = config.model_copy(
        update={
            "multiperiod": MultiPeriodConfig(
                n_periods=3,
                loan_runoff_rate=0.30,
                deposit_runoff_rate=0.50,
            )
        }
    )
    diagnostics = run_multiperiod_diagnostics(config)
    summary = diagnostics.period_summary()

    with pd.option_context("display.float_format", "{:,.4f}".format, "display.width", 160):
        print("Multi-period pricing scenario (synthetic rolling annual projection)")
        print(summary[SUMMARY_COLUMNS].to_string(index=False))
        print()
        print("Active-book components")
        print(summary[COMPONENT_COLUMNS].to_string(index=False))
        print()
        print("Active cohorts at their booked contractual terms")
        print(diagnostics.cohort_summary().to_string(index=False))
    print()
    print(f"NPV: ${diagnostics.npv:,.0f}")
    print(
        "Back-book contribution is a portfolio attribution (active book minus new business), "
        "not a standalone cohort profit. Not an ALM forecast."
    )


if __name__ == "__main__":
    main()
