# AI Bank Pricing Engine — Agent Instructions

> Canonical instructions for Claude Code and Codex. `CLAUDE.md` imports this
> file with `@AGENTS.md` — edit rules here, not there.

## What this project is

Python retail bank balance-sheet pricing engine. **Synthetic-data only.** **Portfolio-level** (one home-loan rate, one term-deposit rate) — not personalised. NZ context (default competitor banks: ANZ, ASB, Westpac, BNZ, Kiwibank).

The engine maximises expected risk-adjusted profit subject to a funding-shortfall penalty, via exhaustive joint grid search with adaptive boundary expansion. A separate diagnostic ML layer trains response models on simulated outcomes and checks they recover sensible rate-response behaviour.

## Module map

The per-module reference — responsibilities, seams, and the invariants each module carries — is in [docs/architecture.md → Module map](docs/architecture.md#module-map-agent-reference). Read the entry for every module you touch before editing it; **When making changes** below applies regardless.

Full flow diagram: [docs/architecture.md](docs/architecture.md) (includes the target-architecture Mermaid diagram). Modelling assumptions: [docs/modelling_assumptions.md](docs/modelling_assumptions.md). Governance: [docs/model_governance.md](docs/model_governance.md). Response model card: [docs/response_model_card.md](docs/response_model_card.md). Competitor ingestion: [docs/competitor_rate_ingestion.md](docs/competitor_rate_ingestion.md). Pricing intelligence: [docs/pricing_intelligence.md](docs/pricing_intelligence.md). Deployment: [docs/deployment.md](docs/deployment.md). Architecture decisions: [docs/adr/](docs/adr/).

## Response model — dual elasticity

Both loan acceptance and deposit uptake use **two elasticities** (see [configs/base.yaml](configs/base.yaml) `response:` block):

- `loan_rate_elasticity` — own-rate sensitivity (negative for loans).
- `loan_competitor_gap_elasticity` — sensitivity to gap vs competitor loan rate.
- `deposit_rate_elasticity` — own-rate sensitivity (positive for deposits).
- `deposit_competitor_gap_elasticity` — sensitivity to gap vs competitor deposit rate.

Refactors that collapse these into a single elasticity term are **not equivalent** and will silently change recommendations.

## Commands

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/run_governed_scenario.py
python3 examples/run_with_competitor_rates.py
python3 examples/run_llm_diagnostics.py
python3 examples/run_multiperiod.py
python3 examples/train_response_models.py

# Optional FastAPI serving layer (requires the `api` extra):
python3 -m pip install -e ".[api]"
python3 -m balance_pricing_engine.api.server   # loopback default; both pricing POST routes + probes

# Docker (see docs/deployment.md):
docker build -t ai-bank-pricing-engine .
export PRICING_ENGINE_API_KEYS="$(openssl rand -hex 32)"
docker run --rm -p 127.0.0.1:8000:8000 -e PRICING_ENGINE_API_KEYS ai-bank-pricing-engine

# NZ pricing-intelligence CLI:
python3 -m balance_pricing_engine.pricing_intelligence collect --render-javascript
python3 -m balance_pricing_engine.pricing_intelligence snapshot --term 12
python3 -m balance_pricing_engine.pricing_intelligence changes --days 7
python3 -m balance_pricing_engine.pricing_intelligence market-state
python3 -m balance_pricing_engine.pricing_intelligence ocr
python3 examples/run_market_intelligence_scenarios.py

# Unattended/scheduled collection (see deploy/scheduling/):
python3 scripts/collect_market_rates_workflow.py --render-javascript --summary-file data/processed/pricing_intelligence/last_run.json

# Competitor-rate CLI (separate subcommand surface):
python3 -m balance_pricing_engine.competitor_rates.cli fetch    --output-dir data/processed/competitor_rates
python3 -m balance_pricing_engine.competitor_rates.cli validate --source-file <path>
python3 -m balance_pricing_engine.competitor_rates.cli apply    --config configs/base.yaml
python3 -m balance_pricing_engine.competitor_rates.cli report   --source-file <path>
```

`live_rates` pytest marker is opt-in — fetches **real** bank pages. Do not run in CI or without intent. To exclude it explicitly: `python3 -m pytest -q -m 'not live_rates'`.

## Configuration

[configs/base.yaml](configs/base.yaml) is the canonical config. Key shapes:

- `random_seed: 42`, `simulation.n_loan_customers: 5000`, `simulation.n_deposit_customers: 5000`.
- `market.*` includes `cash_rate`, `wholesale_funding_spread`, competitor rates, and reference rates required when using `pricing_strategy`.
- `response.coefficients.loan.*` and `response.coefficients.deposit.*` — intercepts, rate anchors, and feature coefficients wired into `synthetic/response.py` (see `ResponseCoefficientsConfig`).
- Choose **one**: `pricing_strategy.{home_loan,term_deposit}_spread_bps` (market-relative, default) **or** `products.{loan,deposit}_rate_grid` (absolute).
- `competitor_rates.enabled: false` by default — when true, reads from `data/processed/competitor_rates/latest.json` with staleness + strict-mode checks.
- `balance_sheet.required_deposit_funding_ratio` and `funding_shortfall_penalty_rate` drive the funding penalty.
- Economic models reject NaN/±infinity, including `model_copy(update=...)`. Both loan elasticities must be ≤ 0; both deposit elasticities must be ≥ 0. Keep finite negative wholesale spreads warning-only. Scenario entry points revalidate existing instances before pricing.
- LLM scenario patches use the explicit numeric `ALLOWED_PATCH_PATHS` allow-list; elasticities, coefficients, governance, paths and execution controls are forbidden. All application methods require affirmative approval plus a named, timestamped `PatchApproval` bound to exact patch content. Preserve atomic full-config validation and separate JSONL proposal/rejection/approval/application outcomes. See [docs/model_governance.md](docs/model_governance.md#financial-inputs-and-llm-scenario-patches-opt-003-gov-007).

## Conventions (observed)

- Python ≥3.9, ruff `line-length=100`, `target-version="py39"`.
- Pydantic **v2**. NumPy pinned `<2.0`. pandas ≥2.1. scikit-learn ≥1.4. joblib ≥1.4.
- `pythonpath = ["src"]` (set in [pyproject.toml](pyproject.toml)) — import as `balance_pricing_engine.*`, **not** `src.balance_pricing_engine.*`.
- Tests live under [tests/unit/](tests/unit/) (one module per source file). Fixtures under [tests/fixtures/](tests/fixtures/).
- Workflow scripts (long-running) → [scripts/](scripts/). Short demos → [examples/](examples/).
- Data folders: [data/raw/](data/raw/) (untouched inputs), [data/processed/](data/processed/) (cleaned), [data/outputs/](data/outputs/) (run artifacts).
- Notebook walkthrough: [notebooks/bank_pricing_engine_walkthrough.ipynb](notebooks/bank_pricing_engine_walkthrough.ipynb).

## Regression contract — do not break

`ScenarioDiagnostics.write_artifacts()` produces a stable artifact set consumed by regression checks:

- `grid_results.csv` — every final evaluated rate pair (columns enumerated in `GRID_RESULT_COLUMNS`).
- `top_results.csv` — top-N by `total_objective`.
- `sensitivity.csv` — when sensitivity provided (includes `delta_loan_rate_bps`, `delta_deposit_rate_bps`, `delta_objective_pct`, `parameter_family`).
- `counterfactuals.csv` — when `counterfactual_decompositions()` is called.
- `cohort_attribution.csv` — when `cohort_attribution()` is called.
- `summary.json` — keys include `best_loan_rate`, `best_deposit_rate`, `objective_value`, `expected_loan_volume`, `expected_deposit_volume`, `funding_shortfall`, `excess_deposit_income`, **`boundary_warning_flag`**, **`initial_boundary_warning_flag`**, **`expansion_rounds`**, `expansion_capped`, `policy_band_breached`, `roe_constraint_relaxed` (additive: true only when a configured `min_return_on_equity` had no finite qualifying candidate in the **final** grid and selection fell back to the unconstrained best).

The boundary fields distinguish "needed expansion" from "still hitting boundary after expansion" — preserve their semantics when refactoring. `excess_deposit_income` credits surplus deposits at `cash_rate` when deposit volume exceeds loan volume.

`decision_record.json` (written by `application.PricingService` when `persist_ledger=True`) is an **additive** artifact on top of this contract — never rename/restructure the files above to make room for it, and never let a decision-record change alter `summary.json`/`metadata.json` semantics.

## When making changes

- Supported API serving goes through `api.server`: numeric loopback by default; every other binding requires valid `PRICING_ENGINE_API_KEYS`. Raw uvicorn launches bypass launcher policy and are unsupported. Keep both POST routes behind the same key, body/YAML, customer, grid/expansion/sensitivity and path-override boundaries in `api/limits.py`; these are delivery limits, not changes to trusted CLI capacity or pricing mathematics. Preserve exclusive private temporary files and cleanup for both API inputs and effective competitor overlays. API errors must not reflect raw YAML, exception text or absolute server paths.
- Honour the synthetic-data-only invariant. Do not add real customer or counterparty data paths.
- Keep portfolio-level scope unless the user explicitly opens up personalised pricing.
- The LLM layer (`llm/`) is decision-support only: every response is tagged `[DECISION-SUPPORT: requires human review]`; never wire its output into automated pricing decisions.
- `OptimizationResult` is a frozen dataclass; backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`, `loan_income_after_losses`) exist deliberately — don't remove without checking call sites.
- Modelling layer is **diagnostic by default, but no longer hard-walled off from the optimiser.** `run_baseline_diagnostics(registry=...)` resolves an **approved registry champion** via `ModelScoringAgent.resolve_response_fn(...)` and wires it into the grid search through `MLResponseFnAdapter` (see [optimization/response_fn.py](src/balance_pricing_engine/optimization/response_fn.py)). Governance invariants — **preserve these**: with no champion, or an *unapproved* champion, the path falls back to the synthetic response and the price is unchanged; an *approved* champion whose artifact is missing, unloadable, or fails integrity/scoring-contract validation raises `GovernanceError` (**fail loud — never silently reprice on the synthetic fallback**). Approval (`approve_model` / `promote_champion`, which take `product="loan"|"deposit"`) records `artifact_sha256`, ordered `feature_names`, `n_features` and `product`; resolution verifies the hash **before** deserialising, then the interface, feature names/order and the rate-response probe (`modelling/champion_integrity.py`). Never backfill a hash at scoring time, never infer product from a model name, and keep legacy approvals without integrity metadata failing closed until explicitly re-approved. Don't conflate ML response models with the synthetic response functions used inside the grid search. The single `train()` entry point in `agentic.py` replaced the former dual `train` / `train_and_recommend` API; `evaluate_binary_classifier` is a backward-compat alias for `evaluate_model` that emits a `DeprecationWarning`; use `evaluate_probabilities` / `evaluate_model` from `evaluation.py` for new code.
- Competitor-rate ingestion is gated behind `competitor_rates.enabled` and `strict` flags. Strict-mode failures should error, not warn.
- `pricing_intelligence` must stay one-directional: it may read `competitor_rates` and `config`, but nothing in `optimization/`, `pricing/`, or `scenarios/` may import it except through `pricing_intelligence.strategy`. Keep "what is the market doing?" separate from "what should we price?".
- `strategy.apply_market_strategy_to_config()` under `CompetitorRateStrategy.MANUAL` **returns the config object unchanged** (not a copy). Every pre-existing scenario depends on that; don't "tidy" it into an unconditional `model_copy`.
- `strategy.apply_cash_rate_strategy_to_config()` holds the same invariant for `market.cash_rate` under `CashRateStrategy.MANUAL` — unchanged config object, not a copy. It is a separate seam from the competitor-rate one above (a different data source, RBNZ vs the bank collectors), not a parameter bolted onto `apply_market_strategy_to_config`.
- `pricing_intelligence.rbnz` parsing anchors on the "Official Cash Rate" heading text, never on the first table matching the expected `Date | series | series` column shape — the RBNZ page renders several charts (OCR, FX rates, TWI) with identical accessible-table shapes, and a numeric plausibility check alone cannot tell them apart (NZD/USD values already sit in a range a naive OCR bound would accept). Same category of defect as the PIE-effective-rate rows in term-deposit collection.
- Change detection must never turn a collection failure into a market event: withdrawal events are scoped to banks that were successfully collected (`detect_changes(banks_collected=...)`). An unreachable bank is unknown, not withdrawn.
- Benchmark coverage (DATA-003): every market-derived strategy (`MARKET_MEDIAN/MEAN/LEADER/SECOND_LOWEST`) needs `pricing_intelligence.min_banks` (default 3) **distinct eligible** banks per product and term, enforced in `strategy.resolve_competitor_rate` -- the one seam every caller (service, CLI, `run_triggered_scenarios`, Pricing Studio, joint overlays) goes through. Count banks after every trust/comparability rule and the own-bank exclusion (`coverage.assess_benchmark_coverage`), never records; never admit a special, a quarantined/untrusted reading or a stale rate to reach the minimum. Below it raise `InsufficientCoverageError`, never substitute the configured rate; descriptive views may show the observed rates with a warning. `min_banks=None` means the documented default, so direct callers cannot bypass the policy by omission. MANUAL needs no snapshot and keeps returning the same config object. Report the oldest contributing input and its age against an explicit evaluation time (`MarketSnapshot.evaluated_at`: `now` live, the run start for `as_of_run_id`), never the wall clock during reconstruction; `as_of` stays the newest input.
- Operational freshness (DATA-004): a failed attempt writes no `collection_runs` row, so collection outcome comes from the product's latest `operational_runs` attempt (`store.get_latest_run_record`), combined with observation age only through `health.assess_operational_freshness`. Keep observation freshness, collection health and benchmark coverage separate facts. No ledger row is UNKNOWN (old observation rows never prove success); a failed/not-attempted bank with in-window rates is DEGRADED; never rewrite observation timestamps, never treat a failed attempt as a refresh, and keep products independent. `SystemHealth.fresh_bank_count` is rate age only; displays of "fresh" banks use `confirmed_fresh_bank_count`.
- Suspicious movements are excluded from `compute_market_activity` (and therefore the market-state classifier) by default. A parser fault must not be able to fake a repricing round.
- Basis points come from `pricing_intelligence.bps` (Decimal). Don't compute them with float subtraction anywhere they reach an event, snapshot, or alert.
- Quarantine review (DATA-002) is operator-only and append-only: `quarantine_reviews` is never updated or deleted (triggers enforce it), collection must **never** approve, reject or void anything, and nothing -- repetition, the peer median, an LLM -- may substitute for an operator decision. Observation rows are never rewritten to reflect a decision: effective trust is the stored `validation_status` overlaid with the ledger *as of the view's time* (`store._effective_observations`), so historical (`as_of`) views must not show a reading approved or voided before the decision. A decision names one exact `rate_changes.id` (never "the latest suspicious event"); REJECT = never trusted, VOID = previously trusted and now withdrawn; both are terminal, and a reading an operator rejected or voided stays untrusted when the same text repeats. Mutating review commands require the hashed-credential check in `review_auth.py` (the token is never a CLI argument, never logged) and record the *authenticated* identity. An approval confirms evidence, not a rate's attractiveness, and never changes a price.
- The peer-median check (`peer_plausibility.py`, DATA-016) is a **review signal**: it quarantines, never rejects or approves, and abstains (`insufficient_evidence`) rather than fabricate a median below `min_peers`. Mortgage and deposit leader sides differ (deposits: above peers; loans: below) -- don't make the bounds symmetric. Balance tiers are bands of the advertised minimum, not exact equality.
- Quarantine escalation counts **distinct** collection runs (`observation_count`; a replayed `run_id` is inert) and only upgrades an otherwise-successful run to exit 2 -- a total failure (1) keeps precedence. Escalation alerts dedupe through `quarantine_escalation_alerts` (once per observation and channel, only confirmed deliveries recorded) and must stay secret-free.
- Rate-tier categorisation is **per-bank**: live pages carry the tier word in the table heading, not the row, so each collector declares `default_fixed_category` for what an unmarked fixed row means (`special` for ASB/BNZ, `standard` for Westpac, `None` where the bank labels both tiers in-row). Don't try to infer this generically — getting it wrong silently drops banks from the market comparison.
- `excluded_deposit_product_markers` does the same on the deposit side for managed-fund/KiwiSaver returns and PIE "effective rate of return" columns — deposits rank highest-is-best, so an inflated row wins market-leader. Non-comparable records are exempt from the conflicting-observation guard: they collide by construction and never reach a statistic.
- A deposit table whose header reads "Rate of return"/"Return Frequency" is a PIE fund table (`pie_term_deposit`) even when no cell says PIE (ANZ, BNZ, Westpac); "Effective rate of return" alone is not. A day-based term maps to months only within 7 days of a whole month (`normalise._whole_months`) — rounding BNZ's 7-day rate to "1 month" collided with its 30-day rate and failed the bank in validation.
- Term-deposit specials (DATA-001) keep `rate_category="special"`. Never relabel them as `term_deposit`. Only a special that `pricing_intelligence/deposit_specials.py` classifies as `comparable` enters the default term-deposit view. That needs explicit availability evidence on the record and no disqualifying evidence, and it goes through `build_market_snapshot(admitted_keys=...)`. An `uncertain` special is excluded **and** degrades the run (exit 2, `special_comparability_uncertain`, bank stays in `banks_succeeded`). A `not_comparable` special is an understood exclusion, not a degradation. Never treat absence of restriction text as eligibility. A negated availability phrase ("Not available to all customers") is evidence against eligibility, never for it, and so is one naming a restricted audience ("Available to all new customers": `restricted_availability`); an unrecognised audience qualifier is uncertain, never evidence. Every word left in the availability clause counts, before or after the audience noun ("all customers aged 65 and over"), and before an audience marker back to its preposition ("to eligible new and existing customers"); only "new and existing", "our/of/the" and the bank's own name leave it unrestricted. A bank whose only accepted rows are uncertain specials stays in `banks_succeeded` (no `no_comparable_rates` collector failure). Health freshness and stale exclusions use the same admissions as the snapshot (`admitted_special_keys`). Market activity/state, dashboard move counts and briefs read `service.get_market_changes`, which counts an admitted special's rate moves by its admission as of the run that recorded each move; don't count activity from a raw `get_changes(rate_category=...)` call. A term-deposit special's `source_text` is material (a reworded special is a new state), so historical snapshots and the history series reassess it from the text current at that point; never reassess history from placeholder or later text. A "special" row in a PIE return table is `pie_term_deposit`, never a special. A term-deposit row with a term but an unreadable rate goes to `skipped_row_keys` (partial, not withdrawn); this is checked per rate cell (`extractors._unreadable_rate_cells`), so a readable sibling cell never hides an unreadable one, and a heading naming a special over a short placeholder like "N/A" counts. Each unreadable cell is keyed from its own heading, never the joined header row (the first balance tier would win), and carries the category it would have parsed as (`models.skipped_row_key`, only when unambiguous) so an unreadable standard never shields a withdrawn special in the same tier, or vice versa; and a cell under a descriptive heading ("Special conditions", "Availability") is never a rate cell, however short; nor is a term or amount cell ("12 Months" under "Special term", "$5,000"). A dash (or blank rate slot) means not offered, wherever the rate column sits, but only in a rate slot: a dash under a descriptive heading ("Notes") never withdraws anything (`extractors._descriptive_heading` is the one column-semantics test). Category-level coverage loss (`low_coverage_categories`) is partial too.
- The legacy `competitor_rates` path (Phase 2d) delegates policy and must not grow its own. It
  uses the collectors' `enrich_term_deposit_record()` fence at `fetch` and again at resolution;
  Phase 2b special comparability (only `comparable` specials, `uncertain` ones warn); the
  `pricing_intelligence.min_banks` minimum for every `market_*` strategy in strict and non-strict
  mode; one most-competitive representative per bank before any aggregate (equal bank weights,
  never row-weighted); and exclusion of `warning`/`failed` readings. Warnings and provenance
  travel on `ResolvedCompetitorRates` (`apply_competitor_rates_with_resolution`) into the
  `COMPETITOR_RATE_FRESHNESS` gate evidence. Never write warning text into the numeric config, and
  never put an absolute path in `provenance()`. Duplicate identity is `observation_key()` (plus row
  text for `non_comparable`); a conflict or an exact repeat fails validation. `latest.csv` cells go
  through `store.neutralise_csv_cell`; the JSON artifacts keep the original text.
