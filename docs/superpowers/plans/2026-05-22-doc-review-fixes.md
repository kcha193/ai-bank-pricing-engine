# Doc Review Fixes Implementation Plan

> **STATUS: ✅ COMPLETE** — doc review fixes — PR #35

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 11 documented defects (DOC-001 through DOC-011) found in the CLAUDE.md / AGENTS.md / README.md review, restoring accurate cross-file consistency and operational safety.

**Architecture:** Pure documentation edits across three files. No code changes. Tasks are ordered by severity (BLOCKER → HIGH → MEDIUM → LOW) so the most dangerous gaps are closed first. Each task ends with a commit so partial completion is safe.

**Tech Stack:** Markdown, git.

---

## File map

| File | Changes |
|---|---|
| `AGENTS.md` | Full rewrite — currently truncated at 21 lines with no closing code fence |
| `CLAUDE.md` | 3 targeted edits: source-of-truth claim (line 3), module map (add `api/`, `llm/`, add alias), |
| `README.md` | 5 targeted edits: Python version, missing examples, FastAPI status, milestone framing, run ledger link |

---

## Task 1: Rewrite AGENTS.md (DOC-001, DOC-002, DOC-008, DOC-009)

**Files:**
- Overwrite: `AGENTS.md`

- [ ] **Step 1: Write the complete AGENTS.md**

Replace the entire file with the following content (the current file is truncated — it has no closing code fence and is missing all safety-critical sections):

```markdown
# AI Bank Pricing Engine — Agent Instructions

> This file is read by Codex and other agent runtimes. For Claude-session-specific architectural
> detail see [CLAUDE.md](CLAUDE.md), which is the source of truth for module-level invariants.

## Project context

Python retail bank balance-sheet pricing engine. **Synthetic data only. Portfolio-level only**
(one home-loan rate, one term-deposit rate) — not personalised. NZ context.

The engine maximises expected risk-adjusted profit subject to a funding-shortfall penalty via
exhaustive joint grid search with adaptive boundary expansion.

## Important commands

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q                           # see live_rates warning below
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/run_with_competitor_rates.py
python3 examples/run_llm_diagnostics.py
python3 examples/run_multiperiod.py
python3 examples/train_response_models.py

# Competitor-rate CLI (separate subcommand surface):
python3 -m balance_pricing_engine.competitor_rates.cli fetch    --output-dir data/processed/competitor_rates
python3 -m balance_pricing_engine.competitor_rates.cli validate --run-file <path>
python3 -m balance_pricing_engine.competitor_rates.cli apply    --config configs/base.yaml
python3 -m balance_pricing_engine.competitor_rates.cli report   --run-file <path>
```

**`live_rates` pytest marker fetches real bank websites — never run in CI and never run without
explicit intent.** Exclude it with: `python3 -m pytest -q -m 'not live_rates'`

## Configuration

[configs/base.yaml](configs/base.yaml) is the canonical config. Key rules:

- Choose **exactly one**: `pricing_strategy.{home_loan,term_deposit}_spread_bps` (market-relative)
  **or** `products.{loan,deposit}_rate_grid` (absolute). Both together is a validation error.
- `competitor_rates.enabled: false` by default. When enabled, reads from
  `data/processed/competitor_rates/latest.json` with staleness + strict-mode checks.
- `balance_sheet.required_deposit_funding_ratio` and `funding_shortfall_penalty_rate` drive
  the funding penalty.

## Conventions

- Python ≥ 3.9, ruff `line-length=100`, `target-version="py39"`.
- Pydantic **v2**. NumPy pinned `<2.0`. pandas ≥ 2.1. scikit-learn ≥ 1.4. joblib ≥ 1.4.
- Import as `balance_pricing_engine.*` — **not** `src.balance_pricing_engine.*`
  (`pythonpath = ["src"]` in pyproject.toml).
- Tests: `tests/unit/` (one module per source file). Fixtures: `tests/fixtures/`.
- Long-running workflow scripts → `scripts/`. Short demos → `examples/`.
- Data: `data/raw/` (untouched inputs), `data/processed/` (cleaned), `data/outputs/` (run artifacts).

## Safety invariants — do not violate

1. **Synthetic data only.** Do not add real customer or counterparty data paths.
2. **Portfolio-level only.** Do not introduce per-customer rate personalisation unless the user
   explicitly requests it.
