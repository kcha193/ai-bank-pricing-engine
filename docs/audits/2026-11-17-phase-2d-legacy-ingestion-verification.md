# Phase 2d verification: legacy competitor-rate ingestion hardening (2026-11-17)

Findings: DATA-006 (P2), DATA-018 (P3) and SEC-010 (P3) from
[2026-11-13-full-repository-audit.md](2026-11-13-full-repository-audit.md).
Base: `master` @ `28ccd14` (PR #74 merged). Evidence:
[`2026-11-17-phase-2d-evidence/`](2026-11-17-phase-2d-evidence/). Everything here was verified
offline on synthetic fixtures. **No live bank page was fetched; no live-rate validation is
claimed.**

## Architectural decision: retained, policy delegated

Active callers of the legacy path are `competitor_rates.cli apply`, `PricingService.run_scenario`
(and through it the `/v1/pricing/*` API whenever `competitor_rates.enabled`),
`examples/run_with_competitor_rates.py` and the audit scripts. Retiring the path would have
broken the API and CLI contracts, and no replacement reads the SQLite store into an
`EngineConfig` overlay through `PricingService`. The path is therefore **retained**. Its
validation and selection now **delegate to the trusted components**, so there is one economic
definition:

| Concern | Before | After (shared component) |
|---|---|---|
| TD categorisation | Generic parser only; fund/PIE-tax rows stayed `term_deposit` | `BankPricingCollector.enrich_term_deposit_record()` at `fetch` and again at resolution (now a classmethod; a bank without a collector gets the base vocabulary) |
| TD specials | Silently excluded (category mismatch) | `pricing_intelligence.deposit_specials` with the configured vocabulary and tier edges |
| Coverage | None: one bank could be a "market" | `pricing_intelligence.min_banks` (default 3) for every `market_*` strategy, strict and non-strict |
| Aggregation | Row-weighted | One most-competitive representative per bank (the snapshot's rule) |
| Untrusted readings | Counted | `validation_status` `warning`/`failed` excluded and warned |
| Duplicate identity | bank/name/category/label/term/frequency (no tier) | `observation_key()` (+ row text for `non_comparable`) |
| Warnings | Returned by the resolver, then dropped by the overlay | Carried on `ResolvedCompetitorRates` into the gate evidence and decision record, and logged by the plain overlay |
| `latest.csv` | Raw text | Formula-neutralised presentation copy |

No import cycle: `competitor_rates.apply` imports `pricing_intelligence.{config,coverage,
deposit_specials}`, none of which import `competitor_rates.apply`. (`balance_pricing_engine.config`
already imported `pricing_intelligence.config`.)

## Root causes

- **DATA-006 categorisation.** `cli._apply_bank_categorisation()` enriched home-loan records
  only (`cli.py:128-131` at base), so `fetch` stored BNZ fund returns as term deposits.
- **DATA-006 strict validation.** The duplicate key had no balance tier and no row identity for
  `non_comparable` rows. Simultaneous tiers, PIE tax-band columns and fund rows all "collided".
- **DATA-006 staleness.** `apply_competitor_rates_to_config()` resolved, overlaid the numbers
  and discarded `validation_warnings`. `PricingService`'s gate re-read the file for staleness, but
  resolution-level issues and source age never reached the decision record.
- **DATA-018.** `_resolve_strategy()` averaged and took the median over every qualifying row.
- **SEC-010.** `write_rate_run()` wrote raw record fields with `pandas.to_csv`.

## Audit reproduction (original scripts, unedited)

`td_tiers_and_legacy_{before,after}.out` and `case_H_{before,after}.out` are the unedited
2026-11-13 scripts. `legacy_after.py`/`.out` replays the shapes through the new interfaces.

| Case | Before (`28ccd14`) | After |
|---|---|---|
| **A.** Legacy BNZ TD classification | `term_deposit` for 12.47% (Growth Fund), 9.76% (Balanced Fund), 8.12% (Moderate Fund) | all three `non_comparable`; ordinary 3.55/4.05/4.20 unchanged |
| **B.** Strict TD validation (audit run: BNZ TD only, no source attempts) | `failed`: `Duplicate competitor rate records found for ('BNZ', ...)` | `failed` only for `Missing expected source` (that run attempted no sources, which strict mode must reject); no duplicate errors |
| **B.** Strict validation, complete 5-bank home-loan + TD fixture run (287 records, all sources succeeded) | `failed`, 45 errors (ANZ, BNZ, Kiwibank, Westpac) | `valid`, 0 errors |
| **C.** Legacy non-strict BNZ 36-month TD benchmark | **8.12%** (a Moderate Fund return) | refused: `No competitor rates matched the configured strategy` (the script ends on this raise, exit 1, as designed). On the 2026-10-30 fixture, which has a real ordinary 3-year row, the benchmark is 4.60% |
| **D.** Mortgage `market_average`, ANZ 4.79 + extra 4.69, ASB 4.89 | 4.79 (row-weighted) | 4.79: ANZ is now represented once, by its better 4.69, and (4.69+4.89)/2 equals the old figure by coincidence |
| **D'.** Same, extra ANZ row is 4.99 | 4.89 (row-weighted: ANZ counted twice) | **4.84**: the extra row does not change ANZ's representative, so the average does not move |
| **H.** Stale legacy source, strict | `CompetitorRateResolutionError: ... stale` | unchanged |
| **H.** Stale legacy source, non-strict | 0.057 applied; overlay "carries no warning/age field"; no log | 0.057 applied (numbers unchanged); WARNING logged with age/threshold; `ResolvedCompetitorRates.stale=True`, `source_age_hours`, `max_staleness_hours`, `source_validation_status`; `PricingService` gate WARN with `competitor_source` in `decision_record.json` |

The Case H script still prints `returned object carries no warning/age field: []`. That is by
design: warnings are deliberately kept out of the numeric `market` config (Phase 1g hash). The
evidence travels on the resolution, the gate and the log.

## Policies

**Representative per bank.** The lowest home-loan / highest deposit rate among a bank's
eligible rows; a tie is broken by `observation_key()`. The aggregate is `sum/len` over
representatives in `selected_banks` order, so it is order-independent. With one row per bank it
is bit-identical to base (fixture: 0.0565 / 0.043).

**TD representative.** It is chosen exactly as before among ordinary rows (frequency preference,
then `observation_key()`). A `comparable` special replaces it only when it is higher at the
same frequency. Choosing the reference balance tier is Phase 2f and is untouched.

**Staleness.** Strict mode raises `CompetitorRateResolutionError`. Non-strict mode resolves and
labels `stale`, age, threshold and status. The CLI prints a `STALE` label and a disclosure, and
`--write` adds `# WARNING` comment lines to the generated YAML. `PricingService` cannot hide
it: the gate WARNs (`REVIEW_REQUIRED`).

**Coverage.** Below `min_banks` distinct banks, every `market_*` strategy fails closed in both
modes. `bank_specific` is exempt. Direct callers may pass `min_banks=` explicitly.

**CSV.** See [competitor_rate_ingestion.md](../competitor_rate_ingestion.md#csv-export-safety-sec-010).

## Tests

New: `tests/unit/test_competitor_rate_legacy_hardening.py` (74 tests, T01–T29 of the brief). It
was written first. RED was confirmed: 62 failed and 10 passed. The 10 that passed are regression
guards for behaviour that was already correct (PIE separation, frequency options, invalid-rate
rejection, mortgage fixture validation, JSON preservation). After the change it is GREEN.

Changed existing tests (seam or policy, not weakened):

- `test_competitor_rate_apply.py`: two single-bank tests (frequency fallback; special filter
  leaving only Westpac) now pass `min_banks=1` explicitly. A one-bank `market_average` is
  exactly what the coverage rule refuses.
- `test_api_security.py`: patches the renamed seam `apply_competitor_rates_with_resolution` and
  accepts `_run_scenario`'s new optional argument. The private-temp-file and cleanup assertions
  are unchanged.

| Run | Result |
|---|---|
| `ruff check .` | All checks passed |
| `pytest -q -m "not live_rates"` with `--cov ... --cov-fail-under=95` | **3220 passed, 0 failed, 2 skipped**; coverage **96.97%** |
| Skipped | 2: the missing-Playwright path cannot run because Playwright is installed (pre-existing) |
| Legacy parse/validate/CSV/CLI focused | 158 passed, 1 skipped |
| TD comparability (`test_pricing_intelligence_term_deposits`, `test_term_deposit_live_structures`) | 66 passed |
| Strategy and coverage (Phase 2c) | 100 passed |
| PricingService, API, gates, config lineage | 248 passed |
| Phase 2a quarantine and peer checks | 46 passed |

Touched-module coverage: `apply.py` 100%, `gates.py` 100%, `pricing_service.py` 99%,
`models.py` 98%, `store.py` 97%, `cli.py` 97%, `validate.py` 93% (pre-existing
required-field branches).

## Compatibility

- Public CLI subcommands, flags and exit codes are unchanged. `apply` prints extra lines.
- `CompetitorRatesConfig`, `latest.json`, history JSON and directory layout are unchanged. The
  `latest.csv` columns are unchanged; cells are neutralised.
- `ResolvedCompetitorRates` gains fields with defaults. `mortgage_records` still lists every
  qualifying row; the representatives are in `mortgage_bank_representatives`.
- `apply_competitor_rates_to_config()` is unchanged in signature and result.
  `load_and_resolve_competitor_rates()` gains keyword-only `now`, `min_banks` and
  `intelligence_config`. `_resolve_strategy()` gains keyword-only `bank_order`.
- `evaluate_gates()` gains optional `competitor_rate_provenance`, and gate evidence is additive.
  The regression artifacts (`summary.json`, `grid_results.csv`, ...) are untouched. Effective
  config hash and `effective_config.json` are unchanged (the overlay is still the four numeric
  market fields).
- **Behaviour change (intended):** a legacy `market_*` strategy over fewer than
  `pricing_intelligence.min_banks` (default 3) distinct banks now raises. **Migration:** select
  ≥3 banks, use `bank_specific`, or set `pricing_intelligence.min_banks` explicitly.
- A TD `market_*` benchmark may change where a bank's comparable special beats its ordinary rate,
  or where a pre-fix file carried fund rows. Both are the intended corrections.

## Remaining risks

1. Fixture-only verification. Live TD page structures still need supervised confirmation (the
   roadmap's live check for 2b/2e).
2. The legacy path does not apply `pricing_intelligence.our_bank` own-bank exclusion. The
   operator's `selected_banks` decides. `our_bank` validation is Phase 2f (DATA-014).
3. The TD reference-tier policy is unchanged (Phase 2f, DATA-005).
4. The `PricingService` gate still re-reads the source file for staleness after resolution. If
   the file is replaced mid-run, the gate judges the new file. The recorded `source_sha256` makes
   that detectable.
5. A `non_comparable` row whose rate is outside 0–15% still fails strict validation (existing
   range rule). No fixture triggers it.
6. CSV neutralisation targets leading-character formula vectors. It is not a general spreadsheet
   sandbox.
