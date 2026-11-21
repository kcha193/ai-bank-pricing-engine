# Annual mortgage credit loss (FIN-001, Phase 4b)

**SYNTHETIC / UNCALIBRATED.** Phase 4b supplies a simulation framework and
offline validation tools. All implemented profiles are illustrative. The
[Phase 4a baseline](audits/2026-11-21-phase-4a-calibration-baseline.md) and its
[ECL methodology](audits/phase_4a_evidence/methodology.md#ecl-and-lvr-bands-fin-001-proposed-phase-4b)
remain the historical evidence. The canonical engine continues to use
`0.0015 + 0.018 × clipped_risk_score^1.4`.

## Annual economic loss

For a booked mortgage, PD is the probability of first default within the next
12 months, conditional on booking. LGD is the fraction of exposure lost
conditional on default. EAD is the NZD exposure conditional on default. The
annual loss is attributed to defaults in the pricing horizon. The horizon
describes default timing, while recoveries and workout costs may occur later.
Any valuation and discount basis for those flows requires explicit agreement.
The simulation uses illustrative severity fractions with no cash-flow model.
IFRS 9 measures discounted cash shortfalls and has its own staging and
12-month and lifetime measurement rules. See the
[Phase 4a source register](audits/phase_4a_evidence/source-register.md#credit-loss-fin-001).

```text
Annual facility loss = sum_s(weight_s × PD_i,s × LGD_i,s × EAD_i,s)
Expected portfolio loss = sum_i(acceptance_probability_i × annual facility loss_i)
Expected booked principal = sum_i(acceptance_probability_i × principal_i)
Booked-principal-weighted annual loss rate = expected portfolio loss / expected booked principal
```

A single scenario has weight 1. Each weight is in [0, 1], and the sum must
equal 1 within an absolute tolerance of `1e-12`. The model checks the original
weights and uses them as supplied. It multiplies within each scenario before
aggregation, so favourable and stressed assumptions remain paired. PD and LGD
are finite and bounded by [0, 1]. EAD and principal are finite and non-negative.
The profile uses principal × scenario and band-specific EAD multiplier. The
direct arithmetic function also accepts independent EAD values.

The shared pricing function weights the annual NZD loss directly by acceptance.
Interest, wholesale funding, deposit requirements, and capital use booked
principal. The adapter needs no division at zero principal, and the illustrative
multiplier produces zero EAD at zero principal. Reporting shows an undefined
ratio as JSON `null` when the denominator is zero. The applicant mean uses only
positive-principal facilities and weights each facility equally. The opportunity
rate uses total conditional loss over total offered principal. The expected-book
rate uses the acceptance-weighted sums above.

## LVR and profile evidence

Phase 4b uses origination LVR and the right-closed intervals proposed in Phase 4a's
methodology. Phase 4a's descriptive report used lower-inclusive, upper-exclusive
intervals. An exact 60%, 80% or 90% fell in the higher band in that report and
falls in the lower band here. The Phase 4b comparison applies the new boundary
convention to both models. Historical Phase 4a counts retain their original basis.

| Origination LVR | Band |
|---|---|
| 0% to 60%, inclusive | `lvr_le_60` |
| Above 60% through 80% | `lvr_gt_60_le_80` |
| Above 80% through 90% | `lvr_gt_80_le_90` |
| Above 90%, including LVR over 100% | `lvr_gt_90` |
| Missing LVR | `unknown` |

Finite band edges must increase strictly, and the final band is open-ended.
An absent LVR vector or a missing value within it uses the explicit unknown
band. Negative and infinite LVRs are invalid. Every scenario supplies parameters
for every band, including unknown, even when the sample contains no facilities
in that band. These are analytical groups. Underwriting eligibility and
regulatory capital have their own definitions and rules. Updated or indexed
current LVR may inform future LGD and collateral stress validation. Its timing
and valuation method need to be explicit.

The built-in `illustrative-mortgage-v1` profile is dated 22 November 2026. It
assigns 80% weight to `base` and 20% to `collateral_stress`, with EAD at 97% and
100% of principal, respectively. PD and LGD increase across the example's LVR
bands. Unknown LVR uses the example's highest-band assumptions. These numbers
were invented for numerical checks and sensitivity analysis. The date records
the simulation basis. It carries no activation rule. Each profile serialises
its version, date, model and LVR bases, horizon, LGD and EAD definitions,
scenarios, review note, evidence classification and validation status. Change
the version when assumptions change. The simulation hash binds the actual
profile contents, even across separate runs that reuse a version label.

## Run the simulation

```python
from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing.credit_loss import illustrative_profile
from balance_pricing_engine.scenarios.credit_loss_simulation import (
    CreditLossSimulationConfig,
    run_credit_loss_simulation,
)

engine = load_config("configs/base.yaml")
simulation = run_credit_loss_simulation(
    CreditLossSimulationConfig(engine_config=engine, profile=illustrative_profile())
)
print(simulation.governance_status.value, simulation.config_hash)
print(simulation.diagnostics.best_result.expected_credit_loss)
```

The simulation accepts static market inputs and generates prospects with the
configured seed. There is no model-registry or competitor-ingestion path.
It uses the existing joint or segmented search, including the same ROE policy,
expansion and funding calculations. `loss_estimate` retains the row-aligned
principal, band, annual loss and scenario PD, LGD and EAD. `effective_config_json` holds immutable snapshot bytes. `effective_config`
returns a fresh copy of the validated simulation inputs. The existing canonical JSON hash scheme
covers that input envelope and its expansion limit. Legacy `EngineConfig`
serialization and pricing-decision artifacts retain their existing identities.
Simulation diagnostics add the full profile to `summary.json` when written.
The `CREDIT_LOSS_SIMULATION_ONLY` gate requires review. Existing blocking gates
take precedence if present.

For an annual roll, call `run_multiperiod_credit_loss_simulation()` with
`CreditLossMultiPeriodSimulationConfig(engine_config=engine, profiles=...)`
from `scenarios.multiperiod`. Supply one profile per configured period. A version
may repeat with identical content. Conflicting profiles under one version
invalidate the schedule. The schedule participates in the simulation hash, and
each cohort keeps its origination profile, original principal, booked rates,
capital basis and booked annual loss. The active cohort's loss is booked annual
loss × geometric loan survival. A later profile affects only that period's new
bookings. The roll evaluates funding on aggregate balances and reconciles each
new cohort to the standalone period objective.

## Future authorised outcome data

The proposed raw grain is a facility origination record, with dated default,
exposure, recovery and workout events linked by facility ID. Derive one record
per facility and prediction horizon for validation. Separate data versions and
prediction dates from event dates to prevent future information leaking into
the model inputs.

| Area | Proposed fields and decisions |
|---|---|
| Population | Pseudonymous `facility_id`, origination date, product, segment, occupancy flag, risk grade, initial balance, observation start/end and source version. Agree booked population and exclusions. |
| Collateral | Origination LVR, dated current LVR and valuation method. Agree collateral rank, shared security, mortgage insurance and missing LVR handling. |
| PD and censoring | Default date, cure date, a redefault flag or dated history, prepayment date and censoring status. Agree default/cure/redefault definitions, 12-month horizon and competing-event treatment. |
| EAD | Balance at default, payments, amortisation schedule and eligible further drawings. Agree timing and reconcile the observed measure to source balances. |
| LGD | Recovery dates and amounts, workout costs, resolution status and date. Agree which flows count and how timing and discounting affect the common valuation basis. Retain unresolved or censored workouts. |
| Scenarios and validation | Profile and prediction versions, scenario weights, vintage, development or holdout assignment, stressed predictions and source reconciliation. Pre-agree performance, uncertainty and stability criteria. |

The repository uses synthetic outcomes only. Public portfolio disclosures can
provide aggregate context. Facility-level outcomes, rights to use them and
representative holdouts require an authorised data and review workflow.

## Offline validation

Pass scored, booked facilities as `CreditLossOutcome` records to
`assess_credit_loss()`. The caller supplies horizon-aligned default labels,
cash-flow totals on a consistent valuation basis and the sample split. A truly
observed zero recovery or cost is an explicit zero, while missing evidence is
`None`. For a scenario mixture, supply the full joint scenario ECL alongside
PD, LGD and EAD predictions aligned to their own diagnostic definitions.

- **PD** reports expected and observed defaults, count and principal
  denominators, Wilson 95% intervals for the observed rate, Brier score and
  ROC AUC where both classes occur. Unknown outcomes stay out of the divisor.
- **LGD** compares predicted and observed severity weighted by observed EAD
  for complete, resolved positive-EAD defaults. Reconcile realised loss as
  observed EAD − recoveries + workout costs. Reports expose unresolved cases,
  missing cash flows, sample counts and the standard error of mean facility
  severity error when at least two facilities are available.
- **EAD** compares booked, predicted and observed balances on matched defaults,
  with aggregate and mean absolute errors. Observed EAD can be available while
  LGD remains unresolved.
- **Annual loss** compares matched predicted and realised NZD loss for observed
  non-defaults and complete resolved defaults. Complete-case bias can be
  material. Assess out-of-time samples by vintage. Paired stress estimates
  quantify the effect on the same selected facilities. `compare_population_mix()`
  reports shifts in LVR and segment shares by count and principal.

Raw counts identify sparse results. Metrics with unavailable denominators or
insufficient classes are returned as missing. The utilities apply no acceptance
thresholds and confer no validation or approval status.

## Matched comparison

The [offline evidence](audits/phase_4b_evidence/comparison.json) uses seed 42,
the same 5,000 loan and 5,000 deposit prospects, the same market and response inputs,
and the same 697 final rate pairs. Both fixed-offer calculations use the legacy
winner's 6.19% mortgage and 4.05% deposit rates. Monetary outputs below are NZD.

| Measure | Legacy | Illustrative at fixed offer | Illustrative reoptimised |
|---|---:|---:|---:|
| Mortgage / deposit offer | 6.19% / 4.05% | 6.19% / 4.05% | 6.09% / 4.20% |
| Expected loans | $509.992m | $509.992m | $640.617m |
| Annual ECL | $3.438m | $1.792m | $2.284m |
| Booked-principal-weighted annual loss | 67.42 bp | 35.13 bp | 35.65 bp |
| Objective | $5.921m | $7.567m | $7.740m |
| Model ROE | 45.17% | 54.40% | 46.52% |
| Outside policy band | Yes | Yes | Yes |

The applicant mean goes from 78.78 to 45.53 bp, and the opportunity
principal-weighted rate goes from 79.83 to 45.43 bp. The JSON records the
loss and volume in each LVR band, including empty bands. At the fixed offer,
ECL falls by NZ$1,646,631.05 and the objective increases by the same amount.
The optimised results each require review. The fixed-offer rows are economic
counterfactuals with no separate pricing decision. The illustrative inputs
demonstrate sensitivity and do not establish that the ECL reduction is justified.

```bash
python -m scripts.run_credit_loss_comparison --output /tmp/phase-4b-comparison.json
python -m pytest -q tests/unit/test_credit_loss*.py
```

## Readiness and approvals

- **ENGINEERING_READY** means the model, simulation contracts and diagnostics exist.
- **SYNTHETIC_VALIDATED** means synthetic numerical and regression checks passed.
- **EMPIRICALLY_VALIDATED** requires independently checked, comparable authorised
  outcome data and pre-agreed criteria. Phase 4b supplies no such evidence.
- **APPROVED_FOR_PRICING** additionally requires an authorised workflow with a
  verifiable approval record for exact versioned content and use. The Phase 4b
  interface cannot grant this status.

Credit Risk owns the default, exposure, loss, population and scenario definitions.
Independent Model Risk reviews performance, holdout calibration, uncertainty,
representativeness and model limitations. The authorised Pricing owner reviews
financial and policy-band impact and approves any activation through the applicable
governance workflow. A synthetic fit, policy-band improvement, or version label
carries no approval authority.