3. **Strict-mode competitor failures must error, not warn.**
4. **LLM layer is decision-support only.** `llm/narrative.py` and `llm/anomaly.py` are tagged
   `[DECISION-SUPPORT: requires human review]` — never wire their output into automated decisions.
5. **`live_rates` marker must never run in CI.**

## Do not break — regression contract

`ScenarioDiagnostics.write_artifacts()` produces a stable artifact schema. Do not rename or
remove these artifact keys:

- `grid_results.csv` columns (`GRID_RESULT_COLUMNS`)
- `top_results.csv`, `sensitivity.csv`, `counterfactuals.csv`, `cohort_attribution.csv`
- `summary.json` keys: `best_loan_rate`, `best_deposit_rate`, `objective_value`,
  `expected_loan_volume`, `expected_deposit_volume`, `funding_shortfall`,
  `excess_deposit_income`, `boundary_warning_flag`, `initial_boundary_warning_flag`,
  `expansion_rounds`, `expansion_capped`, `policy_band_breached`

`OptimizationResult` is a frozen dataclass. Backward-compat aliases —
`objective_value`, `deposit_cost`, `funding_penalty`, `loan_income_after_losses` — exist
deliberately. Do not remove without auditing all call sites.

The response model uses **two elasticities per product** (own-rate + competitor-gap).
Collapsing them into one is not equivalent and will silently change recommendations.
```

- [ ] **Step 2: Verify file renders correctly**

Open `AGENTS.md` and confirm:
- No unclosed code fences
- All five sections present: Important commands, Configuration, Conventions, Safety invariants, Do not break
- `live_rates` warning is visible
- File is ~90 lines

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: restore AGENTS.md — was truncated at 21 lines, missing all safety sections"
```

---

## Task 2: Fix CLAUDE.md source-of-truth claim (DOC-002)

**Files:**
- Modify: `CLAUDE.md` line 3

- [ ] **Step 1: Edit the header note**

Current line 3:
```
> Companion to [AGENTS.md](AGENTS.md). Source of truth for commands and scope is AGENTS.md; this file adds Claude-specific architectural detail verified against the code.
```

Replace with:
```
> This file is the source of truth for module-level invariants, architectural detail, and operating constraints for Claude sessions. [AGENTS.md](AGENTS.md) covers all agent runtimes (Codex, etc.) with a subset of the same context.
```

- [ ] **Step 2: Verify**

Open `CLAUDE.md` and confirm line 3 no longer says AGENTS.md is the source of truth.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: correct CLAUDE.md source-of-truth claim — AGENTS.md was a stub, not canonical"
```

---

## Task 3: Add `api/` and `llm/` to CLAUDE.md module map (DOC-003)

**Files:**
- Modify: `CLAUDE.md`, "Module map" section (after `run_ledger/` entry, before `modelling/`)

- [ ] **Step 1: Add two module map entries**

Locate the line that begins `- [modelling/](src/balance_pricing_engine/modelling/)` in the Module map section. Insert the following two entries immediately before it:

```markdown
- [api/app.py](src/balance_pricing_engine/api/app.py) — FastAPI serving layer. Exposes `POST /run-scenario` (accepts YAML config string, returns optimization summary dict). Optional; not imported by core engine. Requires `fastapi` extra.
- [llm/](src/balance_pricing_engine/llm/) — Tier 4 LLM decision-support layer: `narrative.py` (plain-language scenario narration), `anomaly.py` (config anomaly detection), `_client.py` (Claude API wrapper, default model `claude-sonnet-4-6`). **Decision-support only** — every response is tagged `[DECISION-SUPPORT: requires human review]`. Never wired into automated pricing decisions.
```

- [ ] **Step 2: Verify**

Confirm both entries appear in the module map and no surrounding bullet points were displaced.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add api/ and llm/ modules to CLAUDE.md module map"
```

---

## Task 4: Add missing backward-compat alias to CLAUDE.md module map (DOC-011)

**Files:**
- Modify: `CLAUDE.md`, module map entry for `optimization/grid_search.py` (line ~18)

- [ ] **Step 1: Edit the alias list**

Locate the `optimization/grid_search.py` bullet. It currently ends with:
```
Returns `OptimizationResult` dataclass with backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`).
```

Replace the alias list with:
```
Returns `OptimizationResult` dataclass with backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`, `loan_income_after_losses`).
```

- [ ] **Step 2: Verify**

