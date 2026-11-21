"""Phase 3b (FIN-002) before/after comparison for the multi-period roll.

Run from the repository root against any checkout (master or the fix branch)::

    PYTHONPATH=<checkout>/src python docs/audits/2026-11-21-phase-3b-evidence/phase_3b_comparison.py

Every scenario uses ``configs/base.yaml`` (synthetic, seed 42) with a scenario-specific
``multiperiod`` block. It prints one JSON document; nothing is written.

The "implied back book" is ``period contribution / discount factor - new-business objective``,
which both versions expose, so the before and after figures are computed the same way. The
component breakdown (``period_economics``) exists only on the fixed branch.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

from balance_pricing_engine.config import MultiPeriodConfig, load_config
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics

CONFIG_PATH = Path("configs/base.yaml")

SCENARIOS: dict[str, dict] = {
    "01_fin002_zero_runoff": dict(
        n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=0.0, discount_rate=0.0
    ),
    "02_full_runoff": dict(
        n_periods=2, loan_runoff_rate=1.0, deposit_runoff_rate=1.0, discount_rate=0.0
    ),
    "03_loan_runoff_gt_deposit": dict(
        n_periods=3, loan_runoff_rate=0.5, deposit_runoff_rate=0.1, discount_rate=0.0
    ),
    "04_deposit_runoff_gt_loan": dict(
        n_periods=3, loan_runoff_rate=0.1, deposit_runoff_rate=0.5, discount_rate=0.0
    ),
    "05_rising_rates": dict(
        n_periods=3,
        loan_runoff_rate=0.3,
        deposit_runoff_rate=0.5,
        discount_rate=0.0,
        market_cash_rate_drift_bps=50,
        competitor_loan_rate_drift_bps=50,
        competitor_deposit_rate_drift_bps=50,
    ),
    "06_falling_rates": dict(
        n_periods=3,
        loan_runoff_rate=0.3,
        deposit_runoff_rate=0.5,
        discount_rate=0.0,
        market_cash_rate_drift_bps=-50,
        competitor_loan_rate_drift_bps=-50,
        competitor_deposit_rate_drift_bps=-50,
    ),
    # Deposits run off fully while loans survive: the surviving loans need wholesale funding
    # and push the portfolio deeper below the 65% deposit-funding requirement.
    "07_funding_shortfall": dict(
        n_periods=3, loan_runoff_rate=0.0, deposit_runoff_rate=1.0, discount_rate=0.0
    ),
    # Loans run off fully while deposits survive: deposits accumulate above loan volume.
    "08_excess_deposits": dict(
        n_periods=4, loan_runoff_rate=1.0, deposit_runoff_rate=0.0, discount_rate=0.0
    ),
    "09_four_vintages": dict(
        n_periods=4, loan_runoff_rate=0.3, deposit_runoff_rate=0.5, discount_rate=0.0
    ),
    "10_positive_discount": dict(
        n_periods=3, loan_runoff_rate=0.3, deposit_runoff_rate=0.5, discount_rate=0.08
    ),
}

COMPONENTS = (
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "funding_shortfall_penalty",
    "excess_deposit_income",
    "funding_shortfall",
    "total_active_loan_volume",
    "total_active_deposit_volume",
)


def _run(name: str, multiperiod: dict) -> dict:
    config = load_config(CONFIG_PATH).model_copy(
        update={"multiperiod": MultiPeriodConfig(**multiperiod)}
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        diagnostics = run_multiperiod_diagnostics(config)
    rate = multiperiod.get("discount_rate", 0.08)
    periods = []
    for period, (period_diag, contribution) in enumerate(
        zip(diagnostics.period_diagnostics, diagnostics.period_discounted_contributions)
    ):
        result = period_diag.best_result
        undiscounted = contribution * (1 + rate) ** (period + 1)
        row = {
            "period": period,
            "loan_rate": round(result.loan_rate, 6),
            "deposit_rate": round(result.deposit_rate, 6),
            "new_business_objective": round(result.total_objective, 2),
            "implied_back_book": round(undiscounted - result.total_objective, 2),
            "period_contribution_undiscounted": round(undiscounted, 2),
            "discounted_contribution": round(contribution, 2),
        }
        economics = getattr(diagnostics, "period_economics", None)
        if economics:
            row["components"] = {
                key: round(getattr(economics[period], key), 2) for key in COMPONENTS
            }
        periods.append(row)
    return {"multiperiod": multiperiod, "npv": round(diagnostics.npv, 2), "periods": periods}


def main() -> None:
    print(json.dumps({name: _run(name, mp) for name, mp in SCENARIOS.items()}, indent=1))


if __name__ == "__main__":
    main()
