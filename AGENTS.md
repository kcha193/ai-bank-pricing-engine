# AI Bank Pricing Engine — Agent Instructions

> Canonical instructions for Claude Code and Codex. `CLAUDE.md` imports this
> file with `@AGENTS.md` — edit rules here, not there.

## What this project is

Python retail bank balance-sheet pricing engine. **Synthetic-data only.** **Portfolio-level** (one home-loan rate, one term-deposit rate) — not personalised. NZ context (default competitor banks: ANZ, ASB, Westpac, BNZ, Kiwibank).

The engine maximises expected risk-adjusted profit subject to a funding-shortfall penalty, via exhaustive joint grid search with adaptive boundary expansion. A separate diagnostic ML layer trains response models on simulated outcomes and checks they recover sensible rate-response behaviour.

## Module map

The per-module reference — responsibilities, seams, and the invariants each module carries — is in [docs/architecture.md → Module map](docs/architecture.md#module-map-agent-reference). Read the entry for every module you touch before editing it; **When making changes** below applies regardless.

Full flow diagram: [docs/architecture.md](docs/architecture.md) (includes the target-architecture Mermaid diagram). Modelling assumptions: [docs/modelling_assumptions.md](docs/modelling_assumptions.md). Governance: [docs/model_governance.md](docs/model_governance.md). Response model card: [docs/response_model_card.md](docs/response_model_card.md). Competitor ingestion: [docs/competitor_rate_ingestion.md](docs/competitor_rate_ingestion.md). Pricing intelligence: [docs/pricing_intelligence.md](docs/pricing_intelligence.md). Deployment: [docs/deployment.md](docs/deployment.md). Architecture decisions: [docs/adr/](docs/adr/).

## Response model — dual elasticity

Both loan acceptance and deposit uptake use **two elasticities** (see [configs/base.yaml](configs/base.yaml) `response:` block):

- `loan_rate_elasticity` — own-rate sensitivity (negative for loans).
- `loan_competitor_gap_elasticity` — sensitivity to gap vs competitor loan rate.
- `deposit_rate_elasticity` — own-rate sensitivity (positive for deposits).
- `deposit_competitor_gap_elasticity` — sensitivity to gap vs competitor deposit rate.

Refactors that collapse these into a single elasticity term are **not equivalent** and will silently change recommendations.

## Commands

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/run_governed_scenario.py
python3 examples/run_with_competitor_rates.py
python3 examples/run_llm_diagnostics.py
python3 examples/run_multiperiod.py
python3 examples/train_response_models.py

# Optional FastAPI serving layer (requires the `api` extra):
python3 -m pip install -e ".[api]"
uvicorn balance_pricing_engine.api.app:app --reload   # POST /run-scenario, /v1/pricing/scenarios, /health, /ready

# Docker (see docs/deployment.md):
docker build -t ai-bank-pricing-engine .
docker run --rm -p 8000:8000 ai-bank-pricing-engine

# NZ pricing-intelligence CLI:
python3 -m balance_pricing_engine.pricing_intelligence collect --render-javascript
python3 -m balance_pricing_engine.pricing_intelligence snapshot --term 12
python3 -m balance_pricing_engine.pricing_intelligence changes --days 7
python3 -m balance_pricing_engine.pricing_intelligence market-state
python3 -m balance_pricing_engine.pricing_intelligence ocr
python3 examples/run_market_intelligence_scenarios.py

# Unattended/scheduled collection (see deploy/scheduling/):
python3 scripts/collect_market_rates_workflow.py --render-javascript --summary-file data/processed/pricing_intelligence/last_run.json

# Competitor-rate CLI (separate subcommand surface):
python3 -m balance_pricing_engine.competitor_rates.cli fetch    --output-dir data/processed/competitor_rates
python3 -m balance_pricing_engine.competitor_rates.cli validate --source-file <path>
python3 -m balance_pricing_engine.competitor_rates.cli apply    --config configs/base.yaml
python3 -m balance_pricing_engine.competitor_rates.cli report   --source-file <path>
```

`live_rates` pytest marker is opt-in — fetches **real** bank pages. Do not run in CI or without intent. To exclude it explicitly: `python3 -m pytest -q -m 'not live_rates'`.

## Configuration

[configs/base.yaml](configs/base.yaml) is the canonical config. Key shapes:

- `random_seed: 42`, `simulation.n_loan_customers: 5000`, `simulation.n_deposit_customers: 5000`.
- `market.*` includes `cash_rate`, `wholesale_funding_spread`, competitor rates, and reference rates required when using `pricing_strategy`.
- `response.coefficients.loan.*` and `response.coefficients.deposit.*` — intercepts, rate anchors, and feature coefficients wired into `synthetic/response.py` (see `ResponseCoefficientsConfig`).
- Choose **one**: `pricing_strategy.{home_loan,term_deposit}_spread_bps` (market-relative, default) **or** `products.{loan,deposit}_rate_grid` (absolute).
- `competitor_rates.enabled: false` by default — when true, reads from `data/processed/competitor_rates/latest.json` with staleness + strict-mode checks.
- `balance_sheet.required_deposit_funding_ratio` and `funding_shortfall_penalty_rate` drive the funding penalty.

## Conventions (observed)

- Python ≥3.9, ruff `line-length=100`, `target-version="py39"`.
- Pydantic **v2**. NumPy pinned `<2.0`. pandas ≥2.1. scikit-learn ≥1.4. joblib ≥1.4.
- `pythonpath = ["src"]` (set in [pyproject.toml](pyproject.toml)) — import as `balance_pricing_engine.*`, **not** `src.balance_pricing_engine.*`.
- Tests live under [tests/unit/](tests/unit/) (one module per source file). Fixtures under [tests/fixtures/](tests/fixtures/).
- Workflow scripts (long-running) → [scripts/](scripts/). Short demos → [examples/](examples/).
- Data folders: [data/raw/](data/raw/) (untouched inputs), [data/processed/](data/processed/) (cleaned), [data/outputs/](data/outputs/) (run artifacts).
- Notebook walkthrough: [notebooks/bank_pricing_engine_walkthrough.ipynb](notebooks/bank_pricing_engine_walkthrough.ipynb).

## Regression contract — do not break

`ScenarioDiagnostics.write_artifacts()` produces a stable artifact set consumed by regression checks:

- `grid_results.csv` — every final evaluated rate pair (columns enumerated in `GRID_RESULT_COLUMNS`).
- `top_results.csv` — top-N by `total_objective`.
- `sensitivity.csv` — when sensitivity provided (includes `delta_loan_rate_bps`, `delta_deposit_rate_bps`, `delta_objective_pct`, `parameter_family`).
- `counterfactuals.csv` — when `counterfactual_decompositions()` is called.
- `cohort_attribution.csv` — when `cohort_attribution()` is called.
- `summary.json` — keys include `best_loan_rate`, `best_deposit_rate`, `objective_value`, `expected_loan_volume`, `expected_deposit_volume`, `funding_shortfall`, `excess_deposit_income`, **`boundary_warning_flag`**, **`initial_boundary_warning_flag`**, **`expansion_rounds`**, `expansion_capped`, `policy_band_breached`.

The boundary fields distinguish "needed expansion" from "still hitting boundary after expansion" — preserve their semantics when refactoring. `excess_deposit_income` credits surplus deposits at `cash_rate` when deposit volume exceeds loan volume.

`decision_record.json` (written by `application.PricingService` when `persist_ledger=True`) is an **additive** artifact on top of this contract — never rename/restructure the files above to make room for it, and never let a decision-record change alter `summary.json`/`metadata.json` semantics.

## When making changes

- Honour the synthetic-data-only invariant. Do not add real customer or counterparty data paths.
- Keep portfolio-level scope unless the user explicitly opens up personalised pricing.
- The LLM layer (`llm/`) is decision-support only: every response is tagged `[DECISION-SUPPORT: requires human review]`; never wire its output into automated pricing decisions.
- `OptimizationResult` is a frozen dataclass; backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`, `loan_income_after_losses`) exist deliberately — don't remove without checking call sites.
- Modelling layer is **diagnostic by default, but no longer hard-walled off from the optimiser.** `run_baseline_diagnostics(registry=...)` resolves an **approved registry champion** via `ModelScoringAgent.resolve_response_fn(...)` and wires it into the grid search through `MLResponseFnAdapter` (see [optimization/response_fn.py](src/balance_pricing_engine/optimization/response_fn.py)). Governance invariants — **preserve these**: with no champion, or an *unapproved* champion, the path falls back to the synthetic response and the price is unchanged; an *approved* champion whose artifact is missing or unloadable raises `GovernanceError` (**fail loud — never silently reprice on the synthetic fallback**). Don't conflate ML response models with the synthetic response functions used inside the grid search. The single `train()` entry point in `agentic.py` replaced the former dual `train` / `train_and_recommend` API; `evaluate_binary_classifier` is a backward-compat alias for `evaluate_model` that emits a `DeprecationWarning`; use `evaluate_probabilities` / `evaluate_model` from `evaluation.py` for new code.
- Competitor-rate ingestion is gated behind `competitor_rates.enabled` and `strict` flags. Strict-mode failures should error, not warn.
- `pricing_intelligence` must stay one-directional: it may read `competitor_rates` and `config`, but nothing in `optimization/`, `pricing/`, or `scenarios/` may import it except through `pricing_intelligence.strategy`. Keep "what is the market doing?" separate from "what should we price?".
- `strategy.apply_market_strategy_to_config()` under `CompetitorRateStrategy.MANUAL` **returns the config object unchanged** (not a copy). Every pre-existing scenario depends on that; don't "tidy" it into an unconditional `model_copy`.
- `strategy.apply_cash_rate_strategy_to_config()` holds the same invariant for `market.cash_rate` under `CashRateStrategy.MANUAL` — unchanged config object, not a copy. It is a separate seam from the competitor-rate one above (a different data source, RBNZ vs the bank collectors), not a parameter bolted onto `apply_market_strategy_to_config`.
- `pricing_intelligence.rbnz` parsing anchors on the "Official Cash Rate" heading text, never on the first table matching the expected `Date | series | series` column shape — the RBNZ page renders several charts (OCR, FX rates, TWI) with identical accessible-table shapes, and a numeric plausibility check alone cannot tell them apart (NZD/USD values already sit in a range a naive OCR bound would accept). Same category of defect as the PIE-effective-rate rows in term-deposit collection.
- Change detection must never turn a collection failure into a market event: withdrawal events are scoped to banks that were successfully collected (`detect_changes(banks_collected=...)`). An unreachable bank is unknown, not withdrawn.
- Suspicious movements are excluded from `compute_market_activity` (and therefore the market-state classifier) by default. A parser fault must not be able to fake a repricing round.
- Basis points come from `pricing_intelligence.bps` (Decimal). Don't compute them with float subtraction anywhere they reach an event, snapshot, or alert.
- Rate-tier categorisation is **per-bank**: live pages carry the tier word in the table heading, not the row, so each collector declares `default_fixed_category` for what an unmarked fixed row means (`special` for ASB/BNZ, `standard` for Westpac, `None` where the bank labels both tiers in-row). Don't try to infer this generically — getting it wrong silently drops banks from the market comparison.
- `excluded_deposit_product_markers` does the same on the deposit side for managed-fund/KiwiSaver returns and PIE "effective rate of return" columns — deposits rank highest-is-best, so an inflated row wins market-leader. Non-comparable records are exempt from the conflicting-observation guard: they collide by construction and never reach a statistic.
- `excluded_product_markers` fences subsidised/green/top-up lending, bridging finance, and closed products into the `non_comparable` category. They stay recorded for audit but must never reach a market statistic — a subsidised 1.00% rate would wreck any median it entered.
- Live collection requires `--render-javascript` (the `[browser]` extra). Every tracked bank builds its rate table client-side; the static-HTML path returns `ParserError` for all five.
- Scheduled collection goes through `scripts/collect_market_rates_workflow.py`, which holds a single-instance lock and returns distinct exit codes (0 ok, 1 total failure, 2 **partial** failure, 3 already running). Don't collapse 2 into 0 — an incomplete market view computes a median over fewer banks than it reports.
- `.github/workflows/collector-canary.yml` reaches live bank sites and must stay out of `ci.yml`; it is a breakage canary, not a history store. Durable observation history is local and git-ignored by design.
- Prefer `application.PricingService` over calling `run_baseline_diagnostics()` directly from new callers (API endpoints, CLI commands). It wraps, not replaces, the domain math — do not duplicate governance/lineage/event logic at a new call site instead of going through it.
- `GovernanceError` must keep propagating out of `PricingService.run_scenario()` unchanged — never catch it there and turn it into a `BLOCKED` `PricingDecision`. Only a delivery layer (e.g. the `/v1/pricing/scenarios` FastAPI handler) may translate it into a transport-appropriate error response.
- `governance.gates.evaluate_gates()` policy is intentionally conservative but not maximal (any `BLOCK` → `BLOCKED`; otherwise any `WARN` → `REVIEW_REQUIRED`; else `RECOMMENDED`) — don't add a new gate that blocks on a mere warning-level condition without updating this documented policy.
- `events.publisher.publish_safely()` must keep swallowing publisher exceptions (logged, not raised) and enforcing its `deadline_seconds` wait cap — a broken *or slow/hung* event publisher must never fail, block, or alter a pricing decision.
- `pricing_intelligence.alerts.dispatch_alerts_safely()` follows the same rule for webhook alert delivery: a broken or unreachable webhook is logged and must never change a scheduled collection run's exit code. Only rate moves are pushed to the webhook — a new/withdrawn product is a change but not a reprice — matching the CLI's own `--alerts` scope. `PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL` unset is the default state; nothing else about collection changes when it's absent.
- Dashboard pages **render only**: no freshness, ranking, bps, market-state or scenario economics in `dashboard/pages/` — read them from `DashboardServices` (which calls `pricing_intelligence/health.py`, the service and `strategy`). `pricing_intelligence/health.py` is the single owner of freshness/health semantics; don't re-derive a threshold in the CLI, a frame or a page. `pricing_intelligence` must never import `dashboard`.
- Reading never collects: only `dashboard/pages/operations.py` may import `dashboard/actions.py`, and only an explicit button click (an `on_click` callback) may start a collection — a rerender or page navigation must not. Don't weaken the Operations controls: the optional `PRICING_WATCH_OPS_TOKEN` gate (minimum 16 characters, fails closed, process-wide unlock limiter), the single-flight runner and 60 s cooldown, the 10-minute backup limit (backups are pruned to the newest 30) and the Telegram cooldown.
- Untrusted text (bank-page strings, error messages, `last_error`) is shown only in dataframe cells or passed to `st.link_button` as a validated URL — never through `st.markdown`/`st.write`/`st.info`/`st.warning`/etc. `tests/unit/test_dashboard_boundaries.py` enforces this.
- Update [docs/architecture.md](docs/architecture.md) and [docs/modelling_assumptions.md](docs/modelling_assumptions.md) when changing structural behaviour.

## Roadmap & recent reviews

- Roadmap: [docs/agentic_modelling_roadmap.md](docs/agentic_modelling_roadmap.md).
- Latest codebase review: [docs/codebase_review_2026-05-18.md](docs/codebase_review_2026-05-18.md).
- Diagnostic review: [docs/diagnostic_review_2026-04-26.md](docs/diagnostic_review_2026-04-26.md).
- Step-by-step business+technical walkthrough: [docs/usage_guide.md](docs/usage_guide.md) (includes restricted-network setup).
- Run ledger usage guide: [docs/run_ledger_guide.md](docs/run_ledger_guide.md) (how to produce, read, and sign off ledger artifacts).

## Context Navigation

<!-- context-navigation:start v1 -->
Follow the global context-navigation rule (`~/.claude/CLAUDE.md` for Claude,
`~/.codex/AGENTS.md` for Codex): vault first; Claude uses native search;
Codex uses codebase-memory-mcp (`detect_changes` first); Graphify only on request.
- `graphify-out/` is a dated snapshot, not auto-refreshed — check its age before trusting it; don't hand-edit it.
- codebase-memory-mcp is registered for Codex by this repo's `.codex/config.toml`, which Codex loads only for trusted projects.
<!-- context-navigation:end -->
