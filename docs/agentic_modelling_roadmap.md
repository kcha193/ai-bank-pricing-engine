# Agentic Modelling Roadmap (Governed)

## Current state
The engine is portfolio-level, synthetic-data driven, and uses transparent deterministic response mechanics with known elasticities. It is interpretable and easy to validate, but it is simplified relative to production retail banking dynamics.

## Tiered evolution path

### Tier 0 — Baseline (current champion)
- **Model**: Elasticity/rule-based response with explicit assumptions.
- **Use**: Benchmarking, sensitivity analysis, scenario sanity checks.
- **Controls**: Full transparency and deterministic reproducibility.

### Tier 1 — Enhanced statistical
- **Model options**: Logistic response, multinomial choice, GAM, hierarchical Bayes, hazard/survival for churn/rollover.
- **Data needs**: Offer history, competitor snapshots, acceptance/attrition outcomes, customer/product covariates.
- **Evaluation**: AUC, log loss, Brier, calibration error, directional sanity checks.
- **Governance**: Suitable for regulated use with challenger-first rollout and human approval.

### Tier 2 — Advanced ML
- **Model options**: Gradient boosting (LightGBM/XGBoost/CatBoost), random forest, uplift, quantile uncertainty models.
- **Data needs**: Rich longitudinal observations and treatment assignment traces.
- **Evaluation**: Tier 1 metrics + lift/decile stability, scenario robustness, stress sensitivity.
- **Governance**: Use explainability controls (SHAP/partial dependence), strict monitoring, stronger model risk review.

### Tier 3 — Deep learning / representation
- **Model options**: Feed-forward nets, sequence models, embedding-based representations, deep survival.
- **Data needs**: Large event-level panel data and product histories.
- **Evaluation**: Tier 2 metrics + drift sensitivity and OOD checks.
- **Governance**: Higher complexity risk; decision-support first, constrained promotion criteria.

### Tier 4 — LLM-assisted layer (not price-setting)
- **Safe scope**: Diagnostics narration, scenario summarization, validation report drafting, config anomaly commentary, competitor-rate extraction with citation checks.
- **Not allowed**: Autonomous model promotion or direct price deployment.
- **Controls**: Human review, deterministic fallback, prompt/version control, source citation, hallucination checks, full logs.

## LLM use-case assessment
- Competitor-rate extraction: **Safe with controls** (source citations + parser validation).
- Scenario explanation: **Safe now** (decision-support narrative only).
- NL interface for scenario runs: **Safe with controls** (schema validation + approvals).
- Config anomaly detection: **Safe now** (rule-backed checks).
- Model validation drafting: **Safe with controls** (human sign-off mandatory).
- Synthetic data support: **Future only** (bias/control risk).
- Customer-level pricing explanation generation: **Not recommended** until customer-level decisioning exists and governance controls mature.

## Near-term implementation (this change)
- Introduced governed `ModelTrainingAgent` and `ModelScoringAgent`.
- Added lightweight local `ModelRegistry` JSON lineage.
- Preserved baseline elasticity model as default fallback/champion.
- Added logistic challenger path without new heavy dependencies.
- Added reproducibility controls: fixed seed, config hashing, artifact persistence, report outputs.

## Advanced model design notes
For future LightGBM/XGBoost/survival/deep learning options, keep them behind optional dependencies and maintain the same response-model interface so optimizer integration remains model-agnostic.
