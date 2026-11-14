# Scenario provenance implementation plan

> **For agentic workers:** Use superpowers:executing-plans for this single remediation task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve PR #70's manual-input provenance finding without changing numerical results.

**Architecture:** Add a pure presentation helper to `dashboard/scenario_text.py`, based on `ScenarioRequest`. Render product-specific source explanations after input selection and from the stored result's own request. Keep modelled output labels and separate comparison assumptions.

**Tech stack:** Python, Streamlit AppTest, pytest, Ruff.

**Spec:** PR #70 review comment `discussion_r4576070156` and the user's remediation brief (T01–T15).

## Global constraints

- Synthetic data only; no live collection, deployment or merge.
- Preserve pricing mathematics, comparison values and backend strategy selection.
- Preserve stale-result hiding and explicit execution only.
- MANUAL uses the entered rate directly and ignores offsets.
- Joint scenarios retain separate market strategies, terms and offsets, without manual selection.

## Review focus

- MANUAL rates must never inherit observed provenance or stale offsets.
- Market aggregates and adjusted rates must not be called observed bank quotes.
- Joint deposit strategy fallback must match the backend.
- Missing or invalid inputs must not receive a provenance claim.
- Switching strategies must not expose old results or silently recompute them.

### Task 1: Correct selected-input provenance

**Files:** `dashboard/scenario_text.py`, `dashboard/views/scenarios.py`, `tests/unit/test_dashboard_provenance.py`, `tests/unit/test_dashboard_pages_scenarios.py` (all beneath their existing source/test roots).

**Interface:** `benchmark_provenance(request)` returns immutable per-product presentation entries (`observed`, `text`); invalid/unavailable requests return no entries. No economics are computed.

- [x] Add AppTests for both manual products, every market strategy, offsets, strategy switching, stored request restoration, joint provenance and unavailable data. Add helper edge cases for invalid requests and backend fallback behaviour.
- [x] Run new tests against original implementation and record expected failures. Keep existing numerical and freshness tests as compatibility evidence.
- [x] Implement helper and reuse it in inputs/results. Correct tooltips and comparison-source wording.
- [x] Run the three requested targeted files, Ruff and full offline suite with coverage gate 95%.
- [x] Obtain independent adversarial review, address material findings, inspect final diff.
- Delivery: commit and push existing PR branch. Reply and resolve verified thread; request one fresh Codex review and check current CI/review state.

## Design review

The independent reviewer checked this design before implementation. MANUAL ignores even a stale offset; joint deposit strategy inherits the loan strategy when omitted. Product-aware labels must say second-highest for deposits. Market aggregates are references, not individual bank quotes. These cases are covered by the test plan.

The user explicitly requested continuous remediation through commit/push and review; no separate design approval pause is needed.

## Verification record

- Original targeted baseline: 67 passed.
- New tests against original code: 26 failed, 36 passed. Failures exposed missing source explanations, unconditional observed labels and the absent helper; numerical compatibility cases passed before the fix.
- After the fix: provenance 41 passed, scenario pages 21 passed, freshness 35 passed. The independent reviewer also ran all 97 cases successfully and found no actionable issue.
- Strategy restoration explicitly restores the manual value: Streamlit removes that widget when a market strategy is selected. No widget-persistence behaviour was changed.
- Full offline suite: 2,719 passed, 2 skipped; 96.59% coverage (95% required). Ruff and `git diff --check` passed. Warnings are modelling sanity warnings exercised by the existing suite. No live collection was run.
