# Agentic Modelling Roadmap (Governed)

## Current state (as of 2026-05-23)

The engine is portfolio-level, synthetic-data driven, and uses transparent deterministic response
mechanics with known elasticities. All Tier 0–2 and Tier 4 "safe now" items are implemented.
The pluggable response interface wires approved ML models into the optimizer with governed fallback.
A full run-ledger, competitor-rate ingestion pipeline, segmented pricing, multi-period scenarios,
and the LLM decision-support layer are all live. 369 tests pass; ruff clean.

## Tiered evolution path

### Tier 0 — Baseline ✅ complete (champion)

- **Model:** Elasticity/rule-based response with explicit assumptions.
- **Use:** Benchmarking, sensitivity analysis, scenario sanity checks.
- **Controls:** Full transparency and deterministic reproducibility.
- **Done:** Grid search optimizer, adaptive boundary expansion, sensitivity analysis,
  run-ledger governance layer, multi-period diagnostics, segmented pricing.

### Tier 1 — Enhanced statistical ✅ mostly complete

- **Model options:** Logistic response, multinomial choice, GAM, hierarchical Bayes,
  hazard/survival for churn/rollover.
- **Data needs:** Offer history, competitor snapshots, acceptance/attrition outcomes,
  customer/product covariates.
- **Evaluation:** AUC, log loss, Brier, calibration error, directional sanity checks.
- **Governance:** Suitable for regulated use with challenger-first rollout and human approval.
- **Integration:** Pluggable response interface and champion/challenger workflow.
- **Done:** `LogisticResponseModel`, `ResponseFn` Protocol + `MLResponseFnAdapter`,
  `ModelRegistry` with `approve_model`/`promote_champion`/`get_champion`, full evaluation suite
  (AUC, log loss, Brier, calibration, partial dependence, monotonicity check),
  `ModelTrainingAgent` / `ModelScoringAgent` / `ModelRegistry`, model cards.
- **Remaining:** GAM, hierarchical Bayes, survival/hazard models (defer until real rollover data exists).

### Tier 2 — Advanced ML ✅ mostly complete

- **Model options:** Gradient boosting (LightGBM/XGBoost/CatBoost), random forest, uplift,
  quantile uncertainty models.
- **Data needs:** Rich longitudinal observations and treatment assignment traces.
- **Evaluation:** Tier 1 metrics plus lift/decile stability, scenario robustness, stress sensitivity.
- **Governance:** Explainability controls, strict monitoring, and stronger model risk review.
- **Integration:** Optional dependency adapters and offline scoring artifacts.
- **Done:** `HistGBMResponseModel` as first Tier-2 challenger (sklearn, no extra deps),
  feature importances in model cards, partial dependence + monotonicity checks in `evaluation.py`.
- **Remaining:** LightGBM/XGBoost adapters (defer until Tier-2 champion approved),
  uplift/quantile uncertainty models, lift/decile stability report,
  champion/challenger comparison report (`train_response_models.py` + `reports.py`).

### Tier 3 — Deep learning / representation ⬜ not started

- **Model options:** Feed-forward nets, sequence models, embedding-based representations, deep survival.
- **Data needs:** Large event-level panel data and product histories.
- **Evaluation:** Tier 2 metrics plus drift sensitivity and OOD checks.
- **Governance:** Higher complexity risk; decision-support first, constrained promotion criteria.
- **Gate:** Requires real longitudinal panel data and an approved Tier-2 champion.

### Tier 4 — LLM-assisted layer (not price-setting) ✅ mostly complete

- **Safe scope:** Diagnostics narration, scenario summarization, validation report drafting,
  config anomaly commentary, competitor-rate extraction with citation checks.
- **Not allowed:** Autonomous model promotion, deployment, or direct price setting.
- **Controls:** Human review, deterministic fallback, prompt/version control, source citation,
  hallucination checks, full audit logs.
- **Done:** `ScenarioNarrativeAgent` (narration + structured fallback + audit log),
  `ConfigAnomalyChecker` (rule-backed, no LLM required), competitor-rate ingestion pipeline
  with CLI (`fetch | validate | apply | report`), `[llm]` optional extra,
  `NLScenarioInterface` with approval gate + audit log (`llm/scenario_interface.py`, P3-5).
- **Remaining:** Model validation report drafting (human sign-off).

## LLM use-case assessment

