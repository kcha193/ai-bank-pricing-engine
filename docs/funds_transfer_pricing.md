# Term-matched funds transfer pricing and economic ROE (FIN-003, Phase 4d)

**SYNTHETIC / UNCALIBRATED. Not approved for pricing.**

Phase 4d adds an opt-in simulation that prices external funding from a dated,
term-matched curve. It attributes the result to loan, deposit and Treasury books
and reports a pre-steering economic ROE beside the unchanged model ROE. The
governed decision path, `configs/base.yaml` and the canonical funding economics
are unchanged. No Treasury/ALM or Finance approval exists for any curve,
premium, tenor or ROE definition here.

- Curve, profile and ledger: [`pricing/funds_transfer_pricing.py`](../src/balance_pricing_engine/pricing/funds_transfer_pricing.py)
- ROE bridge: [`pricing/economic_roe.py`](../src/balance_pricing_engine/pricing/economic_roe.py)
- Simulation entry points: [`scenarios/ftp_simulation.py`](../src/balance_pricing_engine/scenarios/ftp_simulation.py)
- Comparison: [`examples/compare_ftp.py`](../examples/compare_ftp.py) and
  [evidence](audits/phase_4d_evidence/comparison.json)
- Verification: [Phase 4d report](audits/2026-11-22-phase-4d-ftp-economic-roe-verification.md)

## FIN-003 root cause

The canonical engine prices every residual funding deficit at one scalar rate,
`wholesale_funding_rate = cash_rate + wholesale_funding_spread`. Surplus
deposits earn `cash_rate`. The scalar has no tenor, no as-of date, no source and
no liquidity basis. A 12-month fixed mortgage and a 60-month funding need get the
same rate. The reported model ROE is
`(objective + capital charge) / required capital`. Its numerator includes the
deposit funding shortfall penalty, which is a management steering charge and not
an economic cost. A steering penalty can therefore move the ROE-floor evidence.

## Curve representation

`FTP(tenor, as_of) = reference(tenor, as_of) + liquidity premium(tenor, as_of)
+ documented adjustments(tenor, as_of)`

An `FTPCurve` records:

| Field | Rule |
|---|---|
| `curve_id`, `version`, `as_of` | Identity. `key = curve_id@version#as_of`, so different dates stay distinct. |
| `currency` | NZD only. Any other currency is rejected (F08). |
| `source`, `instrument` | Required non-empty evidence text (T11). |
| `source_classification` | `SYNTHETIC / UNCALIBRATED` only. |
| `rate_unit` | Required: `fraction`, `percent` or `basis_points`. A quote too large for its unit is ambiguous and rejected (F09). |
| `compounding`, `day_count` | `annual_simple`, `ACT/365F`. Other conventions are rejected (F10). |
| `interpolation` | `exact_only` (default) or `linear_in_tenor`. |
| `extrapolation` | `forbid` only (F11). |
| `max_age_days` | Validity window for staleness (F05). |
| `required_tenors_months` | Must be quoted exactly. A neighbour is never substituted (F04). |
| `allow_negative_rates` | Negative reference rates are rejected unless set. Liquidity premiums are never negative. |
| `points` | Strictly increasing, unique tenors (F01, F02). Finite values only (F03). |

A curve is valid at a valuation time only when `as_of <= valuation_time`
(no look-ahead, F06) and `valuation_time - as_of <= max_age_days` (F05).
The valuation time must be timezone-aware.

**Interpolation.** When the policy allows it, a non-required tenor is
interpolated linearly in months between its two neighbours. The reference,
the liquidity premium and each named adjustment are interpolated separately in
their quote units, then converted. The quote records `interpolated=True` and
its bracket. Linear interpolation of annual simple rates is a convention, not a
claim that the interpolated rate is risk-free or tradeable. Bracketing points
must carry the same adjustment names. Lookups are deterministic (F12).

## Contractual and behavioural tenors

`FTPFundingProfile` keeps the economic dimensions apart:

| Dimension | Field | Use |
|---|---|---|
| Fixed-rate repricing period | `loan_repricing_tenor_months` | Reference rate of the loan FTP (interest-rate component). |
| Remaining life, amortisation, prepayment | `loan_liquidity_buckets` | Weighted liquidity premium and adjustments of the loan FTP. Weights sum to one. |
| Deposit contractual maturity | `deposit_contractual_tenor_months` | All-in deposit FTP credit at maturity. |
| Deposit rollover | `deposit_renewal_basis = contractual_maturity_only` | No credit beyond maturity without evidence. |
| Wholesale liquidity funding | `wholesale_funding_tenor_months` | External marginal rate on the aggregate residual deficit. |
| Surplus reinvestment | `surplus_reinvestment_tenor_months`, `surplus_reinvestment_spread` | Reference rate plus an explicit spread. Surplus never earns the bank's own funding premium. |

