# Remediation Roadmap — 2026-11-13 audit

This roadmap fixes the findings in
[2026-11-13-full-repository-audit.md](2026-11-13-full-repository-audit.md). Each finding has
machine-readable details in
[2026-11-13-full-repository-evidence.json](2026-11-13-full-repository-evidence.json).

Baseline: `master` @ `62b81d2`.

There are no P0 findings. Work is ordered by **risk reduction per change**, not by ease. Each
phase is a set of small, independent PRs.

Rules for every PR:
- **Regression contract.** Keep the `summary.json` / `grid_results.csv` contract. New keys are
  additive only.
- **Invariants.** Keep the `AGENTS.md` invariants, and update `docs/architecture.md` and
  `docs/modelling_assumptions.md` whenever behaviour changes.
- **Verification.** Run `ruff` and `pytest -m 'not live_rates'` on every PR.

---

## Phase 1 — Close fail-open decision controls and misleading outputs (P1/P2, mostly S)

These are small fixes that stop wrong numbers being presented as approved. None of them changes
pricing economics.

| PR | Findings | Files | Change | Acceptance criteria / tests |
|---|---|---|---|---|
| 1a | OPT-001, GOV-002 | `optimization/grid_search.py`, `diagnostics.py`, `scenarios/runner.py`, `governance/gates.py`, `AGENTS.md` | Make `select_best_result` return whether the ROE floor was relaxed. Add the flag as an additive `summary.json` key `roe_constraint_relaxed`. Add gates `ROE_FLOOR_MET`, `OBJECTIVE_POSITIVE` and `VOLUME_NONZERO`, using WARN unless the policy is changed deliberately. Update the gate policy text | A floor of 10.0 gives a relaxed flag plus a WARN gate, so the status is never RECOMMENDED. Negative objective → WARN. Zero loan volume → WARN. Existing `summary.json` keys are unchanged |
| 1b | GOV-001 (with SEC-007) | `modelling/agentic.py` (`resolve_response_fn`, `promote_champion`), `governance/gates.py` | Store `artifact_sha256`, `feature_names` and `n_features` at approval. At resolution, verify the hash, `predict_proba`, the feature count and names, and a probe score with finite values in [0, 1] and a monotone sign. Raise `GovernanceError` on any mismatch | The J2b, J4b and J4c registries from `evidence/mlgov/challenge_j.py` all raise `GovernanceError` from `PricingService.run_scenario`. J1, J2 and J3 behave as before |
| 1c | OPT-003, GOV-007 | `config.py`, `llm/scenario_interface.py` | Set `allow_inf_nan=False` on the economic models. Validate signs: loan elasticities ≤ 0, deposit elasticities ≥ 0. Give the LLM patch path a field allow-list, and log the patch content and approver | A NaN or inf competitor rate, cash rate or elasticity fails validation. A positive loan elasticity fails. A mock patch flipping the signs is rejected |
| 1d | OPT-004, UX-001, UX-003, UX-007 | `pricing_scenario_trigger.py`, `dashboard/scenario_text.py`, `dashboard/views/scenarios.py`, `dashboard/services.py` | Carry `policy_band_breached`, `boundary`, `capped`, shortfall and ROE-relaxed on `ScenarioRow`, and add a one-line status to the alert block. Rename "model optimum" to "Median-benchmark optimum (reference)". Label breaching results "Model-best — outside policy band (review)". Use the human strategy labels | An AppTest shows the summary text agrees with the headline. The alert text contains a status for a breaching row. The deposit rationale never shows `SECOND_LOWEST` for a second-highest selection |
| 1e | SEC-001, SEC-003, SEC-002, SEC-004, SEC-009 | `api/server.py`, `api/app.py`, `api/schemas.py`, `config.py` (limits), `.streamlit/config.toml`, `dashboard/app.py`, `dashboard/settings.py`, `pricing_intelligence/alerts.py`, `docs/deployment.md` | Default the API host to `127.0.0.1`, and set `0.0.0.0` explicitly in the Dockerfile `ENV`. Refuse a non-loopback bind when no keys are set, unless an explicit opt-out is given. Cap customer counts, grid points and YAML length. Pin the Streamlit address to loopback. Log only the exception class for alert failures. Reject API `source_file` overrides. Use `mkstemp` | Default host is loopback. A non-loopback bind with no keys raises. An oversized request returns 422. A token with a newline never appears in `caplog` |
| 1f | UX-002, UX-004 | `dashboard/views/scenarios.py`, `ui.py` | Hide or grey out stale results after an input change. Add persistent MODELLED · synthetic and OBSERVED badges | AppTest: changing an input hides the previous headline. The badges are present on scenario and market pages |
| 1g | GOV-004 | `application/pricing_service.py` | Hash the canonical `config.model_dump(mode="json")` as the decision config hash, and record the file hash separately | An in-memory override gives a hash different from `base.yaml`. The API path is unchanged |

**Dependencies:** 1a lands before 1d (the flags feed the UI). 1b is independent. 1c lands before
re-running any calibration.

---

## Phase 2 — Make ingestion recoverable and honest (P1/P2)

