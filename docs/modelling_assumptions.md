# Modelling Assumptions

## Current milestone

- Home loan demand decreases as the offered loan rate increases.
- Term deposit uptake increases as the offered deposit rate increases.
- Competitor rates influence both products.
- Home loan profitability is reduced by expected credit loss.
- Capital charge uses `loan_rwa_weight`; when `average_loan_tenor_years` is set, the profit module
  applies the maturity-adjusted RWA proxy before optimization and sensitivity runs. When it is
  unset, the raw RWA weight is used. A one-year tenor is neutral; longer tenors increase the proxy
  RWA weight.
- Deposits are treated as funding for lending.
- Funding shortfalls receive a financial penalty.
- `min_return_on_equity`, when configured, is an optimizer floor over the grid's ROE diagnostic
  column. If no evaluated candidate has a finite qualifying ROE, winner selection falls back to the
  unconstrained objective ranking.
- The optimizer chooses one portfolio-level home loan rate and one portfolio-level term deposit rate.
- ML response models are trained on synthetic observed outcomes generated from the explicit
  response functions. By default, scenario runs use the deterministic synthetic response functions.
  If a caller passes a model registry into `run_baseline_diagnostics()`, an approved compatible
  champion model can provide response probabilities through the governed `MLResponseFnAdapter`;
  otherwise the runner falls back to the synthetic response functions. Available adapters include
  logistic regression, histogram gradient boosting, optional LightGBM, optional XGBoost, and an
  optional survival/hazard adapter. The survival adapter reports event probability to match binary
  target metrics where `1` means churn/event observed.
- Multi-period scenario rolls are available for portfolio-level diagnostics. They rerun the baseline
  scenario for `n_periods`, apply `loan_runoff_rate` and `deposit_runoff_rate` to prior-period
  back-book economics, apply `reinvestment_spread_bps` to surviving loan balances, discount period
  contributions with `discount_rate`, and can shift future cash rates with
  `market_cash_rate_drift_bps`.

## Deliberate simplifications

- No personalized pricing yet.
- Trained ML response models do not feed the optimizer by default. They can feed the optimizer only
  through the explicit approved-champion registry path in `run_baseline_diagnostics(registry=...)`,
  with deterministic synthetic fallback when no approved compatible champion is available.
- No prepayment or behavioural maturity modelling yet.
- Multi-period cash-rate drift does not automatically drift competitor loan or deposit rates; the
  implementation warns because competitor gaps will shift mechanically.
- Rates are annualized assumptions within each scenario period.
