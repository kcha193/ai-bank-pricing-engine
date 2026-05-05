# Synthetic Response Model Card

## Purpose

The response functions simulate portfolio-level customer behaviour for the synthetic pricing
engine. They are deterministic ground-truth assumptions used by the optimiser and by diagnostic
modelling examples. They are not calibrated to real customer records.

## Sources

- Synthetic customer generators in `src/balance_pricing_engine/synthetic/customers.py`.
- Behavioural coefficients configured in `configs/base.yaml` under `response.coefficients`.
- Pricing elasticities configured in `configs/base.yaml` under `response`.

## Calibration Window

No historical calibration window is used. Coefficients are judgemental synthetic assumptions for
the current demo scope and must be replaced before any production or personalised pricing use.

## Validation

Current validation is regression and property based:

- Probability outputs are constrained to the unit interval.
- Home loan response decreases as offered loan rates rise.
- Term deposit response increases as offered deposit rates rise.
- Configured coefficients are tested against the prior hard-coded constants for byte-identical
  output under fixed synthetic cohorts.

## Known Limits

- Portfolio-level pricing only; no personalised pricing policy is represented.
- Coefficients are not estimated from observed applications, balances, or account behaviour.
- No macroeconomic regime, seasonality, channel mix, prepayment, churn, or competitor distribution
  is modelled.
- Credit and deposit customer features are synthetic and should not be interpreted as production
  feature definitions.

## Owner

Model owner: Pricing Engine Maintainer.

Review cadence: revisit whenever coefficients, feature definitions, or the synthetic cohort
generators change.
