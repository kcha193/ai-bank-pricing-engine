# Model Governance Notes

This project is a synthetic retail bank balance-sheet pricing prototype. It is not a production
pricing approval system.

## Current Model Boundary

The current engine optimizes one portfolio-level home loan rate and one portfolio-level term
deposit rate. It does not perform personalized pricing, customer-level discrimination testing,
capital allocation, or production limit management.

The response, credit-loss, and funding assumptions are deterministic code-level assumptions. The ML
response models are diagnostic only: they train on synthetic outcomes generated from the known
response functions and help check whether learned models recover sensible rate-response direction.

## Calibration Expectations

Before any production use, replace synthetic assumptions with governed calibration inputs:

- customer response elasticities estimated from controlled historical or experimental data
- credit-loss assumptions aligned to approved PD/LGD/EAD or expected-loss frameworks
- transfer-pricing and wholesale-funding assumptions aligned to treasury curves
- deposit-funding ratio and penalty parameters approved as explicit risk appetite settings

Each calibration update should record source data, sample window, transformations, validation
results, approval owner, and effective date.

## Validation Cadence

For prototype development, run the test suite and examples after pricing-model changes:

```bash
python3 -m pytest -q
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/train_response_models.py
```

For governed use, add scheduled validation that compares recommendations, volumes, objective
components, and sensitivity outputs against prior approved baselines. Use
`ScenarioDiagnostics.write_artifacts()` to persist grid results, top candidates, sensitivity
summaries, and the accepted recommendation.

## Key Model-Risk Checks

- Boundary behavior: final recommendations should not remain at expanded grid boundaries without
  review.
- Elasticity uncertainty: challenger elasticity assumptions should not reverse recommendations
  without explanation.
- Funding interpretation: wholesale funding cost and shortfall penalty are additive by design.
  Document any scenario where the shortfall penalty is intended to replace rather than supplement
  wholesale funding cost.
- Synthetic circularity: ML diagnostics trained on synthetic outcomes do not validate real customer
  behavior.
- Conduct scope: portfolio-level pricing does not remove the need for future fairness and conduct
  controls if the project moves toward segmented or personalized pricing.
