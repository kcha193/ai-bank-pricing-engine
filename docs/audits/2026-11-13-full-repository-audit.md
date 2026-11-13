# Full Repository Audit — 2026-11-13

Independent, audit-first review of the whole repository. No source, test, config, workflow or
model artifact was modified. The only additions are under `docs/audits/`: this report, the
evidence JSON, the remediation roadmap, and the repro scripts and raw outputs in
`docs/audits/2026-11-13-evidence/`. Nothing was committed or pushed.

| Item | Value |
|---|---|
| Branch | `master` |
| Audit SHA | `62b81d2b64d2b3298044100895f6950df59b10c7` (merge of PR #63, 2026-10-31) |
| Working tree at start / end | clean / clean apart from the new `docs/audits/` files |
| Environment | macOS (Darwin 27), Python 3.12.13 venv. numpy 1.26.4, pandas 3.0.2, pydantic 2.13.3, scikit-learn 1.8.0, streamlit 1.57.0, fastapi 0.136.1, pytest 9.0.3, ruff 0.15.12. CI uses Python 3.11 |
| Network | None. No live scrapers, webhooks, Telegram, LLM APIs, deployments or `live_rates` tests |
| Method | A lead reviewer covered finance, optimisation, architecture, testing and synthesis. Three independent read-only workstreams covered ingestion; dashboard and security; and ML and governance. Every P1 from those workstreams was re-checked against source by the lead reviewer before inclusion |

Status labels used throughout: **VERIFIED** (reproduced, or the code does it unambiguously),
**STRONGLY SUSPECTED**, **DESIGN LIMITATION**, **NOT VERIFIED**.

---

## 1. Executive summary and verdict

**Overall verdict:** this is a well-engineered, heavily tested **synthetic prototype**.
- The core single-period arithmetic is exactly right.
- It is **not fit for production pricing or for unattended decision support** in its current form.
- There are **no P0 findings**, **12 P1**, **27 P2** and **34 P3** (73 in total).

What holds up under adversarial testing:

- **Arithmetic.** The single-period objective, volumes, ECL, funding, penalty and capital charge
  match a hand-calculated example exactly. They also match an independent re-implementation across
  all 357 grid points (max |Δ| = 3×10⁻⁸, same optimum).
- **Partial effects.** The direction of each effect is correct for the loan rate, deposit rate,
  competitor moves and funding stresses. Runs are deterministic: identical seeds give
  byte-identical grids.
- **Governance fail-loud invariants.**
  - An approved champion with a missing or corrupt artifact raises `GovernanceError`, and it is
    not downgraded.
  - An unapproved champion gives a bit-identical price.
  - Non-finite objectives are BLOCKED on the governed path.
  - `publish_safely` honours its deadline.
  - The decision record is additive.
- **Boundaries.**
  - The import boundary between `pricing_intelligence` and the optimiser holds.
  - Home-loan and term-deposit separation in the dashboard passes every challenge test.
  - The MANUAL strategy returns the same config object.

What does not hold up:

1. **The canonical recommendation is outside its own policy band.**
   - Every in-band candidate is a modelled loss (best in-band objective −$4.51m).
   - The headline 6.19% home-loan rate is 120 bp above market. It is driven by two uncalibrated
     synthetic inputs:
     - expected credit losses of about 0.8% a year;
     - a deposit prospect pool only 19% the size of the loan pool.
   - The governed status is always REVIEW_REQUIRED.
   - The dashboard still labels the rate "Optimal" (FIN-001, UX-003).
2. **Several optimiser and governance controls fail open.**
   - The `min_return_on_equity` floor is silently ignored when no candidate meets it (OPT-001).
   - Gates cannot see a negative objective, zero volume, or an ML champion that is loadable but
     wrong (GOV-001, GOV-002).
   - The segmented optimiser can return a loss-making result that its own search space strictly
     dominates (OPT-002).
3. **The multi-period NPV is economically inconsistent.** A held book worth +$5.92m in period 0
   is reported as −$0.57m in period 1 (FIN-002).
4. **The ML path cannot represent the four-elasticity design** (MOD-001, MOD-002).
   - The training data holds the competitor rate constant, so the own-rate and gap effects are
     unidentifiable.
   - Training rates stop at 5.49%, yet the optimiser prices at 6.19% or above.
5. **The ingestion path has two silent-failure modes** (DATA-001, DATA-002).
   - Term-deposit "special" rows are dropped with exit 0.
   - Genuine rate moves of 75 bp or more are never admitted, because the quarantine has no
     approval path. After 13 days the bank silently leaves the market.
6. **The dashboard can mislead.** Pricing Studio shows two different "optimal" rates on one
   screen (UX-001).
7. **The API is open by default.** It is unauthenticated on `0.0.0.0` with unbounded compute
   unless an operator sets keys (SEC-001).

**Production readiness: FAIL (NOT READY).** This is expected and documented for a synthetic
prototype. Real use would first need:
- calibrated economics (ECL, FTP and deposit supply);
- closed governance gaps;
- an approval workflow for quarantined rates;
- authenticated, loopback-by-default services.

---

## 2. Scope, tools and verification

| Command | Outcome |
|---|---|
| `python -m ruff check .` | **PASS** (exit 0) |
| `python -m pytest --cov=balance_pricing_engine --cov-report=term-missing --cov-fail-under=95 -m 'not live_rates' -q` | **2177 passed, 1 failed, 2 skipped** in 166 s. Coverage **96.39%**, so the coverage gate passed. Exit 1 |
| The failure: `test_dashboard_launcher.py::test_it_serves_on_localhost_only_on_the_default_port` | **Environment failure.** A real Pricing Watch dashboard was listening on 127.0.0.1:8765, so the launcher correctly chose 8766. The test is non-hermetic (TEST-001) |
| Workstream re-runs | Ingestion: 43 files, 1057 passed / 2 skipped. ML and governance: 22 files, 358 passed. Dashboard and API: 571 passed, 1 failed (same launcher failure) |
| `bandit -r src scripts -q` | 0 High. The 20 B608 hits are false positives (parameterised SQLite). B104 is real (SEC-001) |
| Independent harnesses (repo copy in `docs/audits/2026-11-13-evidence/`) | `fin/challenge_core.py`, `fin/followup.py`, `fin/governed.py`, `fin/nan_status.py`, `fin/imports.py`, `ingest/*.py`, `mlgov/*.py`, `uxsec/*.py` |
| `examples/run_baseline.py`, `examples/train_response_models.py` | **NOT RUN.** They write into `data/outputs/`. The identical `run_baseline_diagnostics` call was run directly, and CI runs both examples |

To reproduce, run from the repo root:

```bash
PYTHONPATH=src .venv/bin/python docs/audits/2026-11-13-evidence/fin/challenge_core.py
```

Ingestion scripts run from their own folder with `PYTHONPATH=<repo>/src:.`.

> **Note on the local venv.** `.venv` was created at `~/Documents/ai-bank-pricing-engine`. Its
> `activate` script and console-script shebangs point there, so `source .venv/bin/activate`
> resolves the wrong interpreter. Call `.venv/bin/python -m …` instead. This is a local
> environment issue, not a repository defect.

---

## 3. System architecture and execution paths (as implemented)

```
                  configs/base.yaml ──► config.EngineConfig (pydantic)
                                              │
  ┌───────────── competitor data ─────────────┼────────────── pricing core ─────────────────┐
  │ competitor_rates/collectors (5 banks)     │  synthetic/customers + response             │
  │   └ extractors → normalise → models       │  rate_grids / market (spread grids)          │
  │ pricing_intelligence/                     │  optimization/grid_search (joint)            │
  │   validation → store (SQLite, atomic())   │  optimization/segmented (tiered, greedy)     │
  │   → snapshot → changes/market_state       │  optimization/response_fn (ML adapter)       │
  │   → strategy.apply_*_to_config ───────────┼─►diagnostics (adaptive expansion, flags)     │
  │   alerts (webhook/Telegram), backup, lock │  scenarios/runner, multiperiod, experiments  │
  │ legacy: competitor_rates/apply+store+cli ─┼─►(when competitor_rates.enabled)             │
  └───────────────────────────────────────────┘                                              │
         │                                       application.PricingService ──► governance   │
         │                                       (gates, lineage, decision_record, shadow),  │
         │                                       events.publish_safely, run_ledger           │
         ▼                                                │                                  │
  scripts/collect_market_rates_workflow.py (lock, exit 0/1/2/3) ─► pricing_scenario_trigger ─┘
         │ (cron/launchd, deploy/scheduling)              (direct run_baseline_diagnostics)
         ▼
  dashboard/ (Streamlit; views render-only; services → health/service/strategy/trigger)
  api/ (FastAPI: /run-scenario, /v1/pricing/scenarios → PricingService; /health, /ready)
  llm/ (narrative/anomaly/validation/scenario patches; decision-support only; mocked here)
  modelling/ (datasets, training agent, registry, champion/challenger, persistence via joblib)
```

**Execution paths:**

| Entry point | Route | Governed? |
|---|---|---|
| CLI and examples | `examples/*.py` → `run_baseline_diagnostics` | Only `run_governed_scenario.py` goes through `PricingService` |
| Scheduled collection | `scripts/collect_market_rates_workflow.py` → `service.collect` → `pricing_scenario_trigger.run_triggered_scenarios` → `run_baseline_diagnostics` | **No gates.** Results go to alerts |
| FastAPI | `/v1/pricing/scenarios` → `PricingService.run_scenario` | Gates and ledger. `GovernanceError` → HTTP error |
| Streamlit | Pricing Studio and Scenario Lab → `services` → `run_triggered_scenarios` / `run_baseline_diagnostics` | **No gates**, by design (decision support) |
| ML | `train_response_models.py` → `ModelTrainingAgent` → registry JSON plus joblib artifacts → `ModelScoringAgent.resolve_response_fn` → `MLResponseFnAdapter` | Only when a registry is passed. Joint grid only, not segmented |

**Trust boundaries:**
- bank web pages (untrusted HTML) → collectors;
- environment secrets (API keys, Ops token, webhook/Telegram, Anthropic) → `secrets/provider`;
- API request body (config YAML) → `PricingService`;
- joblib artifacts on disk → scoring;
- local SQLite.

**Independently verified layering** (`evidence/fin/imports.py`):
- **No violation of the documented rule.** `optimization/`, `pricing/` and `scenarios/` import
  nothing from `pricing_intelligence`.
- `pricing_intelligence` never imports `dashboard`.
- No core package imports `llm`.
- There is one module-level cycle (`pricing_intelligence.health ↔ service`).
- There are package-level inversions: `config` → `competitor_rates` and `pricing_intelligence`,
  and back (ARCH-001).

---

## 4. Scorecard

| # | Domain | Verdict | Basis |
|---|---|---|---|
| 1 | Engineering / code quality | **PASS WITH LIMITATIONS** | ruff clean, 96% coverage, clear seams. Package cycles, hard-coded tier-1 ratio, two ingestion paths |
| 2 | Financial mathematics | **PASS WITH LIMITATIONS** | Single-period arithmetic exact. Calibration drives an out-of-band optimum (FIN-001). **Multi-period economics FAIL** (FIN-002) |
| 3 | Statistical / model validity | **FAIL** (ML layer) | Synthetic response PASS. ML training cannot identify the elasticities and does not cover the priced range; leaky validation; oracle baseline (MOD-001/002/004/005) |
| 4 | Mortgage data reliability | **PASS WITH LIMITATIONS** | Fixture path correct end to end. Quarantine has no approval path (DATA-002); eligibility is partly inferred (DATA-013); live NOT VERIFIED |
| 5 | Term-deposit data reliability | **FAIL** | Specials silently dropped (DATA-001); best-tier selection (DATA-005); legacy path stores fund returns as TDs (DATA-006); live rendering NOT VERIFIED (DATA-007) |
| 6 | Optimisation correctness | **PASS WITH LIMITATIONS** | Joint grid argmax verified; boundary expansion and flags correct. **Segmented FAIL** (OPT-002); ROE floor fail-open (OPT-001) |
| 7 | Model governance | **FAIL** | Fail-loud invariants PASS. Gate coverage, artifact–feature checks and approval controls FAIL (GOV-001/002/003) |
| 8 | Dashboard decision usability | **FAIL** | Product separation PASS (challenge P 4/4). Conflicting "optimal" rates and "Optimal" label on breaching results (UX-001/003) |
| 9 | Security | **PASS WITH LIMITATIONS** (local) / **FAIL** (if network-exposed) | Secrets hygiene and launcher good. API open on `0.0.0.0` by default; secret can leak to log (SEC-001/002) |
| 10 | Operational resilience | **FAIL** (unattended) | Locking, idempotency and exit codes good. No stall deadline, quarantine recovery, backup verification or DB-lock handling (DATA-002/008/009/017) |
| 11 | Testing and release quality | **PASS WITH LIMITATIONS** | 2177 tests, 96%. Economic-invariant gaps, non-hermetic test, Python 3.9 claim untested, no lockfile (TEST-001/002/003) |
| 12 | Production readiness | **FAIL** | Synthetic prototype by design; see section 13 |

---

## 5. Independent financial validation (Phase 1)

### 5.1 Reconstructed single-period objective

All amounts are expected annual NZD over a one-year horizon. Rates are decimals.

| Term | Formula | Source |
|---|---|---|
| Loan volume L | Σ pᵢ·loan_amountᵢ | `pricing/profit.py:74-76` |
| Interest income | Σ pᵢ·loan_amountᵢ·r_L | `profit.py:22-29` |
| ECL | Σ pᵢ·loan_amountᵢ·elrᵢ, with elr = 0.0015 + 0.018·risk^1.4 | `profit.py:32-36`, `synthetic/customers.py:21` |
| Capital charge | L·RWA·0.10·hurdle | `profit.py:39-48` (tier-1 ratio 0.10 hard-coded) |
| Deposit volume D, cost | Σ qⱼ·depⱼ; D·r_D | `profit.py:64-81` |
| Wholesale cost | max(L−D, 0)·(cash + spread) | `profit.py:84-91`, `synthetic/market.py:17-20` |
| Excess deposit income | max(D−L, 0)·cash | `profit.py:94-101` |
| Funding penalty | max(ratio·L − D, 0)·penalty_rate | `pricing/balance_sheet.py:15-29` |
| Objective | income − ECL − capital − dep_cost − wholesale − penalty + excess | `optimization/grid_search.py:215-223` |
| ROE | (objective + capital) / (L·RWA·0.10) | `grid_search.py:224-238` |

The marginal value of one deposit dollar is wholesale + penalty below the funding ratio, wholesale
alone between the ratio and 100%, and cash above 100%. That makes the funding treatment a
coherent soft constraint, not a double count of cost. However, the penalty is a steering device,
not a cash cost, yet it sits in the ROE numerator (FIN-003).

### 5.2 Worked example

`evidence/fin/challenge_core.py::hand_example`:
- **Loans:** two loans of $100k and $200k, acceptance 0.5 and 0.25, ECL 1% and 2%.
- **Deposits:** two deposits of $50k, uptake 1.0 and 0.6.
- **Rates:** r_L 6%, r_D 4%, cash 3%, spread 1%.
- **Balance sheet:** funding ratio 0.9, penalty 2%, RWA 35%, hurdle 12%.

Hand calculation:
- L = 100,000; D = 80,000.
- Income 6,000; ECL 1,500; capital 420; deposit cost 3,200; wholesale 800; shortfall 10,000;
  penalty 200.
- **Objective −120.** The engine matches every component to 1e-12.

### 5.3 Reference optimiser

An independent re-implementation over the canonical grid (21 × 17 = 357 points) matches the
engine within 3×10⁻⁸ and gives the same argmax (5.49% / 4.20%, −$4,508,279).

The objective surface is flat. The best two rows differ by $3.7k (0.08%), so the reported
optimum is sensitive to small modelling changes.

### 5.4 Canonical result

| | Initial (policy band) | After 1 expansion (reported) |
|---|---|---|
| Home loan | 5.49% (band max) | **6.19%** (+120 bp vs 4.99% ref.) |
| Term deposit | 4.20% | **4.05%** |
| Objective | −$4.51m | +$5.92m |
| Loan / deposit volume | $1,750m / $377m | $510m / $329m (D/L = 64.5% ≈ ratio) |
| Flags | boundary | `policy_band_breached=True`, governed status REVIEW_REQUIRED |

Loans accepted at the reported rate are 17% of the $2.94bn applied. Take-up at market is 85.8%
for loans and 49.9% for deposits.

### 5.5 Edge and stress cases (Phase 11)

| Case | Assumption | Expected partial effect | Observed | Result |
|---|---|---|---|---|
| A | Loan rate ↑, rest fixed | L and ECL ↓; deposit side unchanged | L and ECL strictly ↓; deposit cost constant; income non-monotone (volume vs rate) | PASS |
| B | Deposit rate ↑ | D and deposit cost ↑; shortfall ↓ | All strictly as expected; loan side constant | PASS |
| C | Competitor loan −100 bp | Loan recommendation ↓ | 6.19 → 5.84% | PASS |
| D | Competitor deposit +100 bp | Deposit recommendation ↑ | 4.05 → 4.35% | PASS |
| E | Funding ratio 1.0 (and penalty ×5) | Shortfall eliminated or penalised | Corner L ≈ D, shortfall 0, loan 6.39% | PASS |
| F | Wholesale spread +200 bp | Deposits relatively more valuable | Same corner as E, 6.39% / 4.00% | PASS |
| G | OCR ±100 bp, competitors fixed | Partial: deposit value ↑ with OCR | +100: 6.24% / 3.95%; −100: 5.74% / 3.80%, $569m shortfall accepted | PASS (joint effect explained: a smaller loan book needs fewer deposits) |
| K | Narrow band above optimum, expansions 0, 1, 2, 4 | Flags until interior | Capped and flagged at 0, 1 and 2; interior 6.19% at 4; positive-sign elasticity runs to 8.49% and is flagged | PASS (wrong sign accepted: OPT-003) |
| L | `min_return_on_equity` 1.0 or 10.0 (max feasible 0.64) | Infeasibility surfaced | 0 rows qualify; silently returns unconstrained; no flag or gate | **FAIL** (OPT-001) |
| M | NaN competitor or elasticity; inf cash; negative OCR or spread | Reject or block | Config accepts NaN and inf. Direct path returns NaN with no warning. Governed path BLOCKED | **PARTIAL FAIL** (OPT-003) |
| N | Two identical seeded runs | Identical | Identical SHA-256 of `grid_results` | PASS |
| O | Two periods, zero runoff, zero discount | Held book ≈ period-0 contribution | +$5.92m → −$0.57m | **FAIL** (FIN-002) |

The other challenge cases are covered elsewhere: H, I and Q in section 8, J in section 7, and P in
section 9.

### 5.6 Performance (Phase 10)

| Workload | Time |
|---|---|
| 697-row adaptive grid, 1k to 20k customers | 0.02–0.05 s |
| 1 bp grid (16,281 rows) | 0.43 s |

The grid search is not a bottleneck. No measurable performance defect was found in the pricing
core.

---

## 6. Findings — finance, optimisation, architecture and testing

### FIN-001: Canonical recommendation breaches its policy band; driven by uncalibrated inputs

**P1. High confidence. VERIFIED (outcome); DESIGN LIMITATION (calibration).**

**Evidence:**
- `configs/base.yaml:12-20` defines a ±50 / ±40 bp band.
- Section 5.4: every in-band objective is negative.
- `synthetic/customers.py:21`: mean ECL 0.79% a year (5th–95th percentile 0.38–1.26%).
- The deposit prospect balance is $556m against $2.94bn of loan applications (19%).

**Cause:**
- The 65% funding ratio (`base.yaml:47-48`) binds.
- Raising `n_deposit_customers` from 5k to 10k to 25k moves the loan recommendation from 6.19% to
  5.99% to 5.79% (`evidence/fin/followup.json`).
- With a zero funding ratio it is 5.64%.
- So the headline rate is set mainly by the arbitrary ratio of the two synthetic population sizes
  and by ECL at roughly 10–50× typical prime-mortgage loss experience. That comparison needs
  confirmation from a subject-matter expert.

**Docs:** `docs/modelling_assumptions.md` attributes the premium to "no share constraint". It does
not name either driver.

**Impact:** every scenario run, alert and Pricing Studio result recommends a rate far outside
policy. A reviewer could treat the 120 bp premium as a model insight rather than a calibration
artefact.

**Fix:**
- Calibrate ECL and deposit-pool scale (or express deposit supply as a market-share curve).
- Document both as primary drivers.
- Add a canonical-config test asserting the recommendation is inside the policy band, or that a
  documented reason is attached.

**Effort:** M (code); calibration needs SME input.

### FIN-002: Multi-period back-book economics are internally inconsistent

**P1. High confidence. VERIFIED.**

**Evidence (`scenarios/multiperiod.py:83-101`):**
- Surviving loans earn only `cash_rate + reinvestment_spread`, gross. There is no funding cost,
  ECL or capital, and the contractual loan rate is ignored.
- Surviving deposits cost their full rate, with no wholesale saving or penalty relief.

**Repro:** `fin/challenge_core.py::multiperiod_case`. Period-1 back-book = $510m × 2.5% −
$13.32m = **−$0.571m**, against +$5.92m if the book kept its own economics.

**Also:**
- Every period regenerates identical customers (same seed) and re-optimises myopically.
- Reference rates do not drift with competitor drift (`multiperiod.py:115-134`).
- No registry is passed, so an ML champion never applies.

**Impact:** the NPV is materially misstated, with the wrong sign for a held book. It is
diagnostic only (`examples/run_multiperiod.py`), not wired to the API or dashboard, which caps
the severity at P1.

**Fix:**
- Carry the period-0 decomposition forward on surviving balances (loan rate, ECL, capital,
  funding mix), or label the NPV as a "new-business-only proxy" and drop the back book.
- Drift the reference rates.

**Test:** zero runoff and zero discount gives period-1 back-book ≈ period-0 objective.

**Effort:** M.

### OPT-001: `min_return_on_equity` silently not enforced when infeasible; no flag or gate

**P1. High confidence. VERIFIED.**

**Evidence:**
- `optimization/grid_search.py:299-305` falls back to the unconstrained choice.
- The fallback is documented (`modelling_assumptions.md:15-17`) but is not exposed in
  `OptimizationResult`, `summary.json` or `PricingDecision`.
- `governance/gates.py:72-99` has no ROE gate.
- The segmented path does warn (`scenarios/runner.py:461-473`), so the two paths are inconsistent.

**Repro:** `fin/challenge_core.py::roe_cases` and `fin/governed.py`. Floors of 1.0 and 10.0 give 0
qualifying rows, an identical rate, no warning, and governed status unchanged.

**Impact:** a configured hurdle looks enforced but is not.

**Fix:**
- Return a `roe_floor_met` / `roe_constraint_relaxed` flag.
- Add an `ROE_FLOOR` gate (WARN or BLOCK, per the documented policy).
- Add the flag to `summary.json` as an additive key.

**Effort:** S.

### OPT-002: Segmented optimiser is greedy and decoupled; can return a dominated, loss-making result

**P1. High confidence. VERIFIED.**

**Evidence:**
- `optimization/segmented.py:258-265`: loan tiers are optimised assuming 100% wholesale funding
  and no shortfall penalty.
- Deposits are then optimised sequentially in config order (`:126-142, 313-321`).

**Repro:** `fin/followup.py`. One catch-all tier each, on the same 5 bp grid as the joint
optimiser (3–9% and 2–6%):

| | Loan rate | Objective | Shortfall |
|---|---|---|---|
| Segmented | 5.65% | **−$0.27m** | $552m |
| Joint | 6.15% | **+$5.90m** | — |

- The segmented result is $6.17m worse than a point inside its own search space.
- There is no boundary or policy flag. Governed status is REVIEW_REQUIRED only via
  FUNDING_SHORTFALL.
- The segmented path also never consults the registry (`runner.py:210-217`). Lineage is honestly
  reported as synthetic (`application/pricing_service.py:333-340`).

**Fix:**
- Include deposit funding and penalty in the loan-tier objective, e.g. coordinate-descent over
  tiers until convergence, or a joint grid when tier count × grid size is small.
- Assert segmented ≡ joint for a single tier.

**Effort:** M.

### OPT-003: Config accepts non-finite and wrong-sign economic inputs; direct path returns NaN silently

**P2. VERIFIED.**

**Evidence:** `config.py:50-56,153-158` has no `allow_inf_nan=False` and no sign validators.

**Results:**
- NaN competitor or NaN elasticity: 357/357 NaN objectives, the reported "best" is 4.99%, and
  there is no warning.
- inf cash rate: NaN objective.
- A positive loan elasticity is accepted.
- `PricingService` correctly BLOCKs (`OBJECTIVE_FINITE`). But dashboard and alert scenarios use
  `run_baseline_diagnostics` directly (`pricing_scenario_trigger.py:133-135`), so they are not
  protected.

**Related:** GOV-007 (the LLM patch can flip signs).

**Fix:** finite and sign validators on `MarketConfig`, `ResponseConfig` and `BalanceSheetConfig`.

**Effort:** S.

### OPT-004: Scenario rows and alerts drop every decision-quality flag

**P2. VERIFIED.**

**Evidence:**
- `pricing_scenario_trigger.py:52-61`: `ScenarioRow` = label, strategy, competitor_rate,
  our_rate, objective.
- `format_scenario_block` (`:167-191`) sends "Model optimum: 6.19%" to Telegram and the webhook
  with no policy-band, boundary, capped, shortfall or ROE status, even though every canonical row
  breaches the band.

**Fix:** carry the flags on `ScenarioRow` and add a one-line status to the alert block.

**Effort:** S.

### FIN-003: FTP and funding curve is OCR + flat spread for a 12-month fixed product; penalty in the ROE numerator

**P2. DESIGN LIMITATION.**

**Evidence:**
- `synthetic/market.py:17-20`.
- There is no tenor-matched swap or liquidity premium, so market TDs at 3.90% cost more than
  wholesale at 3.70%.
- The ROE numerator includes the notional penalty and no tax or opex (`grid_search.py:226-238`).

Documented in part.

**Fix:** a term-matched FTP curve (1y swap + liquidity premium), and report ROE without the
penalty.

**Effort:** M; needs an SME.

### FIN-004: Capital proxy simplifications

**P3. DESIGN LIMITATION.**

- The tier-1 ratio of 0.10 is hard-coded in three places: `profit.py:43`, `grid_search.py:224`
  and `runner.py:606`.
- A flat 35% RWA ignores the generated LVR.
- `maturity_adjusted_rwa_weight` (`profit.py:51-61`) applies a corporate-style maturity uplift
  that retail mortgage frameworks do not use. The code labels it "Not a regulatory calculation".

**Fix:** make the tier-1 ratio configurable, and optionally add an LVR-banded RWA.

**Effort:** S.

### FIN-005: Stale explanation of the deposit funding mechanism

**P3. VERIFIED.**

`docs/modelling_assumptions.md` says "Why deposits are raised to roughly 100%… 4.00% vs 5.45%".
At the current OCR, wholesale (3.70%) is below the TD rate (4.05%), so deposits stop at the 65%
ratio (D/L = 64.5%). The mechanism is now reversed, not merely restated with new numbers.

**Effort:** S.

### ARCH-001: Package-level dependency inversions

**P3. VERIFIED.**

- `config.py:12-14` imports `competitor_rates.models` and `pricing_intelligence.config`, both of
  which import `config`.
- `optimization` and `modelling` depend on each other, as do `rate_grids` and `optimization`.
- `pricing_intelligence.health` and `service` form a module-level cycle.
- There is no runtime defect. It does make extraction and testing harder.

**Fix:** move the ingestion pydantic models into a leaf `contracts` module.

**Effort:** M.

### TEST-001: Non-hermetic launcher test

**P3. VERIFIED.** Same issue as UX-011.

`tests/unit/test_dashboard_launcher.py:193` assumes port 8765 is free.

**Fix:** set `PRICING_WATCH_PORT` to a port the test has just verified is free.

**Effort:** S.

### TEST-002: High coverage, missing economic invariants

**P2. VERIFIED.**

- At 96.39% coverage, none of FIN-001/002, OPT-001/002/003, MOD-001/002, GOV-001 or DATA-001/002
  was caught. The tests assert documented behaviour, not economic or governance invariants.
- `dashboard/app.py` has 0% coverage, `api/server.py` 76%, `modelling/agentic.py` 88%.
- Hypothesis is used in only two files.

**Fix:** add the invariant tests listed in the roadmap.

**Effort:** M.

### TEST-003: Declared Python ≥3.9 support is untested and probably broken; no lockfile

**P3. STRONGLY SUSPECTED.**

- `pyproject.toml:10` declares `>=3.9`, but CI tests only 3.11 (`ci.yml`).
- `api/schemas.py` and `competitor_rates/models.py` use PEP 604 unions in pydantic models under
  `from __future__ import annotations`, which pydantic cannot evaluate on 3.9 without
  `eval_type_backport`.
- There is no lockfile, and pandas 3.0.2 was resolved locally.

**Fix:** raise `requires-python` to `>=3.11`, or add 3.9 to CI. Add a constraints file.

**Effort:** S.

---

## 7. ML, statistical and governance findings (Phases 2 and 8)

### 7.1 Elasticity trace

| Config key (`base.yaml:52-55`) | Optimiser | Synthetic truth | ML training | ML adapter |
|---|---|---|---|---|
| `loan_rate_elasticity` −170 | `runner.py:260` → `grid_search.py:166-175` | `response.py:36` | `workflows.py:56` → `datasets.py:48,64-69` | `response_fn.py:106-109` |
| `loan_competitor_gap_elasticity` −130 | `runner.py:261` | `response.py:33,37` | `workflows.py:57` | same |
| `deposit_rate_elasticity` +140 | `runner.py:262` → `grid_search.py:131-140` | `response.py:58` | `workflows.py:65` | same |
| `deposit_competitor_gap_elasticity` +110 | `runner.py:263` | `response.py:55,59` | `workflows.py:66` | same |
| `coefficients.*` | passed | passed | **not passed** (`datasets.py:154-160,173-179`) | n/a |

**Synthetic response:**
- Bounded with no NaN.
- Loan acceptance is strictly decreasing in its own rate; deposit uptake is strictly increasing.
- The competitor sign is correct. At 6.19%, a competitor −50 / 0 / +50 bp gives acceptance
  0.102 / 0.174 / 0.280.
- At a fixed competitor rate, the (−170, −130) and (−300, 0) parameterisations are
  observationally identical to within 2.2e-16 (MOD-008).
- **PASS.** The four terms stay separate from config through to the optimiser.

### 7.2 Findings

| ID | Sev | Class | Summary | Evidence | Fix (effort) |
|---|---|---|---|---|---|
| **MOD-001** | P1 | VERIFIED | Training holds the competitor rate constant: std 1.4e-17, corr(offered, gap) = 1.0. Logistic splits −146.75 / 0 / −146.75 (truth −170 / −130). HistGBM has zero competitor response, even inside its training range | `modelling/datasets.py:60-61,99-100`; `workflows.py:55,64`; `features/local_store.py:60,70` | Sample the competitor rate per row, independent of the offered rate; add a competitor-sign check before approval (M) |
| **MOD-002** | P1 | VERIFIED | Training rates span 4.49–5.49%; the optimiser searches up to 6.49% and above. An approved HistGBM is flat at 0.645 above 5.49%, so the loan prices at **8.49%** (+230 bp), with status only REVIEW_REQUIRED | `workflows.py:48`; `optimization/response_fn.py:93-125` (no range guard) | Train on the policy-expanded grid; record the training range; add a gate when the evaluated range exceeds it; HGB `monotonic_cst` (M) |
| MOD-003 | P2 | VERIFIED (latent) | Training generator ignores `response.coefficients`. Max probability difference 0.44 when the intercept changes | `datasets.py:154-160,173-179` | Thread the coefficients through (S) |
| MOD-004 | P2 | VERIFIED | 87.4% of test rows share a customer with train; the test split is never scored; no out-of-time validation; the scoring population uses the same seed | `datasets.py:139`; `reports.py:47-52`; `agentic.py:972-985,1001-1006,1102,1201-1217` | Group split by `customer_id`; score the test split; separate seed (S–M) |
| MOD-005 | P2 | VERIFIED | The "baseline" that challengers must beat reads the true probabilities (an oracle). It is promotable, gives flat 0.5, and is labelled `recommended_challenger`. Promoting it prices at 8.49% | `agentic.py:131-139,1044,1064-1073,1174-1179` | Forbid promotion; derive labels from `model_type` (S) |
| MOD-006 | P3 | DESIGN | ML reproduces the generator; uncertainty quantiles are unused | `memo.py:182` | Document; propagate uncertainty later |
| MOD-007 | P3 | VERIFIED | PDP on the offered rate doesn't recompute the gap | `evaluation.py:43-61` vs `:86-104` | (S) |
| MOD-008 | P3 | DESIGN | Own and gap effects are unidentifiable at a fixed competitor rate. The AGENTS.md "not equivalent" warning holds only when the competitor rate moves | `synthetic/response.py:33-41` | Document in `modelling_assumptions.md` (S) |
| **GOV-001** | P1 | VERIFIED | Approved-champion resolution checks only that the file exists and loads. There is no artifact hash, feature-contract or probe check, and `MODEL_FEATURE_COMPATIBILITY` always PASSes. A permuted-column artifact priced the loan at 4.99% with **zero loan volume**, lineage `approved_ml_champion` and all model gates PASS. A 9-of-10-column artifact gives a raw `ValueError`; a dict artifact gives `AttributeError` | `modelling/agentic.py:1381-1427`; `governance/gates.py:208-223` (confirmed) | Verify `artifact_sha256`, `feature_names_in_`/`n_features_in_` and a probe score at resolution; raise `GovernanceError` (M) |
| **GOV-002** | P1 | VERIFIED | No gate for a negative objective, zero volume, ROE floor, or ML response sanity. Stub diagnostics with those conditions are **RECOMMENDED** | `gates.py:72-98`; see OPT-001 | Add `OBJECTIVE_POSITIVE`, `VOLUME_NONZERO`, `ROE_FLOOR_MET`, `ML_RESPONSE_SANITY`, `ML_CHAMPION_IN_USE` (S–M) |
| GOV-003 | P2 | VERIFIED | Promotion accepted an empty approver and a log-loss of 9.9. The separation-of-duties and pre-approval checks in `model_governance.md:129,138-140` are not enforced | `agentic.py:587-660` | Require a distinct non-empty approver and `passed_validation` evidence (S) |
| GOV-004 | P2 | VERIFIED (in-process) | The decision record hashes the `config_path` file, not the config actually priced. Repro: loan elasticity −60 priced at 6.74%, but the hash and snapshot show `base.yaml` (−170). The API path is safe | `pricing_service.py:486-505,568-575`; `run_ledger/writer.py:43-55` | Hash the canonical `model_dump`; record the file hash separately (S) |
| GOV-005 | P2 | VERIFIED / race SUSPECTED | ML-priced runs get synthetic-only sensitivity, and the memo hard-codes "deterministic synthetic". Lineage is re-read from the registry after pricing | `pricing_service.py:180-183,513-519`; `memo.py:177-187` | Return the lineage captured at resolution (M) |
| GOV-006 | P3 | DESIGN | A supplied registry is ignored without a log in segmented mode or on a contract mismatch | `runner.py:210-217` | `REGISTRY_SUPPLIED_NOT_USED` WARN (S) |
| GOV-007 | P2 | VERIFIED | No elasticity sign validation. `llm.apply_patches` has no field allow-list. A mock patch flipping the signs was applied (→ 8.49%), and the audit log records only `n_patches` | `config.py:153-158`; `llm/scenario_interface.py:60-80,235-248` | Validators, allow-list, log approver and patch content (S) |
| GOV-008 | P3 | VERIFIED | The registry `config_hash` covers only features, target and seed. No artifact hash; non-atomic registry writes; shadow evaluation unpickles unapproved artifacts; broad `except` | `agentic.py:834,886,991-997`; `governance/shadow.py:123` | (M) |
| GOV-009 | P3 | SUSPECTED misuse | The decision record has no "synthetic" marker. The checklist sign-off can approve a BLOCKED run | `run_ledger/checklist.py:294-318` | Add `data_basis`; refuse sign-off when BLOCKED (S) |
| GOV-010 | P3 | DESIGN | No drift monitoring, recalibration trigger or retention policy | `model_governance.md:103-113` | Out of prototype scope |

### 7.3 Challenge J: approved champion artifact missing

Repro: `evidence/mlgov/challenge_j.py`, using temp registries only.

| Case | Result | Verdict |
|---|---|---|
| J1: approved, artifact missing | `GovernanceError` from `run_baseline_diagnostics` **and** `PricingService.run_scenario`, not downgraded | **PASS** |
| J2: approved, corrupt bytes | `GovernanceError` | **PASS** |
| J3 / J3b / J8: unapproved, or approved but not champion | Grid bit-identical to no registry (6.19% / 4.05%) | **PASS** |
| J2b / J4b / J4c: loadable but wrong artifact | AttributeError / ValueError / silent zero-volume repricing | **FAIL** (GOV-001) |
| J5: oracle baseline promoted | 8.49% at boundary | **FAIL** (MOD-005) |
| J6: inverted-sign champion | 3.49%, boundary WARN only | PASS WITH LIMITATIONS |
| J7: trained logistic champion | 6.14% / 4.05%; competitor +50 bp → 6.34% | PASS |

### 7.4 Other governance checks (PASS)

- `publish_safely` returns in 0.205 s against a 0.2 s deadline, with 16 calls in flight.
- A hung publisher adds at most 2.0 s to a request and does not change the decision.
- `decision_record.json` is additive: `summary.json` is identical, and `metadata.json` differs
  only in `run_id` and timestamp.
- LLM output is tagged `[DECISION-SUPPORT…]`. Nothing outside `llm/` imports it except one
  example.
- `GovernanceError` propagates unchanged.

---

## 8. Data ingestion and market intelligence (Phase 4)

### 8.1 End-to-end traces (fixture-based, `evidence/ingest/trace_e2e.py`)

**Mortgage (Kiwibank 1-year Fixed Special, 4.89%):**
1. Fixture `kiwibank_home_loans.html:8`.
2. `collectors/base.py:583` → `extractors.py:213,560` (category `special`, 12 months).
3. `base.py:516` enrichment. LVR 0.8 and equity 0.2 are **inferred** from "special".
4. `validation.py:128` accepts it.
5. `service.py:612` → `store.py:1238/1615` inside `atomic()`.
6. `snapshot.py:207`: median of the 12-month rates is 4.85.
7. `strategy.py:101/200`: MEDIAN → `competitor_loan_rate` and `home_loan_reference_rate` = 0.0485.
8. `dashboard_data.py:69-112` matches.

**Term deposit (ASB 12 months, 4.00%, $5,000+):**
1. Parsed as `term_deposit`, payment frequency unknown.
2. Snapshot category `term_deposit` (`service.py:389`), with the highest rate per bank winning
   (`snapshot.py:265-270`).
3. MEDIAN = 0.0400, LEADER = 0.0405.

Percent is converted to a decimal in one place only (`strategy.py:166`). Basis points use
Decimal and are exact.

### 8.2 Scorecards

**Home lending:**

| Sub-area | Verdict |
|---|---|
| Fixtures and classification | PASS WITH LIMITATIONS |
| Excluded products | PASS |
| LVR / equity eligibility | PASS WITH LIMITATIONS (DATA-013) |
| Cashback | Display only |
| Term normalisation | PASS WITH LIMITATIONS (DATA-011) |
| % and bps | PASS |
| Validation and quarantine | **FAIL** (DATA-002, DATA-016) |
| Storage idempotency and ordering | PASS |
| Change detection | PASS |
| Benchmark and staleness | PASS WITH LIMITATIONS (DATA-003) |
| Own-bank exclusion | PASS WITH LIMITATIONS (DATA-014) |
| MANUAL invariant | PASS |
| Failure visibility | PASS WITH LIMITATIONS (DATA-004) |
| Unattended readiness | **FAIL** |
| Live rendering | NOT VERIFIED |

**Term deposits:**

| Sub-area | Verdict |
|---|---|
| Fixtures | PASS WITH LIMITATIONS (255 accepted rows; no specials, tiers or partial pages) |
| TD vs PIE vs fund/savings rows | PASS (collector path) |
| **Special rows** | **FAIL** (DATA-001) |
| Day-based terms | PASS |
| Payment frequency | PASS WITH LIMITATIONS |
| Balance tiers | PASS WITH LIMITATIONS (DATA-005) |
| Conflicts and duplicates | PASS |
| Storage | PASS |
| Scheduling | PASS WITH LIMITATIONS |
| **Legacy `competitor_rates` path** | **FAIL** (DATA-006) |
| Live rendering | NOT VERIFIED (DATA-007) |

### 8.3 Why term-deposit collection can be missing or empty

These are the priority question from the brief, in order of likelihood. Items 1–5 are verified;
item 6 is strongly suspected.

1. **Category mismatch.** Deposit rows containing "special" are categorised `special`
   (`extractors.py:693-698`, confirmed). The default TD market reads only `term_deposit`
   (`service.py:386-390`, confirmed), so those rows are silently dropped. If every row is
   special, the bank has "no comparable rates".
2. **Tier tables labelled only by headings.** Every row is rejected as conflicting (case I-d).
3. **Legacy path.** It never runs the deposit clean-up (`competitor_rates/cli.py:128-131`), so
   strict validation always fails.
4. **Total failure.** Zero accepted rows means no `collection_runs` row. The freshness table keeps
   saying "Fresh" (`service.py:727-760`, `store.py:2296`; DATA-004).
5. **Quarantine.** A genuine large move stays quarantined; after the staleness window the bank
   drops out of the market (DATA-002).
6. **Partial render (strongly suspected).** The browser readiness check accepts any table
   containing "%" and ignores timeouts (`base.py:193-197,259-266`, `pragma: no cover`; DATA-007).

### 8.4 Findings

| ID | Sev | Class | Summary | Evidence / repro | Fix (effort) |
|---|---|---|---|---|---|
| **DATA-001** | P1 | VERIFIED (mechanism); live prevalence NOT VERIFIED | TD "special" rows are silently excluded. The run exits 0 SUCCESS, MARKET_LEADER = 4.05, and an ASB 4.50% special is ignored. The lead reviewer re-ran this from the preserved copy | `extractors.py:693-698`; `service.py:389`; `orchestration.py:449`; `ingest/td_special_mixed.py` | Treat TD specials as comparable promotions, or flag them as partial with exit 2 (S–M) |
| **DATA-002** | P1 | VERIFIED | Genuine moves of ≥ 75 bp are never admitted. `confirm_suspicious_changes` has no caller (confirmed by grep) and there is no approval command. ASB 3.90 → 4.90 repeated daily: exit 0 throughout; stale 3.90 used on days 8–13; ASB absent from day 14 | `store.py:1783`; `ingest/case_Q_storage.py` Q7 | Authenticated, append-only approve/reject command; exit 2 plus health escalation while a quarantine persists (M) |
| DATA-003 | P2 | VERIFIED | No minimum bank count (a one-bank "median" is accepted); `as_of` shows the newest input while 4 of 5 are 145 h old | `strategy.py:144,173`; `snapshot.py:306`; `config.py:131`; `ingest/case_H_stale.py` | `min_banks`; report the oldest input age and bank count (S) |
| DATA-004 | P2 | VERIFIED | After a total failure, freshness shows "Fresh" (no `collection_runs` row). September U2 is partly fixed | `service.py:727-760`; `store.py:2296`; `dashboard_data.py:25`; `ingest/case_I_td_zero_rows.py` | Freshness from the latest `operational_runs` attempt (S) |
| DATA-005 | P2 | DESIGN | TD benchmark takes each bank's best rate across tiers and frequencies, so a $500k+ tier can lead the market. The frequency preference is used only on the legacy path | `snapshot.py:265-270`; `ingest/td_tiers_and_legacy.py` | Configurable reference tier and frequency (S–M) |
| DATA-006 | P2 | VERIFIED | Legacy path stores fund returns as TDs. Strict mode always fails; non-strict benchmarks BNZ 3-year at 8.12% and drops stale warnings. Reachable via `PricingService` when `competitor_rates.enabled` | `competitor_rates/cli.py:128-131`; `apply.py:28-44` | Run the deposit clean-up; surface warnings, or retire the path (S) |
| DATA-007 | P2 | STRONGLY SUSPECTED | Browser readiness accepts any table with "%"; timeouts ignored; untested | `collectors/base.py:193-197,259-266` | Bank-specific selector or minimum row count; mark a timeout as degraded (M) |
| DATA-008 | P2 | VERIFIED (absence) | No overall run deadline or stall alert. A hung run holding the lock makes later runs exit 3 with only a warning. September U4 still open | `scripts/collect_market_rates_workflow.py`; `deploy/scheduling/*` | Supervisor timeout; escalate repeated exit 3 (S–M) |
| DATA-013 | P2 | DESIGN | LVR 0.8 is inferred from "special" with no flag; package and salary-credit conditions are absent. September U1 still open | `collectors/base.py:516-560` | `eligibility_inferred` flag (S) |
| DATA-016 | P2 | DESIGN | A +60 bp misread is accepted, becomes MARKET_LEADER, and cannot be voided | `ingest/case_Q_storage.py` Q6 | Peer-median check; void event type (M) |
| DATA-019 | P2 | VERIFIED | Every test injects fixture fetchers; the Playwright path is not covered; TD fixtures lack specials, tiers and partial pages | `tests/unit`, `tests/fixtures` | Add the fixtures (S) |
| DATA-009 | P3 | VERIFIED | "database is locked" is uncaught after 5 s; no summary is written | `store.py:610`; workflow `:216`; `ingest/db_locked.py` | WAL mode and busy timeout; classify the error (S) |
| DATA-010 | P3 | VERIFIED | CLI `collect` takes no lock and writes no run ledger | `pricing_intelligence/cli.py:372` | (S) |
| DATA-011 | P3 | VERIFIED | "1 year 6 months" reads as 6; "6-9 months" as 9; "$500k+" as $500 | `normalise.py:79,358`; `ingest/unit_checks.py` | (S) |
| DATA-012 | P3 | VERIFIED | `effective_date_text` is never parsed, but the UI says "not published" | `dashboard/views/rate_changes.py:58` | (S) |
| DATA-014 | P3 | VERIFIED | `our_bank="ASB Bank"` does not exclude ASB; no validation | `snapshot.py:33` | Validate against the known bank list (S) |
| DATA-015 | P3 | VERIFIED | Accepted rows are stored with status "unvalidated" | `store.py` | (S) |
| DATA-017 | P3 | VERIFIED | Backups are manual, with no integrity check and no restore command | `pricing_intelligence/backup.py` | (S–M) |
| DATA-018 | P3 | VERIFIED | Legacy mortgage `market_average` weights by row count (4.79 vs 4.815 per bank) | `competitor_rates/apply.py` | (S) |
| DATA-020 | P3 | VERIFIED | `docs/pricing_intelligence.md` (around line 701) still says "Mortgage-only market view" | | (S) |

### 8.5 Challenge cases H, I and Q

| Case | Result |
|---|---|
| **H: all competitor observations stale or missing** | **PARTIAL FAIL.** All stale: `MarketStrategyError` for both products (PASS); legacy strict mode errors (PASS). Partly stale or thin markets are used without warning (DATA-003); legacy non-strict mode applies a stale 0.057 (DATA-006) |
| **I: TD collection returns no valid rows** | **PASS WITH LIMITATIONS.** Exit 1, a FAILED run record, per-bank health, and DEGRADED/FAILING status in every variant. The strategy keeps using 25 h-old data until the window lapses, and raises on day 8. The freshness table says "Fresh" (DATA-004) |
| **Q: duplicates, failed runs, out-of-order data** | **PASS on integrity, FAIL on quarantine recovery.** Re-running a run is a no-op. Identical data gives 255 unchanged rows. An exact duplicate is removed; a conflicting duplicate rejects both rows with exit 2. A backdated run is rejected. There are no false withdrawals after an outage. Q7 fails (DATA-002) |

---

## 9. Dashboard and business decision UX (Phase 5)

**Product separation.** The dashboard is render-only, takes `product_type` through every service
call, and ranks deposits highest-first in the backend. **Challenge P passes 4/4**
(`evidence/uxsec/challenge/test_challenge_p.py`):
- the two products never mix;
- each ranks in its own direction;
- there is no cross-product substitution when data is missing;
- an AppTest product toggle passes (Westpac 4.25% leads deposits, ANZ 4.79% leads home loans).

**Metric trace.** Every metric below was traced from the view to the backend:

| Metric | View | Backend |
|---|---|---|
| Lowest / Highest / Median | `overview.py:82-93` → `services.py:293-304` | `snapshot.py:73-97` |
| Spread | `overview.py:95-100` → `services.py:295` | `bps.spread_bps` |
| Gap to leader | `market_watch.py:80-85` → `services.py:340` | `bps.py:53-59` |
| Fresh coverage | `overview.py:108-113` → `services.py:851` | `health.py:178` |
| Optimal rate / Objective | `scenarios.py:197-228` → `services.py:631-632` | `grid_search.py:215-223,276-314` |
| Funding shortfall | `scenarios.py:216` → `services.py:634` | `pricing/balance_sheet.py:23` |
| "Model optimum" | `scenario_text.py:69-73` → `services.py:604,653` | `pricing_scenario_trigger.py:152-162` (Match-median row) |

| ID | Sev | Class | Summary | Evidence | Fix (effort) |
|---|---|---|---|---|---|
| **UX-001** | P1 | VERIFIED (AppTest; lead-reviewer confirmed) | Two conflicting "optimal" rates on one screen. Home loan with "Market leader": headline 6.09%, but the text says "Under current assumptions, the model optimum… is 6.15%". For deposits: 4.20% vs 4.15%. The "model optimum" is always the Match-median run (also DATA-021) | `scenario_text.py:69-73`; `scenarios.py:158-159`; `services.py:604,631,653`; `pricing_scenario_trigger.py:152-162` | Rename to "Median-benchmark optimum (reference)"; drop "under current assumptions"; hide when redundant; AppTest that text agrees with headline (S) |
| UX-002 | P2 | VERIFIED | The previous result stays rendered after inputs change, with only a grey caption | `scenarios.py:589-594` | Hide or grey out with a warning (S) |
| UX-003 | P2 | VERIFIED | The headline still says "Optimal" when the governance banner reports a policy-band breach (committed screenshot `pricing-studio-deposit-results.jpeg`). Every canonical run breaches (FIN-001) | `scenarios.py:179-228` | "Model-best (outside policy band — review)", plus the best in-band alternative (S) |
| UX-004 | P2 | VERIFIED | "synthetic" appears once in the dashboard, inside a collapsed expander. Modelled and observed numbers share the same styling | `scenarios.py:113` | Persistent MODELLED · synthetic / OBSERVED badges (S) |
| UX-005 | P2 | DESIGN | No objective waterfall: wholesale cost, ECL, capital and ROE are never shown. In deposit mode the "Objective" is the joint book | `scenarios.py:194-284` | Waterfall from the best-result fields (M) |
| UX-007 | P2 | VERIFIED | The rationale shows raw enum names. Deposit `MARKET_SECOND_LOWEST` actually means second-highest | `services.py:871-878` | Use `_strategy_labels` (S) |
| UX-006 | P3 | VERIFIED | The 4-insight cap can hide the quarantine line; "Data trust" lists stale banks only | `insights.py:19,98`; `overview.py:279-286` | (S) |
| UX-008 | P3 | VERIFIED | Scenario Lab formatting and persistence differ from Pricing Studio; cache unbounded | `scenario_lab.py:24,48,75-82` | (S) |
| UX-009 | P3 | DESIGN | "Increase" is red for both products (arrows are also shown) | `theme.py:49-55` | (S) |
| UX-010 | P3 | VERIFIED | The `?product=` deep link is ignored once the session holds a product | `ui.product_selector` | (S) |
| UX-011 | P3 | VERIFIED | Same as TEST-001 | | |
| UX-012 | P3 | VERIFIED | Panel freshness is cached for 60 s while the header is live; some `_guard` calls omit `product_type` | `_common.py:23-38`; `services.py` | (S) |
| UX-013 | P3 | VERIFIED (calc) | MUTED `#A9B6C8` on the light background is 1.97:1 | `charts.py:519` | (S) |

**How the UI could induce a wrong decision even with correct backend numbers:**
- UX-001 shows two "optimal" rates.
- UX-003 labels a policy-breaching rate "Optimal".
- UX-002 keeps showing a stale result.
- UX-007 shows the inverse strategy name.
- UX-004 and UX-005 make modelled numbers look like observed ones, with no economic breakdown.

**Not verified:** real-browser, mobile, screen-reader and dark-mode rendering. The visual review
used committed screenshots only.

---

## 10. Security, privacy and deployment (Phase 7)

**What is good:**
- No tracked secrets.
- `.env` is ignored.
- API key comparison uses `compare_digest`.
- The Ops token is at least 16 characters, fails closed and has a limiter.
- The launcher binds 127.0.0.1.
- SQL is parameterised.
- Collector URLs are hard-coded `https`, so SSRF isn't reachable.
- LLM keys are handled through `secrets/provider`.
- The Docker image runs as a non-root user.

| ID | Sev | Class | Summary | Evidence | Fix (effort) |
|---|---|---|---|---|---|
| **SEC-001** | P1 | VERIFIED default (lead-reviewer confirmed) | API auth is off unless `PRICING_ENGINE_API_KEYS` is set. Default host is `0.0.0.0`; the docs use `--host 0.0.0.0` and `-p 8000:8000`. No limits on customer count, grid step or config size: requests with no key return 422, not 401, and n = 1e10 and step 1e-6 bp were accepted. `/docs` is open; `persist_ledger` writes to disk | `api/auth.py:45-47`; `api/server.py:40`; `docs/deployment.md:16,65`; `config.py:22-47`; `api/schemas.py`; `evidence/uxsec/exp_api.py` | Loopback default; refuse non-loopback when no keys are set; size caps; `limit_concurrency` (S–M) |
| SEC-002 | P2 | VERIFIED | When the Telegram token or webhook URL contains whitespace or a newline, the token is written to `collect.log` through `str(exc)` (`InvalidURL` embeds `/bot<token>/`). Leaked in 3 of 4 cases | `pricing_intelligence/alerts.py:576-582` (the dashboard path `actions.py:413-416` is safe) | Log the class name only; strip secrets (S) |
| SEC-003 | P2 | STRONGLY SUSPECTED | `.streamlit/config.toml` doesn't pin a loopback address, and the `app.py:3` launch line omits it. Operations is open when `PRICING_WATCH_OPS_TOKEN` is unset | `dashboard/settings.py:75`; `views/operations.py:150-156` | `address="127.0.0.1"`; refuse Ops on a non-loopback bind with no token (S) |
| SEC-004 | P3 | VERIFIED | A file-existence oracle and path disclosure through API-controlled `competitor_rates.source_file`; absolute `run_dir` returned | `api/app.py:128-133,202-213,269`; `competitor_rates/models.py:360` | Reject the override or allowlist a directory; generic errors (S) |
| SEC-005 | P3 | SUSPECTED | `correlation_id` and `scenario_name` are unvalidated; a long name causes a 500 | `api/schemas.py`; `run_ledger/directory.py:20` | Pattern constraint (S) |
| SEC-006 | P3 | VERIFIED | Actions use `@v7` tags with no `permissions:` block; no lockfile or Dependabot; the base image has no digest pin; `.dockerignore` lacks `.env` | `.github/workflows/*.yml`; `Dockerfile:7` | (S) |
| SEC-007 | P3 | DESIGN | `joblib.load` without an integrity check (not reachable from the API) | `modelling/persistence.py:18-20`; `governance/shadow.py:123` | SHA-256 at approval (with GOV-001) (M) |
| SEC-008 | P3 | SUSPECTED | The dashboard opens SQLite read-write | `pricing_intelligence/store.py:559-587` | Use `read_only` (M) |
| SEC-009 | P3 | VERIFIED | Fixed `/tmp` directory for API config files | `api/app.py:233-238` | `mkstemp` (S) |
| SEC-010 | P3 | SUSPECTED | CSV formula injection in the legacy `latest.csv` | `competitor_rates/store.py:33-34` | Escape (S) |

**Data sensitivity.** All data is synthetic or public advertised rates. If confidential customer
or bank data were substituted, these become high-impact:
- SEC-001 (open API);
- SEC-003 (LAN dashboard);
- SEC-007 (joblib);
- run ledgers written in plaintext to `data/outputs/`.

**Not verified:** actual network binds, DNS rebinding and Streamlit origin checks, the
`GITHUB_TOKEN` scope, a Docker build.

---

## 11. Operational resilience (Phase 10)

**Good:**
- single-instance lock with exit codes 0/1/2/3, including partial-failure 2;
- idempotent re-runs;
- `atomic()` transactions;
- backdated-run rejection;
- withdrawal events scoped to banks that were collected;
- suspicious moves excluded from market activity;
- best-effort events and alerts with deadlines.

**Gaps:**
- quarantine with no recovery path (DATA-002);
- no overall run deadline or stall alert (DATA-008);
- an uncaught DB lock (DATA-009);
- backups that are manual and unverified (DATA-017);
- freshness shown as "Fresh" after a total failure (DATA-004);
- the dashboard opens the store read-write (SEC-008).

There is no metrics or tracing stack beyond structured logging (`observability/logging.py`),
which is reasonable for a single-user local tool. The grid search is not a performance concern
(section 5.6).

---

## 12. Previous-audit regression register

| Source | Item | Status at 62b81d2 |
|---|---|---|
| Codebase review 2026-05-18 | P0-1 `datetime.utcnow` | **Fixed and verified.** Only local `_utcnow` helpers remain |
| | P0-2 `_parameter_family("baseline")` | **Fixed and verified** (`diagnostics.py`) |
| | P0-3 `competitor_rates.enabled` | **Fixed and verified** (`false`) |
| | P1-1 `excess_deposit_income` in sensitivity columns | **Fixed and verified** |
| | P1-7 reference rate overwritten by competitor | **Still the behaviour.** Now commented in `apply.py:34`; documented |
| | P1-8 funding ratio soft vs hard | **Fixed** (`model_governance.md:181-183`) |
| | P2-4 multi-period | **Implemented, but economically inconsistent** (FIN-002) |
| | P2-5 capital/RWA hurdle | **Implemented, with limitations.** Tier-1 ratio hard-coded; ROE floor fail-open (FIN-004, OPT-001) |
| | P2-8 segmented pricing | **Implemented, but sub-optimal and greedy** (OPT-002) |
| Diagnostic review 2026-04-26 | Grid guard, duplication, import hack | **Fixed and verified** |
| | Adaptive boundary expansion | **Fixed and verified** (challenge K) |
| | Document wholesale × penalty interaction | **Apparently fixed.** One section is now stale (FIN-005) |
| Pricing-intelligence audit 2026-09-06 | Self-bank exclusion | **Fixed and verified** (the legacy path has none; matching is brittle, DATA-014) |
| | Suspicious repeat becomes trusted | **Fixed and verified.** Over-corrected: no approval path (DATA-002) |
| | BNZ Standard parsed as special | **Apparently fixed** |
| | Low-equity condition inverted; mutable mortgage identity | **Fixed and verified** / **apparently fixed** |
| | NaN/inf inputs (ingestion); backdated run; malformed terms | **Fixed and verified.** New term edge cases in DATA-011 |
| | Stale/partial dashboard misleading | **Partly fixed** (DATA-004) |
| | Canary hides partial failure; CI excludes live tests | **Fixed and verified** |
| | CLI unknown states; U7 format checks | **Unverified** (not re-checked) |
| | U1 eligibility | **Still open** (DATA-013) |
| | U2 attempted-fetch ledger | **Partly fixed** (DATA-004, DATA-010) |
| | U3 quarantine approval | **Still open, with worse demonstrated consequences** (DATA-002) |
| | U4 overall deadline | **Still open** (DATA-008) |
| | U5 compact history; U6 coarse parser version | **Still open** (by design / `models.py:12`) |
| | Unattended monitoring readiness | **Not ready** |

There are **no regressions** in the sense of fixed behaviour breaking again. Three items were
implemented with new defects: P2-4, P2-8 and the September U3 over-correction.

---

## 13. Production-readiness assessment

**Assumption:** "production" means pricing recommendations that a real NZ bank's pricing
committee would rely on, or an unattended collector whose outputs feed decisions.

**Verdict: NOT READY.** Production blockers:
1. Uncalibrated economics drive the recommendation (FIN-001, FIN-003): ECL, deposit-pool scale
   and FTP.
2. Governance fails open on ROE, sign, degenerate results and loadable-but-wrong champions
   (OPT-001, GOV-001, GOV-002).
3. The ML path cannot represent competitor response or the priced rate range (MOD-001,
   MOD-002), and validation is leaky (MOD-004).
4. Ingestion silently drops data and cannot recover from quarantine (DATA-001, DATA-002).
5. The UI shows conflicting and misleading "optimal" labels (UX-001, UX-003).
6. Services are unauthenticated and bind all interfaces by default (SEC-001, SEC-003).

**Acceptable today, as designed:** supervised, local, single-user exploration of synthetic
scenarios and of public advertised-rate history, with a human reading every number.

The governance labels RECOMMENDED / REVIEW_REQUIRED / BLOCKED describe **engineering controls on
synthetic output**. They are not model-risk approval and they are not regulatory compliance
(GOV-009).

**What should not be automated or deployed yet:**
- any automated publication or pricing action driven by "Model optimum";
- Telegram or webhook alerts that include pricing recommendations, until OPT-004 and UX-001 are
  fixed;
- ML champion pricing;
- unattended TD benchmarking;
- the API on any non-loopback interface;
- the legacy `competitor_rates` strict path for term deposits.

---

## 14. Unverified items and residual risks

- **Live bank pages:** whether TD pages carry "special" rows, balance tiers or abbreviations;
  browser readiness and partial renders; Akamai blocking from the scheduler host; current page
  structures against the fixtures; eligibility footnotes.
- **RBNZ OCR:** current value and in-month review lag.
- **Calibration:** NZ mortgage ECL, deposit supply and elasticity levels. All are judgemental;
  none were estimated.
- **Rendering:** real browser, mobile, accessibility and screen-reader behaviour.
- **Deployment:** network binds, Docker build, GitHub token scope, branch protection.
- **Python:** 3.9 compatibility (TEST-003).
- **Not run:** `examples/run_baseline.py` and `train_response_models.py`. Equivalent calls were
  verified.
- **Regulatory and conduct:** fair-customer-outcome and NZ regulatory review (CCCFA, CoFI, RBNZ
  capital). Out of scope for engineering review and **not assessed**. No compliance claim is
  made.

---

## 15. Checkpoint

**Status: complete.** All phases (0–12) were executed.

The delegated workstreams could not write files. Their full reports were transcribed and their
scripts and outputs preserved in `docs/audits/2026-11-13-evidence/{ingest,mlgov,uxsec}`.

To resume or extend, start from `docs/audits/2026-11-13-full-repository-evidence.json` and the
roadmap.
