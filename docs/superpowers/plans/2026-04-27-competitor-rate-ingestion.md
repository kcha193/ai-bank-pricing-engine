# Competitor Rate Ingestion Implementation Plan

> **STATUS: ✅ COMPLETE** — competitor rate ingestion pipeline — PR all PRs pre-#30

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build an auditable official-source-first NZ bank competitor-rate ingestion, validation,
storage, and application workflow.

**Architecture:** Add a focused `competitor_rates` package beside the existing pricing modules.
Canonical Pydantic models carry audit fields; extraction writes raw snapshots and canonical
processed artifacts; application overlays resolved competitor rates onto existing `EngineConfig`
only when explicitly enabled.

**Tech Stack:** Python 3.9+, Pydantic v2, PyYAML, pandas, standard-library HTTP/HTML helpers,
pytest, ruff.

---

### Task 1: Data Models and Source Registry

**Files:**
- Create: `src/balance_pricing_engine/competitor_rates/__init__.py`
- Create: `src/balance_pricing_engine/competitor_rates/models.py`
- Create: `src/balance_pricing_engine/competitor_rates/sources.py`
- Test: `tests/unit/test_competitor_rate_models.py`

- [x] **Step 1: Write failing model tests**

Add tests that construct a valid `CompetitorRateRecord`, reject missing core fields, and assert the
source registry contains 10 official bank/product pages.

- [x] **Step 2: Run model tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_competitor_rate_models.py -q`
Expected: import failure for the new package.

- [x] **Step 3: Implement Pydantic models and registry**

Define enums/literals, `CompetitorRateRecord`, `SourceAttempt`, `ValidationSummary`,
`CompetitorRateRun`, and strategy config models. Add the five-bank official URL registry.

- [x] **Step 4: Run model tests and verify they pass**

Run: `python3 -m pytest tests/unit/test_competitor_rate_models.py -q`
Expected: all tests pass.

### Task 2: Normalisation

**Files:**
- Create: `src/balance_pricing_engine/competitor_rates/normalise.py`
- Test: `tests/unit/test_competitor_rate_normalise.py`

- [x] **Step 1: Write failing normalisation tests**

Cover `6 months`, `six months`, `1 year`, `18 months`, `30 days`, `60 days`, `90 days`,
`floating`, `variable`, frequency labels, balances, and LVR/equity conditions.

- [x] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_competitor_rate_normalise.py -q`
Expected: import failure.

- [x] **Step 3: Implement normalisation helpers**

Return structured term objects with raw labels preserved and derived months/days where possible.

- [x] **Step 4: Run tests and verify they pass**

Run: `python3 -m pytest tests/unit/test_competitor_rate_normalise.py -q`
Expected: all tests pass.

### Task 3: Extraction and Fixtures

**Files:**
- Create: `src/balance_pricing_engine/competitor_rates/extractors.py`
- Create: `tests/fixtures/competitor_rates/sample_bank_home_loan.html`
- Create: `tests/fixtures/competitor_rates/sample_bank_term_deposit.html`
- Test: `tests/unit/test_competitor_rate_extractors.py`

- [x] **Step 1: Write failing extractor tests**

Use small fixture HTML tables for home-loan standard/special/floating rates and term-deposit
terms/tier/frequency variants. Assert parsed records preserve raw context, source URL, hash, and
terms.

- [x] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_competitor_rate_extractors.py -q`
Expected: import failure.

- [x] **Step 3: Implement parser utilities and official extractor wrappers**

Use `html.parser` to extract table/list text, regex patterns for percentage-bearing rows, and
bank/product-specific category hints. Implement static HTTP fetch with raw snapshot writing.

- [x] **Step 4: Run tests and verify they pass**

Run: `python3 -m pytest tests/unit/test_competitor_rate_extractors.py -q`
Expected: all tests pass.

### Task 4: Validation and Storage

**Files:**
- Create: `src/balance_pricing_engine/competitor_rates/validate.py`
- Create: `src/balance_pricing_engine/competitor_rates/store.py`
- Create: `tests/fixtures/competitor_rates/latest.json`
- Test: `tests/unit/test_competitor_rate_validate_store.py`

- [x] **Step 1: Write failing validation/storage tests**

Cover numeric bounds, missing required fields, duplicate detection, missing expected sources,
parser zero-rate failures, stale latest files, day-over-day movement warnings, and output paths.

- [x] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_competitor_rate_validate_store.py -q`
Expected: import failure.

