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

## Deposit response calibration (2026-09-02)

The term-deposit response was recalibrated once live-verified deposit market
data existed (see [pricing_intelligence.md](pricing_intelligence.md)). This is
the deferred half of the 2026-08-26 loan-side recalibration.

The symptom was the same as the loan side, in mirror image: pricing **at** the
market won only **31.5%** uptake. Offering exactly the market rate should win
roughly a fair share, not a third. The cause was the same too -- a stale anchor.

| Parameter | Was | Now | Why |
| --- | --- | --- | --- |
| `coefficients.deposit.rate_anchor` | 0.045 | 0.039 | The anchor is the reference price at which the own-rate term vanishes. At 4.50% the model treated paying 3.90% into a 3.90% market as unattractive. |
| `coefficients.deposit.intercept` | -0.65 | -0.44 | Re-solved so pricing at market wins ~50% uptake. |
| `deposit_rate_elasticity` | 95 | 140 | Combined own+gap raised from 170 to 250. |
| `deposit_competitor_gap_elasticity` | 75 | 110 | Scaled with the own-rate term, preserving the documented own:gap ratio. |
| `market.competitor_deposit_rate` | 0.045 | 0.039 | Stale default, 60 bps above the verified market. |
| `market.term_deposit_reference_rate` | 0.0400 | 0.039 | Stale spread anchor. |

Deposits are calibrated **less elastic than home loans** (combined 250 vs 300)
on purpose. Term-deposit rates are openly compared, but deposits lack the broker
channel that makes mortgage pricing so price-driven, and existing deposits tend
to auto-roll.

Resulting uptake curve (own 140, gap 110, anchor 3.90%):

| Offered | vs market | Uptake |
| --- | --- | --- |
| 3.50% | -40 bps | 27.0% |
| 3.90% | +0 bps | 49.9% |
| 4.20% | +30 bps | 67.7% |
| 4.50% | +60 bps | 81.6% |

### Effect on the joint recommendation

| | Before | After |
| --- | --- | --- |
| Home loan rate | 6.59% | 6.39% |
| Term deposit rate | 4.20% | 4.00% |
| Objective | $2,392,880 | $4,065,058 |
| Loan volume | $182M | $311M |
| Deposit volume | $182M | $312M |

The **loan** rate moved even though only deposit parameters changed: cheaper
deposit funding lowers the cost of funds, so the optimiser can price loans lower
and win more volume. This is joint optimisation working as intended, and is the
reason the two sides should not be calibrated in isolation.

### Why deposits are raised to roughly 100% of the loan book

`balance_sheet.required_deposit_funding_ratio` (0.65) is a **minimum, not a
target**. Retail deposits at 4.00% substitute for wholesale funding at
`cash_rate + wholesale_funding_spread` = 5.45%, a 145 bps saving, so the
optimiser funds as much of the book with deposits as the response model allows.
It stops near 100% of loan volume because beyond that, surplus deposits are only
credited at `cash_rate` (4.25%), which is a far weaker incentive.

This is economically coherent given the assumptions, but note what the model
does **not** represent: retail deposit supply is unconstrained here, whereas a
real bank faces a finite deposit market, liquidity and concentration limits, and
a rising marginal cost of gathering deposits. Read the deposit volume as "the
model would fund the book retail if it could", not as a raisable amount.

### Still not recalibrated (at the time)

`cash_rate` (4.25%) and `wholesale_funding_spread` (1.2%) remain unverified.
There is no RBNZ/OCR/swap-rate source in the engine yet, and guessing would be
worse than leaving them explicit. This is the last unverified assumption in the
funding curve.

## Cash-rate correction (2026-09-03)

The gap above was closed the same day it was written. `pricing_intelligence.rbnz`
adds a live-verified NZ data source outside the bank collectors: the RBNZ's own
published Official Cash Rate series
(`pricing_intelligence/rbnz.py`, `python3 -m balance_pricing_engine.pricing_intelligence ocr`).

