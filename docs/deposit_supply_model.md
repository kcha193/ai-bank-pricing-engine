# Deposit supply simulation (FIN-001, Phase 4c)

**SYNTHETIC / UNCALIBRATED · SIMULATION ONLY**

The canonical engine treats the total balance of its synthetic prospects as
deposit opportunity. Increasing the sample from 5,000 to 25,000 changes the
market the engine assumes and moves its selected mortgage rate from 6.19% to
5.79%. Phase 4c adds an opt-in way to hold that opportunity fixed while
varying the precision of the numerical sample. It also distinguishes a
deposit booking from the funding the booking contributes. The governed
pricing path continues to use the canonical economics and inputs.
[Phase 4a baseline](audits/2026-11-21-phase-4a-calibration-baseline.md#deposit-supply-fin-001)

## Product, stock, flow and horizon

The pricing simulation evaluates comparable 12-month NZD retail term deposits
over one 12-month horizon. The profile records its ID, version, currency,
horizon, source, evidence status and review notes. Every segment has an
explicit flow classification and a stated opportunity amount. Channel,
balance tier and customer-segment rules allow separate estimates where the
synthetic features support those distinctions. The illustrative digital
stratum uses engagement of at least 0.5. The established relationship
stratum uses at least five relationship years. These thresholds group
synthetic features for numerical sampling. Balance segments use inclusive
lower and exclusive upper bounds, matching the optimiser tier boundaries.
Valid segments represent
disjoint opportunities. Where several flow classes draw on the same
synthetic characteristics, the sample represents distinct stated flow pools
and the profile must establish that those pools are disjoint.

A **flow** amount describes principal available during the horizon. A
**stock** amount describes principal held at a point in time. A stock-based
profile must state the fraction of each stock pool that becomes available
during the horizon. For example, NZ$100m of stock with a 25% maturity
fraction yields NZ$25m of opportunity before reach and participation.
Profile amounts have no automatic annual refresh. Separately identified
external inflows can be represented by a flow alternative with an explicit
reconciliation. A stock or maturity bucket alone cannot establish net new
money.

The eligible opportunity for segment `s` is

```text
A_s = available principal during the horizon × reachable share × eligible participation
```

Reach describes which balances the offer can reach. Participation describes
which of those balances are eligible. Offered rates then determine how much
of that eligible opportunity books. Amounts and shares must be finite and
non-negative. All shares and probabilities lie in [0, 1].

## Numerical sample and conversion

The simulation normalises representative balance weights within each segment.
For raw balance `b_i` in segment `s`, the sample represents
`w_i = A_s × b_i / sum(b_j)`, where the sum uses that segment's balances.
The original balance and behavioural features remain on each prospect. If a NZ$10,000 prospect represents substantially more
opportunity through its weight, the optimiser still prices it in the tier
eligible for an individual NZ$10,000 balance. Positive opportunity requires
representative sample support, and zero opportunity contributes zero.

At the candidate deposit offer, expected gross bookings equal
`sum(w_i × q_i)`, where `q_i` is the synthetic uptake probability times
the segment's conversion multiplier, capped at one. Full uptake exhausts
the fixed eligible opportunity. Replicating identical
rows divides their weights and preserves their combined contribution, up to
floating-point precision. Independently drawn samples can change the estimated
mix, bookings and optimal offers. The comparison reports variation over
different deposit seeds with the same opportunity and loan sample.
Those results measure numerical approximation under the synthetic model.

## Booked flows and incremental funding

Each booked amount belongs to one disjoint flow class.

| Flow class | Booking | Funding attribution |
|---|---|---|
| `EXTERNAL_NEW_MONEY` | Money newly entering the modelled bank | Increases funding relative to the reference balance. Only the eligible fraction is usable for the modelled portfolio. |
| `RETAINED_MATURITY` | An existing maturity that books a new term | Only the part expected to leave without the offer can avoid runoff. The counterfactual runoff is stated explicitly and drawn from the opening funds. |
| `INTERNAL_MIGRATION` | Money transferred from another account or tier at the same bank | Preserves total bank liquidity. The source expense displaced by a booking can change the cost of that funding. |
| `EXISTING_BALANCE` | A booking attributed to balances already in the opening funding base | Preserves total bank liquidity and the existing funding base. Any stated interest cost change is measured against the source expense. |

Gross bookings reconcile across the four classes. The opening funding
position already includes existing balances. The reference subtracts
counterfactual maturity runoff from the opening funds. The reference is
disclosed bank-level evidence only. The loan side prices one horizon of
new business and has no opening asset book. The opening deposits already
fund assets outside the model, so they are never offset against new
loans. Counterfactual runoff equals the full available retained maturity
pool times its stated runoff probability. Changing offer reach or participation changes
retention under the offer while the reference runoff stays fixed. Actual
runoff is the counterfactual runoff less the portion avoided through the
offer. The avoided portion equals retained gross bookings times the stated
runoff probability. Missing runoff evidence gives zero incremental credit
and appears in the unsupported attribution. The report distinguishes principal increment,
eligible increment, usable funding for new lending and the closing
bank-level funding position.

```text
net incremental principal = external bookings + avoided maturity runoff
reference funding         = opening funding − counterfactual maturity runoff   (disclosed)
closing bank funding      = reference funding + net incremental principal       (disclosed)
usable funding            = recognised eligible increment                       (new lending)
```

Phase 4c review (2026-11-22) corrected an earlier draft that added the
reference funding to usable funding. That draft let an invented opening
book fund new loans at no cost. Raising only the unallocated opening
balance moved the selected mortgage from 6.29% to 5.89%, which repeats
the FIN-001 symptom through a different input. With the correction, a
change in the opening balance leaves the selected offer and objective
unchanged. A test checks this:
`test_opening_funding_cannot_move_the_selected_offer`. The same change
removes an asymmetry: a renewal pool's opening balance was counted at full
value, while avoided runoff from that pool took the funding-eligibility
haircut.

An offer displaces source principal on migration and existing-balance
bookings, and on the share of renewed principal that would have stayed
without the offer. The source product's interest on that displaced
principal is the counterfactual expense. The reference book includes
interest on remaining source balances after counterfactual runoff and
on any unallocated opening balance at its own stated rate.

```text
incremental interest     = gross offered interest − displaced source expense   (objective)
bank-level interest      = reference interest + incremental interest            (disclosed)
```

The objective carries the incremental interest and acquisition costs
only. The incremental interest is signed. It is negative when an offer
reprices displaced source balances below their source rate.

Booked interest remains an expense where the portfolio cannot recognise the
booked funding. Interest is annualised on the scenario balances. The model
has no within-year cash-flow timing or term-matched transfer-pricing curve.
Acquisition and retention costs are disclosed separately.
The gross-to-eligible reconciliation also shows incremental principal
disallowed by funding eligibility.

The usable funding for new lending feeds three portfolio-level terms once.
Wholesale funding cost applies to positive loans less usable deposits.
Surplus deposits earn the synthetic cash rate. The shortfall penalty
applies to positive required deposits less usable deposits, with required
deposits calculated as expected loans times the configured ratio.
At the selected loan volume, the funding-value comparison reports
wholesale cost avoided and additional surplus income against zero
incremental deposit funding. Shortfall-penalty relief is a separate steering effect.
Wholesale funding cost and the management shortfall penalty remain
separate. The report distinguishes both terms from deposit interest and
acquisition costs. A zero funding ratio removes the shortfall penalty.
Model ROE adds back the capital charge and divides by required capital.
Its numerator includes acquisition costs and both steering penalties.

## Saturation and concentration

The segment opportunity bounds gross bookings even at full conversion.
Funding recognition depends separately on eligibility and concentration.
The illustrative concentration threshold limits newly usable funding in the
defined modelled pool. It is expressed in NZD. A synthetic row weight
represents a share of an economic pool and cannot measure the concentration
of real connected depositors.

- `HARD_LIMIT` caps recognised incremental volume. Bookings and their
  expenses remain visible, together with the amount denied recognition.
- `SOFT_PENALTY` leaves recognition intact and charges a separate shadow
  cost on volume above the limit.
- `DIAGNOSTIC_ONLY` reports the threshold and excess volume without
  changing recognition or charges.

The stress comparison reports whether the cap binds and reoptimises a
matching run with the cap removed. It reports both the selected-portfolio
objective change and the objective change at the same offer. Thresholds and penalties are invented control
settings. Treasury/ALM would need connected depositor exposure, maturity,
channel and runoff evidence to set limits.

## Run and inspect a simulation

```python
from balance_pricing_engine.config import load_config
from balance_pricing_engine.scenarios.deposit_supply_simulation import (
    DepositSupplySimulationConfig,
    run_deposit_supply_simulation,
)
from examples.compare_deposit_supply import illustrative_profiles

request = DepositSupplySimulationConfig(
    engine_config=load_config("configs/base.yaml"),
    profile=illustrative_profiles()["fixed_market_base"],
)
simulation = run_deposit_supply_simulation(request)
print(simulation.governance_status.value, simulation.config_hash)
print(simulation.diagnostics.best_result)
```

The separate request binds the full profile, engine assumptions and
simulation controls into canonical JSON and a SHA-256 hash. The output
includes the selected pricing diagnostics and a detailed supply estimate.
The joint and segmented optimisers each use the same funding treatment.
The simulation grid reports gross bookings and usable funding for new
lending separately, with additional acquisition-cost and concentration-penalty
components. The existing search and gates supply boundary, policy-band
and ROE-floor evidence. The one-tier segmented result uses the same
objective as joint pricing. Larger coordinate-descent runs report
best-known results, with optimality unproven.

`fixed_rates` specifies the loan and deposit offer pair for comparisons
on the joint path. `deposit_sample_seed` redraws only the deposit population.
The optional Phase 4b `credit_loss_profile` uses the same synthetic annual
PD/LGD/EAD calculation as the separate credit-loss simulation.
The reproducible example exercises low and high market opportunity,
reachable share and conversion, higher renewals and cannibalisation, a
binding concentration threshold, a high funding-ratio stress and sample
sizes of 1,000, 5,000 and 25,000. It records both fixed and reoptimised results.

```bash
python -m examples.compare_deposit_supply --output /tmp/phase-4c-comparison.json
python -m pytest -q tests/unit/test_deposit_supply*.py
```

## Maturity and repeated periods

The Phase 4c simulation evaluates one horizon. The existing Phase 3b and
Phase 4b cohort projections continue to carry their original booked rates,
loss and capital basis on surviving balances and to aggregate funding once
per period. Phase 4c profiles are unavailable to those projections.
A stock-flow roll for these profiles would require vintage-level opening
balances, renewals, internal transfers, external inflow, actual runoff,
funding eligibility and a dated schedule of disjoint new opportunities.
Those inputs are required to recognise each retained maturity once, preserve
booked rates on surviving funding and prevent rebooking the same opportunity.

## Validation and approval

The `DEPOSIT_SUPPLY_SIMULATION_ONLY` warning requires at least
`REVIEW_REQUIRED`. Existing blocking gates take precedence. Configuration
lineage records the inputs that produced the estimate. It confers no
empirical validity or activation authority.

Public balance stocks and repricing data can support perimeter and scale
checks. They cannot identify a bank's effective reach, offered conversion,
incremental retention or source of deposits. Stock changes mix flows and
reclassifications. Repricing time differs from original maturity. At a fixed
competitor rate, own-rate and competitor-gap response are collinear.
Authorised offer and booking histories need rejected offers and denominators,
comparable competitor observations and independent rate variation.
Validation needs source-product cost, opening and closing funding, maturities
and actual runoff, with disjoint vintages held out.

Treasury/ALM validates liquidity value, eligibility, reconciliation, term
matching and concentration. Deposits reviews market definition, renewals,
cannibalisation and conversion. Pricing reviews offer response, commercial
impact and policy-band evidence. Independent Model Risk reviews the
assumptions, numerical behaviour, holdout results and uncertainty. Any
activation needs recorded approval for the exact version, mandate and
economic assumptions. All provided profiles remain illustrative.
[Evidence matrix](audits/phase_4c_evidence/evidence-matrix.csv) ·
[Phase 4c verification](audits/2026-11-22-phase-4c-deposit-supply-verification.md)
