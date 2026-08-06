# Governed Scenario Example Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `examples/run_governed_scenario.py`, a new example script that runs the baseline scenario through `application.PricingService.run_scenario()` instead of calling `run_baseline_diagnostics()` directly, demonstrating the governed application-layer path that `CLAUDE.md` says new callers should prefer.

**Architecture:** Single new script. Loads `configs/base.yaml`, builds a `PricingScenarioRequest`, calls `PricingService().run_scenario()`, and prints a decision-centric console summary (status, rates, governance gates, model lineage). No production code changes — `PricingService`, `contracts.py`, and `run_baseline.py` are all untouched.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations` for `|` unions, matching existing `examples/*.py` style), stdlib only — no new dependencies.

## Global Constraints

- Full spec: `docs/superpowers/specs/2026-08-06-run-governed-scenario-example-design.md`.
- Do not modify `examples/run_baseline.py` or any file under `src/balance_pricing_engine/` — this is additive only.
- No new automated test (per spec Non-Goals) — `run_baseline.py` has none either; verification is running the script and reading its output, plus a full-suite regression check.
- ruff line-length 100, target-version `py39` (`pyproject.toml`) — keep lines within 100 chars.
- No inline comments except the one `GovernanceError`-propagation comment called for by the spec (explains a non-obvious invariant); don't add narrative comments elsewhere.

---

### Task 1: Create `examples/run_governed_scenario.py`

**Files:**
- Create: `examples/run_governed_scenario.py`

**Interfaces:**
- Consumes: `balance_pricing_engine.config.load_config(path: Path) -> EngineConfig` (existing, used identically in `examples/run_baseline.py:7,17`); `balance_pricing_engine.application.contracts.PricingScenarioRequest` (existing, `src/balance_pricing_engine/application/contracts.py:36-63`); `balance_pricing_engine.application.pricing_service.PricingService` (existing, `src/balance_pricing_engine/application/pricing_service.py:95-101`), specifically `PricingService().run_scenario(request) -> PricingDecision`.
- Produces: nothing consumed by later tasks — this is the only task in the plan.

- [ ] **Step 1: Write the script**

Create `examples/run_governed_scenario.py` with exactly this content:

```python
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
        print(f"  {gate.code}: {gate.status.value} — {gate.message}")
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
```

- [ ] **Step 2: Run the script and verify output**

Run: `.venv/bin/python3 examples/run_governed_scenario.py`

Expected: exits 0. Output includes:
- A `Governed pricing decision` block with a `Status:` line reading `REVIEW_REQUIRED` (the unmodified `configs/base.yaml` baseline trips a `POLICY_BAND_BREACH: WARN` gate — the optimizer's true optimum sits outside the config's originally configured ±50bps/±40bps spread band once adaptive boundary expansion resolves it, which is expected, by-design governance behavior, not a failure — though the exact loan/deposit rates and dollar figures matching `run_baseline.py`'s baseline output are not asserted here, only the decision status and successful completion).
- A `Governance gates` block listing multiple `CODE: PASS — message` lines (at minimum `CONFIG_VALID`, `COMPETITOR_RATE_FRESHNESS`, `MODEL_APPROVAL_STATUS[loan]`, `MODEL_APPROVAL_STATUS[deposit]`, `MODEL_ARTIFACT_AVAILABLE[loan]`, `MODEL_ARTIFACT_AVAILABLE[deposit]`, `MODEL_FEATURE_COMPATIBILITY[loan]`, `MODEL_FEATURE_COMPATIBILITY[deposit]`, `BOUNDARY_WARNING`, `EXPANSION_CAPPED`, `POLICY_BAND_BREACH`, `FUNDING_SHORTFALL`, `LINEAGE_COMPLETE`, `OBJECTIVE_FINITE`).
- A `Model lineage` block showing `loan:` and `deposit:` each with `response_source=deterministic_synthetic, used_fallback=True` (no registry was supplied, so both products fall back to the synthetic response — see `governance/lineage.py:49-55`).
- A trailing `Execution time: <N>.NNs` line.

`Status: REVIEW_REQUIRED` (via `POLICY_BAND_BREACH: WARN`) is the correct, expected result for the unmodified baseline config — do not treat it as a bug to fix. If the script instead raises an exception, or `Status:` comes back `BLOCKED`, stop and diagnose before proceeding — those are the actual failure signals worth investigating, not something to paper over.

- [ ] **Step 3: Run the full test suite as a regression check**

Run: `.venv/bin/python3 -m pytest -q`

Expected: `598 passed` (same count as the pre-existing baseline — this task adds no new tests, so the count must not change). No failures.

- [ ] **Step 4: Run ruff as a regression check**

Run: `.venv/bin/python3 -m ruff check .`

Expected: `All checks passed!` (the new file must be lint-clean under the repo's `line-length=100` / `target-version="py39"` config in `pyproject.toml`).

- [ ] **Step 5: Commit**

```bash
git add examples/run_governed_scenario.py
git commit -m "$(cat <<'EOF'
feat: add governed-path example script

Demonstrates application.PricingService.run_scenario() end to end --
the seam CLAUDE.md says new callers should prefer -- so examples/ has a
reference alongside run_baseline.py's direct-domain-layer walkthrough.
Closes the gap where only api/app.py's /v1/pricing/scenarios endpoint
exercised the governed path.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
