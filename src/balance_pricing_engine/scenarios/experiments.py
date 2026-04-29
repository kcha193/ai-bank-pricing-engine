"""Small scenario experiments used by notebooks and scripts."""

from __future__ import annotations

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics, run_baseline_diagnostics


def build_higher_funding_penalty_config(
    config: EngineConfig,
    multiplier: float = 1.5,
) -> EngineConfig:
    """Return a copy of the config with a higher funding shortfall penalty."""
    return config.model_copy(
        update={
            "balance_sheet": config.balance_sheet.model_copy(
                update={
                    "funding_shortfall_penalty_rate": (
                        config.balance_sheet.funding_shortfall_penalty_rate * multiplier
                    )
                }
            )
        }
    )


def run_higher_funding_penalty_scenario(
    config: EngineConfig,
    multiplier: float = 1.5,
) -> ScenarioDiagnostics:
    """Run baseline diagnostics after multiplying the funding shortfall penalty."""
    return run_baseline_diagnostics(
        build_higher_funding_penalty_config(config, multiplier=multiplier)
    )
