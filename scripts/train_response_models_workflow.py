"""Train synthetic response models from the command line."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from balance_pricing_engine.config import load_config
from balance_pricing_engine.modelling.evaluation import RateResponseCheck
from balance_pricing_engine.modelling.workflows import run_response_model_workflow


def main() -> None:
    """Run the response modelling workflow and print metrics and response checks."""
    config = load_config(Path("configs/base.yaml"))
    result = run_response_model_workflow(config)

    print_response_model_report(result.loan_model_report)
    print()
    print_response_model_report(result.deposit_model_report)


def print_response_model_report(report: dict[str, Any]) -> None:
    """Print a response model report returned by the modelling workflow."""
    print(report["title"])
    print("  Logistic regression metrics")
    for key, value in report["metrics"].items():
        print(f"    {key}: {value:.4f}")
    print("  Calibration")
    for row in report["calibration"].itertuples(index=False):
        print(
            f"    bin={int(row.bin)}, "
            f"mean_pred={row.mean_predicted_probability:.3f}, "
            f"observed={row.observed_rate:.3f}, "
            f"count={int(row.count)}"
        )
    print_rate_response_check(report["response_check"])


def print_rate_response_check(check: RateResponseCheck) -> None:
    """Print a compact rate-response sanity check."""
    status = "pass" if check.is_directionally_sane else "review"
    print(f"  Rate response sanity check: {status}")
    print(f"    expected_direction={check.expected_direction}")
    print(f"    violations={check.violations}")
    for row in check.rate_response.itertuples(index=False):
        print(f"    rate={row.offered_rate:.2%}, mean_pred={row.mean_predicted_probability:.3f}")


if __name__ == "__main__":
    main()
