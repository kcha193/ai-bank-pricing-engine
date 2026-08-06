"""Run the baseline scenario through the governed application-layer path.

Unlike run_baseline.py (which calls run_baseline_diagnostics() directly to
walk through the domain math), this script goes through
application.PricingService -- the seam CLAUDE.md says new callers (API, CLI,
examples) should prefer over calling the domain layer directly, so that
governance evaluation, lineage recording, and decision-record construction
happen the same way they do in api/app.py's /v1/pricing/scenarios endpoint.
"""

from __future__ import annotations

from pathlib import Path

from balance_pricing_engine.application.contracts import PricingScenarioRequest
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.config import load_config

CONFIG_PATH = Path("configs/base.yaml")


def main() -> None:
    config = load_config(CONFIG_PATH)
    request = PricingScenarioRequest(
        config=config,
        scenario_name="governed_baseline",
        config_path=CONFIG_PATH,
    )

    # No try/except GovernanceError here: PricingService.run_scenario()
    # documents that GovernanceError from an approved champion with a
    # missing/unloadable artifact must propagate unchanged, never be
    # downgraded to a BLOCKED PricingDecision. Only a delivery layer (e.g.
    # the /v1/pricing/scenarios FastAPI handler) is allowed to translate it
    # into a transport-appropriate error response. This run supplies no
    # registry, so model lineage always resolves to the synthetic fallback
    # and GovernanceError cannot be raised here -- but the calling
    # convention is kept correct regardless.
    decision = PricingService().run_scenario(request)

    print("Governed pricing decision")
    print(f"  Decision ID:             {decision.decision_id}")
    print(f"  Correlation ID:          {decision.correlation_id}")
    print(f"  Status:                  {decision.status.value}")
    print(f"  Scenario:                {decision.scenario_name}")
    print()
    print(f"  Home loan rate:          {decision.best_loan_rate:.2%}")
    print(f"  Term deposit rate:       {decision.best_deposit_rate:.2%}")
    print(f"  Objective value:         ${decision.objective_value:,.0f}")
    print(f"  Expected loan volume:    ${decision.expected_loan_volume:,.0f}")
    print(f"  Expected deposit volume: ${decision.expected_deposit_volume:,.0f}")
    print(f"  Funding shortfall:       ${decision.funding_shortfall:,.0f}")
    print()
    print("Governance gates")
    for gate in decision.governance.gates:
        print(f"  {gate.code}: {gate.status.value} - {gate.message}")
    print()
    print("Model lineage")
    for product, lineage in decision.model_lineage.items():
        label = f"{product}:"
        print(
            f"  {label:<9} response_source={lineage.response_source}, "
            f"used_fallback={lineage.used_fallback}"
        )
    print()
    print(f"Execution time: {decision.execution_duration_seconds:.2f}s")


if __name__ == "__main__":
    main()
