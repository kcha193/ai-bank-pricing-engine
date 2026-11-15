# PR #72 quarantine review fixes

> **For agentic workers:** Use superpowers:executing-plans for the two fixes, then superpowers:requesting-code-review and superpowers:verification-before-completion.

**Goal:** Close both P1 review findings without automatic decisions or historical look-ahead.

**Architecture:** Preserve the stored warning on materially unchanged pending readings. Apply review decisions to change events at read time, using the query timestamp, just as observation trust already does. Keep raw event evidence and the append-only review ledger intact.

**Tech stack:** Python >=3.9, SQLite, pytest; no new dependencies.

**Spec:** PR #72 discussions r4576723951 and r4576723955 and the supplied review-remediation request.

## Constraints

- Work on `feat/phase-2a-quarantine-governance`; never merge automatically.
- Synthetic offline fixtures only; no live collection, production databases, alerts or deployment.
- Preserve operator authentication, exact event targeting, distinct-run escalation and product separation.
- Preserve Decimal basis points, public interfaces and existing database compatibility.

## Review focus

- Peer insufficiency and recovery must retain the same pending case and count distinct runs.
- Approval/void boundaries are inclusive; earlier queries must remain unchanged.
- Companion events follow single-hop approval, but stale multi-hop companions remain suspicious.
- Legacy databases, including approvals that already cleared event flags, need time-aware reads.
- Rejected/voided repeats remain excluded; genuinely different readings follow normal validation.

## Task 1: Pending quarantine persistence

Files: `service.py`, `store.py`, `quarantine_store.py`, and quarantine regression tests.

- [x] Add failing offline tests for a peer-only +60 bps flag followed by collector outages, repeat, peer recovery, replayed run, explicit approval/rejection and a fresh valid reading. Parameterise both products.
- [x] Run tests and record the trust-bypass failure.
- [x] Preserve pending material identity in validation and remove the store's implicit confirmation transition.
- [x] Run focused review, escalation, peer and audit tests.

## Task 2: Historical change visibility

Files: `quarantine_store.py`, `store.py`, review regression tests, `docs/architecture.md`, `docs/modelling_assumptions.md`.

- [x] Add failing before/after approval and void tests, including precise boundaries, companions, multi-hop chains and both products.
- [x] Run tests and record historical activity changing after a later approval.
- [x] Project effective change flags/endpoints using time-bounded reviews; stop rewriting events during operator approval. Preserve legacy confirmation API compatibility.
- [x] Verify raw event immutability alongside effective public event semantics; document the shared trust boundary.
- [x] Run focused tests, offline Q6/Q7 replay, Ruff and full offline tests with the 95% coverage gate.
- [ ] Inspect diff, request independent review, fix actionable issues, commit and push to PR #72.
- [ ] Reply to verified threads, resolve them, request `@codex review`, and fetch final reviews/checks. Report pending external work accurately.

## Execution evidence

- Stayed on the existing PR branch as requested. No worktree or routine approval pause was needed.
- Used `.venv/bin/python` because system Python 3.14 has no pytest; the existing venv runs Python 3.12.13.
- Initial RED: four failures reproduced both P1s independently for deposits and loans.
- Independent review exposed legacy multi-hop timestamp recovery, including eligibility reversions. Added RED regressions and reused existing active-baseline reconstruction.
- Additional RED coverage exposed exact-key-only rebasing across eligibility changes; reused trusted supersession and pairing rules.
- Two old store tests explicitly required automatic confirmation. Replaced those obsolete expectations with stronger identity, warning, ledger and trusted-fallback assertions.
- Final focused run: 251 passed. Independent final re-review: no actionable issues; 184 tests passed independently.
- Q6/Q7 replay output matches the preserved evidence payload exactly. Baseline and response-model examples exit successfully.
- Final full offline run: **2,871 passed, 2 skipped**, **96.79% coverage** (required 95%); Ruff and `git diff --check` pass. No live collection, production database, real notification or deployment was used.