Verified 2026-09-03 against
[the RBNZ's exchange-rates-and-wholesale-interest-rates page](https://www.rbnz.govt.nz/statistics/key-statistics/exchange-rates-and-wholesale-interest-rates):
the current OCR is **2.50%**, not the 4.25% `configs/base.yaml` had been carrying.
A 175 bps gap -- larger than the loan or deposit anchor corrections above.

| Parameter | Was | Now | Why |
| --- | --- | --- | --- |
| `market.cash_rate` | 0.0425 | 0.025 | 175 bps above the live-verified RBNZ OCR |

### What this changed

Correcting `cash_rate` alone moved the baseline recommendation materially --
more than the earlier informal estimate of "~25 bps" suggested, because that
estimate was taken in isolation against a *different* (pre-deposit-recalibration)
baseline. Against the current calibration, the effect is structural, not
marginal:

| | Before | After |
| --- | --- | --- |
| Home loan rate | 6.39% | **6.19%** |
| Term deposit rate | 4.00% | **4.05%** |
| Objective | $4,065,058 | **$5,920,859** |
| Loan volume | $311M | **$510M** |
| Deposit volume | $312M | **$329M** |
| Funding shortfall | $0 | **$2.6M** |
| Wholesale funding cost | $0 | **$6.7M** |

A lower cash rate makes wholesale funding cheaper relative to deposits. The
optimiser responds by leaning on wholesale funding at the margin instead of
paying up for the last increment of deposits: a funding shortfall and a
wholesale funding cost both reappear, where the previous (overstated) cash
rate had pushed the optimum to a deposit-only-funded corner that does not
represent the real trade-off. See "Why deposits are raised to roughly 100% of
the loan book" above -- that section's numbers are now stale in the same way
the earlier sections were before their own corrections; the mechanism it
describes still holds, just at the corrected cash rate.

### What the cash-rate correction deliberately left alone

- **`wholesale_funding_spread` (1.2%) was left untouched.** RBNZ also publishes
  the 90-day bank bill rate alongside the OCR (2.98% as at Aug-2026, an implied
  ~48 bps spread over the OCR) -- a useful corroborating data point, but not a
  directly comparable benchmark for `wholesale_funding_spread`, which is meant
  to represent a bank's broader/longer-duration wholesale funding cost
  (covered bonds, term funding, issuance costs), not the 90-day bank-bill
  spread specifically. Changing it without a genuinely comparable verified
  source would be guessing, the same discipline applied to the loan and
  deposit sides above.
- **The correction is not scheduled or persisted.** Unlike bank rates, RBNZ's
  own published series already carries its full monthly history back to 1999
  in a single page load, so there is nothing to accumulate locally -- see
  `pricing_intelligence/rbnz.py`'s module docstring. `market.cash_rate` in
  `configs/base.yaml` is a config constant, not a live feed, so it will drift
  stale again exactly as it did before; revisit it the same way whenever the
  OCR moves materially. `strategy.apply_cash_rate_strategy_to_config()`
  (`CashRateStrategy.MANUAL` default, `RBNZ_OCR` to source it live) exists
  precisely so a scenario run can pull the current rate on demand without
  needing `configs/base.yaml` to be edited by hand each time.
- **`RBNZ_OCR` can lag an in-month OCR review.** The dashboard chart this reads
  is explicitly labelled "(End of Month)": each row is the OCR that applied at
  a given month's close, not a live "as of right now" figure. If the RBNZ
  Monetary Policy Committee changes the OCR partway through a month, that
  change is not reflected in this series until the new month itself closes and
  RBNZ republishes the chart -- so a scenario run in the days after an in-month
  decision can still pick up the pre-decision rate. `resolve_cash_rate()`
  rejects an observation that is implausibly old (more than
  `MAX_OCR_OBSERVATION_AGE_DAYS`, i.e. longer than a normal OCR review cycle
  plus publish lag) as a stale/stalled fetch, and its `rationale` always names
  the "as at" month so this is visible in any pricing memo -- but neither
  catches a genuine in-month change that simply hasn't reached the EOM series
  yet. Treat `RBNZ_OCR` output as "the OCR as at the last closed month," and
  cross-check against RBNZ's own OCR review calendar before pricing off it in
  the days immediately following a scheduled review.

### A parsing lesson worth keeping

The RBNZ page renders several charts as an accessibility companion to visual
charts it draws with JavaScript: each has a data table with an identical
`Date | series | series` column shape. An early parse of this data grabbed the
first such table on the page and returned NZD/USD exchange-rate values (around
0.55-0.60) as if they were the OCR -- a plausible-looking float in roughly the
right numeric neighbourhood, silently wrong. The parser now anchors on the
"Official Cash Rate" heading text and takes the table that follows it, not the
first table matching the expected shape -- the same category of defect as the
PIE-effective-rate and managed-fund rows found during term-deposit live
verification: two data series sharing a table shape are not the same series,
and only a semantic anchor (a heading, a label) tells them apart, not a
numeric plausibility check.

A second, related lesson (2026-09-04): the parse window used to start at the
first literal occurrence of the substring `"chart__table"` after the heading,
intending to land inside the table. On the live page that substring first
appears inside the *header row's own* `class="chart__table-row"` attribute --
after that row's opening `<tr class="`, truncating it out of the window
entirely. The header row therefore never matched the row regex at all on a
real page (only hand-written test fixtures, which added a `"chart__table"`
marker ahead of the header specifically to route around this, ever exercised
it) -- silently defeating both the header-skip logic and, once added, the
column-order validation described above. The window now anchors on the
literal `"<table"` tag instead, which always precedes every row inside it
structurally. The general shape of the mistake: an anchor string chosen for
convenience turned out to also appear *inside* the very row it was meant to
keep intact.


### Independently audited mortgage benchmarks (2026-09-06)

A benchmark is an external competitor assumption, not the customer rate selected
by the optimiser. Exclude configured `our_bank` before its mean, median or order
statistic is computed. Mortgage competitiveness is lower-is-better; the existing
term-deposit path uses the opposite direction and must retain that distinction.

Special-rate snapshots are a tier-specific, term-specific universe. On the audit
date, BNZ advertised a Standard column, so the default 12-month special benchmark
contained ANZ, ASB, Kiwibank and Westpac: median 4.99%, minimum 4.95%, mean 4.98%.
Full promotional eligibility cannot be inferred from the normalised fields alone.
Repeated suspicious values are not corroboration, and stale trusted fallback
rates retain their original age. Dashboard freshness describes the selected trusted rate,
not a bank's oldest observation at another term. System Health remains bank-wide: its age
uses the oldest resolved trusted confirmation across tracked terms, while quarantine stays
a separate flag. Scenario exclusion warnings describe only the selected term. A stored
scenario recommendation is hidden after loaded engine YAML changes, until an explicit rerun;
its fingerprint covers the exact content supplied to the optimiser. Standard and non-comparable
mortgage events remain audit history, with no ranks or medians borrowed from special-rate snapshots.
Market-state thresholds are transparent
heuristics, not empirically validated signals or optimisation objectives.

Using exactly the same current manual config, the pre-intelligence source at
`c2c30be` and audited source produced identical result fields and byte-identical
`grid_results.csv` and `summary.json`. Earlier default-config recalibrations are
separate from the intelligence seam. The model remains synthetic and does not
estimate strategic competitor reaction or validate advertised-rate eligibility.
See the [audit](audits/2026-09-06-pricing-intelligence-audit.md) for scenario outputs.
