# Governed Scenario Example Script Design

## Context

`CLAUDE.md` states: "Prefer `application.PricingService` over calling
`run_baseline_diagnostics()` directly from new callers (API endpoints, CLI
commands)." Today only `api/app.py`'s `/v1/pricing/scenarios` endpoint goes
through `PricingService`. Every script under `examples/` — `run_baseline.py`,
`run_multiperiod.py`, `run_with_competitor_rates.py`,
`run_llm_diagnostics.py` — still calls `run_baseline_diagnostics()` (or
similar domain-layer entry points) directly. There is no reference example
showing the governed application-layer path outside the API, so the pattern
new callers are supposed to follow is undocumented in the one place
(`examples/`) developers are most likely to copy from.

This gap was identified while reviewing repo status: it's a carry-over open
item from before PR #42 (modular-monolith architecture merge), never closed.

## Goals

- Add one new example script demonstrating `PricingService.run_scenario()`
  end to end: config load → governed decision → console summary.
- Make the governance surface visible: decision status
  (RECOMMENDED/REVIEW_REQUIRED/BLOCKED), the full evaluated gate list, and
  model lineage (which response source drove each product).
- Preserve the existing `run_baseline.py` untouched — it remains the
  domain-math walkthrough; the new script is the governed-decision
  walkthrough.
- Demonstrate the correct caller discipline for `GovernanceError`: let it
  propagate, don't catch-and-hide it.

## Non-Goals

- No trained/approved `ModelRegistry` champion, no shadow-challenger
  evaluation, no `JSONLEventPublisher` audit trail. These are real
  `PricingScenarioRequest` options but pull in `modelling/` training as a
  prerequisite and would make this a training-workflow example, not a
  governed-path example.
- No ledger persistence (`persist_ledger=True`) and no sensitivity analysis
  (`include_sensitivity=True`) in this script — both are already demonstrated
  by `run_baseline.py` (ledger) and its sensitivity summary block.
- No new automated test. `run_baseline.py` has none either; example scripts
  in this repo are illustrative, not test-covered.
- No changes to `PricingService`, `contracts.py`, or any non-`examples/`
  file.

## Design

**File:** `examples/run_governed_scenario.py` (new).

**Flow:**

1. `load_config(CONFIG_PATH)` with `CONFIG_PATH = Path("configs/base.yaml")`
   — same config source as `run_baseline.py`, for an apples-to-apples
   comparison between the two scripts.
2. Build the request:

   ```python
   request = PricingScenarioRequest(
       config=config,
       scenario_name="governed_baseline",
       config_path=CONFIG_PATH,
   )
   ```

   `config_path` is passed even though `persist_ledger` is `False`, because
   `PricingService._config_hash()` hashes the real source YAML when
   `config_path` is given (falling back to a JSON-dump hash otherwise) — the
   decision record is more representative of a real call this way, at no
   extra cost.
3. `decision = PricingService().run_scenario(request)` — **no**
   `try/except GovernanceError`. A code comment explains why: this call is
   documented (in `pricing_service.py` and `CLAUDE.md`) to let
   `GovernanceError` propagate unchanged when an approved champion's artifact
   is missing/unloadable, and callers must not downgrade that into a
   `BLOCKED` decision. Nothing in this minimal run can trigger it (no
   registry is supplied, so model lineage always resolves to the synthetic
   fallback), but the script still models the correct calling convention
   rather than silently implying it's safe to swallow.
4. Print a console summary, structured differently from
   `run_baseline.py`'s domain-math printout — this one narrates the
   *decision*, not the *math*:

   ```text
   Governed pricing decision
     Decision ID:             <uuid>
     Correlation ID:          <uuid>
     Status:                  RECOMMENDED
     Scenario:                governed_baseline

     Home loan rate:          8.10%
     Term deposit rate:       4.85%
     Objective value:         $9,250,626
     Expected loan volume:    $508,992,979
     Expected deposit volume: $330,238,746
     Funding shortfall:       $606,690

   Governance gates
     CONFIG_VALID: PASS — Scenario configuration passed EngineConfig validation.
     COMPETITOR_RATE_FRESHNESS: PASS — Competitor-rate ingestion is disabled; using static config rates.
     MODEL_APPROVAL_STATUS[loan]: PASS — No approved ML champion for loan; using the deterministic synthetic response function (safe default).
     ...
     (one line per GateResult: `{code}: {status} — {message}`)

   Model lineage
     loan:    response_source=deterministic_synthetic, used_fallback=True
     deposit: response_source=deterministic_synthetic, used_fallback=True

   Execution time: 0.42s
   ```

   Rate/currency formatting reuses the same `:.2%` / `:,.0f` conventions as
   `run_baseline.py`, applied inline (`f"{decision.best_loan_rate:.2%}"` etc.)
   — `PricingDecision`'s rate/volume fields are always-present floats (unlike
   `run_baseline.py`'s optional market-reference rates), so the
   `_format_rate`/`_format_bps` `None`-handling helpers aren't needed here and
   won't be copied over.

**Imports required (new to `examples/`):**

```python
from balance_pricing_engine.application.contracts import PricingScenarioRequest
from balance_pricing_engine.application.pricing_service import PricingService
```

## Testing Plan

Manual verification only, matching `run_baseline.py`'s own verification
bar:

- `python3 examples/run_governed_scenario.py` runs to completion against
  `configs/base.yaml` and exits 0.
- Output shows `Status: RECOMMENDED` for the unmodified baseline config (no
  BLOCK/WARN gates expected under default settings).
- `python3 -m pytest -q` and `python3 -m ruff check .` stay green (no
  existing code touched, so this is a regression check, not new coverage).