| PR | Findings | Files | Change | Acceptance criteria / tests |
|---|---|---|---|---|
| 2a | DATA-002, DATA-016 | `pricing_intelligence/store.py`, `cli.py`, `orchestration.py`, `health.py` | Add an authenticated, append-only `approve` / `reject` / `void` command that calls `confirm_suspicious_changes`. Return exit 2 and a health escalation while any reading stays quarantined for N runs. Add a peer-median plausibility check | Q7 replay: the approved move is admitted and becomes trusted; unapproved, it escalates on day N. Q6: a +60 bp outlier is flagged, and a void event removes it from the snapshot |
| 2b | DATA-001, DATA-019 | `competitor_rates/extractors.py`, `pricing_intelligence/service.py`, fixtures | Treat TD "special" rows as comparable promotions (tagged), or mark the bank partial with exit 2. Add fixtures for a TD special, balance tiers, k/m abbreviations and a partial page | `evidence/ingest/td_special_mixed.py` either counts the 4.50% special or exits 2 with a warning |
| 2c | DATA-004, DATA-003 | `pricing_intelligence/dashboard_data.py`, `health.py`, `strategy.py`, `snapshot.py`, `config.py` | Take freshness from the latest attempt in `operational_runs`. Add a `min_banks` setting. Show the oldest input age and bank count on the strategy result and in the UI | Case I-b: a total outage shows "Failed / stale", not "Fresh". Case H: a one-bank median is refused, or WARNs when below `min_banks` |
| 2d | DATA-006, DATA-018, SEC-010 | `competitor_rates/cli.py`, `apply.py`, `store.py` | Either retire the legacy path for term deposits, or run the deposit clean-up there, surface warnings, average per bank and escape CSV | Legacy strict TD validation passes on the fixtures; non-strict mode emits stale warnings |
| 2e | DATA-007, DATA-008, DATA-009, DATA-010 | `collectors/base.py`, `scripts/collect_market_rates_workflow.py`, `store.py`, `deploy/scheduling/*` | Use a bank-specific readiness selector or minimum row count, and record a timeout as degraded. Add an overall run deadline and escalate repeated exit 3. Use WAL plus a busy timeout and classify `database is locked`. Make CLI `collect` take the lock | `db_locked.py` writes a summary with a classified error. The deadline is enforced in a fake-clock test |
| 2f | DATA-005, DATA-013, DATA-014 | `snapshot.py`, `config.py`, `base.py` | Configurable TD reference tier and payment frequency. An `eligibility_inferred` flag. Validate `our_bank` against the known bank list | A $500k+ tier no longer sets the default benchmark. `our_bank="ASB Bank"` errors or normalises |

**Live verification required before closing 2b and 2e.** Run a supervised collection that
confirms TD page structures (specials, tiers) and readiness timing. Record it as dated evidence.

---

## Phase 3 — Fix optimiser and multi-period economics (P1, M)

| PR | Findings | Files | Change | Acceptance criteria / tests |
|---|---|---|---|---|
| 3a | OPT-002 | `optimization/segmented.py` | Put deposit funding and penalty into loan-tier selection. Use coordinate descent over tiers until no tier changes, or an exhaustive joint search when the tier grid is small | A single catch-all tier gives the same result as `optimize_joint_rates` on the same grid (`evidence/fin/followup.py`). Multi-tier objective ≥ single-tier objective |
| 3b | FIN-002 | `scenarios/multiperiod.py` | Carry each cohort's own loan rate, ECL, capital and funding mix on surviving balances, and drift the reference rates with competitor drift. Otherwise relabel the output "new-business NPV" and drop the back book | Zero runoff and zero discount: period-1 contribution ≈ 2 × period-0. Runoff of 1 gives a back book of 0 |
| 3c | TEST-002 | `tests/unit/` | Add property and metamorphic tests for the invariants listed below | All new tests fail on the current master for 3a/3b/1a (confirming they detect the defects) and pass after the fixes |

Invariant tests to add in 3c:
- partial effects A and B;
- segmented ≡ joint for a single tier;
- held-book NPV;
- the ROE-relaxed flag;
- finite-input rejection;
- the canonical config inside the policy band, or a documented reason attached.

---

## Phase 4 — Calibration and economic representation (needs subject-matter validation)

These items are judgemental. **Do not tune elasticities until the recommendation matches the
market** (`modelling_assumptions.md` already warns against this).

| Item | Findings | Proposal | Validation needed |
|---|---|---|---|
| ECL level | FIN-001 | Replace `0.0015 + 0.018·risk^1.4` with a PD × LGD mapping calibrated to prime NZ residential experience, by LVR band | Credit-risk SME; published bank disclosures |
| Deposit supply | FIN-001 | Replace "N synthetic prospects" with a market-size × share curve, so the deposit pool is not an artefact of `n_deposit_customers` | Treasury SME |
| FTP curve | FIN-003 | Term-matched wholesale cost (1y swap + liquidity premium); report ROE without the steering penalty, and add tax and opex optionally | Treasury / ALM SME |
| Capital | FIN-004 | Configurable tier-1 ratio; LVR-banded RWA; drop the maturity uplift for retail | Capital SME |
| Presentation | UX-005 | Objective waterfall in Pricing Studio | — |
| Documentation | FIN-005, MOD-008 | Correct the deposit-funding mechanism text; document the own/gap identifiability conditions | — |

