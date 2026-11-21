# Phase 4 calibration methodology and delivery roadmap

**Proposed for review.** Phase 4a retains the synthetic economics. The
[public evidence register](source-register.md) defines the comparators and
their limits. Each method needs evidence for the target population and a
separate approval record before its use in a priced recommendation.

## ECL and LVR bands (FIN-001, proposed Phase 4b)

The engine generates an annual loss rate from a bounded synthetic risk score:
`0.0015 + 0.018 × risk_score^1.4`. LVR enters the score alongside credit score
and a loan-to-income term. The pricing grid estimates losses as the sum of
acceptance probability × requested balance × synthetic loss rate. This makes
the reported loss rate sensitive to the accepted mix, even when the loss
formula stays fixed. The distribution across applicants, the
requested-balance-weighted rate and the expected-book loss rate answer
different questions.

### Proposed representation

For a one-year pricing horizon, estimate annual loss as
`Σᵢ aᵢ × Σₛ wₛ × PDᵢ,ₛ × LGDᵢ,ₛ × EADᵢ,ₛ`. Here `aᵢ` is the probability the
facility books at the candidate price. Scenario weights `wₛ` sum to one.
PD is the probability of first default within 12 months **conditional on
booking**, LGD is the loss fraction conditional on default, and EAD is the
conditional exposure at default in NZD. If timing within the year affects
severity or balance, integrate across default-time buckets with first-default
probabilities. Estimate conditional loss or use consistent joint risk
scenarios when severity and EAD are dependent. Report annual loss in NZD and
divide by the expected booked principal for the portfolio loss rate. Keep the
booked-cohort ECL rate and its version available to the multi-period roll.

- **PD.** Define the default event, observation start, cure/redefault rule
  and horizon. Start with pooled vintage estimates by LVR and risk band.
  Treat prepayment and refinancing explicitly as competing events or handle
  censoring under a documented assumption. Test whether the approved
  point-in-time or through-the-cycle basis fits the pricing decision.
- **LGD.** Follow defaulted facilities through cure, recovery and resolution.
  Specify collateral valuation, recoveries, workout costs and discounting.
  Test severity against current LVR and house-price stress, with uncertainty
  for unresolved defaults and sparse bands.
- **EAD.** Project balance at default using amortisation and the contractual
  product terms. Include further drawings only for facilities with eligible
  undrawn commitments. Keep EAD and initial booked balance distinct.
- **LVR.** Start review with `≤60%`, `>60–80%`, `>80–90%`, `>90%` and
  an unknown-valuation bucket. These are analytical intervals. Define
  origination versus indexed current LVR, collateral rank, treatment of
  insurance and shared security, and whether owner-occupied, investor or
  non-prime lending is in scope. Use origination LVR for the origination
  mix and test updated LVR and collateral value in the LGD model. Use
  pooling with uncertainty intervals where high-LVR or default counts
  are small. Publish band edges with the estimates.

The [public register](source-register.md) covers NZ IFRS 9, asset quality,
bank disclosures and new lending by LVR (ECL-01 through ECL-04). Accounting
impairment expense is a period movement and can include transfers and
forward-looking changes. Arrears are a delinquency state. A default rate
omits loss severity and exposure. Regulatory expected loss and capital
inputs can use distinct definitions or conservatism. NZ IFRS 9 12-month ECL
captures the lifetime cash shortfalls arising from defaults possible within
12 months. Specify the pricing economic-loss convention and provide a bridge
before comparing any of these metrics.

**Required evidence.** In an authorised validation environment, request
facility-vintage records with offers and booked exposures, product and
occupancy tags, credit-risk grades, origination and updated valuations, default
and cure dates, repayments, prepayments, recovery cash flows and collection
costs. Keep observation and outcome dates. Reconcile to approved aggregate
balances and net realised losses. Compare a through-cycle benchmark and a
recent vintage view. Validate calibration and uncertainty by band and
vintage on out-of-time vintages. Report actual versus expected default counts,
defaulted balances and losses, including stressed collateral and censored
recoveries. Document representativeness of prime NZ residential mortgages.
Aggregate public metrics provide reasonableness checks.

**Credit Risk decisions.** Approve default and cure definitions, horizon and
discounting, population, LVR valuation and bands, treatment of prepayments,
workouts, economic scenarios and sparse segments, and tolerated error before
testing. Agree how to quantify changes in book mix with Pricing. Require
evidence before changing the current formula.

## Deposit supply and funding availability (FIN-001, proposed Phase 4c)

