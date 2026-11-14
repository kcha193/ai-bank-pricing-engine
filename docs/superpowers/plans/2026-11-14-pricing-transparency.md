# Pricing recommendation transparency implementation plan

**Goal:** Fix OPT-004, UX-001, UX-003 and UX-007 without changing pricing economics.

**Spec:** Phase 1d in `docs/audits/2026-11-13-remediation-roadmap.md` and the owner's detailed Phase 1d request.

**Architecture:** Carry each run's own diagnostic evidence through additive result fields. Share pure presentation helpers between alerts and dashboard services. Keep median benchmarking as a reference to the existing median run, rather than a fifth scenario. Keep legacy `model_optimum` accessors. Streamlit renders backend-provided status and labels; it does not calculate governance or economics.

**Constraints:** Synthetic data only; portfolio pricing; no ingestion, governance-gate, optimiser or artifact changes. No live collection, external alert delivery, deployment or automatic pricing. Python 3.9 compatible. Existing positional constructors remain usable.

## Review focus

- Missing evidence must require review rather than imply a clean or approved result.
- Different strategies retain their own flags, values and objectives.
- Deposit strategies and joint rationales use highest/second-highest terminology.
- Initial boundary expansion is distinct from an unresolved final boundary.
- Diagnostic review status is not a full governance decision or operational approval.

## Task 1: Diagnostic evidence and alert presentation

- [x] Add failing trigger tests for independent diagnostic propagation, four unique scenarios, median reference aliases, numeric identity and JSON persistence.
- [x] Add offline alert tests for both products, combined warnings and legacy/missing metadata.
- [x] Add pure reporting helpers and additive `ScenarioRow`/`ScenarioComparison` fields. Populate exclusively from each run's diagnostics.
- [x] Render compact per-scenario diagnostic status, product-aware labels and a median reference without duplicating its comparison row.
- [x] Run focused trigger/alert tests.

## Task 2: Dashboard services and Pricing Studio

- [x] Add failing service and AppTests for selected/reference separation, metadata, policy-aware headlines, combined warnings and product-aware rationales.
- [x] Extend view models additively and carry backend reporting summaries through `DashboardServices`.
- [x] Render model-best product labels and diagnostic reasons beside results. Use warning styling for warning-level conditions.
- [x] Update reference wording in text, charts and comparison tables; preserve selected numeric values.
- [x] Run focused dashboard, dual-product, boundary and governance regressions.

## Task 3: Verification and delivery

- [x] Document selected/reference semantics and synthetic before/after examples in pricing intelligence and dashboard documentation. Update architecture/modelling descriptions only for presentation seams.
- [x] Run Ruff, complete offline pytest suite and coverage (minimum 95%). Compare baseline rates, objectives and `summary.json` output.
- [x] Inspect final diff and conduct a fresh branch review; fix substantive findings with regression tests.
Delivery: commit, push and open the requested PR without merging; record the outcome in PR metadata and the authoritative project status.

No in-policy alternative will be inferred from a warning flag. This change preserves selection and shows the existing policy result; constrained alternative selection is outside this presentation fix.


## Verification outcome

- Ruff: passed.
- Complete offline suite: 2,502 passed, 2 skipped.
- Coverage run: 2,502 passed, 2 skipped; 96.56% (95% required).
- Focused dashboard/service/trigger/alert/workflow/governance checks: 653 passed.
- New Phase 1d regressions: 26 passed, including AppTest evidence for all three modes.
- Five canonical baseline artifacts are byte-identical to master `68d79dd`.
- Independent review found a non-finite-objective severity gap; failing-first regressions and a presentation-only fix now preserve the existing BLOCK severity. Re-review found no outstanding Critical/Important findings.
- No live collection, external alert delivery, deployment, automatic pricing action or merge.
