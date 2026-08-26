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

## Response calibration (2026-08-26)

The home-loan response coefficients were recalibrated against the live-verified
NZ market. Before this, the engine recommended **8.10%** into a market whose
1-year median is **4.99%** -- a 311 bps premium at which it still believed 7% of
customers would accept.

Two things were wrong:

| Parameter | Was | Now | Why |
| --- | --- | --- | --- |
| `coefficients.loan.rate_anchor` | 0.065 | 0.0499 | The anchor is the reference price at which the own-rate term vanishes, so it must track the market being priced into. At 6.5% the model treated a 6.5% loan as neutrally priced against a 4.99% market. |
| `coefficients.loan.intercept` | 1.10 | 2.39 | Re-solved to hold acceptance at market at ~86%, so moving the anchor changed the *slope reference*, not the baseline win rate. |
| `loan_rate_elasticity` | -85 | -170 | Combined own+gap elasticity of -150 left 13.6% acceptance at +260 bps. In a broker-intermediated, price-transparent mortgage market, pricing 260 bps above every competitor wins approximately nothing. |
| `loan_competitor_gap_elasticity` | -65 | -130 | Scaled with the own-rate term, preserving the documented own:gap ratio. |
| `market.competitor_loan_rate` | 0.0675 | 0.0499 | Stale default, 176 bps above the verified market. |
| `market.home_loan_reference_rate` | 0.0525 | 0.0499 | Stale spread anchor. |

Resulting acceptance curve (own -170, gap -130, anchor 4.99%):

| Offered | vs market | Acceptance |
| --- | --- | --- |
| 4.99% | +0 bps | 85.8% |
| 5.50% | +51 bps | 59.0% |
| 6.00% | +101 bps | 26.5% |
| 7.00% | +201 bps | 1.9% |

The baseline recommendation moved from **8.10% to 6.59%**, and expected loan
volume from $540M to $182M.

### What was deliberately not changed

- **The elasticities were not tuned until the recommendation equalled the market
  price.** That would be fitting the answer, not calibrating the model. The
  residual ~160 bps premium is structural: the optimiser maximises
  *unconstrained* risk-adjusted profit, modelling no competitive reaction and no
  market-share or volume constraint. A premium is a coherent optimum for a model
  with no share constraint; real banks price nearer market because of strategic
  constraints this engine does not represent.
- **The funding curve was left alone.** `cash_rate` (4.25%) is high relative to a
  4.99% mortgage market, but no verified OCR/swap source exists yet -- changing
  it would be guessing. Testing showed it is not the binding constraint anyway:
  dropping cash_rate to 2.50% moves the recommendation only 25 bps. Proper
  treatment needs RBNZ/swap-rate intelligence.
- **The deposit side was left alone.** Term-deposit market data is phase 2 and
  unverified, so `deposit` coefficients and `competitor_deposit_rate` remain the
  original synthetic assumptions.

Recalibration should be revisited whenever the market moves materially away from
the anchor. The anchor is a constant in config, not a live feed, so it will drift
again -- that drift is what produced the 311 bps error above.
