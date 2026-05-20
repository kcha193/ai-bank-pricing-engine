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
- Multi-period scenario rolls are available for portfolio-level diagnostics. They rerun the baseline
  scenario for `n_periods`, apply `loan_runoff_rate` and `deposit_runoff_rate` to prior-period
  back-book economics, discount period contributions with `discount_rate`, and can shift future cash
  rates with `market_cash_rate_drift_bps`.

## Deliberate simplifications

- No personalized pricing yet.
- Trained ML response models do not feed the optimizer yet.
- No prepayment or behavioural maturity modelling yet.
- Multi-period cash-rate drift does not automatically drift competitor loan or deposit rates; the
  implementation warns because competitor gaps will shift mechanically.
- Rates are annualized assumptions within each scenario period.
