"""Run the baseline joint pricing scenario."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from balance_pricing_engine.config import load_config
from balance_pricing_engine.scenarios.runner import run_baseline_scenario


def main() -> None:
    config = load_config(Path("configs/base.yaml"))
    result = run_baseline_scenario(config)

    print("Best joint pricing decision")
    print(f"  Home loan rate:          {result.loan_rate:.2%}")
    print(f"  Term deposit rate:       {result.deposit_rate:.2%}")
    print(f"  Objective value:         ${result.objective_value:,.0f}")
    print(f"  Loan income after loss:  ${result.loan_income_after_losses:,.0f}")
    print(f"  Deposit cost:            ${result.deposit_cost:,.0f}")
    print(f"  Wholesale funding cost:  ${result.wholesale_funding_cost:,.0f}")
    print(f"  Funding penalty:         ${result.funding_penalty:,.0f}")
    print(f"  Expected loan volume:    ${result.expected_loan_volume:,.0f}")
    print(f"  Expected deposit volume: ${result.expected_deposit_volume:,.0f}")
    print(f"  Funding shortfall:       ${result.funding_shortfall:,.0f}")


if __name__ == "__main__":
    main()