| Use case | Status | Notes |
| --- | --- | --- |
| Competitor-rate extraction | **Live** | Source citations + parser validation in place |
| Scenario explanation / narration | **Live** | `ScenarioNarrativeAgent`; deterministic fallback |
| Config anomaly detection | **Live** | `ConfigAnomalyChecker`; pure-rules, no LLM required |
| Natural language scenario-run interface | **Live** | `NLScenarioInterface`; schema validation + approval gate + audit log |
| Model validation report drafting | **Safe with controls** | Human sign-off mandatory; not yet implemented |
| Synthetic data support | **Future only** | Bias/control risk |
| Customer-level pricing explanation | **Not recommended** | Until customer-level decisioning + mature governance |

## What has been built (infrastructure beyond tiers)

- **Run ledger** (`run_ledger/`): timestamped governance artifacts per pricing run — metadata,
  config snapshot, memo, governance checklist, all CSV/JSON outputs.
- **Competitor-rate pipeline** (`competitor_rates/`): fetch → validate → apply → report CLI;
  staleness checks, strict-mode enforcement, `apply` wires rates into `EngineConfig`.
- **Segmented pricing** (`optimization/segmented.py`): rate-band tier optimization across
  customer segments; independent of the portfolio optimizer.
- **Multi-period diagnostics** (`scenarios/multiperiod.py`): roll N periods forward with
  cash-rate drift; produces a `MultiPeriodDiagnostics` summary frame.
- **Pluggable response interface** (`optimization/response_fn.py`): `ResponseFn` Protocol,
  `SyntheticLoanResponseFn` / `SyntheticDepositResponseFn` adapters,
  `MLResponseFnAdapter`, `GovernanceError` / `require_approved`.

## Agentic workflow pattern

- `ModelTrainingAgent`: trains candidates, runs metrics/calibration checks, persists artifacts,
  versions reports, updates registry lineage, recommends champion or challenger.
- `ModelScoringAgent`: validates schema, loads scoring artifacts when present, returns scored
  outputs and diagnostics, falls back to deterministic baseline; exposes `resolve_response_fn`
  for wiring an approved champion into the grid-search optimizer.
- `ModelRegistry`: lightweight local JSON audit trail — model metadata, artifact paths,
  governance status, champion/challenger labels. Supports `approve_model`, `promote_champion`,
  `get_champion`.

## Open items by priority

> **All P2 and P3 items are complete as of 2026-06-01.** See individual plan files in `docs/superpowers/plans/` for implementation details and PR references.

### P2 — ✅ all complete (PR #36)

| ID | Item | Status |
| --- | --- | --- |
| P2-2 | Champion/challenger comparison report | ✅ `build_champion_challenger_report` in `modelling/reports.py` |
| P2-3 | Auto-generate model card from `ModelRegistry` | ✅ `generate_model_cards_from_registry` / `generate_card_for_champion` in `modelling/agentic.py` |
| P2-6 | FastAPI `GET /health` + optional sensitivity response | ✅ `/health` + `include_sensitivity` in `api/app.py` |
| P2-7 | Streamlit dashboard: profit-surface + waterfall | ✅ `dashboard/app.py` |
| P2-10 | Run-ledger checklist sign-off functions | ✅ `sign_off_item` / `sign_off_all_items` in `run_ledger/checklist.py` |

### P3 — ✅ all complete (PR #37)

| ID | Item | Status |
| --- | --- | --- |
| P3-1 | Tenor and reinvestment dynamics in multi-period | ✅ `reinvestment_spread_bps` + `loan_tenor_years` in `MultiPeriodConfig` |
| P3-2 | Capital/RWA term + ROE hurdle constraint | ✅ `average_loan_tenor_years` + `min_return_on_equity` + `maturity_adjusted_rwa_weight` |
| P3-3 | LightGBM/XGBoost optional-dep adapters | ✅ `LightGBMResponseModel` + `XGBoostResponseModel` behind `[ml]` extra |
| P3-4 | Survival/hazard response model | ✅ `SurvivalResponseModel` + `build_attrition_dataset` in `modelling/agentic.py` |
| P3-5 | NL scenario-run interface (Tier 4) | ✅ `NLScenarioInterface` with approval gate + audit log in `llm/scenario_interface.py` |

## Advanced model design notes

For future LightGBM/XGBoost, survival, migration, prepayment, credit-linked pricing, and deep learning:

- keep optional dependencies behind adapters,
- define explicit targets,
- publish feature schema,
- require calibration and stability checks,
- add explainability outputs,
- preserve the `ResponseFn` interface so optimizer integration remains model-agnostic,
- mark outputs as decision-support until governance approval.
