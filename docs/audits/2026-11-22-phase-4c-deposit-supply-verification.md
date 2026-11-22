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
| Fixed opportunity | 6.34% / 3.85% | 540.000 | 208.456 | 180.156 | 161.724 | 2.903 | 35.47% |
| Low market opportunity | 6.34% / 3.70% | 324.000 | 98.809 | 75.991 | 68.056 | 2.024 | 28.37% |
| High market opportunity | 6.34% / 3.80% | 756.000 | 275.015 | 248.576 | 223.328 | 3.781 | 42.58% |
| Low reachable share | 6.34% / 3.75% | 338.400 | 111.540 | 86.932 | 77.876 | 2.081 | 28.83% |
| High reachable share | 6.34% / 3.85% | 655.200 | 254.274 | 225.974 | 202.960 | 3.384 | 39.36% |
| Low conversion | 6.34% / 3.75% | 540.000 | 128.531 | 103.923 | 93.168 | 2.276 | 30.41% |
| High conversion | 6.34% / 3.85% | 540.000 | 238.777 | 210.477 | 189.012 | 3.221 | 38.05% |
| High renewal | 6.34% / 3.80% | 540.000 | 204.196 | 173.507 | 155.507 | 2.895 | 35.41% |
| High cannibalisation | 6.34% / 3.60% | 540.000 | 142.963 | 97.777 | 87.714 | 2.208 | 29.85% |
| Hard concentration cap | 6.34% / 3.50% | 540.000 | 119.968 | 103.681 | 93.073 | 2.667 | 33.57% |
| Supply stress and 85% funding target | 6.69% / 3.70% | 324.000 | 98.809 | 75.991 | 68.056 | 1.199 | 36.85% |

Usable funding in the supply rows is recognised incremental funding only.
The NZ$300m illustrative opening book and its interest are disclosed in
the JSON (`reference_funding`, `active_funding`, `active_interest_cost`) but
never offset against new loans. Every selected mortgage is inside the
common grid and no run reports a boundary warning. The legacy model counts
every booked deposit as usable funding. The supply simulation recognises
less funding and selects higher mortgage rates. The objective difference
against legacy therefore reflects the funding basis. It is not evidence
that either model is better calibrated.

At the fixed 6.19% / 4.05% offer, the base profile has NZ$263.309m of gross
bookings, NZ$227.563m of incremental principal and NZ$204.280m of usable
funding. The objective is NZ$2.576m and model ROE is 26.43%. At the same
offer, the high-cannibalisation profile shifts NZ$140m of addressable
external opportunity to internal migration. Gross bookings stay near
NZ$263.3m and net incremental principal falls to NZ$180.087m. The
high-renewal case holds the runoff counterfactual fixed and changes
conversion. At the fixed offer, the supply stress raises the funding
shortfall from NZ$127.215m to NZ$326.877m, and the objective falls to
−NZ$0.182m. The stress profile has lower opportunity and an 85% funding
target. The [machine-readable comparison](phase_4c_evidence/comparison.json)
records every fixed and reoptimised scenario, including deposit expense,
wholesale cost, shortfall, funding value, original bands and gate outcomes.

The hard threshold is NZ$100m of newly recognised funding. It excludes
NZ$104.280m of eligible funding at the fixed legacy offer and lowers that
offer's objective by NZ$5.735m. Reoptimisation changes the offers from
6.34% / 3.85% to 6.34% / 3.50%. The new pair raises less incremental
funding and falls below the cap. Its objective is NZ$235,713 lower
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
all select 6.34% / 3.85%. The legacy sample sweep selects 6.34%, 6.19% and
5.79% mortgages as capacity grows with the number of generated prospects.
The eight-draw comparison measures the spread in numerical estimates
at matched offers. It supplies no empirical confidence interval and does
not impose identical optima on independently drawn samples.

## Funding and maturity checks

Reconcile the disjoint booked flows before testing funding. External inflows
and evidenced avoided maturity runoff can contribute incremental funding.
Internal migrations and bookings of existing funding change attribution
and may change expense. Their principal cannot create new consolidated
liquidity. Compare recognised incremental funding against the new loans once
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
simulation review gates. `test_credit_loss_profile_changes_only_the_loss_line_at_a_fixed_offer`
now covers that check.

Final offline verification on 22 November 2026 used Python 3.12.13. The
commands were run from the Phase 4c worktree.

- `ruff check .`: clean.
- `pytest -q -m "not live_rates"` with coverage: 3,846 passed, 2 skipped,
  0 failed. Total coverage is 97.19%, against a 95% minimum.
  `deposit_supply.py` has 99% coverage and `deposit_supply_simulation.py`
  has 100%.
- The 4a, 4b and 4c evidence regenerated byte for byte after the review
  fix. `configs/base.yaml` is unchanged.
- The canonical result is unchanged: 6.19% / 4.05%, objective
  NZ$5,920,859, model ROE 45.17%, `REVIEW_REQUIRED`, policy band breached,
  `config_hash` `cf725ba5…`.

## Codex-to-Claude handoff

Codex built Phase 4c up to the evidence step and ran out of tokens
before final verification. Claude Code recovered the staged work from the
`feat/phase-4c-deposit-supply` worktree. It committed that work unchanged
as its own commit, so Codex's part stays separable in history.

**Codex completed:**

- The supply and simulation modules.
- The optimiser, runner, diagnostics and gate integration.
- 53 tests.
- The comparison script and evidence.
- This document and the model documentation.

**Claude completed:**

1. Recovery and a check of all 26 acceptance categories against the
   tests.
2. Tests for the gaps: a governed configuration cannot carry a supply
   profile; unsupported horizons and fixed offers are rejected; the
   Phase 4b and 4c paths compose correctly.
3. An independent whole-branch review. It found one critical issue: the
   opening deposit book was counted as funding for new loans at no cost.
   Raising only the invented opening balance moved the selected mortgage
   from 6.29% to 5.89%.
4. The fix for that issue. Usable funding for new loans is now
   recognised incremental funding only. The opening book is disclosed,
   not offset. The fix also removed a funding-eligibility asymmetry on
   renewals.
5. Regenerated evidence and tables, the final verification above, and
   the PR.

The corrected basis lowers the published simulation objectives. In
the base fixed-opportunity case the objective falls from NZ$9.572m to
NZ$2.903m, and the selected mortgage rises from 6.14% to 6.34%. Under
the earlier draft, most of that headline came from the invented opening
book.

The review's minor findings are deferred and recorded in the PR.

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
