# Phase 4d verification — term-matched FTP and economic ROE

**FIN-003 (P2) · SYNTHETIC / UNCALIBRATED · NOT APPROVED FOR PRICING**

Phase 4d starts from `373b21d`, the merge of
[Phase 4c, PR #83](https://github.com/kcha193/ai-bank-pricing-engine/pull/83).
The [Phase 4a methodology](phase_4a_evidence/methodology.md#term-matched-ftp-and-roe-fin-003-proposed-phase-4d)
sets the proposed curve, attribution and ROE bridge. The
[model document](../funds_transfer_pricing.md) holds the specification.

| Status level | Phase 4d |
|---|---|
| ENGINEERING VERIFIED | **Yes.** Contracts, validation, attribution and governance are tested. |
| SYNTHETIC NUMERICALLY VERIFIED | **Yes.** Reconciliations hold on illustrative inputs. |
| EMPIRICALLY CALIBRATED | **No.** No dated market curve, issuance spread or behavioural life is available. |
| APPROVED FOR PRICING | **No.** No Treasury/ALM, Finance, Capital, Model Risk or Pricing approval exists. |

## FIN-003 root cause

The canonical funding rate is the scalar `cash_rate + wholesale_funding_spread`
with no tenor, date, source or liquidity basis. Surplus earns `cash_rate`.
The model ROE numerator contains the deposit funding shortfall penalty, a
management steering charge. At the canonical offer that penalty is
NZ$46,643, and model ROE is 45.17% against a pre-steering 45.43%. In the
Phase 4c supply case at the same offer, the penalty is NZ$2.290m, and the two
measures are 26.43% and 39.26%.

## Model architecture

- `pricing/funds_transfer_pricing.py`: `FTPCurve`, `FTPCurvePoint`,
  `FTPAdjustment`, `FTPQuote`, `FTPLiquidityBucket`, `FTPFundingProfile`,
  `FTPRateSet`, `FTPMarketState`, `FTPAllocationLedger`, illustrative and
  scalar-equivalent curves.
- `pricing/economic_roe.py`: `EconomicROEBridge`, reporting only.
- `scenarios/ftp_simulation.py`: `FTPSimulationConfig` / `run_ftp_simulation`
  and `FTPMultiPeriodSimulationConfig` / `run_ftp_multiperiod_simulation`.
- Canonical seams, all additive: `MarketState.surplus_reinvestment_rate`
  (returns `cash_rate`), read by the joint grid, the segmented solver and the
  cohort roll; `ScenarioDiagnostics.ftp_market`; `FTP_SIMULATION_ONLY` gate;
  `summary.json` `ftp_simulation` key; `_roll_portfolio` period hooks;
  `DepositSupply.profile`.

## Curve specification and conventions

NZD only, timezone-aware `as_of`, explicit `rate_unit` with a plausibility
bound, `annual_simple` compounding with ACT/365F, `exact_only` or
`linear_in_tenor` interpolation, `forbid` extrapolation, `max_age_days`
validity, exact `required_tenors_months`, explicit negative-rate policy,
non-negative liquidity premiums and named adjustments. The illustrative curve
quotes 3, 6, 12, 24, 36 and 60 months. Its 12-month all-in rate equals the
legacy 3.70%. The construction was declared before any run and is not fitted
to a mortgage outcome.

| Tenor | Reference | Liquidity premium | All-in |
|---:|---:|---:|---:|
| 3m | 2.60% | 0.60% | 3.20% |
| 6m | 2.65% | 0.75% | 3.40% |
| 12m | 2.75% | 0.95% | 3.70% |
| 24m | 2.90% | 1.20% | 4.10% |
| 36m | 3.05% | 1.40% | 4.45% |
| 60m | 3.30% | 1.70% | 5.00% |

## Tenor matching and transfer allocation policy

The illustrative profile prices a 12-month fixed mortgage. The repricing
reference is the 12-month point (2.75%). The liquidity component weights 25%
at 12 months, 35% at 36 months and 40% at 60 months (1.4075%). The loan FTP is
4.1575%. The 12-month term deposit is credited 3.70% at contractual maturity,
with no rollover credit. The aggregate deficit pays the 12-month all-in rate
(3.70%), and surplus earns the 3-month reference (2.60%).

## Worked example: canonical offer, term-matched 12-month curve

Fixed offer 6.19% / 4.05%. Amounts in NZ$.

| Line | Loan book | Deposit book | Treasury | Consolidated |
|---|---:|---:|---:|---:|
| Customer loan interest | 31,568,486 | | | 31,568,486 |
| Expected credit loss | −3,438,164 | | | −3,438,164 |
| Customer deposit interest | | −13,320,584 | | −13,320,584 |
| Loan FTP (509.99m × 4.1575%) | −21,202,905 | | +21,202,905 | 0 |
| Deposit FTP (328.90m × 3.70%) | | +12,169,423 | −12,169,423 | 0 |
| External wholesale (181.09m × 3.70%) | | | −6,700,270 | −6,700,270 |
| Surplus income | | | 0 | 0 |
| **Contribution** | **6,927,417** | **−1,151,162** | **2,333,212** | **8,109,468** |

Internal transfers net to exactly 0.0. The consolidated total equals the
independent external reference within NZ$1e-8.

**Economic ROE bridge.** Economic profit before capital NZ$8,109,468;
shortfall steering penalty −NZ$46,643; model ROE numerator NZ$8,062,824;
capital charge −NZ$2,141,965; model objective NZ$5,920,859. Required capital
is 509.99m × 0.35 × 10% = NZ$17,849,709. Model ROE 45.17%; economic ROE
45.43%. Tax and operating expense are not modelled.

## Fixed-offer comparison

Every row prices 6.19% / 4.05%, except `surplus_deposits` (7.50% / 4.50%).
NZ$m. Steering is the shortfall penalty. Book columns are FTP attribution.

| Scenario | Wholesale rate | Wholesale | Steering | Loan book | Deposit book | Treasury | Objective | Model ROE | Economic ROE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Scalar equivalent | 3.700% | 6.700 | 0.047 | 9.261 | −1.151 | 0.000 | 5.921 | 45.17% | 45.43% |
| Term-matched 12m | 3.700% | 6.700 | 0.047 | 6.927 | −1.151 | 2.333 | 5.921 | 45.17% | 45.43% |
| Liquidity premium ×1.5 | 4.175% | 7.560 | 0.047 | 3.338 | 0.411 | 3.500 | 5.061 | 40.35% | 40.61% |
| Liquidity premium ×0.5 | 3.225% | 5.840 | 0.047 | 10.516 | −2.713 | 1.167 | 6.781 | 49.99% | 50.25% |
| Steeper curve | 3.950% | 7.153 | 0.047 | 5.652 | −0.329 | 2.333 | 5.468 | 42.63% | 42.90% |
| Inverted curve | 3.200% | 5.795 | 0.047 | 9.477 | −2.796 | 2.333 | 6.826 | 50.24% | 50.50% |
| Repricing tenor 24m | 3.700% | 6.700 | 0.047 | 6.162 | −1.151 | 3.098 | 5.921 | 45.17% | 45.43% |
| Loan liquidity 60m | 3.700% | 6.700 | 0.047 | 5.436 | −1.151 | 3.825 | 5.921 | 45.17% | 45.43% |
| Wholesale tenor 36m | 4.450% | 8.058 | 0.047 | 6.927 | −1.151 | 0.975 | 4.563 | 37.56% | 37.82% |
| Funding shortfall (85% target) | 3.700% | 6.700 | 1.883 | 6.927 | −1.151 | 2.333 | 4.085 | 34.88% | 45.43% |
| Surplus deposits | 3.700% | 0.000 | 0.000 | 0.348 | −3.631 | −4.792 | −8.130 | −1788.08% | −1788.08% |
| Phase 4c supply | 3.700% | 11.311 | 2.290 | 6.927 | −2.253 | 2.333 | 2.576 | 26.43% | 39.26% |
| Phase 4b credit loss | 3.700% | 6.700 | 0.047 | 8.574 | −1.151 | 2.333 | 7.567 | 54.40% | 54.66% |
| 4b + 4c + FTP | 3.700% | 11.311 | 2.290 | 8.574 | −2.253 | 2.333 | 4.223 | 35.66% | 48.48% |

At a fixed offer, a curve shock moves only wholesale cost, surplus income and
attribution. Interest, ECL, volumes, capital and the shortfall penalty stay
the same (T39). The repricing-tenor and loan-liquidity rows change only the
loan and Treasury books. The surplus row books 7.50% loans at a tiny volume,
so its ROE is extreme; it exists to exercise the surplus credit.

## Reoptimised comparison

Each row reruns the canonical adaptive search on its own request.

| Scenario | Loan / deposit | Wholesale | Steering | Objective | Model ROE | Economic ROE |
|---|---:|---:|---:|---:|---:|---:|
| Scalar equivalent | 6.19% / 4.05% | 6.700 | 0.047 | 5.921 | 45.17% | 45.43% |
| Term-matched 12m | 6.19% / 4.05% | 6.700 | 0.047 | 5.921 | 45.17% | 45.43% |
| Liquidity premium ×1.5 | 6.24% / 3.95% | 6.587 | 0.000 | 5.072 | 44.02% | 44.02% |
| Liquidity premium ×0.5 | 6.04% / 4.05% | 12.417 | 2.433 | 7.062 | 40.26% | 50.00% |
| Steeper curve | 6.19% / 4.05% | 7.153 | 0.047 | 5.468 | 42.63% | 42.90% |
| Inverted curve | 5.99% / 4.05% | 14.832 | 3.351 | 7.172 | 37.86% | 49.94% |
| Repricing tenor 24m | 6.19% / 4.05% | 6.700 | 0.047 | 5.921 | 45.17% | 45.43% |
| Loan liquidity 60m | 6.19% / 4.05% | 6.700 | 0.047 | 5.921 | 45.17% | 45.43% |
| Wholesale tenor 36m | 6.24% / 3.95% | 7.020 | 0.000 | 4.638 | 41.28% | 41.28% |
| Funding shortfall (85% target) | 6.34% / 3.95% | 2.161 | 0.097 | 4.657 | 49.66% | 50.45% |
| Surplus deposits | 6.19% / 4.05% | 6.700 | 0.047 | 5.921 | 45.17% | 45.43% |
| Phase 4c supply | 6.34% / 3.85% | 7.089 | 1.223 | 2.903 | 35.47% | 45.36% |
| Phase 4b credit loss | 6.09% / 4.20% | 9.759 | 0.712 | 7.740 | 46.52% | 49.70% |
| 4b + 4c + FTP | 6.14% / 3.85% | 15.203 | 3.789 | 4.386 | 33.88% | 52.79% |

Every run breaches its original policy band, reports no boundary warning and
is `REVIEW_REQUIRED`. Cheaper external funding (lower premium, inverted curve)
lowers the selected mortgage and accepts a larger steering penalty: the
economic ROE stays near 50% while the model ROE falls below 41%. That gap is
the FIN-003 effect. Internal-only tenor changes never move the selected
offer. The Phase 4c supply row reproduces the published Phase 4c fixed-offer
and reoptimised results, because the 12-month wholesale rate equals the legacy
rate. The machine-readable [comparison](phase_4d_evidence/comparison.json)
records the full A–Q decomposition, ledger, ROE bridge, rates, quotes, gates
and provenance for all 28 runs.

## Multi-period limitations

The FTP roll reuses Phase 3b with one curve and valuation time per period.
Cohorts keep their booked coupon and their origination FTP rates. Each
period's curve prices that period's new business and the aggregate external
funding of the active book once. A scalar-equivalent schedule reproduces the
Phase 3b roll exactly. Repricing events, hedges, break costs, rollover of
original funding and deposit supply are not modelled. Deposit supply is
rejected because Phase 4c has no authorised stock-flow schedule.

## Phase 4c compatibility and deferred findings

The FTP simulation consumes recognised incremental funding only. An opening
balance cannot move the result, internal migrations add no funding and hard
caps still bound the deposit FTP credit. The supply horizon and term must
match the FTP deposit tenor.

| Deferred PR #83 finding | Assessment | Action |
|---|---|---|
| A. Supply gate keyed on a field set after a private call | Confirmed on private paths only. No public entry point accepts a supply profile without the gate. Severity: minor. | Fixed here at the seam: `_evaluate_pricing_population` binds the supply profile and FTP market itself. Regression `test_t48_internal_seam_cannot_skip_the_simulation_gates`. |
| B. `portfolio()` caps absorb any overrun | Tiers are disjoint by construction, and `estimate()` rejects overlap, so overruns are rounding in practice. Minor. | Deferred; separate issue. |
| C. Tier interest excludes reference interest | Disclosure inconsistency; objective unaffected. Minor. | Deferred; separate issue. |
| D. STOCK profile accepts external new money | Semantic validation gap; no FTP path depends on it. Minor. | Deferred; separate issue. |
| E. Segmented heuristic start values gross bookings at the wholesale rate | Affects only coordinate-descent starts. Exact search (used here) is unaffected; best-known results are disclosed. Minor. | Deferred; separate issue. |

No material bypass was found, so no prerequisite PR was needed.

## Phase 4b compatibility

At a fixed offer, the ECL line is identical across curves. The loss profile
changes only ECL and the objective by the same amount. Reoptimisation can still
change aggregate ECL through mix. `CREDIT_LOSS_SIMULATION_ONLY` and the profile
version travel with the diagnostics.

## Governance status

`FTP_SIMULATION_ONLY` (WARN) is bound wherever an FTP market priced the search.
Every run is at least `REVIEW_REQUIRED`; an injected non-finite objective
gives `BLOCKED`. `EngineConfig`, `PricingScenarioRequest`, the API,
collectors and dashboard carry no FTP field. `ROE_FLOOR_MET` still uses the
model ROE.

## Reproduction and verification

```bash
python -m examples.compare_ftp --output /tmp/phase-4d-comparison.json
python -m scripts.run_calibration_baseline --output /tmp/phase-4a-calibration-report.json
cmp docs/audits/phase_4a_evidence/calibration-report.json /tmp/phase-4a-calibration-report.json
python -m scripts.run_credit_loss_comparison --output /tmp/phase-4b-comparison.json
cmp docs/audits/phase_4b_evidence/comparison.json /tmp/phase-4b-comparison.json
python -m examples.compare_deposit_supply --output /tmp/phase-4c-comparison.json
cmp docs/audits/phase_4c_evidence/comparison.json /tmp/phase-4c-comparison.json
python -m pytest -q tests/unit/test_ftp_*.py
python -m ruff check .
python -m pytest --cov=balance_pricing_engine --cov-report=term-missing \
  --cov-fail-under=95 -m "not live_rates" -q
```

Offline verification on 22 November 2026, Python 3.12.13:

- The Phase 4a, 4b and 4c evidence regenerated byte for byte.
  `configs/base.yaml` is unchanged.
- The canonical result is unchanged: 6.19% / 4.05%, objective NZ$5,920,859,
  model ROE 45.17%, `REVIEW_REQUIRED`, `config_hash` `cf725ba5…`.
- Full suite: 3,935 passed, 2 skipped (`-m "not live_rates"`), 97.29% coverage.
  The new modules `funds_transfer_pricing.py`, `economic_roe.py` and
  `ftp_simulation.py` are at 100%. Ruff check is clean.
- FTP tests: 89 passed (`test_ftp_curve`, `test_ftp_attribution`,
  `test_ftp_simulation`, `test_ftp_multiperiod`, `test_ftp_comparison`).
  Neighbouring supply, segmented, multi-period, invariant, gate and scenario
  suites: 299 passed together.
- The Phase 4d comparison regenerated byte for byte in this environment.
  The FTP comparison test allows platform rounding in calculated values
  and compares selected rates, identities and governance exactly.

## Missing calibration evidence and SME review

- **Treasury/ALM:** dated NZD curve instruments and as-of data, marginal
  issuance spreads by tenor, liquidity buffer costs, hedging basis, mortgage
  amortisation, prepayment and refinancing profiles, deposit rollover
  evidence, interpolation policy and transfer elimination.
- **Finance:** profit bridge, tax and opex basis, economic ROE definition.
- **Capital:** denominator compatibility with the Phase 4e capital basis.
- **Model Risk:** independent reproduction, curve back-test and validation.
- **Pricing:** commercial and customer implications of any term-matched rate.

The [evidence matrix](phase_4d_evidence/evidence-matrix.csv) records each
driver, its illustrative treatment, required data and review owner.
Empirical calibration and approval for pricing are outstanding.