`loan FTP = reference(repricing) + Σ weight_b × (premium_b + adjustments_b)`.
The illustrative profile prices a 12-month fixed mortgage with liquidity
buckets of 25% at 12 months, 35% at 36 months and 40% at 60 months. These
weights are invented. They show that the whole mortgage is not treated as
maturing after its fixed period. They are not a validated behavioural life.

## Funding basis and external rates

Only two curve rates reach the consolidated objective:

- `residual wholesale deficit = max(funding requirement − eligible funds, 0)`,
  priced at `reference + premium + adjustments` at the wholesale tenor;
- `surplus funds = max(eligible funds − funding requirement, 0)`, credited at
  the reinvestment rate.

The funding requirement is expected new loan principal. Eligible funds are
the gross expected deposit volume in the legacy basis
(`gross_expected_deposit_volume`). With a Phase 4c profile they are its
recognised incremental funding (`phase_4c_recognised_incremental_funding`).
Both are computed once on the aggregate portfolio, after tiers are pooled,
so nonlinear terms never apply per cohort or per tier. The shortfall penalty
keeps its canonical form and stays a separate steering charge.

**Boundary conditions.** At zero loans all eligible funds are surplus. At
zero deposits the full requirement is a deficit. When requirement equals
funds, both terms are zero. A single external rate applies to the whole
deficit and a single reinvestment rate to the whole surplus. Bucketed
external funding (several tenors in one deficit) is out of scope.

## Optimiser integration

`FTPMarketState` subclasses `MarketState` and overrides `wholesale_funding_rate`
and the new `surplus_reinvestment_rate` property. The canonical `MarketState`
returns `cash_rate` from that property, so canonical results are unchanged.
The joint grid, the segmented solver and the cohort roll read surplus income
through it. `cash_rate` keeps its meaning as the policy rate.

The optimiser maximises the consolidated objective. Internal FTP transfers
cancel, so they cannot change which offer wins. The loan repricing tenor and
the loan liquidity buckets change attribution only. The wholesale tenor,
premiums, slope and reinvestment rate change the external rates and can move
the selected offer. The comparison shows both effects.

## Internal transfer pricing

`FTPAllocationLedger` uses one sign convention: each line is the signed
contribution to its own unit.

```
Loan business    = loan interest − ECL − loan FTP × loans
Deposit business = deposit FTP × eligible funds − deposit interest − acquisition cost
Treasury         = loan FTP × loans − deposit FTP × eligible funds
                   − wholesale rate × deficit + reinvestment rate × surplus
```

Internal allocations, one per side of each transfer, sum to exactly zero:
`(−charge, +charge, +credit, −credit)`. Loan + deposit + Treasury equals the
external economic contribution
`loan interest − ECL − deposit interest − acquisition − wholesale + surplus`,
which the ledger computes independently, without any FTP rate.
`ledger_from_result` also recomputes the external wholesale and surplus lines
from volumes and the curve rates and fails if the optimiser disagrees.

Nothing is counted twice. Liquidity premium reaches the consolidated result
only through the external wholesale rate on the residual deficit. Deposit
interest appears once, in the deposit book. The capital charge and steering
penalties are not allocated. They sit below the economic contribution in the
ROE bridge. Treasury's result is the mismatch carry between the internal
rates and the external position. ALM hedging P&L is not modelled.

## Economic ROE (reporting only)

```
economic profit before capital = loan interest − ECL − deposit interest
                                 − acquisition cost − external wholesale
                                 + surplus income
model ROE numerator            = economic profit − shortfall penalty
                                 − concentration penalty
model objective                = model ROE numerator − capital charge
required capital               = loans × effective RWA weight × 10%
economic ROE                   = economic profit / required capital
```

`EconomicROEBridge` reports both ROE measures, the denominator inputs, every
numerator line and the steering difference. The basis is pre-tax and
pre-operating-expense. Neither tax nor operating expense is modelled, so
neither is excluded silently. Acquisition, credit loss and external funding
are genuine economic costs and stay in. Zero required capital gives `None`
("not available"). The model ROE keeps its legacy zero at zero capital.
`ROE_FLOOR_MET`, `min_return_on_equity` and rate selection still use the model
ROE. Changing that metric requires separate Finance, Capital and Model Risk
approval and its own change request.

