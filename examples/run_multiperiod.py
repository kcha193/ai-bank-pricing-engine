"""Run a multi-period pricing scenario roll."""

from __future__ import annotations

from pathlib import Path

from balance_pricing_engine.config import MultiPeriodConfig, load_config
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics


CONFIG_PATH = Path("configs/base.yaml")


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

    print("Multi-period pricing scenario")
    print(diagnostics.period_summary().to_string(index=False))
    print()
    print(f"NPV: ${diagnostics.npv:,.0f}")


if __name__ == "__main__":
    main()
