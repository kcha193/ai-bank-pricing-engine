# AI Bank Pricing Engine — Agent Instructions

> This file is read by Codex and other agent runtimes. For Claude-session-specific architectural
> detail see [CLAUDE.md](CLAUDE.md), which is the source of truth for module-level invariants.

## Context Navigation (Graphify + Codebase Memory)

Codex does not auto-load `CLAUDE.md`, so these rules are inlined here too
(kept in sync with `CLAUDE.md`'s "Context Navigation" section).

### 4-Layer Query Rule
1. **First:** query `graphify-out/graph.json` or `graphify-out/wiki/index.md`
   to understand code structure and connections
2. **Second:** query the Obsidian vault for decisions, progress, and project context
3. **Third:** for live/ad-hoc structural questions the static graphify graph
   doesn't answer (call chains, impact of uncommitted changes, dead code,
   symbol search), use codebase-memory-mcp's MCP tools directly. Call
   `detect_changes` first — it does not auto-watch or auto-reindex; if it
   reports drift, re-run `index_repository` before trusting graph results
4. **Fourth:** only read raw code files when editing
   or when the first three layers don't have the answer

### Codebase Memory (codebase-memory-mcp)
- codebase-memory-mcp is a structural code index only; it has no LLM inside it.
- It does not replace graphify or `AI_Context`.
- If this project is not already indexed in the current agent session, ask the
  agent to "index this project" once.
- Its cache lives outside the repo at `~/.cache/codebase-memory-mcp/`.
- If codebase-memory-mcp is unavailable, skip to raw code reads; nothing
  depends on it being present.
- The project's `.codex/config.toml` is a template only — Codex CLI loads
  config exclusively from `~/.codex/config.toml`, not a project-local file.
  Copy the `[mcp_servers.codebase-memory-mcp]` stanza into your real
  `~/.codex/config.toml` to get these tools under Codex; committing this repo
  file alone does not register the server.

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
python3 examples/run_governed_scenario.py
python3 examples/run_with_competitor_rates.py
python3 examples/run_llm_diagnostics.py
python3 examples/run_multiperiod.py
python3 examples/train_response_models.py

# Optional FastAPI serving layer (requires the `api` extra):
python3 -m pip install -e ".[api]"
uvicorn balance_pricing_engine.api.app:app --reload   # /run-scenario, /v1/pricing/scenarios, /health, /ready

# Docker (see docs/deployment.md):
docker build -t ai-bank-pricing-engine .

# Competitor-rate CLI (separate subcommand surface):
python3 -m balance_pricing_engine.competitor_rates.cli fetch    --output-dir data/processed/competitor_rates
python3 -m balance_pricing_engine.competitor_rates.cli validate --source-file <path>
python3 -m balance_pricing_engine.competitor_rates.cli apply    --config configs/base.yaml
python3 -m balance_pricing_engine.competitor_rates.cli report   --source-file <path>
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
6. **Governed ML pricing path must fail loud.** An ML champion can drive the optimiser only via
   `run_baseline_diagnostics(registry=...)` (→ `ModelScoringAgent.resolve_response_fn` →
   `MLResponseFnAdapter`, in `optimization/response_fn.py`). No champion or an unapproved champion
   ⇒ synthetic fallback, price unchanged. An *approved* champion with a missing/unloadable artifact
   ⇒ raise `GovernanceError`; never silently reprice on the synthetic fallback. This propagates
   unchanged through `application.PricingService.run_scenario()` — only a delivery layer (e.g. the
   `/v1/pricing/scenarios` FastAPI handler) may translate it into an error response.
7. **Event publishing must never affect pricing.** `events.publisher.publish_safely()` catches and
   logs publisher exceptions; a broken/misconfigured event publisher must never fail or alter a
   pricing decision.

New orchestration/governance packages: `application/` (`PricingService`), `governance/` (gates,
lineage, decision record, shadow evaluation), `features/` (versioned local feature-store
boundary), `events/` (in-process domain events), `observability/` (structured logging). See
[CLAUDE.md](CLAUDE.md), [docs/architecture.md](docs/architecture.md),
[docs/deployment.md](docs/deployment.md), and [docs/adr/](docs/adr/) for detail.

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

## Session wrap-up

If this session changed the project's status, milestone, or current focus, update the "AI bank pricing engine" entry in `~/vault/AI_Context/Memory/Durable_Facts/Kevin/current-projects.md` (status, current focus, last-updated date) before ending the session. Skip this if nothing status-relevant changed.
