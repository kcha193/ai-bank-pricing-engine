# Pricing Watch premium dashboard implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Finish the recovered UI UX Pro Max redesign, preserving the six destinations and both markets.

**Architecture:** Continue on `feature/ui-ux-pro-max-dashboard`. Keep calculations and collection in existing services; change presentation, charts and interaction only. Preserve Claude's generated master and add verified application/page overrides.

**Tech stack:** Streamlit 1.57, Python, Altair 6, existing pytest/AppTest fixtures, Playwright.

**Spec:** User's dashboard recovery and completion brief; `design-system/nz-pricing-watch/MASTER.md`; `docs/dashboard_redesign.md`.

## Global constraints

- Six destinations and legacy redirects remain available.
- Home loans rank lowest first; term deposits rank highest first, as supplied by the backend.
- No collection or optimisation during rendering/navigation. Keep Operations safeguards intact.
- No customer data; browser checks use disposable synthetic fixture databases.
- No new runtime dependency or backend pricing/collector refactor.
- Retain the existing master design system; overrides resolve its marketing-pattern mismatch.

## Recovery assessment

`HEAD` and fetched `origin/master` both equal `bd27002` (PR #62). PRs #60/#61/#62 are merged and implemented. Existing feature branch has no tracked modifications or staged files. The only untracked work is Claude's generated master. The older pricing-watch worktree is clean and on its separate branch. No newer redesign handoff/plan or project-local UI UX Pro Max installation exists; use the actual installed global skill and its search script.

Completed baseline: six destinations, dual-product services/charts/scenarios, explicit collection controls and deposit pipeline fixes. Partial: premium hierarchy, responsive cards, chart interaction and accessible data alternatives. Missing: page overrides, current screenshots at all requested widths and an end-to-end visual report. Blocked: none; in-app browser execution unavailable, standalone installed Playwright is the fallback.

## Review focus

- Returning to a page after changing product/filters must preserve the latest choice.
- Empty, stale or missing deposits must never show mortgage numbers.
- Long labels and charts must fit a 390px screen and both themes.
- Charts must preserve missing-date gaps and explain non-zero rate axes.
- Rendering Operations must never start a job; scenario results remain mode-specific and human-reviewed.

### Task 1: Shared visual system and executive overview

Files: `theme.py`, `ui.py`, `products.py`, `views/_common.py`, `views/overview.py`, `.streamlit/config.toml`; design-system overrides.

- [x] Establish shared typography, semantic surfaces, responsive metric/card grids, visible focus and reduced motion.
- [x] Refine compact identity/status bar, page/section hierarchy, product panels and selected-bank analysis.
- [x] Verify existing overview, header, product-switch and boundary tests.

### Task 2: Analytical destinations and charts

Files: `charts.py`, `views/market_watch.py`, `views/rate_changes.py`, `views/trends.py`; chart/page tests.

- [x] Keep backend ranking; improve direct labels, responsive legends, step-line interaction and theme-safe series contrast.
- [x] Add accessible chart-data tables and a clear history comparison journey.
- [x] Verify null gaps, both ranking directions, filters and empty/error states.

### Task 3: Pricing Studio and Operations & Trust

Files: `views/scenarios.py`, `views/system_health.py`, `views/operations_trust.py`; scenario/health tests.

- [x] Add reusable workflow/review cues and financial trade-off chart; preserve explicit scenario execution.
- [x] Surface each product's freshness, run and quarantine summary within its health tab.
- [x] Verify mode-specific results and Operations safety with existing tests and focused regressions.

### Task 4: Render, verify and deliver

Files: browser QA utility, screenshots, `docs/dashboard_redesign.md`, architecture/assumptions docs where behaviour changes.

- [x] Render all six pages in light/dark at 1440/1024/768/390px, exercise filters, product/navigation, scenarios, missing/error data and keyboard focus. Fix rendered defects.
- [x] Run `python3 -m ruff check .` and `python3 -m pytest -q -m 'not live_rates'` in the existing virtual environment.
- [x] Review complete diff independently and prepare task-only delivery. Open a review PR after checks pass; do not merge.

## Execution record

The supplied brief explicitly requests implementation through PR delivery; proceed without another design approval round. Continue in the existing requested branch rather than moving recovered files to a new worktree. Cosmetic changes use rendered checks; new behaviour gets regression tests. Baseline and final evidence are recorded in the redesign report.

Final evidence: 2,178 tests passed, 2 skipped; Ruff clean; 85 rendered checks plus keyboard, returning navigation, mobile scenario and operational safety interactions passed. Independent review found dark chart text and a mobile header overlap; both were corrected and visually rechecked. See `docs/dashboard_visual_review_2026-10-31.md`.
