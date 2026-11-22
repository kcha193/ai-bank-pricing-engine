# Modelling Assumptions

## Dashboard competitor assumptions

Pricing Studio MANUAL competitor rates are user-entered hypothetical assumptions, applied
directly without an offset. Market strategies derive a reference from observed comparable rates;
any user offset adjusts that reference into a hypothetical input, not an observed bank quote.
Joint scenarios use separate market strategies and offsets for home loans and term deposits.
Comparison runs retain their own assumptions. Model-best rates, expected volumes and objectives
remain synthetic model outputs. These provenance labels change no response, pricing or financial
calculation and grant no pricing approval.

### Stored scenario provenance under WAL

A Pricing Studio result is displayable only while its pre-calculation committed database
revision, request, configuration and available terms still match. SQLite trigger revisions
track committed observations, trust decisions and operational evidence even before WAL
checkpointing. Unknown or legacy provenance hides the result. Dashboard data/schema access
is read-only; native SQLite coordination files are allowed. This changes display eligibility,
not any economic assumption, optimiser selection, response function or pricing approval.

### Benchmark coverage and input age (DATA-003, Phase 2c)

A market-derived competitor assumption (median, mean, leader, second-best) is only formed from
at least `pricing_intelligence.min_banks` distinct eligible banks per product and term (default
3 of the five tracked NZ banks), counted after every trust, comparability and own-bank
exclusion rule. Below that the assumption is refused, never replaced by the configured rate;
MANUAL remains available and is labelled an assumption, not a market benchmark. Each resolved
assumption reports its eligible banks, the oldest contributing observation and its age at an
explicit evaluation time. The check is a gate: for identical valid assumptions the competitor
input, and so every elasticity, response, funding and optimiser output, is unchanged.

## API execution boundary (Phase 1e)

