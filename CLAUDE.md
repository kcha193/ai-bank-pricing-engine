# AI Bank Pricing Engine — Claude Instructions

> This file is the source of truth for module-level invariants, architectural detail, and operating constraints for Claude sessions. [AGENTS.md](AGENTS.md) covers all agent runtimes (Codex, etc.) with a subset of the same context.

## What this project is

Python retail bank balance-sheet pricing engine. **Synthetic-data only.** **Portfolio-level** (one home-loan rate, one term-deposit rate) — not personalised. NZ context (default competitor banks: ANZ, ASB, Westpac, BNZ, Kiwibank).

The engine maximises expected risk-adjusted profit subject to a funding-shortfall penalty, via exhaustive joint grid search with adaptive boundary expansion. A separate diagnostic ML layer trains response models on simulated outcomes and checks they recover sensible rate-response behaviour.

## Module map (verified against [src/balance_pricing_engine/](src/balance_pricing_engine/))

- [config.py](src/balance_pricing_engine/config.py) — Pydantic v2 `EngineConfig` + `ResponseCoefficientsConfig`. Validates that **exactly one** of `pricing_strategy` (market-relative spreads) or `products` (absolute grids) is provided. Refuses spread grids that would generate negative rates. `model_validator(mode="after")` emits `warnings.warn` for: negative `wholesale_funding_spread`, penalty dominated by funding spread, `n_*_customers < 1000`, spread band < 50 bps.
- [market.py](src/balance_pricing_engine/market.py) — `MarketRates`, `SpreadGridConfig`, `bps_to_rate`, `build_market_relative_grid`.
- [rate_grids.py](src/balance_pricing_engine/rate_grids.py) — `build_configured_rate_grids`: dispatches between market-relative spread grids and absolute fallback grids.
- [synthetic/](src/balance_pricing_engine/synthetic/) — `customers.py` (loan + deposit generators), `market.py` (`MarketState`), `response.py` (`loan_acceptance_probability`, `deposit_uptake_probability`).
- [pricing/](src/balance_pricing_engine/pricing/) — `profit.py` (loan income, ECL, deposit cost, wholesale funding) + `balance_sheet.py` (`calculate_funding_result`, shortfall + penalty).
- [optimization/grid_search.py](src/balance_pricing_engine/optimization/grid_search.py) — `evaluate_joint_rate_grid`, `select_best_result`, `optimize_joint_rates`, `optimize_joint_market_relative_rates`. Returns `OptimizationResult` dataclass with backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`, `loan_income_after_losses`). Accepts optional `loan_response_fn` / `deposit_response_fn` callables so an approved ML champion can drive acceptance/uptake instead of the synthetic response.
- [optimization/response_fn.py](src/balance_pricing_engine/optimization/response_fn.py) — pluggable response interface that decouples the optimiser from the response source: `ResponseFn` Protocol, `SyntheticLoanResponseFn` / `SyntheticDepositResponseFn` adapters, `MLResponseFnAdapter` (wraps a scored ML model), and the governance guards `GovernanceError` / `require_approved`.
- [diagnostics.py](src/balance_pricing_engine/diagnostics.py) — `BoundaryDiagnostics`, `ObjectiveDecomposition`, `evaluate_adaptive_joint_rate_grid` (boundary-expansion wrapper), `run_sensitivity_analysis` (returns `delta_loan_rate_bps`, `delta_deposit_rate_bps`, `delta_objective_pct`, `parameter_family`; accepts `n_seeds` for Monte Carlo cohort uncertainty). Default sensitivity cases perturb all four elasticities and two customer-feature coefficients by ±25%.
- [scenarios/runner.py](src/balance_pricing_engine/scenarios/runner.py) — `run_baseline_diagnostics`, `ScenarioDiagnostics` dataclass, `write_artifacts()` persistence. `ScenarioDiagnostics` also exposes `counterfactual_decompositions(top_n)` and `cohort_attribution()`.
- [scenarios/experiments.py](src/balance_pricing_engine/scenarios/experiments.py) — small scenario helpers (e.g. `run_higher_funding_penalty_scenario`).
- [run_ledger/](src/balance_pricing_engine/run_ledger/) — governed pricing-run ledger layer. Single `write_run_ledger()` entry point (re-exported from `__init__.py`) orchestrates: `directory.py` (timestamped `data/outputs/runs/{timestamp}_{scenario_name}/` directory), `metadata.py` (`RunMetadata` dataclass + `build_run_metadata` + `write_run_metadata`), `config_snapshot.py` (YAML copy), `memo.py` (`write_pricing_memo` — markdown recommendation/decomposition/risk memo), `checklist.py` (`write_governance_checklist` — markdown approval checklist). Wraps `ScenarioDiagnostics.write_artifacts()` and adds new artifacts on top.
- [api/app.py](src/balance_pricing_engine/api/app.py) — FastAPI serving layer. Exposes `POST /run-scenario` (original, stable — accepts YAML config string, returns optimization summary dict), `POST /v1/pricing/scenarios` (versioned, governed — backed by `application.PricingService`, typed request/response in [api/schemas.py](src/balance_pricing_engine/api/schemas.py)), `GET /health`, `GET /ready`. Optional; not imported by core engine. Requires `fastapi` extra. Both `POST` routes carry `dependencies=[Depends(require_api_key)]` (see `api/auth.py`). [api/server.py](src/balance_pricing_engine/api/server.py) — launcher used by the Dockerfile `CMD`; resolves optional direct-TLS kwargs from env vars (`resolve_tls_kwargs`) before calling `uvicorn.run`. [api/auth.py](src/balance_pricing_engine/api/auth.py) — opt-in `X-API-Key` check (`require_api_key` FastAPI dependency); no-op unless `PRICING_ENGINE_API_KEYS` is set, reads it via `secrets.SecretsProvider`.
- [application/](src/balance_pricing_engine/application/) — application-layer orchestration seam. `pricing_service.py` — `PricingService.run_scenario(request: PricingScenarioRequest) -> PricingDecision`: resolves feature lineage, calls `run_baseline_diagnostics(registry=...)`, resolves model lineage, evaluates governance gates, optionally runs the shadow-challenger seam and sensitivity analysis, optionally persists a run ledger + additive `decision_record.json`, publishes domain events, and returns a `PricingDecision`. Imports no delivery-layer dependency (no FastAPI). `contracts.py` — `PricingScenarioRequest`, `PricingDecision`, `GovernanceSummary`, `ExecutionContext`. `GovernanceError` from a broken approved champion propagates out of `run_scenario()` unchanged — never downgraded to a `BLOCKED` decision.
- [features/](src/balance_pricing_engine/features/) — lightweight, versioned feature-store boundary for the synthetic diagnostic models only. `contracts.py` — `FeatureSchema` (name/version/columns/target + SHA-256 `fingerprint`), `FeatureLineage`, `FeatureProvider` protocol. `definitions.py` — centralizes `modelling/datasets.py`'s `LOAN_FEATURE_COLUMNS`/`DEPOSIT_FEATURE_COLUMNS` into versioned schemas. `local_store.py` — `LocalFeatureStore`, the only implementation, backed by existing synthetic dataset builders. Does not wire unapproved ML output into the optimizer; does not introduce personalised pricing.
- [governance/](src/balance_pricing_engine/governance/) — governance control plane. `gates.py` — `GateStatus`/`GateResult`/`evaluate_gates()`/`DecisionStatus`/`decision_status_from_gates()`. `lineage.py` — `ModelLineage`, `resolve_model_lineage()` (read-only re-derivation of the same registry decision `ModelScoringAgent.resolve_response_fn` already made — does not duplicate its `GovernanceError` check). `decision_record.py` — `DecisionRecord`, `build_decision_record()`, `write_decision_record()` (additive `decision_record.json`). `shadow.py` — `evaluate_shadow_challenger()`: narrow, optional champion/challenger comparison; never alters selected rates, never raises.
- [events/](src/balance_pricing_engine/events/) — in-process domain events. `models.py` — immutable `DomainEvent` + factory functions (`pricing_scenario_requested`, `pricing_decision_produced`, `governance_review_required`, `pricing_decision_blocked`, `model_resolved`, `run_ledger_written`). `publisher.py` — `EventPublisher` protocol, `NoOpEventPublisher` (default), `InMemoryEventPublisher` (tests), `JSONLEventPublisher` (local audit file), `KafkaEventPublisher` (requires `[kafka]` extra — blocks on send-future ack and raises on failure or timeout; failures propagate, not swallowed here, but every call site routes through `publish_safely()`, which logs rather than retries, so this is best-effort, not durable at-least-once delivery), `publish_safely()` (runs the publish on its own daemon thread — not a shared pool, so a permanently hung publisher can't pin every worker and degrade later requests too — capped at `MAX_CONCURRENT_PUBLISHES` in-flight threads via a semaphore so a sustained outage can't leak threads without bound either, and caps the wait at `deadline_seconds` (default 2s) so a slow/hung publisher can't stall the pricing path; catches/logs publish failures, including ones that finish late in the background, and drops (logs, doesn't spawn) any publish once the concurrency cap is hit — never fails the pricing decision).
- [observability/](src/balance_pricing_engine/observability/) — `logging.py`: `configure_json_logging()`, `log_event()`, `ExecutionMetrics`, `stage_timer()`. Standard library only.
- [secrets/](src/balance_pricing_engine/secrets/) — secrets-provider seam. `provider.py` — `SecretsProvider` Protocol (`get_secret(name) -> str | None`), `EnvSecretsProvider` (default), `StaticSecretsProvider` (dict-backed, tests/local dev). Swap point for a future Vault/AWS Secrets Manager/GCP Secret Manager adapter; currently used by `llm._client.build_client`/`is_llm_available` (for `ANTHROPIC_API_KEY`) and `api.auth.require_api_key` (for `PRICING_ENGINE_API_KEYS`).
- [llm/](src/balance_pricing_engine/llm/) — Tier 4 LLM decision-support layer: `narrative.py` (plain-language scenario narration), `anomaly.py` (config anomaly detection), `model_validation.py` (`ModelValidationReportAgent` — validation-report narration for a `ModelRecord`, same deterministic-fallback/audit-log pattern as `narrative.py`), `_client.py` (Claude API wrapper, default model `claude-sonnet-4-6`; resolves `ANTHROPIC_API_KEY` via `secrets.SecretsProvider`). **Decision-support only** — every response tagged `[DECISION-SUPPORT: requires human review]`. Never wired into automated pricing decisions.
- [modelling/](src/balance_pricing_engine/modelling/) — diagnostic ML layer:
  - `datasets.py` — feature schemas (`LOAN_FEATURE_COLUMNS`, `DEPOSIT_FEATURE_COLUMNS`) and dataset builders.
  - `models.py` — `build_logistic_regression_model`, `build_hist_gradient_boosting_model`, `fit_response_model`, `predict_response_probability`.
  - `evaluation.py` — `evaluate_probabilities(y_true, y_probability)` (pure metrics dict), `evaluate_model(model, frame, features, target)` (thin wrapper), `evaluate_calibration`, `compute_lift_table` / `check_lift_monotonicity`, `compute_rate_uplift` (two-point scenario-comparison uplift via `compute_partial_dependence` — not a trained causal model; the synthetic generators have no treatment/control structure to train one against).
  - `persistence.py` — joblib `save_model` / `load_model`.
  - `agentic.py` — **governed agentic workflow**: `ModelTrainingAgent`, `ModelScoringAgent`, `ModelRegistry`, `ModelRecord`, `TrainingConfig`, `TrainingResult`, plus `ElasticityBaselineModel`, `ElasticityResponseModel`, `LogisticResponseModel`, `HistGBMResponseModel`, `BootstrapEnsembleResponseModel` (bootstrap ensemble; `predict_probability` returns the ensemble mean so it slots into `train()` candidates unchanged, `predict_probability_quantiles` exposes per-row uncertainty bands from replica spread). Single `train(*, dataset, candidates, registry, ...)` entry point replacing the former dual `train` / `train_and_recommend` API.
  - `workflows.py` — orchestration helpers. `reports.py` — `train_response_model_report`, `build_champion_challenger_report`, `build_lift_stability_report` (bootstrap-resamples scored predictions to assess decile lift stability under sampling noise — not model-refit stability across training splits).
- [competitor_rates/](src/balance_pricing_engine/competitor_rates/) — ingestion pipeline with its own CLI ([cli.py](src/balance_pricing_engine/competitor_rates/cli.py)) and subcommands `fetch | validate | apply | report`. Backed by `sources.py` (official bank pages), `extractors.py`, `normalise.py`, `validate.py`, `store.py`, `apply.py`. [collectors/](src/balance_pricing_engine/competitor_rates/collectors/) adds per-bank adapters (`anz.py`, `asb.py`, `bnz.py`, `westpac.py`, `kiwibank.py`) over a shared `base.py` that owns fetching, retry/backoff, snapshotting, enrichment and per-bank failure isolation — a bank changing its website should require editing only that bank's collector.
- [pricing_intelligence/](src/balance_pricing_engine/pricing_intelligence/) — NZ public-market intelligence layer built **on top of** `competitor_rates`, not replacing it. `bps.py` (exact Decimal basis points), `config.py` (all thresholds/timeouts), `store.py` (SQLite historical observation store, one row per distinct advertised state), `validation.py`, `changes.py` (`RateChange` events), `snapshot.py` (per-term dispersion + standard competition ranking), `positioning.py`, `market_state.py` (deterministic LOW/NORMAL/ELEVATED/HIGH), `alerts.py`, `service.py` (`PricingIntelligenceService` facade), `rbnz.py` (RBNZ Official Cash Rate: `fetch_ocr_series`/`OcrSeries`, a single fetch already carries the full published monthly history, so unlike bank rates there is no local store or scheduled collection for it — parsing anchors on the "Official Cash Rate" heading text, never on column shape alone, since the page renders several charts with an identical `Date | series | series` accessible-table shape), `strategy.py` (`CompetitorRateStrategy`/`apply_market_strategy_to_config` for bank rates, `CashRateStrategy`/`apply_cash_rate_strategy_to_config` for the OCR — two independent seams, both `MANUAL`-default), `dashboard_data.py`, `cli.py` (`ocr` subcommand). **Answers "what is the market doing?" only — it never prices anything.**
- [grid_utils.py](src/balance_pricing_engine/grid_utils.py) — `build_inclusive_numeric_grid`: shared helper for inclusive numeric grids used by the optimiser.
- [reporting.py](src/balance_pricing_engine/reporting.py), [plotting.py](src/balance_pricing_engine/plotting.py) — output formatting.
- [utils/random.py](src/balance_pricing_engine/utils/random.py) — `make_rng` for reproducible RNGs.

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
python3 -m balance_pricing_engine.competitor_rates.cli validate --run-file <path>
python3 -m balance_pricing_engine.competitor_rates.cli apply    --config configs/base.yaml
python3 -m balance_pricing_engine.competitor_rates.cli report   --run-file <path>
```