The current engine sums each synthetic deposit amount weighted by its uptake
probability. The prospect count and amount distribution set the gross
opportunity pool. More prospects both increase the pool and resample the
synthetic mix. The output represents gross booked deposits, with no market
share ceiling, maturity wall, rollover, cannibalisation or funding-value
haircut. The ratio target affects the objective through a soft dollar
penalty. It can bind economically even with no hard constraint on selection.

### Proposed representation

For each comparable term, channel and balance tier in a fixed horizon, model
`expected gross bookings = addressable deposit opportunity × eligible share
× product participation × rate-dependent conversion`.

- Use NZD of available flows over the horizon as the addressable opportunity.
  Sector deposit stocks can bound stock-based estimates. Convert stock to
  eligible flows using maturity ladders, external inflows and dated
  assumptions. Define share as the bank's reachable share of those flows
  before product participation and response. Bound all fractions in
  `[0, 1]` and reconcile disjoint segments to their parent market.
- Split external new money and retained maturities. Use matured principal
  once, with rollover or runoff outcomes. Track internal migrations from
  savings and other term buckets separately. For new-funding attribution,
  subtract the displaced internal balance and credit only the incremental
  funding. Attribute incremental interest and any acquisition or retention
  cost against the source product's counterfactual. Define the comparator
  explicitly when retention avoids an external outflow.
- Track usable funding by maturity, concentration and stability. Validate
  top-balance exposures and runoff in stress. Treasury should decide
  whether concentration and funding requirements enter as hard limits or
  shadow costs. Keep the economic cost of funding, incremental retail
  acquisition expense and management steering visible individually.
- Treat synthetic prospects as a numerical sample of response heterogeneity.
  Normalise scenario weights to a fixed segment opportunity so increasing
  sample size changes approximation error instead of expanding market
  capacity. Require convergence and caps under high candidate rates.

**Required evidence.** Use aggregate stocks, size/term distributions and
repricing statistics from DEP-01 through DEP-03 as context. Request term-level
maturities, renewal offers, net inflows, product migration, rate and
competitor histories, channel, tier, balance-weighted conversions, and
acquisition and servicing costs in an authorised environment. Distinguish
customer counts, booked balances, stock and flows. Include rejected offers
and exposure denominators where available.

**Calibration.** Construct and reconcile an addressable-flow series before
estimating conversion. Estimate conversion and retention under independent
variation of offered rates and market gaps, with term/channel and date
controls or governed experiments. At a fixed competitor rate,
`β_own × offered_rate + β_gap × (offered_rate − competitor_rate)` identifies
only the sum `β_own + β_gap` from changes to the offer. Rate changes may also
respond to expected demand. Use credible independent movements or document
identification limits and uncertainty. Validate gross and incremental
balances, rollover and mix on held-out vintages and rate regimes. Stress
market size, share, conversion, funding ratio and concentration separately
before interactions. Choose ranges from evidence or pre-declared illustrative
shocks before viewing pricing outcomes.

**Treasury and Deposits decisions.** Which term and balance tiers are
substitutable? What is the eligible and reachable market over the decision
horizon? What share of gross bookings is new money, retained balance or
cannibalisation? Which costs change at the margin? What maturity, runoff and
concentration limits matter in normal and stressed conditions? What balance
definition, time horizon and limit apply to the deposit funding ratio?
Approve each definition and the conversion validity range with Pricing.

## Term-matched FTP and ROE (FIN-003, proposed Phase 4d)

The current annualised funding proxy is OCR plus a flat wholesale spread.
Apply it to the residual `max(loans − deposits, 0)`; surplus deposits earn
OCR. The proxy anchors short-term money rates. A fixed-rate mortgage needs an
interest-rate risk reference for its repricing cash flows, and funding the
remaining mortgage principal can extend beyond the first fixed period. A
term deposit's contractual maturity and its behavioural renewal and runoff
carry separate risks. The [public register](source-register.md) documents
market reference and liquidity context (FTP-01 through FTP-04).

### Proposed curve and attribution

`FTP(t, as_of) = term-matched market reference(t, as_of) + liquidity premium
(t, as_of) + explicitly supported adjustments(t, as_of)`.

1. Store NZD curve date/time, NZ business-calendar conventions, source,
   tenor, quote units, reference instrument and index, compounding,
   timestamp, validity window, and interpolation and extrapolation policy.
   Capture approved wholesale issuance or funding spreads by term and
   basis, hedging basis and measurable liquidity costs where applicable.
   For a 12-month fixed pricing challenge, a dated 1-year NZD swap
   reference with the corresponding funding premium is a candidate.
   Document fixed period, liquidity maturity and instrument basis
   separately. Record interpolation and fail the calibration view on
   missing or stale required inputs under the approved policy.