- Deposit reference depositor (DATA-005): every category-filtered term-deposit view the service builds (live, previous run, `as_of_run_id`, history series, stale exclusions, health) applies `pricing_intelligence.deposit_reference` inside `build_market_snapshot` via `deposit_reference.assess_reference_eligibility` -- the one eligibility function; don't re-implement it. Published bounds are inclusive; a missing bound is not zero; an `unknown` frequency is never at maturity; a known incompatible frequency is excluded unless explicitly listed in `fallback_frequencies`; no annual-equivalent conversion. Unstated tiers/frequencies are admitted only under the `admit_unstated_*` flags and always disclosed (`BankTermRate.eligibility_note`, rationale, `BenchmarkCoverage.eligibility_caveat_banks`). A Phase 2b comparable special must pass the same check. Representative selection is a total order (frequency match, rate, category, name, key) -- never row order. Ineligible rows stay stored and listed in `reference_exclusions`; `all_tiers=True` / `rate_category=None` is the descriptive view (`deposit_reference=None`), never the benchmark.
- Mortgage eligibility provenance (DATA-013): `CompetitorRateRecord.lvr_eligibility_basis()` / `eligibility_inferred` are derived from the record's own text and LVR fields, never stored and never part of `observation_key()`. Don't add a column or let a serialised value override them; don't change the inferred 80%/20% values.
- Bank identity (DATA-014): `our_bank`, `selected_banks` and the legacy `bank` are canonicalised by `competitor_rates.bank_identity` from collector-declared `bank` + `bank_aliases` only. No fuzzy matching, no ad hoc alias maps elsewhere; unknown or duplicate names fail validation. Exclusions use `snapshot._banks_match` (= `same_bank`).
- `excluded_product_markers` fences subsidised/green/top-up lending, bridging finance, and closed products into the `non_comparable` category. They stay recorded for audit but must never reach a market statistic — a subsidised 1.00% rate would wreck any median it entered.
- The dashboard's Run Pricing Watch and Dry Run collect **both** markets by default (one `--product` subprocess each, in sequence). The combined status is never better than the worst market's — don't let a successful home-loan run report success over a failed term-deposit run, and don't merge the two markets' bank results into one list.
- Browser readiness (DATA-007) is product-specific: a page is ready only when it names the product being collected **and** the bank's own parser yields at least `readiness_min_rate_rows` comparable rows. Rates for the other product never make a page ready (`WRONG_PRODUCT`). A readiness timeout or partial render is a classified per-bank failure (`ReadinessTimeoutError` / `PartialRenderError`), never ignored or downgraded to success. Never add a step that bypasses a CAPTCHA, login, bot check or 403.
- Collection deadline (DATA-008): scheduled runs go through the supervisor (`pricing_intelligence/supervisor.py`), which terminates the worker's whole process tree at `collection_run_timeout_seconds`; don't replace it with GNU `timeout` (absent on macOS) and don't add `--no-supervisor` to a schedule. A timed-out worker must not write after the deadline; the supervisor writes the `collection_timeout` summary and claims `run_record_persisted: true` only when the row was actually written. Exit-3 escalation state lives in the `<lock>.skips.json` sidecar, per product, never in SQLite; a repeated run id is counted once. A timeout or worker failure after an observation commit still needs a FAILED operational attempt when no RunRecord exists: preserve committed product counts and collection linkage, keep observation timestamps unchanged, and mark requested banks operationally failed so health degrades. Never replace an existing RunRecord; protect the check/insert with the store write transaction.
- SQLite (DATA-009): file-backed writable stores use WAL plus a bounded busy timeout; WAL does not remove single-writer contention. Classify only SQLite BUSY/LOCKED as `database_locked` (`sqlite_errors.is_database_locked`), never corruption, disk-full, permission or schema errors. No unbounded retries, and no shared global connection.
- CLI `collect` (DATA-010) takes the product's shared lock before opening a writable store and goes through `run_operational_cycle` (RunRecord, 0/1/2 classification, exit 3 with no fetch on contention). `--no-persist` is a lock-free, read-only preview that writes nothing.
- Live collection requires `--render-javascript` (the `[browser]` extra). Every tracked bank builds its rate table client-side; the static-HTML path returns `ParserError` for all five.
- Scheduled collection goes through `scripts/collect_market_rates_workflow.py`, which holds a single-instance lock and returns distinct exit codes (0 ok, 1 total failure, 2 **partial** failure, 3 already running). Don't collapse 2 into 0 — an incomplete market view computes a median over fewer banks than it reports.
- `.github/workflows/collector-canary.yml` reaches live bank sites and must stay out of `ci.yml`; it is a breakage canary, not a history store. Durable observation history is local and git-ignored by design.
- Prefer `application.PricingService` over calling `run_baseline_diagnostics()` directly from new callers (API endpoints, CLI commands). It wraps, not replaces, the domain math — do not duplicate governance/lineage/event logic at a new call site instead of going through it.
- `GovernanceError` must keep propagating out of `PricingService.run_scenario()` unchanged — never catch it there and turn it into a `BLOCKED` `PricingDecision`. Only a delivery layer (e.g. the `/v1/pricing/scenarios` FastAPI handler) may translate it into a transport-appropriate error response.
- `governance.gates.evaluate_gates()` policy is intentionally conservative but not maximal (any `BLOCK` → `BLOCKED`; otherwise any `WARN` → `REVIEW_REQUIRED`; else `RECOMMENDED`) — don't add a new gate that blocks on a mere warning-level condition without updating this documented policy.
- Segmented pricing (OPT-002, Phase 3a) optimises tier rates jointly on the full portfolio objective in `optimization/segmented.py`. Never reintroduce a per-tier objective, a sequential/accumulated deposit funding benefit, or a per-tier ROE floor. One loan and one deposit tier must reproduce `optimize_joint_rates` (rates, objective and every component) on the same grid — always solve that case exactly. Report coordinate descent as a best-known result (`optimality_proven: false`), never as optimal; keep `segmented_solver` in `summary.json` additive. Expanded rounds keep the previous winner as a candidate (an expanded grid can drop it), so expansion never worsens the (meets ROE floor, objective) key.
- Multi-period roll (FIN-002, Phase 3b): `scenarios/multiperiod.py` books each period's winner as a `cohort_accounting.BookedCohort`. Surviving cohorts keep their booked loan rate, deposit rate, ECL rate and capital basis; never reprice them at the cash rate, a reinvestment spread or a drifted market rate without an explicit rollover mechanism. Wholesale funding, excess-deposit income and the shortfall penalty are computed once on aggregate active balances (`evaluate_active_portfolio`), never per cohort and summed. One cohort at full survival must reconcile to its single-period objective (checked at run time). `back_book_incremental_contribution` = active - new business, a portfolio attribution, not standalone cohort profit. `period_summary()`'s first five columns keep their meaning (`total_objective` = new-business objective). `reinvestment_spread_bps` stays accepted but unapplied. Competitor drift moves the reference rates unless `reference_rate_drift: hold`. It is a rolling synthetic projection with synthetic response (no registry), not an ALM forecast.
- `ROE_FLOOR_MET`, `OBJECTIVE_POSITIVE` and `VOLUME_NONZERO` are WARN-level gates (OPT-001/GOV-002): a relaxed ROE floor, a zero/negative objective, or zero/non-finite loan or deposit volume must never reach `RECOMMENDED`. `ROE_FLOOR_MET` must not PASS on missing or non-finite ROE evidence when a floor is configured. Keep `select_best_result()` returning `OptimizationResult`; the fallback is disclosed through `select_best_result_with_disclosure()` → `SelectionResult`, not by changing which row wins.
- `decision_record.config_hash` and `metadata.config_hash` are the canonical hash of the **effective validated** config (`config_identity.effective_config_hash`), computed after overrides and competitor-rate resolution; `source_config_file_sha256` is the separate original-file hash. Never hash the source file or the temp overlay as `config_hash`, never add a second differently-defined `config_hash`, and keep `effective_config.json` byte-identical to the hashed canonical JSON. Configuration identity is not model approval.
- `events.publisher.publish_safely()` must keep swallowing publisher exceptions (logged, not raised) and enforcing its `deadline_seconds` wait cap — a broken *or slow/hung* event publisher must never fail, block, or alter a pricing decision.
- `pricing_intelligence.alerts.dispatch_alerts_safely()` follows the same rule for webhook alert delivery: a broken or unreachable webhook is logged and must never change a scheduled collection run's exit code. Only rate moves are pushed to the webhook — a new/withdrawn product is a change but not a reprice — matching the CLI's own `--alerts` scope. `PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL` unset is the default state; nothing else about collection changes when it's absent.
- Scenario reporting preserves each run's own policy, initial/final boundary, expansion, ROE-relaxation, shortfall and volume evidence. `ScenarioComparison.median_reference` refers to Match median, never a fifth strategy; `model_optimum` is a legacy alias. Selected model-best and median reference outputs must stay explicitly distinct. Missing evidence requires review; diagnostic summaries never grant approval or replace full governance gates. Product-aware strategy labels come from `scenario_reporting.py` (deposits: highest/second-highest).
- Dashboard pages **render only**: no freshness, ranking, bps, market-state or scenario economics in `dashboard/views/` — read them from `DashboardServices` (which calls `pricing_intelligence/health.py`, the service and `strategy`). `pricing_intelligence/health.py` is the single owner of freshness/health semantics; don't re-derive a threshold in the CLI, a frame or a page. `pricing_intelligence` must never import `dashboard`.
- Reading never collects: only `dashboard/views/operations.py` may import `dashboard/actions.py` (and only the `views/operations_trust.py` composer may import that page), and only an explicit button click (an `on_click` callback) may start a collection — a rerender or page navigation must not. Don't weaken the Operations controls: the optional `PRICING_WATCH_OPS_TOKEN` gate (minimum 16 characters, fails closed, process-wide unlock limiter), the single-flight runner and 60 s cooldown, the 10-minute backup limit (backups are pruned to the newest 30) and the Telegram cooldown.
- Token-free Operations require an effective numeric loopback Streamlit binding. Every administrative callback must recheck authorisation. Non-loopback, hostname or unknown bindings require a valid Operations token and session unlock; the token does not authenticate read-only pages. Preserve `.streamlit/config.toml` and supported launch commands binding to `127.0.0.1`. Remote dashboards need independently authenticated ingress covering all pages.
- Dashboard page modules live in `dashboard/views/`, never `dashboard/pages/`: a `pages/` folder beside `app.py` puts Streamlit into legacy multipage mode until `st.navigation` first runs, which breaks deep links (`tests/unit/test_dashboard_redesign.py` guards this). Shared components (`ui.py`) are scanned like pages for untrusted text; design tokens (`theme.py`) and the Command Centre sentences (`insights.py`) are Streamlit-free and template backend values only — no causes, forecasts or recommendations.
- The dashboard is dual-product. Every `DashboardServices` market read takes `product_type` and must pass it to **every** backend call it makes (snapshot, exclusions, quarantine, activity, changes, history incl. `term_market_from_rates`, health); pages pass it positionally through `cached` so it keys the cache. Never derive ranking direction or leaders in the UI (deposits rank highest-first in the backend), never show a rate without naming its product (`dashboard/products.py`), and never substitute one product's data for the other's missing data. Pricing Studio's term-deposit and joint scenarios go through the same seams as the home-loan one; each scenario type keeps its own stored result.
- A stored Pricing Studio result renders only while `dashboard/freshness.py` says it is current (request, database stamp, config fingerprint and market terms all match; pre-provenance session entries are stale). Anything else is hidden, never captioned-and-shown, and never silently recomputed. Modelled output carries `ui.modelled_badge()` and observed market data `ui.observed_badge()`; never label a quarantined fallback as a newly observed price or synthetic output as observed.
- Dashboard provenance is committed content, never the main SQLite file's mtime or a fresh connection's `data_version`. `pricing_intelligence/revision.py` owns the singleton token and exact INSERT/UPDATE/DELETE trigger coverage for every application table. Install tracking only on writable store initialisation, after migrations. Missing/invalid tracking is unknown: hide stored scenarios and bypass cached reads. Capture the stamp before scenario calculation. Dashboard data reads use `ObservationStore.open_reader()` (live `mode=ro`, bounded timeout, no migrations or copies); SQLite WAL/SHM coordination is allowed, application data/schema writes are not.
- Untrusted text (bank-page strings, error messages, `last_error`) is shown only in dataframe cells or passed to `st.link_button` as a validated URL — never through `st.markdown`/`st.write`/`st.info`/`st.warning`/etc. `tests/unit/test_dashboard_boundaries.py` enforces this.
- `Pricing Watch.command` is the private launcher: it must keep serving on `127.0.0.1` only (never `0.0.0.0`), run from the repository it lives in, and never print `.env` values. `tests/unit/test_dashboard_launcher.py` enforces this; keep it executable (git mode `100755`).
- Dashboard market scenarios deliberately go through `pricing_scenario_trigger.run_triggered_scenarios` and `run_baseline_diagnostics` (the same seam as the scheduled workflow and `examples/run_market_intelligence_scenarios.py`, no ML registry), not `application.PricingService`. That is intentional: they are decision support and are never persisted as pricing decisions.
- Update [docs/architecture.md](docs/architecture.md) and [docs/modelling_assumptions.md](docs/modelling_assumptions.md) when changing structural behaviour.

## Roadmap & recent reviews

- Roadmap: [docs/agentic_modelling_roadmap.md](docs/agentic_modelling_roadmap.md).
- Latest codebase review: [docs/codebase_review_2026-05-18.md](docs/codebase_review_2026-05-18.md).
- Diagnostic review: [docs/diagnostic_review_2026-04-26.md](docs/diagnostic_review_2026-04-26.md).
- Step-by-step business+technical walkthrough: [docs/usage_guide.md](docs/usage_guide.md) (includes restricted-network setup).
- Run ledger usage guide: [docs/run_ledger_guide.md](docs/run_ledger_guide.md) (how to produce, read, and sign off ledger artifacts).

## Context Navigation

<!-- context-navigation:start v1 -->
Follow the global context-navigation rule (`~/.claude/CLAUDE.md` for Claude,
`~/.codex/AGENTS.md` for Codex): vault first; Claude uses native search;
Codex uses codebase-memory-mcp (`detect_changes` first); Graphify only on request.
- `graphify-out/` is a dated snapshot, not auto-refreshed — check its age before trusting it; don't hand-edit it.
- codebase-memory-mcp is registered for Codex by this repo's `.codex/config.toml`, which Codex loads only for trusted projects.
<!-- context-navigation:end -->
