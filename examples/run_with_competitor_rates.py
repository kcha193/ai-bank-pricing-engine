"""Run the pricing scenario with validated competitor-rate inputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from balance_pricing_engine.competitor_rates.apply import (
    apply_competitor_rates_to_config,
    load_and_resolve_competitor_rates,
)
from balance_pricing_engine.competitor_rates.models import CompetitorRatesConfig
from balance_pricing_engine.config import load_config
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run baseline scenario with competitor rates")
    parser.add_argument("--config", default="configs/base.yaml")
    parser.add_argument("--source-file", default="data/processed/competitor_rates/latest.json")
    parser.add_argument(
        "--strict",
        type=lambda v: v.lower() not in {"false", "0", "no"},
        default=True,
        metavar="BOOL",
        help="Fail on stale or invalid data (default: true). Pass 'false' to warn only.",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    competitor_config = config.competitor_rates or CompetitorRatesConfig()
    competitor_config = competitor_config.model_copy(
        update={"enabled": True, "source_file": args.source_file, "strict": args.strict}
    )
    config = config.model_copy(update={"competitor_rates": competitor_config})
    resolved = load_and_resolve_competitor_rates(competitor_config)
    applied = apply_competitor_rates_to_config(config)
    diagnostics = run_baseline_diagnostics(applied)
    result = diagnostics.best_result
    decomposition = diagnostics.winner_decomposition()

    print("Competitor rate inputs")
    print(
        f"  Mortgage competitor reference:     {resolved.mortgage_reference_rate:.2%} "
        f"({resolved.mortgage_strategy})"
    )
    print(
        f"  Term deposit competitor reference: {resolved.term_deposit_reference_rate:.2%} "
        f"({resolved.term_deposit_strategy})"
    )
    print(f"  Banks included:                    {', '.join(competitor_config.selected_banks)}")
    print(f"  Mortgage selected term:            {competitor_config.mortgage.term_months} months")
    print(f"  Term deposit selected term:        {competitor_config.term_deposit.term_months} months")
    print(f"  Captured at UTC:                   {resolved.captured_at_utc.isoformat()}")
    if resolved.validation_warnings:
        for warning in resolved.validation_warnings:
            print(f"  WARNING: {warning}")
    print()
    print("Best joint pricing decision")
    print(f"  Home loan rate:          {result.loan_rate:.2%}")
    print(f"  Term deposit rate:       {result.deposit_rate:.2%}")
    print(f"  Objective value:         ${result.objective_value:,.0f}")
    print()
    print("Winner objective decomposition")
    print(f"  Loan interest income:      ${decomposition.loan_interest_income:,.0f}")
    print(f"  Expected credit loss:      ${decomposition.expected_credit_loss:,.0f}")
    print(f"  Deposit interest cost:     ${decomposition.deposit_interest_cost:,.0f}")
    print(f"  Wholesale funding cost:    ${decomposition.wholesale_funding_cost:,.0f}")
    print(f"  Funding shortfall penalty: ${decomposition.funding_shortfall_penalty:,.0f}")
    print(f"  Total objective:           ${decomposition.total_objective:,.0f}")


if __name__ == "__main__":
    main()