## Phase 4c compatibility

- The Phase 4c supply profile must use the 12-month horizon, and its term
  must equal `deposit_contractual_tenor_months`. Otherwise the request is
  rejected.
- Eligible funds are the recognised incremental funding. The opening book
  never funds new loans. An opening-balance change cannot move the result
  (T20). Internal migrations change attribution and expense only (T21).
  Hard concentration caps still bound recognised funding and the deposit FTP
  credit (T22).
- Signed incremental deposit expense, source counterfactuals, eligibility,
  concentration and disjoint segments come from Phase 4c unchanged.
- Multi-period FTP rejects any supply profile: Phase 4c has no authorised
  stock-flow schedule.

The runner now binds the supply profile and any FTP market to diagnostics at
`_evaluate_pricing_population`, the seam every simulation uses. A private caller
that passes `deposit_supply` or an FTP market can no longer skip
`DEPOSIT_SUPPLY_SIMULATION_ONLY` or `FTP_SIMULATION_ONLY`.

## Phase 4b compatibility

A curve change does not alter PD, LGD or EAD. At a fixed offer the ECL line is
identical across curves. Reoptimisation can still select another offer and so
change aggregate ECL through the accepted mix. The credit loss profile and its
`CREDIT_LOSS_SIMULATION_ONLY` warning travel with the diagnostics.

## Multi-period roll

`run_ftp_multiperiod_simulation` reuses the Phase 3b roll with one dated curve
per period and one valuation time per period.

- Booked cohorts keep their contractual coupon, ECL rate and capital basis.
- `allocation_policy = locked_at_origination`: a cohort's loan and deposit FTP
  rates are those of its origination curve. A later curve never rebooks them.
- Each period's curve prices that period's new business and the aggregate
  external funding of the whole active book, once.
- Treasury absorbs the gap between locked internal rates and the period's
  external rates.
- Discounted period contributions reconcile to the NPV. Every period ledger
  reconciles to the Phase 3b active-portfolio objective.
- Two curves with the same `curve_id` and `version` but different content are
  rejected as ambiguous.

The roll does not model repricing events, hedges, break costs or rollover of
the original funding. A scalar-equivalent curve schedule reproduces the
Phase 3b roll exactly.

## Governance

- `EngineConfig`, `PricingScenarioRequest`, the API, collectors and dashboard
  carry no FTP fields. An `ftp` key in a governed config is ignored and never
  reaches the hash or the decision.
- Every FTP diagnostics carries `FTP_SIMULATION_ONLY` (WARN) through the
  standard gate set, so every result is at least `REVIEW_REQUIRED`. Existing
  BLOCK gates still yield `BLOCKED`.
- The canonical bytes of the full request (engine, curve, tenors,
  interpolation, valuation time, funding profile, supply and loss profiles,
  controls) determine the config hash. The result stores those bytes, so later
  caller mutation cannot change recorded evidence.

## Illustrative inputs

`illustrative_curve` uses a pre-declared construction. The reference is a
2.50% policy-rate proxy plus a term slope (3m +0.10%, 6m +0.15%, 12m +0.25%,
24m +0.40%, 36m +0.55%, 60m +0.80%). The premium rises with tenor (0.60%,
0.75%, 0.95%, 1.20%, 1.40%, 1.70%). The 12-month all-in rate equals the legacy
scalar (3.70%), so the term structure, not the level, drives differences.
Shocks scale the slope or the premium. No quote is an observed NZ market rate.
`scalar_equivalent_curve` reproduces the legacy funding rates exactly.

## Evidence dependencies

**Treasury/ALM:** curve instruments and as-of data, marginal versus average
funding basis, tenor coverage and interpolation, behavioural lives and
prepayment, liquidity premiums and buffer costs, hedging basis, transfer
elimination. **Finance:** profit bridge, tax and opex basis, economic ROE
definition. **Capital:** denominator compatibility with Phase 4e.
**Model Risk:** independent reproduction and validation. **Pricing:**
commercial and customer rate implications.

## Reproduction

```bash
python -m pytest -q tests/unit/test_ftp_*.py
python -m examples.compare_ftp --output /tmp/phase-4d-comparison.json
cmp docs/audits/phase_4d_evidence/comparison.json /tmp/phase-4d-comparison.json
```
