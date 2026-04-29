# Agentic Modelling Roadmap (Governed)

## Current state (Tier 0)
- Deterministic elasticity-based synthetic response functions remain the transparent baseline.
- Portfolio-level optimizer uses expected value calculations for volume, cost, loss, funding shortfall and objective.
- Suitable as benchmark and stress/scenario tool; not sufficient as full behavioural production model.

## Tier roadmap

### Tier 0 — Baseline elasticity
- **Problem solved:** transparent benchmark and sensitivity testing.
- **Data:** synthetic customer features + configured elasticities.
- **Training/calibration:** parameter calibration by scenario assumptions.
- **Evaluation:** directional checks, objective sensitivity, boundary checks.
- **Explainability/regulation:** very high; strong for challenger baseline.
- **Integration:** default response adapter.

### Tier 1 — Enhanced statistical models
- **Models:** logistic, multinomial choice, GAM, hazard/survival.
- **Targets:** acceptance/open/churn/rollover event probabilities.
- **Data:** customer/product/rate offer history + competitor context.
- **Evaluation:** AUC, log loss, Brier, calibration error, lift.
- **Explainability:** high to medium (coefficients/partial effects).
- **Regulatory fit:** good with controls and documentation.
- **Integration:** pluggable response interface and champion/challenger workflow.

### Tier 2 — Advanced ML
- **Models:** gradient boosting, random forests, calibrated ensembles.
- **Targets:** propensity and volume-quality trade-off signals.
- **Data:** richer behavioural and temporal features.
- **Evaluation:** Tier 1 metrics + stability/stress scenario metrics.
- **Explainability:** medium with SHAP/global PDP.
- **Regulatory fit:** acceptable with strict governance and independent validation.
- **Integration:** optional dependency adapters and offline scoring artifacts.

### Tier 3 — Deep representation models
- **Models:** DNN, sequence models, deep survival, embeddings.
- **Targets:** dynamic response/churn/migration probabilities.
- **Data:** large longitudinal histories and event sequences.
- **Evaluation:** discrimination + calibration + drift robustness.
- **Explainability:** medium/low unless paired with explanation tooling.
- **Regulatory fit:** future-only unless strong model-risk controls are in place.
- **Integration:** isolated optional module and challenger-only initially.

### Tier 4 — LLM-assisted support (not decisioning)
- **Use:** diagnostics summaries, validation narratives, scenario commentary, competitor-rate extraction with citations, config checks.
- **Not allowed:** autonomous model promotion, deployment, or direct price setting.
- **Controls:** human review, source citation, structured schema validation, deterministic fallback, logging, prompt/version control, hallucination checks.

## LLM use-case classification
- Competitor-rate extraction: **Safe with controls**.
- Scenario explanation/commentary: **Safe now** (decision-support only).
- Natural language scenario runner: **Safe with controls**.
- Config validation/anomaly detection: **Safe now** with deterministic rules fallback.
- Model validation report drafting: **Safe with controls**.
- Synthetic data support: **Future only** (bias/privacy controls).
- Customer-level pricing explanations: **Not recommended** until personalized pricing governance exists.

## Advanced model design notes
For LightGBM/XGBoost, survival, migration, prepayment, and credit-linked pricing:
- define explicit targets,
- publish feature schema,
- require calibration and stability checks,
- add explainability outputs,
- mark outputs as decision-support until governance approval.

## Agentic workflow pattern
- `ModelTrainingAgent`: trains candidate models, runs metrics/calibration, persists artifacts, recommends champion candidate, requires human approval.
- `ModelScoringAgent`: loads artifact, validates schema, produces scored outputs and diagnostics, falls back to deterministic baseline if unavailable.
- `ModelRegistry`: lightweight local JSON audit trail for model metadata and governance status.