**Acceptance:** documented calibration evidence, and a canonical recommendation inside the
policy band or explicitly justified.

---

## Phase 5 — ML pipeline validity and approval controls (P1/P2, M)

| PR | Findings | Files | Change | Acceptance criteria / tests |
|---|---|---|---|---|
| 5a | MOD-001, MOD-003 | `modelling/datasets.py`, `workflows.py`, `features/local_store.py` | Sample the competitor rate per row, independently of the offered rate. Pass `response.coefficients` through | std(competitor) > 0; \|corr(offered, gap)\| < 0.95. The logistic model recovers both signs. Changing the coefficients changes the training ground truth |
| 5b | MOD-002 | `workflows.py`, registry schema, `optimization/response_fn.py`, `governance/gates.py` | Train on the policy-expanded grid and store the training rate range. Add a `RATE_OUTSIDE_TRAINING_RANGE` gate. Use HGB `monotonic_cst` | An approved HGB with the base config cannot reach RECOMMENDED; the range gate fires |
| 5c | MOD-004, MOD-005 | `modelling/agentic.py`, `reports.py` | Group split by `customer_id`; score and persist `test_metrics`. Forbid promoting the oracle baseline, and derive labels from `model_type` | Customer overlap is 0%. `promote_champion(baseline)` raises |
| 5d | GOV-003, GOV-005, GOV-008, GOV-009 | `agentic.py`, `pricing_service.py`, `run_ledger/memo.py`, `checklist.py`, `decision_record.py` | Require a non-empty approver distinct from the trainer, plus `passed_validation`. Use the lineage captured at resolution. Make the memo and sensitivity lineage-aware. Add `data_basis: synthetic` to the decision record. Refuse checklist sign-off when BLOCKED. Write the registry atomically | An empty approver raises. The decision record contains `data_basis`. Sign-off of a BLOCKED run raises |

---

## Phase 6 — Hygiene (P3, independent, any order)

- **CI and supply chain** (TEST-003, SEC-006):
  - Set `requires-python>=3.11`, or add 3.9 to the CI matrix.
  - Add a constraints or lock file.
  - Add `permissions: contents: read` and pin actions by SHA.
  - Pin the Docker base image by digest.
  - Add `.env*` to `.dockerignore`.
  - Enable Dependabot.
- **Launcher test** (TEST-001 / UX-011): make it hermetic by pinning `PRICING_WATCH_PORT` to a
  free port.
- **Architecture** (ARCH-001): move the ingestion pydantic contracts into a leaf module; break
  the `health ↔ service` cycle.
- **Dashboard polish:** UX-006, UX-008, UX-009, UX-010, UX-012, UX-013.
- **Ingestion polish:**
  - DATA-011: term and balance parse edge cases.
  - DATA-012: parse effective dates, or change the UI copy.
  - DATA-015: status naming.
  - DATA-017: scheduled backup with an integrity check and a restore command.
  - DATA-020: docs.
- **Security polish:**
  - SEC-005: identifier patterns.
  - SEC-007: integrity hash; done in 1b.
  - SEC-008: read-only dashboard store.
- **ML polish:**
  - MOD-006: document the circularity.
  - MOD-007: PDP gap recompute.
  - GOV-006: `REGISTRY_SUPPLIED_NOT_USED` WARN.
  - GOV-010: drift and retention policy, as a design note.

---

## Quick wins and architectural changes

**Quick wins (S, high value):** 1a, 1c, 1d, 1e, 1g, 2c, 2d. Also the CI `permissions:` block and
the hermetic launcher test.

**Architectural changes (M, needs a plan):**
- 2a: quarantine approval workflow.
- 3a: segmented coordination.
- 3b: cohort back-book.
- 5a and 5b: ML data design.
- Phase 4: calibration.
- ARCH-001: contracts module.

None justifies a framework change or a service split. The modular monolith (ADR-0001) remains
appropriate.

## Recommended PR boundaries

- One finding cluster per PR, as in the tables above.
- Each PR adds its regression test first. It should fail on `62b81d2` and pass after the fix.
- Do not combine calibration changes (Phase 4) with code-correctness fixes (Phases 1–3). That
  keeps economic shifts attributable in `grid_results.csv` diffs.
- Ingestion PRs touching parsing (2b, 2e) need a dated live-verification note before merge.

## What should not be automated or deployed until the listed phases land

| Do not automate or deploy | Until |
|---|---|
| Alerts or Telegram messages containing "Model optimum" | 1a, 1d |
| ML champion pricing | 1b, 5a, 5b, 5c |
| Unattended TD benchmarking | 2a–2c, plus live verification |
| The API on any non-loopback interface | 1e |
| Any use of recommendations outside synthetic exploration | Phase 4 calibration evidence and an independent model validation outside this codebase |
