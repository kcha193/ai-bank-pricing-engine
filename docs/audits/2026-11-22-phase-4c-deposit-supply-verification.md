# Phase 4c verification — deposit opportunity and incremental funding

**FIN-001 (P1) · SYNTHETIC / UNCALIBRATED**

Phase 4c starts from `5d6774e`, the merge of
[Phase 4b, PR #82](https://github.com/kcha193/ai-bank-pricing-engine/pull/82).
The November audit reproduced a change from 6.19% to 5.79% in the selected
mortgage rate when deposit prospects rose from 5,000 to 25,000. In the original
model, adding prospects added deposit principal and resampled behaviour at
the same time. All booked deposits then counted as available funding. The
[full audit](2026-11-13-full-repository-audit.md#fin-001-canonical-recommendation-breaches-its-policy-band-driven-by-uncalibrated-inputs),
[remediation roadmap](2026-11-13-remediation-roadmap.md#phase-4--calibration-and-economic-representation-needs-subject-matter-validation)
and [Phase 4a methodology](phase_4a_evidence/methodology.md#deposit-supply-and-funding-availability-fin-001-proposed-phase-4c)
trace the finding and set the validation requirements.

## Comparison basis

The Phase 4a controls use a canonical result of 6.19% home loan and 4.05% term
deposit, an NZ$5.921m objective and 45.17% model ROE. That run is
`REVIEW_REQUIRED` and breaches its mortgage policy band. Phase 4c isolates
deposit supply by holding the loan sample, loss assumptions, market inputs
and search rules constant across the matched cases. Fixed offers hold both
rates at the legacy winner. Reoptimised cases report their selected offers
and the policy and governance outcomes. The required-funding-ratio stress
changes the target explicitly. [Phase 4a baseline](2026-11-21-phase-4a-calibration-baseline.md#canonical-baseline)

Fixed opportunity converts the segment's stated stock or flow to horizon
principal, then applies reach and eligible participation. Normalised
weights determine how much of the opportunity a synthetic sample
represents. Raw deposit amounts still assign balance tiers and the
existing features still drive uptake. Independently sampled prospects can
shift realised conversion and the selected rates. Compare numerical
variation separately from the fixed opportunity.

### Reoptimised results

All alternatives use the same 15,933 candidate pairs. The original bands
determine policy compliance. Monetary amounts in the table are NZ$m.
Every selected pair requires review and breaches its original policy band.
The legacy model has no explicit incremental-funding attribution.

| Scenario | Mortgage / deposit | Eligible opportunity | Gross bookings | Net incremental | Usable funding | Objective | Model ROE |
|---|---:|---:|---:|---:|---:|---:|---:|
| Legacy, 5,000 | 6.19% / 4.05% | 556.487 | 328.903 | — | 328.903 | 5.921 | 45.17% |
| Fixed opportunity | 6.14% / 3.60% | 540.000 | 142.962 | 123.554 | 366.912 | 9.572 | 59.76% |
| Low market opportunity | 6.19% / 3.70% | 324.000 | 98.809 | 75.991 | 324.056 | 8.986 | 62.34% |
| High market opportunity | 6.09% / 3.60% | 756.000 | 201.880 | 182.471 | 419.938 | 10.048 | 56.81% |
| Low reachable share | 6.19% / 3.70% | 338.400 | 103.427 | 80.609 | 328.212 | 9.041 | 62.65% |
| High reachable share | 6.14% / 3.50% | 655.200 | 146.336 | 130.049 | 372.804 | 9.823 | 61.01% |
| Low conversion | 6.19% / 3.60% | 540.000 | 101.374 | 81.965 | 329.482 | 9.196 | 63.52% |
| High conversion | 6.14% / 3.55% | 540.000 | 150.253 | 132.444 | 374.938 | 9.729 | 60.54% |
| High renewal | 6.14% / 3.60% | 540.000 | 149.894 | 127.366 | 370.153 | 9.584 | 59.82% |
| High cannibalisation | 6.19% / 3.50% | 540.000 | 119.969 | 82.050 | 329.605 | 9.153 | 63.28% |
| Hard concentration cap | 6.19% / 3.40% | 540.000 | 99.445 | 85.944 | 333.151 | 9.474 | 65.08% |
| Supply stress and 85% funding target | 6.34% / 3.45% | 324.000 | 64.305 | 49.454 | 300.290 | 7.849 | 75.47% |

At the fixed 6.19% / 4.05% offer, the base profile has NZ$263.309m of gross
bookings, NZ$227.563m of incremental principal and NZ$460.280m of usable
funding. The objective is NZ$7.426m and model ROE is 53.60%. At the same
offer, shifting NZ$140m of addressable external opportunity to internal
migration leaves gross bookings near NZ$263.3m and reduces
net incremental principal to NZ$180.087m. The high-renewal case holds the
runoff counterfactual fixed and changes conversion. Supply stress raises
the fixed-offer shortfall from zero to NZ$70.877m under the lower-opportunity
profile. The [machine-readable comparison](phase_4c_evidence/comparison.json)
records every fixed and reoptimised scenario, including deposit expense,
wholesale cost, shortfall, funding value, original bands and gate outcomes.

The hard threshold is NZ$100m of newly recognised funding. It excludes
NZ$104.280m of eligible funding at the fixed legacy offer and lowers that
offer's objective by NZ$3.858m. Reoptimisation changes the offers from
6.14% / 3.60% to 6.19% / 3.40%. The new pair raises less incremental
funding and falls below the cap. Its objective is NZ$97,914 lower
than the uncapped winner. A constraint can change the selected portfolio
even when its final winner has no excess volume.

### Sample size and numerical variation

The table reports NZ$m. Independent draws change only the deposit sample
seed. The legacy opportunity is the sum of generated balances.

| Deposit samples | Legacy opportunity | Fixed opportunity | Fixed-offer gross bookings | Mean gross, 8 draws | Gross standard deviation |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 113.270 | 540.000 | 262.899 | 263.836 | 0.769 |
| 5,000 | 556.487 | 540.000 | 263.309 | 263.718 | 0.234 |
| 25,000 | 2,782.667 | 540.000 | 263.726 | 263.662 | 0.139 |

The fixed profile has NZ$800m of addressable annual flow and NZ$540m of
eligible opportunity at every sample count. The three seeded supply runs
all select 6.14% / 3.60%. The legacy sample sweep selects 6.34%, 6.19% and
5.79% mortgages as capacity grows with the number of generated prospects.
The eight-draw comparison measures the spread in numerical estimates
at matched offers. It supplies no empirical confidence interval and does
not impose identical optima on independently drawn samples.

## Funding and maturity checks

Reconcile the disjoint booked flows before testing funding. External inflows
and evidenced avoided maturity runoff can contribute incremental funding.
Internal migrations and bookings of existing funding change attribution
and may change expense. Their principal cannot create new consolidated
liquidity. Compare active usable funding against the loan portfolio once
for wholesale cost, surplus income and the shortfall penalty. Check a
binding concentration scenario for its impact on recognised funds and
the objective, and disclose the constrained volume.

Both the single-rate and tiered grids need to reconcile on an equivalent
catch-all pair. For several tiers, check balance eligibility against the
raw principal and compare the small exact solver with independent
enumeration. Compare every selected financial component and governance
flag, including policy-band and ROE-floor evidence.

The Phase 4c opportunity has one explicit horizon. A repeated annual
projection requires a product-level opening-book reconciliation, contract
maturities, external runoff, rollover vintage and a dated supply schedule.
Phase 4c keeps that unsupported mode unavailable. The existing Phase 3b
and 4b multi-period simulations retain their own assumptions and regression
checks. In those simulations, surviving cohorts retain booked rates and
full runoff removes all surviving funding. These checks supply continuity
evidence for the existing roll. [Phase 3b verification](2026-11-21-phase-3b-multiperiod-economics-verification.md)
and [Phase 4b model](../credit_loss_model.md)

## Reproduction

Run from the repository root in an installed development environment.

```bash
python -m scripts.run_calibration_baseline --output /tmp/phase-4a-calibration-report.json
cmp docs/audits/phase_4a_evidence/calibration-report.json /tmp/phase-4a-calibration-report.json
python -m scripts.run_credit_loss_comparison --output /tmp/phase-4b-comparison.json
cmp docs/audits/phase_4b_evidence/comparison.json /tmp/phase-4b-comparison.json
python -m examples.compare_deposit_supply --output /tmp/phase-4c-comparison.json
cmp docs/audits/phase_4c_evidence/comparison.json /tmp/phase-4c-comparison.json
python -m pytest -q tests/unit/test_deposit_supply*.py
python -m ruff check .
python -m pytest -q -m "not live_rates"
python -m pytest --cov=balance_pricing_engine --cov-report=term-missing \
  --cov-fail-under=95 -m "not live_rates" -q
```

The Phase 4a, 4b and 4c reports reproduced their committed JSON byte
for byte. Against the Phase 4b merge, the canonical run reproduced
the grid, top results, sensitivity, counterfactuals, cohort attribution,
summary and effective configuration byte for byte. The combined Phase
4b/4c fixed-offer check reconciled the ECL change to the objective
change, held supply and expected loan volume fixed, and retained both
simulation review gates.

<!-- Add final offline verification counts here. -->

## Empirical evidence and approval

The [deposit supply evidence matrix](phase_4c_evidence/evidence-matrix.csv)
records the assessment on 22 November 2026, required comparisons and
outstanding validation. Public sector stocks help bound a market. Repricing
buckets measure remaining time to reprice, and account counts do not group
connected depositors. Estimating reachable flows, participation,
conversion and net funding requires matched authorised evidence. With a
fixed competitor rate, own-rate and competitor-gap effects cannot be
identified separately from offer variation. [Phase 4a deposit source register](phase_4a_evidence/source-register.md#deposit-supply-fin-001)

- **Treasury/ALM** reviews the horizon, stock-flow reconciliation, eligible
  funding basis, term matching, concentration limits and funding value.
- **Deposits** reviews reachable opportunity, maturity/renewal flows,
  source-product migrations and customer conversion.
- **Pricing** reviews own-rate and competitor-gap evidence, commercial
  outcomes and policy-band impact.
- **Independent Model Risk** reviews numerical and modelling assumptions,
  out-of-time validation, uncertainty, reconciliation and activation controls.

Empirical validation and activation approvals are outstanding. Phase 4d
can use the explicit funding classifications in engineering work. Term
matching, FTP and economic ROE still require their own evidence and
approval. [Phase 4a roadmap](phase_4a_evidence/methodology.md#4d--term-matched-ftp-and-economic-roe-fin-003)