`live_rates` pytest marker is opt-in — fetches **real** bank pages. Do not run in CI or without intent.

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
- Update [docs/architecture.md](docs/architecture.md) and [docs/modelling_assumptions.md](docs/modelling_assumptions.md) when changing structural behaviour.

## Roadmap & recent reviews

- Roadmap: [docs/agentic_modelling_roadmap.md](docs/agentic_modelling_roadmap.md).
- Latest diagnostic review: [docs/diagnostic_review_2026-04-26.md](docs/diagnostic_review_2026-04-26.md).
- Step-by-step business+technical walkthrough: [docs/usage_guide.md](docs/usage_guide.md) (includes restricted-network setup).
- Run ledger usage guide: [docs/run_ledger_guide.md](docs/run_ledger_guide.md) (how to produce, read, and sign off ledger artifacts).

## Context Navigation (Graphify + Codebase Memory)

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

### When to rebuild the graph
- After structural changes (new modules, major refactors)
- Headless: `graphify update .` (only processes modified files)
- Skill: `/graphify . --update` (same behavior, runs through the skill — also accepts `--obsidian` to refresh the vault)
- The graph is persistent — NO need to rebuild every session

### Do NOT
- Don't manually modify files inside `graphify-out/`
- Don't re-read the entire codebase if the graph already has the information

### Codebase Memory (codebase-memory-mcp)
- codebase-memory-mcp is a structural code index only; it has no LLM inside it.
- It does not replace graphify or `AI_Context`.
- If this project is not already indexed in the current agent session, ask the
  agent to "index this project" once.
- Its cache lives outside the repo at `~/.cache/codebase-memory-mcp/`.

## Session wrap-up

If this session changed the project's status, milestone, or current focus, update the "AI bank pricing engine" entry in `~/vault/AI_Context/Memory/Durable_Facts/Kevin/current-projects.md` (status, current focus, last-updated date) before ending the session. Skip this if nothing status-relevant changed.