API requests are synthetic-data decision support and carry delivery-specific customer,
grid, adaptive-expansion and sensitivity budgets before computation. The budgets bound
worst-case work; they do not truncate a grid, lower expansion rounds, change a recommendation
or recalibrate the model. Requests that exceed a budget receive HTTP 422. Trusted local CLI
capacity remains unchanged. See [deployment.md](deployment.md#api-request-limits) for defaults
and operator overrides. Existing economics, response elasticities, governance gates and ledger
provenance remain unchanged. Engineering controls grant no real banking pricing approval and
authorise no automatic pricing action.

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
  unconstrained objective ranking. That fallback is disclosed, not silent: `roe_constraint_relaxed`
  is true on `ScenarioDiagnostics` and in `summary.json`, and the `ROE_FLOOR_MET` governance gate
  WARNs. The flag describes the final grid after adaptive boundary expansion, so a floor that the
  initial grid cannot meet but the expanded grid can is not relaxed. The segmented optimiser applies
  the same floor at portfolio level (never per tier): it is relaxed only when no evaluated tier
  combination qualifies. Under exhaustive search that means none exists; under coordinate descent
  it means none was found (see "Segmented tier pricing" below).
- The optimizer chooses one portfolio-level home loan rate and one portfolio-level term deposit rate.
- ML response models are trained on synthetic observed outcomes generated from the explicit
  response functions. By default, scenario runs use the deterministic synthetic response functions.
  If a caller passes a model registry into `run_baseline_diagnostics()`, an approved compatible
  champion model can provide response probabilities through the governed `MLResponseFnAdapter`;
  otherwise the runner falls back to the synthetic response functions. Available adapters include
  logistic regression, histogram gradient boosting, optional LightGBM, optional XGBoost, and an
  optional survival/hazard adapter. The survival adapter reports event probability to match binary
  target metrics where `1` means churn/event observed.
- Multi-period scenario rolls are available for portfolio-level diagnostics. They price new
  business each period at that period's market, carry earlier cohorts at their booked contractual
  terms with `loan_runoff_rate` / `deposit_runoff_rate` runoff, evaluate the whole active book, and
  discount period contributions with `discount_rate`. See
  [Multi-period cohort accounting](#multi-period-cohort-accounting-fin-002-phase-3b-2026-11-21).

## Segmented tier pricing (OPT-002, Phase 3a, 2026-11-21)

Segmented pricing sets one loan rate per LVR tier and one deposit rate per balance tier. Every
combination is judged on the full portfolio objective, the same formula as the joint grid search:

```text
objective = loan interest income - expected credit loss - capital charge
          - deposit interest expense - wholesale funding cost
          - funding shortfall penalty + excess deposit income
```

**Why the old optimiser was wrong.** It chose each loan tier as if every loan were wholesale funded
and no shortfall penalty applied, then chose deposit tiers one at a time against the loans already
fixed, crediting each deposit tier with a funding benefit that depended on the deposit tiers
before it. The full objective was computed only after the rates were set. The funding terms couple
every tier: a loan-tier rate moves total loan volume, which moves required deposits, the shortfall
penalty, wholesale cost and excess-deposit income, so the best loan rate depends on the deposit
rates and vice versa. On the November 13 audit grid (one catch-all tier, loans 3-9%, deposits 2-6%,
5 bp steps) the old optimiser chose 5.65% with a $552m shortfall and an objective of -$0.27m, while
the joint optimiser on the same grid chose 6.15% for +$5.90m.

**Search.** Each tier is scored once per candidate rate. Portfolio totals are summed in tier order,
and the funding and capital terms are applied by the canonical `pricing/` functions (which accept
candidate arrays elementwise with identical arithmetic). Then:

- *Exact search* enumerates every tier combination when the count is at most
  `SolverSettings.exact_search_max_combinations` (default 2,000,000; hard ceiling 10,000,000). The
  per-side combination totals are built in full; pairings are evaluated in blocks of about 250,000
  loan-by-deposit candidates, but a block always holds every deposit-side combination, so when
  those alone exceed the block size the bound is one row of the deposit side (at most the exact
  threshold). One tier per side is always exact and is not enumerated twice. The result is the
  proven optimum on the tier grids (`optimality_proven: true`).
- *Coordinate descent* is used above the threshold. Each pass revisits every loan tier, then every
  deposit tier, evaluating all of that tier's rates against the complete objective with the other
  tiers fixed (so a loan candidate sees the current total deposit volume, and the shortfall penalty
  is recomputed for every candidate). A move is accepted only if it improves the objective by more
  than `max(objective_tolerance, 1e-12 * |objective|)` (default `objective_tolerance` $1e-6), or
  if it newly meets the ROE floor. The (meets ROE floor, objective) key therefore never decreases;
  without a floor the objective never decreases, but a move that first meets the floor may lower
  it. No state can repeat, and
  the search stops when a full pass makes no move (converged) or at `max_iterations` passes
  (default 100; `iteration_limit_reached: true`, `solver_converged: false`). A fixed point is a
  local optimum only: on the benchmark fixtures descent finished 0.2-0.3% below the exact optimum,
  which is why exact search is preferred wherever it is affordable.
- *Multi-start.* Descent runs from up to `max_starts` (default 8) deterministic, de-duplicated
  starts: the previous expansion round's winner, the best uniform-price portfolio, a tier-local
  start (each loan tier at its wholesale-funded margin optimum, each deposit tier at its wholesale
  displacement optimum), the lowest, highest and middle grid rates, and the rates nearest the market
  reference rates. No randomness is used. The best final state across starts is reported.

**Uniform baseline safeguard.** When a loan rate appears in every loan-tier grid and a deposit rate
appears in every deposit-tier grid, the best such uniform portfolio is evaluated on the same
customers and parameters, reported as `uniform_baseline_objective`, used as a descent start and kept
as a final candidate even if `max_starts` truncates the start list. Without an ROE floor the
segmented objective is therefore never below it; with a floor the guarantee is on the
(meets floor, objective) key, so a qualifying segmented result may sit below a non-qualifying
uniform one. When the grids share no rate, no uniform comparator exists and
`uniform_baseline_feasible` is false; no dominance claim is made.

**Selection and ties.** Candidates meeting `min_return_on_equity` (portfolio ROE, the joint
definition) are preferred; then higher objective, lower funding shortfall, smaller summed absolute
spread to the market reference rates (undefined, so skipped, unless both reference rates are set),
then enumeration order (first tier slowest). Resolving a large exact tie on distance costs one
Python call per tied candidate; ties that large need degenerate inputs. This matches
`select_best_result_with_disclosure`, so one catch-all tier per side reproduces the joint optimiser
exactly, including ROE-floor selection.

**Boundary expansion.** Each expanded round is re-optimised on the full objective. The previous
round's tier rates are added to their tiers' candidate sets and seed descent, so an enlarged search
never returns a worse result on the (meets floor, objective) key; without a floor, never a lower
objective. This matters because an expanded grid need not contain the old rates: a 5.00-6.20%
tier grid in 0.5% steps appends 6.20% off-lattice, and its expansion (5.00, 5.50, ..., 7.00,
7.40%) drops it. Boundary warnings, `policy_band_breached` and expansion
counts keep their meaning; convergence never clears a boundary warning.

**Diagnostics.** `summary.json` gains an additive `segmented_solver` object: `solver_method`
(`EXACT` or `COORDINATE_DESCENT`), `solver_converged`, `solver_iterations`, `solver_starts`,
`candidate_evaluations` (including the uniform-baseline evaluations), `best_known_objective`,
`optimality_proven`, `search_space_size`, `uniform_baseline_feasible`,
`uniform_baseline_objective`, `iteration_limit_reached` and `cycle_detected`. Segmented
`grid_results.csv` rows remain tier decomposition rows, not alternative portfolios.

**Runtime.** Response scoring is O(sum of tier grid sizes x customers), as before. Exact search is
O(product of tier grid sizes) in vectorised blocks (about 0.03 s and an 80 MB peak at two million
combinations on Apple arm64). Each descent pass costs O(sum of tier grid sizes).

**Unchanged.** Elasticities, ECL, funding-rate and capital calibration, the single-period economic
formula, and the joint optimiser are unchanged; calibration remains Phase 4 work.

## Multi-period cohort accounting (FIN-002, Phase 3b, 2026-11-21)

`run_multiperiod_diagnostics()` is a **rolling annual scenario projection** on synthetic data, not
a calibrated production ALM forecast, a contractual discounted-cash-flow model or a globally
optimised lifetime NPV. Each period's new business is priced myopically by the standard
single-period engine; nothing optimises across periods.

**Why it changed.** Before Phase 3b the surviving book earned `cash_rate + reinvestment_spread_bps`
on its loan balances (not the booked loan rate), carried no ECL, capital, wholesale funding,
shortfall penalty or excess-deposit income, and kept only its deposit interest. In the audit case
(base config, two periods, zero runoff, zero discount) a book worth +$5.92m in period 0 was worth
-$0.57m in period 1 with no change in its contracts.

**Cohorts.** The winning single-period result in period `c` is booked as a `BookedCohort`
(`scenarios/cohort_accounting.py`): origination period, loan rate, deposit rate, original loan and
deposit balances, booked ECL, and the effective RWA weight and capital hurdle used to price it.
Booking checks that the result's interest income, deposit cost and capital charge reconcile to
`rate x balance` and `expected_capital_charge`; an untraceable result is rejected. A segmented
cohort is booked at its volume-weighted average rates, which reproduce its income exactly.

**Timing and survival.** A cohort contributes its full economics in its own period. At the start
of each later period `t` its balances are the originals times `(1 - runoff) ** (t - c)`, with
independent loan and deposit runoff. `loan_tenor_years` overrides the loan runoff with
`min(1, 1 / tenor)` (an approximation; contractual term, repricing date and amortisation are not
modelled separately). A runoff of 1 removes the cohort's balance after its own period; a cohort
whose loan and deposit balances are both zero contributes nothing.

**Period equations.** For the active cohorts `k` (every earlier survivor plus the new cohort) at
the period-`t` market:

```text
loan_interest_income  = sum_k  surviving_loan_k    x booked_loan_rate_k
expected_credit_loss  = sum_k  booked_ECL_k        x loan_survival_k
capital_charge        = sum_k  expected_capital_charge(surviving_loan_k, rwa_k, hurdle_k)
deposit_interest_cost = sum_k  surviving_deposit_k x booked_deposit_rate_k
L_t = sum_k surviving_loan_k        D_t = sum_k surviving_deposit_k
wholesale_funding_cost    = expected_wholesale_funding_cost(L_t, D_t, cash_t + spread)
excess_deposit_income     = expected_excess_deposit_income(L_t, D_t, cash_t)
funding_shortfall_penalty = calculate_funding_result(L_t, D_t, ratio, penalty).funding_penalty
active_portfolio_objective = income - ECL - capital - deposit cost - wholesale - penalty + excess
```

This is the single-period objective formula applied to the whole book: one cohort at full
survival reproduces its single-period objective (checked for every new cohort at run time).

- *Contractual rates.* A surviving fixed-rate loan keeps its booked rate; a rate move changes only
  new business and the period funding terms. The roll has no maturity, rollover or reinvestment
  event. `reinvestment_spread_bps` is retained for configuration compatibility, reported on the
  diagnostics, and **not applied**; a non-zero value warns.
- *ECL.* The booked loss rate is carried on surviving balances (constant loss rate). This is not
  lifetime or IFRS 9 ECL: no staging, PD/LGD term structure or migration.
- *Capital.* `expected_capital_charge` on each cohort's surviving loans with its booked effective
  RWA weight (`maturity_adjusted_rwa_weight` from `balance_sheet.average_loan_tenor_years`, as in
  the single-period engine) and hurdle. Capital is linear in balances, so the cohort sum equals the
  charge on the total book; nothing is counted twice.
- *Funding.* Wholesale funding, excess-deposit income and the shortfall penalty are calculated once
  on the aggregate balances at the period's market. They are not calculated per cohort and summed:
  a deposit-rich cohort funds a loan-heavy one. Surviving loans are not assumed wholesale funded,
  nor surviving deposits assumed surplus. Wholesale funding and surplus cash reprice each period.

**New business versus back book.** `new_business_objective` is the period's optimised objective
(the new cohort alone). `back_book_incremental_contribution = active_portfolio_objective -
new_business_objective` is a portfolio attribution: it includes the change in the nonlinear
funding terms that the surviving book causes, so it is not a standalone contractual profit of the
earlier cohorts. In the zero-runoff, zero-drift audit case every term is homogeneous of degree
one, so the period-1 back book equals the period-0 objective and the period-1 total is twice it.

**Market drift.** Period `t` applies `t x drift` once, from the period-0 values, to `cash_rate`,
`competitor_loan_rate` and `competitor_deposit_rate`. With `reference_rate_drift:
track_competitor` (default) `home_loan_reference_rate` and `term_deposit_reference_rate` move by
the same competitor drift, so a `pricing_strategy` spread grid keeps quoting spreads to the market
benchmark. Use `reference_rate_drift: hold` when the reference rates are an independently sourced
benchmark that should not move. Zero drift leaves the period-0 market unchanged. A drift that makes
a drifted market rate negative fails with a `ValueError` naming the period and field, and every
period's config is revalidated (including the `pricing_strategy` negative-rate check). Cash-rate
drift without competitor drift still warns because competitor gaps shift mechanically. Drift never
reprices a booked cohort.

**Discounting.** End-of-period: `discount_factor[t] = 1 / (1 + discount_rate) ** (t + 1)` and
`NPV = sum_t active_portfolio_objective[t] x discount_factor[t]`. No terminal value; balances still
outstanding after the last period are not valued. The NPV is a simplified annual risk-adjusted
contribution, without amortisation, prepayment, tax or operating cost.

**Synthetic cohorts.** Every period re-simulates the same seeded prospect population
(`random_seed`), so with unchanged market inputs each period's expected new business is identical.
Volumes are expectations over a stationary prospect pool, not distinct observed acquisitions; a
cohort's identity is its origination period, never individual customers.

**Provenance.** `MultiPeriodDiagnostics.provenance` records the projection type, response model
(`synthetic`: the roll calls the single-period engine without a model registry, so no ML champion
drives it), pricing mode, effective config hash, population policy, runoff basis, reference-rate
policy and discount convention.

**Outputs.** `period_summary()` keeps `period`, `best_loan_rate`, `best_deposit_rate`,
`total_objective` (still the period's new-business objective) and `discounted_contribution` (the
discounted full active-book contribution; its values changed because the back book was wrong), then
adds the new/surviving/total volumes, objective attribution, every component, the shortfall and
the discount factor. `cohort_summary()` lists each active cohort per period at its booked terms.

## Financial calibration evidence (Phase 4a)

The [Phase 4a baseline](audits/2026-11-21-phase-4a-calibration-baseline.md) reproduces
the canonical synthetic configuration. The loss proxy and the ratio of the generated
deposit and loan opportunity pools materially affect the rate pair. The report
quantifies those effects alongside funding and capital sensitivities. Proposed credit loss,
deposit supply, FTP and capital methods require independent evidence and SME validation.

## Annual mortgage credit loss simulation (Phase 4b, FIN-001)

The [credit loss model](credit_loss_model.md) adds explicit annual PD, LGD and EAD
for an opt-in synthetic comparison. It uses right-closed origination LVR bands and
a separate unknown band. Candidate ECL is the acceptance-weighted sum of facility
losses in NZD. Loan interest, funding and capital still use booked principal.
The annual loss assumption and profile are held at origination for cohort runoff.
The [matched comparison](audits/phase_4b_evidence/comparison.json) reports both fixed-offer
and reoptimised results. Canonical pricing retains the Phase 4a legacy formula and
configuration. The Phase 4a methodology and descriptive LVR bands remain historical evidence.

## Fixed deposit opportunity simulation (Phase 4c, FIN-001)

The [deposit supply model](deposit_supply_model.md) holds NZD of eligible
opportunity fixed over one 12-month horizon. It normalises sample weights
within disjoint segments and scores uptake on the original prospect features.
The raw amount still assigns an individual balance tier. Gross bookings
can include new money, retained maturities, internal transfers and existing
balances. Only eligible new money and explicit avoided runoff can add
usable funding. The opening book funds assets outside the new-business
horizon. It is disclosed but never offset against new loans.

Joint and segmented simulation pricing use the recognised incremental
funding once for wholesale cost, surplus income and shortfall steering. Offered
interest, displaced source interest, acquisition costs and any concentration
penalty are reported separately. The Phase 4a canonical configuration and
prospect-based economics still operate in the governed pricing path.
The supply profile applies to a single horizon. Product-level opening-book
and maturity data would be needed for a reconciled annual roll.

## Deliberate simplifications

Economic inputs must be finite: NaN and ±infinity are rejected in YAML, direct models, copy
updates, API/dashboard configuration and LLM patches. This covers market/reference rates, funding
parameters, coefficients, grids, tier bounds and multi-period assumptions. Both loan elasticities
must be ≤ 0; both deposit elasticities must be ≥ 0; zero is valid. These checks do not recalibrate
responses or change documented bounds. Finite negative wholesale spreads remain supported with
a warning. LLM patches cannot alter elasticities or coefficients; see
[patch governance](model_governance.md#financial-inputs-and-llm-scenario-patches-opt-003-gov-007).

- No personalized pricing yet.
- Trained ML response models do not feed the optimizer by default. They can feed the optimizer only
  through the explicit approved-champion registry path in `run_baseline_diagnostics(registry=...)`,
  with deterministic synthetic fallback when no approved compatible champion is available.
- No prepayment or behavioural maturity modelling yet.
- Multi-period cash-rate drift does not automatically drift competitor loan or deposit rates; the
  implementation warns because competitor gaps will shift mechanically. Competitor drift moves the
  reference rates too unless `reference_rate_drift: hold`.
- The multi-period roll carries booked cohorts at constant loss rates with geometric runoff; no
  amortisation schedule, rollover, terminal value or lifetime ECL.
- Rates are annualized assumptions within each scenario period.

## Dashboard interpretation

The premium dashboard presents the same portfolio-level synthetic scenario economics. Its
financial-value comparison plots each existing scenario's `objective`; it does not estimate
realised profit or introduce a new optimisation criterion. A negative value is a modelled loss.
Advertised-rate history uses step lines and retains missing-data gaps. Rate axes are narrowed
for comparison and labelled in percent; dispersion and movement remain on separate bps axes.
Deposit market leadership means the highest comparable deposit rate, not the best bank margin.
Term-deposit specials count as comparable only on explicit page evidence of public
availability and like-for-like terms (balance band, payment frequency). Otherwise they are
recorded and excluded, and uncertain ones degrade the collection (exit 2). See
`docs/pricing_intelligence.md` -> "Term-deposit specials and extraction completeness".
Each product's health summary reflects its own backend freshness and collector state.

For legacy competitor-rate inputs with no ordinary deposit at the configured term, comparable
specials follow the configured payment-frequency ordering and exact-before-combined matching.
The highest advertised rate within that preference group supplies the bank reference; equal
rates use the observation key. With no preference match, the highest comparable special wins.
Ordinary-deposit selection and the higher-rate, same-frequency special-replacement rule remain
unchanged. This selection rule does not alter optimiser mathematics or special eligibility policy.

The selected scenario's **model-best** candidate maximises the existing objective under
its stated assumptions. The **Median-benchmark optimum (reference)** is the existing median
comparison run, not an additional strategy or the selected assumption's answer. Policy-band,
boundary, expansion, ROE-relaxation and funding-shortfall evidence is displayed per run. Clear
diagnostic flags do not imply approval; incomplete evidence requires review. No compliant
alternative is inferred from a warning flag, and no candidate is silently substituted.
Phase 1d changes presentation only; rates, objectives, response models and artifacts are unchanged.

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

### Benchmark comparability assumptions (Phase 2f, 2026-11-21)

- **Reference depositor (DATA-005).** The default term-deposit benchmark describes a
  $10,000 deposit with interest at maturity (`pricing_intelligence.deposit_reference`).
  Only balance tiers and payment frequencies that depositor can get are candidates; a rate
  with no published tier or frequency is admitted but labelled "eligibility not
  established". No annual-equivalent conversion is made between payment frequencies.
- **Inferred LVR (DATA-013).** An 80% LVR / 20% equity condition inferred from a
  special-tier rule keeps its numeric values and is flagged `eligibility_inferred`.
  A bound with missing or conflicting source evidence has unknown provenance.
  Historical reads use evidence available by their evaluation cutoff.
- **Own bank (DATA-014).** `our_bank` is canonicalised against the collector registry's
  canonical names and declared aliases; unknown names fail configuration.

These change which observations qualify for a benchmark, never the pricing equations. A
different benchmark rate legitimately changes a market-derived scenario's output; for an
identical resolved competitor assumption the optimiser output is unchanged.
Repeated suspicious values are not corroboration, and stale trusted fallback
rates retain their original age. Dashboard freshness describes the selected trusted rate,
not a bank's oldest observation at another term. System Health remains bank-wide: its age
uses the oldest resolved trusted confirmation across tracked terms, while quarantine stays
a separate flag. Scenario exclusion warnings describe only the selected term. Stale-only terms
remain visible in the Overview audit matrix, but live selectors and their default use only
snapshot terms. With no live terms, market pages show guidance; stored scenarios for unavailable
terms are hidden. A stored scenario recommendation is hidden after loaded engine YAML changes,
until an explicit rerun;
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

Pricing Watch quarantine badges describe only the representative observation selected by the
trusted snapshot. A suspicious sibling remains in audit history without labelling a healthy
representative as a fallback. Snapshot selection, freshness limits and pricing benchmarks are
unchanged. Historical event details require the event's observation key and a state beginning
no later than the event; unavailable historical parser evidence stays explicitly unavailable.


### Quarantine review time and repeated evidence (PR #72)

An unchanged reading is not independent confirmation. A pending peer-flagged reading
remains quarantined even when later peer collection fails or the peer signal passes.
The current peer assessment may report `insufficient_evidence`; this does not erase the
stored suspicion. Only an explicit operator decision can approve or reject that reading.
A genuinely different reading is validated normally against trusted evidence.

Observation trust and change-event visibility both apply the append-only review ledger
at the requested time. Before approval, a suspicious movement and its companions remain
excluded from activity and market-state statistics. Approval becomes effective at its
timestamp; void removes the event from current statistics at its timestamp while retaining
earlier historical visibility. Operator decisions never rewrite collected event rows.
Approved rate endpoints are reconciled to the last trusted predecessor at approval time;
companions describing a superseded intermediate hop remain suspicious. Old databases
whose approvals cleared event flags recover original suspicion from observation evidence
and the ledger. These controls apply separately to home loans and term deposits and
change no synthetic pricing mathematics.