2. Match interest-rate repricing to fixed coupon and principal cash flows
   including amortisation and supported prepayment assumptions. Use
   funding cash-flow buckets or a validated replicating profile for
   liquidity tenor and mortgage refinancing risk. Match term deposits to
   contractual cash flows and credit stable funding beyond maturity only
   where renewal is validated. Define how negative rates/spreads,
   breaks, reinvestment and optionality are treated. Publish curve
   sensitivities with hypothetical inputs labelled illustrative.
3. Attribute external marginal wholesale cost to residual deficits per
   approved tenor bucket and credit surplus funds at the appropriate
   reinvestment rate. If product P&L uses internal FTP, charge the loan
   and credit the deposit to a central treasury book using the same
   matched legs. Eliminate the internal transfers on consolidation.
   Reconcile residual market funding and investible surplus once across
   the active portfolio, including the booked cohorts. Document gross
   asset funding versus funding net of equity so capital and wholesale
   conventions reconcile.
4. Keep retail interest expense as the customer coupon times the
   applicable balances. Isolate any attributed cost for regulatory
   liquidity buffers and structural funding limits. If the liquidity
   premium already includes a cost, show its attribution within the
   premium. Show the deposit funding shortfall and steering charge as
   management terms. Confirm the cash flow to which each spread or
   adjustment applies so any funding or liquidity cost is counted once.

### Proposed reporting bridge

The current reported ROE uses
`(model objective + capital charge) / required capital`. Its numerator
includes the steering penalty and excludes tax and operating expense.
Use the same synthetic components to illustrate a proposed pre-tax,
pre-opex economic ROE at unchanged capital basis:

- `economic profit before capital = loan interest − ECL − retail
  interest expense − residual wholesale funding cost + surplus income`
  under the current economic components.
- `economic ROE = economic profit before capital / required capital`
  where capital is positive. Show economic ROE as unavailable when the
  denominator is zero. Approved operating costs, tax and distinct liquidity
  costs would require explicit adjustments and a named basis.
- `model objective = economic profit before capital − capital charge −
  steering penalty` on consistent components. At unchanged capital and
  other costs, the economic ROE bridge adds back the steering penalty to
  the numerator of current ROE. Keep the existing ROE field and floor
  semantics during any reporting-only change. Changing the floor metric,
  its threshold or selection requires explicit approval and a migration.

**Treasury/ALM decisions.** Approve curve instruments and as-of data, the
marginal versus average funding basis, tenor coverage and interpolation,
behavioural lives, structural funding and liquidity costs, hedging basis,
and transfer elimination. Finance and Model Risk should approve the ROE
numerator, capital denominator, tax/opex basis and any future gate migration.
An illustrative curve scenario uses an offline proxy shock, with no
production curve fit or rollout.

## Capital basis and LVR-banded RWA (FIN-004, proposed Phase 4e)

The engine uses a 10% tier-1 capital ratio and a flat 35% loan risk weight.
At that unadjusted weight the implied capital allocation is 3.5% of booked
principal and the 12% hurdle costs 42 bp of booked principal per year.
When a maturity tenor is configured, the synthetic uplift scales the risk
weight by `1 + 0.06 × (clamp(tenor in years, 1, 5) − 1)`. A 12-month
tenor applies no uplift. This relationship is an illustration. Regulatory
capital regimes and transition dates are tracked in CAP-01 through CAP-03.

**Proposed configurable basis.** Preserve a named flat synthetic option
for reproducibility. Add a versioned optional LVR-banded RWA schedule,
with explicit bound conventions, eligibility and missing-LVR handling.
Compute expected RWA as `Σᵢ aᵢ × exposureᵢ × risk_weight(bandᵢ, basis)`.
For a regulatory allocation, apply an approved configurable tier-1
allocation ratio. State jurisdiction, institution approach, effective date,
product treatment and the relationship between minimum, buffers and
management target. An economic-capital option needs a separately supported
unexpected-loss allocation, including horizon, confidence level and
concentration/diversification policy. Record which capital basis supplies
the denominator and use that same basis for the hurdle charge.
Keep the return hurdle configurable and document its pre-tax/post-tax
basis and consistency with the numerator.