- [x] **Step 3: Implement validation and artifact persistence**

Write `latest.json`, `latest.csv`, immutable history JSON, and
`validation_report_latest.json`.

- [x] **Step 4: Run tests and verify they pass**

Run: `python3 -m pytest tests/unit/test_competitor_rate_validate_store.py -q`
Expected: all tests pass.

### Task 5: Config and Application

**Files:**
- Modify: `src/balance_pricing_engine/config.py`
- Create: `src/balance_pricing_engine/competitor_rates/apply.py`
- Test: `tests/unit/test_competitor_rate_apply.py`
- Test: `tests/unit/test_market.py`

- [x] **Step 1: Write failing application tests**

Cover disabled manual behavior, bank-specific, market average, market min/max, market median,
frequency preference, special mortgage filtering, stale strict failure, and engine run with fixture
rates.

- [x] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_competitor_rate_apply.py tests/unit/test_market.py -q`
Expected: missing config/apply behavior.

- [x] **Step 3: Implement config models and application helpers**

Add optional `competitor_rates` config. Use `model_copy(update=...)` to return modified configs
without mutating the original.

- [x] **Step 4: Run tests and verify they pass**

Run: `python3 -m pytest tests/unit/test_competitor_rate_apply.py tests/unit/test_market.py -q`
Expected: all tests pass.

### Task 6: CLI and Example

**Files:**
- Create: `src/balance_pricing_engine/competitor_rates/cli.py`
- Create: `examples/run_with_competitor_rates.py`
- Modify: `configs/base.yaml`
- Test: `tests/unit/test_competitor_rate_cli.py`
- Test: `tests/unit/test_examples.py`

- [x] **Step 1: Write failing CLI/example tests**

Assert commands parse, validate fixture source files, apply prints selected assumptions, `--write`
creates generated configs, and examples avoid import-path mutation.

- [x] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_competitor_rate_cli.py tests/unit/test_examples.py -q`
Expected: missing CLI/example behavior.

- [x] **Step 3: Implement CLI and example**

Add `fetch`, `validate`, `apply`, and `report`. Update base config with disabled
`competitor_rates` defaults.

- [x] **Step 4: Run tests and verify they pass**

Run: `python3 -m pytest tests/unit/test_competitor_rate_cli.py tests/unit/test_examples.py -q`
Expected: all tests pass.

### Task 7: Documentation, Gitignore, and Verification

**Files:**
- Modify: `.gitignore`
- Create: `docs/competitor_rate_ingestion.md`
- Modify: `README.md`
- Modify: `pyproject.toml`

- [x] **Step 1: Add docs and live marker configuration**

Document sources, commands, validation, application, and limitations. Add `live_rates` pytest
marker and ignore generated raw/processed artifacts.

- [x] **Step 2: Run full verification**

Run:

```bash
python3 -m pytest -q
python3 -m ruff check .
python3 -m balance_pricing_engine.competitor_rates.cli validate --source-file tests/fixtures/competitor_rates/latest.json
python3 examples/run_with_competitor_rates.py --source-file tests/fixtures/competitor_rates/latest.json --strict false
```

Expected: all commands exit 0.

- [x] **Step 3: Run non-strict live fetch smoke command**

Run:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli fetch --strict false
```

Expected: command attempts all official URLs, writes reports when available, and reports any
source/parser failures clearly.
