# ADR 0003: Governance-Centred Decision Record

## Status

Accepted (2026-07-13).

## Decision

Add an explicit, typed governance layer (`governance/gates.py`, `governance/lineage.py`,
`governance/decision_record.py`) that evaluates a fixed set of gates over every completed
scenario, derives a `DecisionStatus` (`RECOMMENDED` / `REVIEW_REQUIRED` / `BLOCKED`), and
serialises the result — plus model lineage, feature lineage, and stage timings — into an
additive `decision_record.json` artifact written alongside the existing run-ledger files.
Do not rename, remove, or restructure any existing artifact
(`summary.json`, `metadata.json`, `grid_results.csv`, etc.).

## Context

The engine already had strong governance primitives: `ModelRegistry` approval/champion
workflow, boundary-diagnostics warnings, policy-band checks, and a fail-loud
`GovernanceError` for a broken approved-champion state. What it lacked was a single,
queryable record that ties all of these together per decision — the boundary flags,
policy-band flag, model lineage, and feature lineage lived in different objects
(`ScenarioDiagnostics`, `RunMetadata`, ad hoc registry lookups) with no unified "was this
decision safe to act on, and why" answer, and no explicit typed severity model beyond
booleans scattered across `summary.json`/`metadata.json`.

## Alternatives considered

1. **Extend `RunMetadata`/`summary.json` in place** with more boolean flags.
2. **Explicit gates + decision record as a new additive artifact** (chosen): a
   `GateResult` (code, status, severity, evidence, remediation) per check, aggregated into
   a `DecisionStatus`, serialised into `decision_record.json`.
3. **A general-purpose rules engine** (configurable gate DSL, pluggable policy packs).

## Trade-offs

| | Extend existing files | Decision record (chosen) | Rules engine |
| --- | --- | --- | --- |
| Backward compatibility | Risky — changes documented schema | Safe — purely additive file | Safe if additive, but high complexity |
| Explainability per gate | Poor (flat booleans) | Good (code, message, severity, evidence, remediation per gate) | Good, but over-engineered for ~10 fixed gates |
| Extensibility | Ad hoc | Add a new `GateResult`-returning function | Highest, but unnecessary today |
| Implementation cost | Low | Moderate | High |

## Consequences

- `evaluate_gates()` runs a fixed, documented gate set: config validity, competitor-rate
  freshness/strictness, model approval status, model artifact availability, model/feature
  compatibility, boundary warning, expansion-cap, policy-band breach, funding shortfall,
  lineage completeness, and objective finiteness.
- Policy is conservative but not maximal, and is documented, not implicit: a BLOCK on any
  gate yields `BLOCKED`; any WARN (with no BLOCK) yields `REVIEW_REQUIRED`; a clean gate
  set yields `RECOMMENDED`. The underlying optimiser recommendation is preserved even when
  `REVIEW_REQUIRED` — governance does not silently discard a result a human might still
  need to see, it only withholds the "safe to act on" label.
- The `GovernanceError` fail-loud invariant is **not** re-implemented or downgraded here:
  it still raises out of `ModelScoringAgent.resolve_response_fn`
  (via `run_baseline_diagnostics(registry=...)`) and propagates out of
  `PricingService.run_scenario` unchanged. The governance gates only run over scenarios
  that completed successfully; an inconsistent governed model state is never represented
  as a `BLOCKED` decision, because it must never reach the point of producing rates at
  all.
- `decision_record.json` is additive: written only when `persist_ledger=True`, into the
  same run directory `write_run_ledger()` already creates, without touching any other
  file's contents or schema.

## Conditions that would justify moving to MLflow (or a similar governed platform)

- Multiple models/teams need a shared, concurrency-safe, queryable registry beyond a
  single local JSON file (`ModelRegistry`).
- Governance approvals need a workflow UI, role-based access control, or multi-approver
  sign-off beyond a markdown checklist and a JSON registry record.
- Decision records need to be queried/aggregated across thousands of historical runs
  (a database, not per-run JSON files, becomes the right storage).
- Regulatory reporting requires a tamper-evident, centrally audited store rather than
  filesystem artifacts under version control or a shared drive.

Until then, typed in-process gates plus an additive JSON decision record give full
explainability at the cost the current single-team, prototype-scale project can bear.
