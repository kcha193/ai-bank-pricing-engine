# Agentic Modelling Roadmap (Governed)

## Current state
The engine is portfolio-level, synthetic-data driven, and uses transparent deterministic response mechanics with known elasticities. It is interpretable and easy to validate, but it is simplified relative to production retail banking dynamics.

## Tiered evolution path

### Tier 0 — Baseline (current champion)
- **Model:** Elasticity/rule-based response with explicit assumptions.
- **Use:** Benchmarking, sensitivity analysis, scenario sanity checks.
- **Controls:** Full transparency and deterministic reproducibility.

### Tier 1 — Enhanced statistical
- **Model options:** Logistic response, multinomial choice, GAM, hierarchical Bayes, hazard/survival for churn/rollover.
- **Data needs:** Offer history, competitor snapshots, acceptance/attrition outcomes, customer/product covariates.
- **Evaluation:** AUC, log loss, Brier, calibration error, directional sanity checks.
- **Governance:** Suitable for regulated use with challenger-first rollout and human approval.
- **Integration:** Pluggable response interface and champion/challenger workflow.

### Tier 2 — Advanced ML
- **Model options:** Gradient boosting (LightGBM/XGBoost/CatBoost), random forest, uplift, quantile uncertainty models.
- **Data needs:** Rich longitudinal observations and treatment assignment traces.
- **Evaluation:** Tier 1 metrics plus lift/decile stability, scenario robustness, and stress sensitivity.
- **Governance:** Explainability controls, strict monitoring, and stronger model risk review.
- **Integration:** Optional dependency adapters and offline scoring artifacts.

### Tier 3 — Deep learning / representation
- **Model options:** Feed-forward nets, sequence models, embedding-based representations, deep survival.
- **Data needs:** Large event-level panel data and product histories.
- **Evaluation:** Tier 2 metrics plus drift sensitivity and OOD checks.
- **Governance:** Higher complexity risk; decision-support first, constrained promotion criteria.

### Tier 4 — LLM-assisted layer (not price-setting)
- **Safe scope:** Diagnostics narration, scenario summarization, validation report drafting, config anomaly commentary, competitor-rate extraction with citation checks.
- **Not allowed:** Autonomous model promotion, deployment, or direct price setting.
- **Controls:** Human review, deterministic fallback, prompt/version control, source citation, hallucination checks, full logs.

## LLM use-case assessment
- Competitor-rate extraction: **Safe with controls** (source citations plus parser validation).
- Scenario explanation: **Safe now** (decision-support narrative only).
- Natural language interface for scenario runs: **Safe with controls** (schema validation plus approvals).
- Config anomaly detection: **Safe now** (rule-backed checks).
- Model validation drafting: **Safe with controls** (human sign-off mandatory).
- Synthetic data support: **Future only** (bias/control risk).
- Customer-level pricing explanation generation: **Not recommended** until customer-level decisioning exists and governance controls mature.

## Near-term implementation
- Introduced governed `ModelTrainingAgent` and `ModelScoringAgent`.
- Added lightweight local `ModelRegistry` JSON lineage.
- Preserved baseline elasticity model as default fallback/champion.
- Added logistic challenger path without new heavy dependencies.
- Added reproducibility controls: fixed seed, config hashing, artifact persistence, versioned report outputs.
- Kept all recommendations gated by human approval.

## Agentic workflow pattern
- `ModelTrainingAgent`: trains candidates, runs metrics/calibration checks, persists artifacts, versions reports, updates registry lineage, and recommends a champion or challenger candidate.
- `ModelScoringAgent`: validates schema, loads scoring artifacts when present, returns scored outputs and diagnostics, and falls back to deterministic baseline if unavailable.
- `ModelRegistry`: stores a lightweight local JSON audit trail for model metadata, artifact paths, governance status, and champion/challenger labels.

## Advanced model design notes
For future LightGBM/XGBoost, survival, migration, prepayment, credit-linked pricing, and deep learning options:
- keep optional dependencies behind adapters,
- define explicit targets,
- publish feature schema,
- require calibration and stability checks,
- add explainability outputs,
- preserve the same response-model interface so optimizer integration remains model-agnostic,
- mark outputs as decision-support until governance approval.