**Evidence and decisions.** Request the applicable rules and transition
schedule, an approved eligible exposure and LVR distribution, reconciled
RWA/allocated-capital summaries, the business target and a dated hurdle
basis. Distinguish total mortgage life, remaining term, initial repricing
period and any maturity adjustment in the applicable rule. Retire the
illustrative uplift only under an approved retail methodology. Capital and
Finance should approve the risk-weight source and basis, LVR/eligibility
mapping, missing-value rule, denominator and hurdle. Test booked-cohort
capital preservation and agree an explicit reallocation policy for later
changes in rule, valuation or term.

## SME validation checklist

The roles below describe prospective reviewers. Sign-off would identify the
approved parameter set, evidence snapshot, validation results, owner and
effective date under the [calibration approval workflow](../../model_governance.md#calibration-cadence-and-approval-process).

- [ ] **Credit Risk** confirms prime mortgage scope, outcome definitions,
  ECL horizon, PD/LGD/EAD assumptions, LVR banding, censoring and holdout
  results. Investigate bias by vintage and extreme bands.
- [ ] **Treasury/ALM and Deposits** reconcile the market and funding
  opportunity, gross versus incremental deposits, conversion and runoff,
  funding limits, term instruments, premiums, and stress assumptions.
- [ ] **Capital/Finance** reconcile capital allocation, LVR risk weights,
  maturity treatment and the hurdle. Review the economic ROE bridge and
  treatment of tax, opex and regulatory liquidity.
- [ ] **Pricing** validates comparable terms/rates, realistic volumes
  and commercial reasonableness over the objective surface. Review the
  best in-band alternative and document material policy or outcome concerns.
- [ ] **Model Risk** independently reproduces the source and results,
  checks data quality, leakage, coverage and stress bounds, compares the
  preceding baseline, and confirms versioning, rollback and monitoring.
  Approve thresholds before scoring and record rejection or approval with
  the evidence package.

## Proposed Phase 4b–4f roadmap

All file targets are proposed. **Engineering-ready** work can use
synthetic fixtures to establish contracts and test arithmetic.
**Evidence-dependent** work estimates or validates new assumptions.
**Approval-dependent** work enables revised economics or changes decision
policy. The Phase 4a evidence register and sensitivities establish the
review baseline for each phase. The legacy single-period, segmented
and booked-cohort reconcile-and-report invariants continue to apply.

### 4b — ECL calibration and PD/LGD model (FIN-001)

- **Dependencies and files.** Phase 4a baseline, governed ECL evidence
  and Credit Risk definitions. Start at `config.py`, `synthetic/customers.py`,
  `pricing/profit.py`, `scenarios/runner.py`, `scenarios/multiperiod.py`,
  and `scenarios/cohort_accounting.py`. Preserve shared consumption of
  per-loan loss in `optimization/grid_search.py` and `optimization/segmented.py`.
- **Engineering-ready tests.** Synthetic PD/LGD/EAD identities, zero exposure,
  acceptance-weighted loss, exact band boundaries, unknown LVR, finite
  inputs, legacy parity, one-tier parity and cohort preservation. Extend
  `test_profit_and_balance_sheet.py` and the Phase 3c financial invariants.
- **Evidence-dependent.** Gather 12-month default and EAD observations
  and resolved or censored recoveries by LVR, vintage and mortgage segment.
  Establish out-of-time calibration error and uncertainty by band.
- **Approval and acceptance.** Credit Risk validates the loss definition
  and population. Model Risk independently validates the loss estimates.
  Require one signed, versioned profile that reconciles to the held-out
  loss ledger within pre-agreed tolerances and explains rate/mix changes
  and policy-band exceptions before activation.
- **Risk if wrong.** Underpricing loss, overstating return, and adverse
  risk selection, particularly in sparse or stressed LVR bands.

### 4c — Deposit market size and supply constraints (FIN-001)

- **Dependencies and files.** Phase 4a flow definitions and a stable
  comparison basis with 4b. Work across `config.py`, `synthetic/customers.py`,
  `synthetic/response.py`, `pricing/balance_sheet.py`, the optimisers and
  the single-period and multi-period scenario paths.
- **Engineering-ready tests.** Non-negative bounded supply, deposit
  flow conservation, saturation, no double counting of maturities or
  migrations, same opportunity when generator sample count changes and
  portfolio-level funding aggregation. Extend
  `test_invariants_funding_objective.py` and `test_invariants_multiperiod.py`.
- **Evidence-dependent.** Reconcile sector term stocks to eligible
  maturities and inflows. Estimate reachable share, participation,
  conversion and runoff from independently varying offers and observed
  volume. Validate held-out gross and net flows by term and regime.
- **Approval and acceptance.** Treasury/ALM and Deposits sign the market
  definition, conversion range and concentration/funding policy. Pricing
  and Model Risk confirm validation. Require no breaches of approved caps
  or reconciliation identities, stated uncertainty for each key driver,
  and a documented commercial impact and back-test.
- **Risk if wrong.** Apparent funding relief from balances the bank
  cannot raise or already holds. Margin dilution and maturity mismatch.

### 4d — Term-matched FTP and economic ROE (FIN-003)

- **Dependencies and files.** Phase 4a funding waterfall and 4c funding
  classifications, plus coordinated capital and ROE definitions with 4e.
  Work across `config.py`, `synthetic/market.py`, `pricing/profit.py`,
  `pricing/balance_sheet.py`, `scenarios/cohort_accounting.py`, the
  optimisers and diagnostics. Review `governance/gates.py` if a later
  approval covers a decision-metric change.
- **Engineering-ready tests.** Curve as-of validity and missing tenors,
  exact and interpolated term quotes, balanced funding legs, transfer
  elimination and ROE bridge. Cover zero capital, stale quotes and
  existing ROE selection semantics in Phase 3c invariants and
  `test_governance_gates.py`.
- **Evidence-dependent.** Obtain dated market curves, marginal issue
  spreads, hedge basis and product cash flows or behavioural lives.
  Reconcile representative FTP quotes and funding costs by bucket.
- **Approval and acceptance.** Treasury/ALM signs term and liquidity basis.
  Finance and Model Risk validate the ROE definition and P&L bridge.
  Demonstrate one reconciled portfolio waterfall and a dated benchmark
  back-test. Any change to the ROE gate needs a separate recorded
  metric and threshold decision.
- **Risk if wrong.** Negative carry, understated funding risk, double
  counting liquidity and false ROE-floor passes.

### 4e — Configurable capital and LVR-banded RWA (FIN-004)

- **Dependencies and files.** Reuse the band and exposure data contracts
  from 4b and align ROE with 4d. Work across `config.py`,
  `pricing/profit.py`, both optimisers, `scenarios/runner.py`,
  `scenarios/multiperiod.py`, `scenarios/cohort_accounting.py` and
  diagnostics.
- **Engineering-ready tests.** Flat-option legacy parity, hand-calculated
  portfolio RWA, exact LVR edges, missing LVR, explicit maturity basis,
  zero capital, one-tier equality and cohort preservation. Extend
  `test_profit_and_balance_sheet.py` and the optimisation and
  multi-period invariants.
- **Evidence-dependent.** Obtain applicable dated risk-weight and capital
  policies, exposure/LVR distributions, aggregate RWA and a documented
  target ratio/hurdle. Reconcile by product and band.
- **Approval and acceptance.** Capital/Finance approves the regulatory or
  economic basis, maturity treatment and hurdle. Model Risk reproduces
  RWA, denominator and capital charges within agreed tolerances across
  all pricing paths. Switch basis only through a versioned change.
- **Risk if wrong.** Misallocated capital, misleading product returns
  and conflation of model parameters with a regulatory requirement.

### 4f — Objective waterfall, financial documentation and integrated validation (UX-005, FIN-005, MOD-008)

- **Dependencies and files.** Phase 4a supports a synthetic waterfall
  immediately. Integrated sign-off depends on approved 4b–4e definitions
  and the response estimation design. Work in `dashboard/views/scenarios.py`,
  `dashboard/services.py`, `dashboard/models.py`,
  `docs/modelling_assumptions.md`, `docs/model_governance.md` and
  `docs/economic_invariant_testing.md`.
- **Engineering-ready tests.** Render every component with sign and unit.
  AppTest verifies a joint-book waterfall in both product views, modelled
  labels, policy and boundary disclosure, in-band comparison where
  available, lineage and stale-result behaviour. Extend
  `test_dashboard_pages_scenarios.py` and
  `test_dashboard_services_scenarios.py`.
- **Evidence-dependent.** Check each source and period, waterfall
  reconciliation and holdout performance. Document the current marginal
  funding mechanism and own-rate versus competitor-gap identification.
  Re-run the canonical scenario and stress grids across the validated
  profiles and reconcile each component.
- **Approval and acceptance.** Pricing walk-through and independent Model
  Risk review confirm the display is traceable to the same run and
  conveys uncertainty and evidence labels. Require a defensible in-band
  model result or a documented policy exception, supported by evidence
  independent of the selected interest rates.
- **Risk if wrong.** A numerically correct waterfall can still give
  unwarranted confidence in a selected rate or obscure a material policy
  exception.