Confirm the alias `loan_income_after_losses` now appears in both the module map entry and the "When making changes" section — they should be consistent.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add loan_income_after_losses to OptimizationResult alias list in module map"
```

---

## Task 5: Fix README stale scope framing (DOC-004, DOC-006)

**Files:**
- Modify: `README.md`, "Current Scope" section

- [ ] **Step 1: Edit the Current Scope section**

Current text (first sentence of Current Scope):
```
The current engine chooses one portfolio-level home loan rate and one portfolio-level term deposit
rate. It maximizes expected risk-adjusted profit while penalizing funding shortfalls.
```

And the final sentence of the file:
```
FastAPI, dashboards, and production model integration come later.
```

Replace the full "Current Scope" section with:

```markdown
## Current Scope

The engine chooses one portfolio-level home loan rate and one portfolio-level term deposit rate,
maximising expected risk-adjusted profit while penalising funding shortfalls.

The project also includes:
- A diagnostic ML layer that trains response models on synthetic observed outcomes and validates
  rate-response behaviour.
- An LLM decision-support layer (`llm/`) for plain-language narration and config anomaly detection
  (human review required — never wired into automated decisions).
- A FastAPI endpoint (`POST /run-scenario`) in `api/` for local HTTP access.

Every pricing run produces an auditable ledger under `data/outputs/runs/` containing a config
snapshot, run metadata, a business-facing pricing memo, and a governance checklist — all generated
offline with no external dependencies. See [docs/run_ledger_guide.md](docs/run_ledger_guide.md).

Dashboards and production deployment are not yet implemented.
```

- [ ] **Step 2: Verify**

Confirm:
- "first milestone is intentionally small" is gone
- "FastAPI ... come later" is gone
- `docs/run_ledger_guide.md` is linked
- LLM and API layers are described with accurate status

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update README Current Scope — remove stale milestone framing, add api/llm status"
```

---

## Task 6: Add Python version + missing examples to README quickstart (DOC-005, DOC-007)

**Files:**
- Modify: `README.md`, "Quickstart" section

- [ ] **Step 1: Add Python version prerequisite**

Locate the Quickstart section. Add a prerequisite line before the code block:

```markdown
## Quickstart

Requires Python ≥ 3.9.

```bash
python3 -m venv .venv
...
```

- [ ] **Step 2: Add missing example scripts**

The current quickstart code block ends with:
```bash
python3 examples/train_response_models.py
```

Extend it to:
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 examples/run_baseline.py               # core grid-search + diagnostics
python3 examples/run_with_competitor_rates.py  # with live competitor rate ingestion
python3 examples/run_llm_diagnostics.py        # LLM scenario narration (requires ANTHROPIC_API_KEY)
python3 examples/run_multiperiod.py            # multi-period scenario
python3 examples/train_response_models.py      # diagnostic ML layer
```

- [ ] **Step 3: Verify**

Confirm the quickstart code block:
- Has `Requires Python ≥ 3.9` before the block
- Contains all five example scripts with inline comments
- Still references `docs/usage_guide.md` for restricted-network notes

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add Python version floor and all example scripts to README quickstart"
```

---

## Self-Review

**Spec coverage check:**

| DOC | Severity | Addressed in |
|-----|----------|-------------|
| DOC-001 | BLOCKER | Task 1 — AGENTS.md full rewrite |
| DOC-002 | BLOCKER | Task 2 — CLAUDE.md header note |
| DOC-003 | HIGH | Task 3 — CLAUDE.md module map |
| DOC-004 | HIGH | Task 5 — README Current Scope |
| DOC-005 | MEDIUM | Task 6 — README quickstart |
| DOC-006 | MEDIUM | Task 5 — README Current Scope |
| DOC-007 | MEDIUM | Task 6 — README quickstart |
| DOC-008 | MEDIUM | Task 1 — AGENTS.md live_rates warning |
| DOC-009 | LOW | Task 1 — AGENTS.md title updated to "Agent Instructions" |
| DOC-010 | LOW | Task 5 — README run_ledger_guide.md link |
| DOC-011 | LOW | Task 4 — CLAUDE.md alias list |

All 11 defects covered. No gaps.

**Placeholder scan:** No TBD, TODO, or "similar to Task N" patterns present. All code blocks contain complete content.

**Type consistency:** No code symbols used — pure documentation edits. N/A.
