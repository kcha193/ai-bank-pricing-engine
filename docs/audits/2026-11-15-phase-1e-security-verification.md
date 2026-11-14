# Phase 1e security implementation evidence — 2026-11-15

This records remediation of SEC-001, SEC-002, SEC-003, SEC-004 and SEC-009.
It is implementation evidence, not a new repository audit or production approval.

Base: master `2eb9ab3` (PRs #65–#68 merged). Branch:
`fix/phase-1e-api-deployment-security`. Work began on 2026-11-14.

## Before and after

| Boundary | Before | After |
| --- | --- | --- |
| API launch | `0.0.0.0`, keys optional | `127.0.0.1`; every other bind requires well-formed keys before serving |
| API work | Unbounded customers, YAML and grids | Shared limits on both POST routes, before simulation/allocation |
| Expansion/sensitivity | Initial size did not bound subsequent work | Conservative cumulative budgets, including six-decimal rounding and raw-bps allocation |
| Alert failure | Exception strings could reveal token-bearing URLs | Fixed categories, trusted exception labels, destination category and alert ordinal |
| Streamlit Operations | Unset token permitted actions regardless of binding | Numeric loopback only without a token; every callback checks policy |
| API filesystem | Arbitrary competitor source paths, reflected errors | Canonical defaults only; safe validation messages and relative ledger references |
| Temporary configs | Predictable shared directories, default write permissions | Exclusive private files and cleanup, including effective competitor overlays |

Raw uvicorn launch commands are no longer supported; use the module launcher in
[deployment.md](../deployment.md). It enforces uvicorn concurrency limits. Application body
and workload limits remain enforced independently. Docker explicitly binds inside its
container and requires runtime keys; examples publish only to host loopback.

## Regression traceability

| Requested cases | Evidence |
| --- | --- |
| T01–T04 | `test_api_security.py`: launcher default, external bind with/without rotating keys, malformed keys and safe startup failures |
| T05–T07, T14 | Both POST routes: missing/invalid keys return 401; both rotation keys permit canonical bounded requests |
| T08–T12 | Both routes reject YAML/body/customer/grid/combined/expansion/sensitivity excess before execution; streamed bodies and subnormal steps covered |
| T13 | Existing API baseline and sensitivity success tests; six baseline artifacts compared byte-for-byte against master |
| T15–T16 | `test_pricing_intelligence_alert_security.py`: newline/whitespace inside actual tokens/URLs, mocked default urllib transport, raw log records and stdout/stderr checked |
| T17–T19 | Dashboard Operations/settings tests: actual CLI/env precedence without starting a server, unsafe/unknown binds, direct callback refusal and valid unlock |
| T20–T21 | API overrides refused even when disabled; existing trusted custom-file ingestion tests still pass |
| T22 | Sanitised YAML/schema/service errors; ledger response contains no absolute server path; schema-approved field names remain useful |
| T23–T25 | Mode 0600, request-owned files, concurrent requests, write/serialisation failure cleanup, execution failure cleanup and unchanged snapshot/hash |
| T26 | Parsed Docker bind configuration is rejected without runtime keys and accepted with keys; daemon-free configuration tests |
| T27 | Full offline suite includes financial input, ROE/gates, ML champion integrity, LLM patch and Phase 1d transparency regressions |

Core regression batches were written and observed failing before their fixes. Additional
transport and compatibility checks extend those batches. Binding/auth tests
first reproduced 24 failures; API resource/path/temp tests reproduced 34 failures; malformed
scalar conversion and effective-overlay tests reproduced four failures. Alert tests reproduced
nine leaks, and dashboard tests reproduced eleven unsafe-binding/callback failures plus the
non-string address edge case.

Independent review found a raw-bps allocation bypass: separately converted endpoints could
collapse to the same float while the domain attempted 10,000,001 initial points. Four new
both-product/both-route regressions failed before correction and passed after it. Initial
counts are now validated in original units and preserved for combined budgets. Scoped
independent re-review found no remaining issue in that fix.

The first full run also caught six Phase 1c message regressions. Sanitisation now retains only
field names declared by the server schema, preserving useful `market.cash_rate` errors without
reflecting values, arbitrary mapping keys or raw exception messages. All six regressions pass.

## Verification

- Ruff and `git diff --check`: pass.
- Focused API/dashboard/alerts/application/governance/integration set: 567 passed.
- Additional launch/configuration set: 112 passed; financial-input/API set: 241 passed.
- Full offline suite: **2,637 passed, 2 skipped**.
- Mandatory full coverage run: **2,637 passed, 2 skipped; 96.56% coverage**, exceeding the 95% gate.
- Canonical `grid_results.csv`, `top_results.csv`, `summary.json`, `sensitivity.csv`,
  `counterfactuals.csv` and `cohort_attribution.csv`: **byte-identical** to master.
- Docker smoke: not run because the installed CLI cannot reach a Docker daemon. No daemon
  was started and no service was deployed or externally exposed.

All tests use synthetic data, saved fixtures or mocked transports. No bank collection,
Telegram/webhook delivery, production configuration change or automatic pricing action occurred.

## Compatibility and remaining boundaries

The API intentionally rejects custom filesystem paths, YAML aliases/ambiguous mappings and
work above its budgets. `run_dir` is a relative string; readiness exposes a config basename.
Trusted CLI paths, economics, model controls and ledger provenance remain compatible.

The Operations token protects administrative actions only. Remote read-only access needs
authenticated ingress and TLS. API shared keys provide neither user roles nor per-client
quotas. Uvicorn concurrency is a launcher control, not a CPU deadline or durable disk quota.
Ledger retention, independently reviewed network security, calibration and real model-risk
approval remain outside this synthetic decision-support implementation.
