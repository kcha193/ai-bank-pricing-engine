# Modelling Assumptions

## Current milestone

- Home loan demand decreases as the offered loan rate increases.
- Term deposit uptake increases as the offered deposit rate increases.
- Competitor rates influence both products.
- Home loan profitability is reduced by expected credit loss.
- Deposits are treated as funding for lending.
- Funding shortfalls receive a financial penalty.
- The optimizer chooses one portfolio-level home loan rate and one portfolio-level term deposit rate.
- ML response models are diagnostic only and are trained on synthetic observed outcomes generated
  from the explicit response functions.

## Deliberate simplifications

- No personalized pricing yet.
- Trained ML response models do not feed the optimizer yet.
- No capital, prepayment, deposit runoff, or behavioural maturity modelling yet.
- Rates are annualized single-period assumptions.
