# Phase 4a — financial calibration baseline

**SYNTHETIC / UNCALIBRATED**

**NO PRODUCTION ECONOMIC CALIBRATION APPLIED.**

The canonical run selects **6.19% home loan and 4.05% term deposit**. The mortgage
is 70 bp above the configured ceiling. The model objective is **NZ$5.921m** and
model ROE is **45.17%**. The governed result is **REVIEW_REQUIRED** for the policy
breach and funding shortfall. These are results for generated prospects under
illustrative assumptions. They provide no evidence of observed bank portfolio
performance or an approved customer rate.

[Machine-readable baseline and sensitivities](phase_4a_evidence/calibration-report.json) ·
[Public source register](phase_4a_evidence/source-register.md) ·
[Assumption evidence matrix](phase_4a_evidence/evidence-matrix.csv) ·
[Methodology proposals, SME checklist and Phase 4b–4f roadmap](phase_4a_evidence/methodology.md)

## Audit scope and continuity

This analysis starts from `bf64684951199f64c69979b4a4062f7144375d31`, the
`master` merge of [PR #80](https://github.com/kcha193/ai-bank-pricing-engine/pull/80).
Phase 3a verified joint tier pricing, Phase 3b reconciled booked cohort economics,
and Phase 3c added economic invariant tests. Those checks establish behaviour
under synthetic inputs. This phase evaluates the assumptions that drive those
results, following FIN-001, FIN-003 and FIN-004 in the November audit.

The review traced canonical YAML through seeded customer generation, dual-elasticity
response, the adaptive joint optimiser, profit and balance-sheet terms, the
scenario runner, and application governance. It also checked segmented and
multi-period consumers for future calibration dependencies. The existing
`diagnostics.run_sensitivity_analysis` provides one-at-a-time parameter cases
and seed summaries. Phase 4a adds explicit loss and deposit opportunity
perturbations, governed per-case financial outputs, and matched fixed offers at
the script layer. It uses the existing engine and applies no changes to
configuration, pricing formulas, response functions, optimiser, or gates.

- [November 13 full audit](2026-11-13-full-repository-audit.md) and [remediation roadmap](2026-11-13-remediation-roadmap.md)
- [Phase 3a joint pricing](2026-11-21-phase-3a-segmented-joint-optimisation-verification.md), [Phase 3b cohort economics](2026-11-21-phase-3b-multiperiod-economics-verification.md) and [Phase 3c invariants](2026-11-21-phase-3c-economic-invariants-verification.md)
- [Modelling assumptions](../modelling_assumptions.md), [invariant testing](../economic_invariant_testing.md) and [model governance](../model_governance.md)

## Canonical baseline

Seed 42, 5,000 loan applicants and 5,000 deposit prospects, original synthetic response and economic
inputs, and disabled competitor ingestion. The original grid has 21 mortgage
rates and 17 deposit rates in 5 bp steps. The search expanded once from 357 to
697 pairs in the final grid and found an interior point outside the original
mortgage band. The best pair restricted to the original grid is 5.49% / 4.20%
with a **−NZ$4.508m** objective. The spread references remain static.

| Measure | Baseline |
|---|---:|
| Home loan offer / reference | 6.19% / 4.99% (+120 bp) |
| Home loan policy band | 4.49%–5.49% (−50 to +50 bp) |
| Term deposit offer / reference | 4.05% / 3.90% (+15 bp) |
| Term deposit policy band | 3.50%–4.30% (−40 to +40 bp) |
| Expected loan volume | NZ$509,991,691.60 |
| Expected deposit volume | NZ$328,903,313.81 |
| Loan interest income | NZ$31,568,485.71 |
| Expected credit loss | NZ$3,438,163.91 |
| ECL / expected loan volume | 0.6742% (67.42 bp) |
| Deposit interest expense | NZ$13,320,584.21 |
| Wholesale funding rate / cost | 3.70% / NZ$6,700,269.98 |
| Required Tier 1 capital (proxy) | NZ$17,849,709.21 |
| Capital charge | NZ$2,141,965.10 |
| Deposit target / required deposits | 65.00% / NZ$331,494,599.54 |
| Funding shortfall | NZ$2,591,285.73 |
| Funding-shortfall penalty | NZ$46,643.14 |
| Excess deposit income | NZ$0.00 |
| Total objective | **NZ$5,920,859.37** |
| Model ROE | **45.17%** |
| Initial / final boundary warning | True / false |
| Expansion rounds / capped | 1 / false |
| Policy-band breach | True, mortgage only |
| ROE-floor relaxation | False (no configured floor) |
| Governance status | **REVIEW_REQUIRED** |

Amounts are expected balances or annualised NZD flows. The objective equals
gross loan interest minus ECL, deposit expense, wholesale funding cost, capital
charge and steering penalty, plus surplus deposit income. Model ROE adds back
the capital charge and divides by required Tier 1 capital. It includes the
steering penalty and omits tax and operating expenses. Adding back the
NZ$46,643 penalty on the existing capital denominator illustrates a 45.43%
pre-steering ratio. The engine continues to report 45.17% and use its current
governance definition.

Effective configuration SHA-256 (`effective_config_canonical_json_sha256`):
`cf725ba53fd0be8f2f9afef254f890c47fba044c33513deba33853fa956e7ac3`.

Original YAML SHA-256:
`da6bfe40235799a08f66fbf458d7a5b9334fa7d3f72007be123ead9473c56fda`.

## Economic evidence

### Credit loss (FIN-001)

The model applies `0.0015 + 0.018 × clipped_risk_score^1.4` to acceptance-weighted
principal each year. The risk score blends credit score, LVR and a loan-to-income
component. It has no independently estimated PD, LGD or EAD. The prospect
mean is **78.78 bp** and the median is **77.08 bp**. The 5th to 95th percentile
range is **37.51–126.35 bp** (full range **16.76–174.15 bp**). The opportunity
balance-weighted ECL rate is **79.83 bp**, falling to **67.42 bp** after weighting
by modelled acceptance and balance.

| Indicative LVR band | Prospects | Expected balance (NZ$m) | ECL (NZ$m) | ECL / expected balance |
|---|---:|---:|---:|---:|
| Below 60% | 1,164 | 189.863 | 0.961 | 50.60 bp |
| 60% to below 80% | 2,095 | 212.303 | 1.493 | 70.30 bp |
| 80% to below 90% | 1,148 | 77.672 | 0.683 | 87.89 bp |
| 90% through 100% | 593 | 30.153 | 0.302 | 100.22 bp |

The bands describe generated exposure. The loss formula and acceptance function
both use inputs correlated with LVR, so the differences do not estimate causal
LVR effects. Public asset-quality and bank disclosure metrics have different
denominators, horizons, risk populations and measurement objectives. As a scale
comparison, the ANZ disclosure gives about **5.1 bp** of Stage 1 mortgage
allowance per dollar of Stage 1 exposure. This is an accounting stock ratio
across a disclosed book. It cannot verify the audit's proposed prime-loss
multiple or serve as a target for new originations. Credit Risk needs agreed
default, recovery, exposure and horizon definitions, along with a matched
multi-vintage evidence set and an independent holdout. The proposed pricing
loss model estimates PD, conditional LGD and exposure at default within
approved cohorts. [Public evidence ECL-01 to ECL-04](phase_4a_evidence/source-register.md#credit-loss-fin-001)

### Deposit supply (FIN-001)

Generated deposit capacity is **NZ$556.49m**, or **18.95%** of the
**NZ$2.937bn** loan opportunity. At 4.05%, the expected deposit intake is
NZ$328.90m with **59.10%** balance-weighted conversion and **59.08%** mean
prospect conversion. At the 3.90% reference it is NZ$277.64m with **49.89%**
balance-weighted conversion. The top ten generated balances account for
**1.52%** of deposit opportunity. The top ten expected amounts account for
**1.55%** at the selected rate. These concentration metrics reflect the seeded
generator and its balance cap. The achieved deposit-to-loan ratio is **64.49%**.

The generator has no addressable market ceiling, market share, incremental
funding classification, cannibalisation or marginal acquisition-cost model.
Published deposit stocks and repricing buckets can bound a defined market.
Internal exposure, offer, maturity and source-of-funds data are needed to
estimate what share is reachable and net incremental. The proposed product
of addressable market, eligible share, participation and rate-dependent
conversion requires a common period and perimeter, a flow reconciliation,
and Treasury/Deposits validation. [Public evidence DEP-01 to DEP-03](phase_4a_evidence/source-register.md#deposit-supply-fin-001)

### FTP and economic ROE (FIN-003)

The 3.70% proxy combines synthetic OCR of 2.50% and a 120 bp wholesale spread.
It prices `max(loan volume − deposit volume, 0)`. The cost of retail deposits
is their paid interest. The penalty charges a separate target shortfall at
180 bp and expresses management steering. A bank's term liquidity and
regulatory buffer costs need separate attribution before an FTP model can
credit or charge each product without double counting.

Treasury/ALM needs a dated term-matched NZD reference curve, marginal
funding spreads and effective product cash flows. A 12-month fixed mortgage
reprices at refix, while a deposit can repay at maturity or behave differently
under approved early-redemption and rollover assumptions. FTP would add a
supported liquidity premium and explicit documented adjustments to the
matching term reference. The model currently has no such curve. Finance,
Treasury/ALM and Model Risk must agree the economic ROE definition and
reconcile it before using it in decision policy. [Public evidence FTP-01 to FTP-04](phase_4a_evidence/source-register.md#ftp-and-funding-fin-003)

### Capital (FIN-004)

The canonical proxy multiplies expected loan balances by flat 35% RWA,
10% Tier 1 and a 12% hurdle. The model offers an illustrative maturity uplift of 6% per year above one year, clamped at five
years. The canonical config leaves average loan tenor unset. The uplift is
therefore inactive and the risk weight stays at 35%.

Applicability of public mortgage rules depends on institution, exposure
classification, prudential approach and effective date. Capital/Finance
must choose a regulatory proxy or economic capital basis, document the
exposure measure and hurdle, and reconcile cohort and portfolio results.
The proposal is an explicit configurable Tier 1 ratio, optional LVR-banded
RWA and an explicit capital basis for future review. [Public evidence CAP-01 to CAP-03](phase_4a_evidence/source-register.md#capital-fin-004)

## Illustrative sensitivity results

Each adaptive case changes one declared assumption unless named as an
interaction. The competitor benchmark is one factor that updates both the
behavioural competitor rate and the policy reference for one product. The
response anchor remains fixed. Deposit-size cases multiply balances on the
same 5,000 prospects, isolating opportunity size from response behaviour.
Count cases regenerate the deposit sample with seed 42 and keep the 5,000
loan draws. Conversion cases shift deposit log odds by ±0.50 at a given
offer. None of these shocks is an estimate or a forecast.

| Factor and perturbation | Mortgage / deposit | Objective (NZ$m) | Model ROE |
|---|---:|---:|---:|
| Canonical | 6.19% / 4.05% | 5.921 | 45.17% |
| ECL 0.5× / 1.5× | 6.09% / 4.20% · 6.24% / 3.95% | 7.842 / 4.303 | 46.98% / 39.16% |
| 10,000 / 25,000 deposit prospects | 5.99% / 3.85% · 5.79% / 3.50% | 8.575 / 12.581 | 42.92% / 43.23% |
| Deposit opportunity 0.5× / 2× | 6.34% / 4.20% · 5.99% / 3.85% | 3.808 / 8.576 | 42.79% / 42.92% |
| Conversion log odds −0.50 / +0.50 | 6.19% / 4.25% · 6.14% / 3.95% | 5.263 / 6.590 | 41.49% / 44.88% |
| Funding target 0% / 80% | 5.64% / 3.20% · 6.29% / 4.00% | 11.976 / 4.928 | 35.60% / 47.16% |
| Wholesale spread −50 / +100 bp | 5.99% / 4.05% · 6.24% / 3.95% | 7.172 / 4.244 | 37.86% / 38.79% |
| Illustrative 12m wholesale proxy at 4.20% | 6.24% / 3.95% | 5.033 | 43.77% |
| RWA 25% / 45% | 6.14% / 4.15% · 6.19% / 4.05% | 6.554 / 5.309 | 57.78% / 35.13% |
| Capital hurdle 8% / 16% | 6.14% / 4.15% · 6.19% / 4.05% | 6.668 / 5.207 | 41.27% / 45.17% |
| Mortgage benchmark −50 / +50 bp | 5.99% / 4.00% · 6.34% / 4.20% | 4.812 / 7.107 | 40.04% / 46.15% |
| Deposit benchmark −50 / +50 bp | 6.14% / 3.95% · 6.19% / 4.25% | 6.686 / 5.179 | 45.36% / 41.02% |

All 32 perturbation or control rows remain `REVIEW_REQUIRED` and breach their
own original policy band. Zero target illustrates the strength of the steering
assumption. It relaxes the objective and changes both selected offers, so its
higher objective cannot be treated as an improvement under the canonical
policy. Likewise, changing RWA shifts the ROE denominator. A higher ROE in
that comparison cannot establish a lower capital requirement.

The illustrative FTP curve uses 6-, 12- and 24-month reference points of
3.00%, 3.20% and 3.40%, plus a flat illustrative 100 bp liquidity premium.
Only the 12-month point is used. This enters the existing scalar wholesale
proxy. OCR, the deposit expense formula and the penalty treatment stay fixed.
It has no effect on internal transfer credits or actual term funding policy.

### Matched interactions

A four-cell design uses a control, each factor alone and both factors together.
The reported NZD interaction is objective(both) − objective(A) − objective(B)
+ objective(control). The first two designs permit each scenario to re-optimise.
The last three hold both offers at the canonical pair except for the explicit
mortgage −25 bp or deposit +15 bp factor. This isolates cash-flow effects from
reselections. Fixed-offer cases have no optimiser boundary or expansion status.
They apply the other existing gates to the exact offer pair and original policy
bands.

| Design | Mode | Interaction (NZ$m) |
|---|---|---:|
| ECL 0.5× × wholesale spread +100 bp | Adaptive | −0.336 |
| Deposit opportunity 2× × target 80% | Adaptive | −0.108 |
| Deposit opportunity 2× × mortgage 5.94% | Fixed offers | +6.053 |
| 12m proxy 4.20% × deposit offer 4.20% | Fixed offers | +0.240 |
| RWA 45% × mortgage 5.94% | Fixed offers | −0.439 |

At the canonical offers, doubling deposit capacity lowers the objective to
NZ$3.043m because surplus balances earn 2.50% and cost 4.05%. At the lower
fixed mortgage offer, modelled loan demand rises and doubled deposits reduce
wholesale reliance and steering costs. The objective there rises from
NZ$4.388m to NZ$7.563m. The difference illustrates the funding and surplus
kinks under these assumptions. It does not establish a causal effect of raising
deposits or supporting a lower customer rate.

## Reproduction and controls

With the project installed in an isolated environment, run from its root.

```bash
python -m pip install -e ".[dev,dashboard]"
python -m scripts.run_calibration_baseline --output /tmp/phase-4a-calibration-report.json
python -m ruff check .
python -m pytest -q -m "not live_rates"
python -m pytest --cov=balance_pricing_engine --cov-report=term-missing \
  --cov-fail-under=95 -m "not live_rates" -q
```

The runner needs no external data, credentials, alerts or persistent run ledger.
It reads the source YAML and writes a single JSON artifact. It uses the governed
application for the canonical run and config-only shocks, and the engine's joint
grid and gates for transformed samples and fixed pairs. JSON schema version 1
stores rates as fractions, rate deltas as bp and money as NZD. The report excludes
timestamps and decision IDs for reproducibility with the same environment.
Floating-point last digits can differ across numerical runtimes. The committed
snapshot was generated on macOS arm64 with Python 3.12.13, NumPy 1.26.4,
pandas 3.0.6 and Pydantic 2.14.3.

Each `config_hash` identifies the validated configuration only. Loss and deposit
balance multipliers and fixed offers are explicit analysis inputs outside
`EngineConfig`. A matching hash across those rows preserves the same config;
the case ID and `perturbations` identify what was evaluated. All deltas use the
canonical result. Each interaction lists its matched four cells. Public evidence
supports the measurement choices and the validation plan. It does not populate
a sensitivity input.

## Verification

Ruff passed. Both offline pytest runs passed **3,670 tests** with **0 failures**
and **2 skips**. The skips cover missing-Playwright paths on a machine where
Playwright is installed. Package coverage is **97.08%** against a 95% gate.
The 20 Phase 4a tests include independent financial reconciliation, the
committed artifact comparison, deterministic CLI output, YAML protection and
evidence status checks. A separate run on the base commit matched all selected
financial components, ROE, governance status and effective configuration hash.
Live-rate tests were excluded.

## Calibration decision

Phase 4a establishes the evidence and validation baseline. The conceptual
reviewers are Credit Risk for loss, Treasury/ALM and Deposits for funding,
Capital/Finance for capital and returns, Pricing for commercial reasonableness,
and Model Risk for independent validation. The evidence matrix records no
approvals. Phase 4b calibration is **not ready** pending authorised comparable
mortgage outcomes, agreed loss definitions and independent validation criteria.
Engineering preparation with synthetic fixtures is specified in the roadmap.
Any later calibration or change to decision policy needs its own approval.
[Methodology, SME checklist and phase acceptance criteria](phase_4a_evidence/methodology.md)
