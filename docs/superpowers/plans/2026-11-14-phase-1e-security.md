# Phase 1e API and deployment security implementation plan

**Goal:** Close SEC-001, SEC-002, SEC-003, SEC-004 and SEC-009 without changing pricing economics.

**Spec:** Phase 1e in `docs/audits/2026-11-13-remediation-roadmap.md`, expanded by the user's implementation brief.

**Architecture:** Enforce network policy in the supported API launcher. Keep resource limits and untrusted YAML handling in the API delivery boundary, leaving trusted CLI configuration and domain mathematics unchanged. Resolve the effective Streamlit binding before offering or executing Operations actions.

**Constraints:** Synthetic data only; no live collection, alerts, deployment or automatic pricing. Preserve governance, lineage, ledger snapshots and existing success response shapes. No new dependency. Work from master `2eb9ab3` on `fix/phase-1e-api-deployment-security`; one PR, no merge.

## Tasks

1. API binding and authentication (`api/server.py`, `api/auth.py`, API tests, Dockerfile).
   - Write and run failing regressions for loopback defaults, external binding rejection, valid rotation, malformed keys and safe startup errors.
   - Default to 127.0.0.1; require valid keys for external binding. Preserve constant-time comparisons. Bound uvicorn concurrency to four by default. Support only the module launcher in documentation.
   - Run focused API tests before proceeding.
2. API resource and file boundaries (`api/limits.py`, `api/app.py`, API security tests).
   - Write and run failing regressions before implementation on both POST routes.
   - Bound streamed JSON bodies, YAML bytes/depth/nodes, customers, grid dimensions, joint size, cumulative expansion and sensitivity work before simulation. Reject aliases and malformed YAML safely.
   - Preserve the canonical competitor source path for baseline compatibility; reject every path override, even when disabled. Keep trusted CLI paths unchanged.
   - Materialise YAML with exclusive mode-0600 temporary files; clean up on write, execution and success paths. Return relative run references and sanitised errors.
   - Verify concurrent files, original config hashes/snapshots, baseline economics and API response compatibility.
3. Independent delivery fixes (parallel-agent workflow).
   - Alert agent: reproduce secret-bearing exception leaks, then use fixed failure categories and safe metadata; preserve deadlines and retries.
   - Dashboard agent: test effective CLI/env/config binding and callback authorisation; pin 127.0.0.1; preserve Operations token length, limiter and action controls.
   - Review both diffs and run their focused suites.
4. Documentation and release evidence.
   - Update deployment, API/dashboard usage, architecture, assumptions and operational contracts with secure examples and explicit compatibility limitations.
   - Run Ruff, full offline tests, full coverage with 95% gate and focused integration tests. Docker smoke only if daemon available, without publishing ports.
   - Inspect final diff; obtain independent security review, resolve material findings, commit, push feature branch and open the requested PR. Update project status.

## Review focus

- Tiny grid steps, numeric overflow and floating-point expansion rounding must not bypass budgets.
- Chunked bodies, YAML aliases/recursive structures and deep malformed documents must fail before expensive work.
- Malformed rotation entries and non-ASCII credentials must fail safely without revealing secrets.
- Streamlit CLI precedence and stale unlocked sessions must not bypass callback authorisation.
- Temporary-file write failures and concurrent requests must preserve cleanup and ledger provenance.

## Decisions and evidence

- User explicitly requested execution and a PR; no additional design or publication approval is needed within that scope.
- Dedicated feature branch in the clean existing checkout fulfils the requested isolation; no extra worktree is needed.
- API limits remain delivery-specific so trusted offline workflows retain their existing capacity.
- Docker CLI exists, but daemon is unavailable (`/var/run/docker.sock` missing); do not start or configure Docker.

## Completion evidence

Implementation and independent review are complete. Full offline tests and coverage both pass:
2,637 passed, 2 skipped; coverage 96.56%; Ruff and diff checks clean. Six canonical artifacts
are byte-identical to master. The raw-bps allocation issue found in review was reproduced,
fixed and independently rechecked. Useful schema-approved validation field names preserve
Phase 1c compatibility without reflecting submitted values. See
`docs/audits/2026-11-15-phase-1e-security-verification.md` for regression traceability,
compatibility restrictions and the Docker daemon limitation. Delivery is one PR; no merge.
