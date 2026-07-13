# ADR 0002: Local Feature Store Interface Before Feast/Databricks

## Status

Accepted (2026-07-13).

## Decision

Introduce a small `FeatureProvider` protocol (`features/contracts.py`) with a single
implementation, `LocalFeatureStore` (`features/local_store.py`), backed by the existing
synthetic customer generators and dataset builders. Centralize the previously duplicated
loan/deposit feature column lists into versioned, fingerprinted `FeatureSchema` objects
(`features/definitions.py`). Do not introduce Feast, a warehouse-backed store, or any
externally running feature service.

## Context

Before this change, `LOAN_FEATURE_COLUMNS` / `DEPOSIT_FEATURE_COLUMNS`
(`modelling/datasets.py`) were the only schema definition, referenced directly by
training code (`examples/train_response_models.py`), the governed registry
(`modelling/agentic.py`), and the optimizer adapter (`optimization/response_fn.py`). There
was no versioning, no fingerprint to detect a silent schema drift between training and
serving, and no single lineage object to attach to a pricing decision or model record.
The task's target architecture calls for a "feature-store-ready" boundary without
overbuilding a generic enterprise platform for a synthetic, single-team prototype.

## Alternatives considered

1. **Feast (or another managed feature store)**: full point-in-time correctness,
   online/offline store separation, multi-team feature sharing.
2. **Local versioned schema + provider protocol** (chosen): a `FeatureSchema` with a
   name, version, and content fingerprint; a `FeatureProvider` protocol with
   `training_dataset()` / `serving_features()`; one local implementation.
3. **No change**: keep referencing raw column-list constants everywhere.

## Trade-offs

| | Feast/managed store | Local provider (chosen) | No change |
| --- | --- | --- | --- |
| True point-in-time correctness | Yes | No (documented limitation — synthetic data has no real "as of" history) | No |
| Operational cost | New running infrastructure | None (in-process) | None |
| Schema/version tracking | Yes | Yes (fingerprint + version string) | No |
| Training/serving consistency | Enforced by the store | Enforced by both paths calling the same generator/dataset-builder functions and validating against the same `FeatureSchema` | Not enforced |
| Migration cost later | N/A | Low — `FeatureProvider` is designed to be implemented by a real store | High — no seam exists |

## Consequences

- `FeatureSchema.fingerprint` (a SHA-256 hash of name/version/columns/target) is recorded
  in `FeatureLineage`, which flows into the governance decision record
  (`docs/adr/0003-governance-centred-decision-record.md`), giving an audit trail answer to
  "were training and serving features the same shape for this decision?"
  (`MODEL_FEATURE_COMPATIBILITY` gate in `governance/gates.py`).
  `FeatureSchema.validate()` raises `FeatureSchemaError` before scoring proceeds if a
  frame is missing required columns — the "detect obvious schema mismatches" requirement.
- `LocalFeatureStore.serving_features()` is used for lineage recording and compatibility
  checks; it does **not** replace the optimizer's own per-rate scoring loop
  (`optimization/response_fn.MLResponseFnAdapter`), which still varies `offered_rate`
  across the full grid. This is a deliberate scope limit — the feature layer observes and
  validates, it does not intermediate every scoring call.
- The `as_of` field on `FeatureLineage` exists as a contract point but is trivially
  "now" for this synthetic implementation — there is no real historical feature store to
  query point-in-time against. This is documented, not hidden.
- No wiring changes the existing governance invariant: unapproved or absent champions
  still fall back to the deterministic synthetic response; the feature layer only adds
  lineage/validation around that decision, it does not change it.

## Conditions that would justify moving to Feast (or similar)

- Real (non-synthetic) customer data enters the system and point-in-time correctness
  (avoiding label leakage from future data) becomes a real risk.
- Multiple models/teams need to share a canonical online feature store with low-latency
  serving guarantees.
- Feature computation becomes expensive enough that recomputing per request (as
  `LocalFeatureStore` does today) is no longer viable.
- A dedicated MLOps/feature platform team exists to operate the additional
  infrastructure.

Until then, a local, versioned, fingerprinted provider is enough to satisfy the training/
serving consistency and lineage requirements this prototype has today.
