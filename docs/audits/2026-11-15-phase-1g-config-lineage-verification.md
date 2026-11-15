# Phase 1g configuration-lineage evidence — 2026-11-15

Remediation of GOV-004. Implementation evidence, not a new audit or production approval.
Base: master `aeaa1c2` (PR #70 merged). Branch: `fix/phase-1g-effective-config-lineage`.
Synthetic data only; no live collection, no external calls.

## Before and after (audit repro, 500 customers, `config_path=configs/base.yaml`)

`loan_rate_elasticity` −170 (base) vs in-memory override −60. Same script before and after.

| Observation | Before | After |
| --- | --- | --- |
| `decision_record.config_hash`, base | `da6bfe40…56fda` (= sha256 of `base.yaml`) | `b9102ee7…bcb810` (canonical effective config) |
| `decision_record.config_hash`, override −60 | `da6bfe40…56fda` (identical to base) | `04b646db…bed59` (differs from base) |
| `source_config_file_sha256` | absent | `da6bfe40…56fda` for both runs |
| `config_snapshot.yaml` | `base.yaml` copy | `base.yaml` copy (unchanged; labelled as source provenance) |
| `effective_config.json` | absent | present; `sha256(file) == config_hash` |
| Best loan rate (both runs) | 0.0549 | 0.0549 (pricing unchanged) |

## Semantics

* `config_hash` (decision record and `metadata.json`): SHA-256 of
  `json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)`
  of the config **after** revalidation and competitor-rate resolution. One definition, in
  `config_identity.py`.
* `source_config_file_sha256`: SHA-256 of the caller's original YAML bytes; `null` when no
  readable source file exists. Never the temp overlay.
* `config_hash_basis` (decision record): `effective_config_canonical_json_sha256`.
* `effective_config.json` (new ledger artifact): the exact hashed bytes.
* `config_snapshot.yaml`: unchanged behaviour (caller's file; the competitor overlay when
  resolution is enabled). It is no longer the verification target for `config_hash`.
* Pre-GOV-004 records/ledgers: file-bytes `config_hash`, none of the new fields. Not rewritten.

## Test traceability

| Requested cases | Evidence |
| --- | --- |
| T01–T03, file-name independence | `test_config_identity.py` |
| T04–T07, T11 | `test_effective_config_lineage.py::test_in_memory_override_*`, `test_equivalent_overrides_*` |
| T08 | `test_config_without_source_path_hashes_effective_config`, `test_file_sha256_if_present_*` |
| T09–T10 | `test_resolved_competitor_rates_drive_hash_and_source_stays_independent` (loan and deposit each shown to matter) |
| T12–T13 | `test_persisted_ledger_carries_verifiable_effective_config`, `test_competitor_ledger_effective_config_json_*` |
| T14–T15 | `test_existing_summary_and_grid_contracts_unchanged` |
| T16 | `test_pricing_values_unchanged_by_hashing`; existing `test_run_scenario_matches_baseline_recommendation_exactly` |
| T17 | `test_api_security.py::test_ledger_records_effective_hash_original_yaml_hash_and_safe_relative_reference`; both-route security tests unchanged |
| T18 | `test_direct_write_run_ledger_records_effective_hash_and_source_hash`; unchanged `PricingService` suites |
| T19 | `test_legacy_decision_record_json_without_new_fields_is_still_readable` (new fields additive with defaults) |
| T20 | Full suite below |

## Verification

* `ruff check .`: clean.
* `pytest -m "not live_rates"`: 2740 passed, 2 skipped.
* Coverage: 96.60% (gate 95%).
