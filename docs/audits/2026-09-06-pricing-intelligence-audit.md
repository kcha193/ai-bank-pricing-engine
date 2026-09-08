# Independent NZ mortgage pricing-intelligence audit — 6 September 2026

**Overall verdict: PASS WITH MATERIAL LIMITATIONS.** This is the verdict after the
fixes in this audit. The original implementation could promote repeated parser
errors into pricing benchmarks, misclassify current bank products, and include
the bank being priced in its own competitor benchmark. Those reproduced defects
are fixed. Supervised project monitoring and synthetic scenario analysis are
usable with source review. Fully unattended, authoritative monitoring is not
ready: complete collection failures are not persisted in the observation-run
ledger, quarantined rates have no reviewed approval workflow, and canonical
eligibility remains incomplete.

This assessment concerns public mortgage advertisements and synthetic,
portfolio-level pricing. It does not certify customer eligibility, negotiated
rates, production model calibration, or the separate term-deposit/OCR collectors.
Shared deposit behaviour was regression-tested; its live sources were not
independently recertified in this mortgage audit.

**Evidence and method.** Architecture was reconstructed through Graphify, project
context and source inspection. Existing fixtures and assertions were challenged
against rendered official pages, controlled independent calculations, real
SQLite writes, Streamlit AppTest, and the application pricing service. The dated
[evidence file](2026-09-06-pricing-intelligence-evidence.json) retains the 70
canonical observations with source hashes and row context, complete scenario
results, arithmetic checks and regression hashes. Raw HTML, temporary databases
and detailed command logs remain outside the repository in `/tmp/pricing-audit-*`;
these are session-local diagnostics, not durable repository dependencies.
Additional reviewers began scoped checks but hit usage limits; a final separate
review attempt also failed for that reason. The primary auditor reproduced the
findings and reviewed the final diff locally. No independent final-review signoff
is claimed.

## Verification scorecard

Statuses describe the post-fix implementation within this audit's scope.

| Area | Status | Evidence / qualification |
| --- | --- | --- |
| Collectors | PASS | Five rendered official sources; 70 accepted rows, zero rejected |
| Live source accuracy | WARN | Rates and terms matched; eligibility footnotes are incomplete |
| Rate classification | WARN | Current BNZ/Kiwibank defects fixed; bank-specific fallback assumptions remain fragile |
| Term normalisation | PASS | Required conversions, textual variants, malformed/negative/zero cases |
| Rate arithmetic | PASS | Exact Decimal bps; percent-to-decimal seam traced |
| Validation | WARN | Bounds, missing evidence, duplicates and quarantine exercised; plausible first-observation errors remain possible |
| Historical storage | WARN | Persistence/idempotence/order checked; compact history is not a ledger of every attempted fetch |
| Change detection | PASS | Up/down/unchanged/new/removed/eligibility/promotion/category and recovery cases |
| Freshness handling | WARN | Age preserved and stale rows excluded; wholly failed attempts absent from store |
| Market snapshot | PASS | Independent 5/4/2/1/0-bank calculations and live term-specific universes |
| Ranking | PASS | Competition ties 1,2,2,4,4 verified |
| Competitive features | PASS | Signed distances, counts and own-bank exclusion verified |
| Market state | WARN | Deterministic/explainable; thresholds are heuristics, not calibrated signals |
| Failure isolation | WARN | Individual fetch/storage-snapshot failures isolated; no universal local wall-clock deadline |
| Self-bank exclusion | PASS | Service and config overlay exclude configured bank before benchmark selection |
| Optimiser integration | PASS | All five strategies plus offset traced through actual PricingService |
| Manual override | PASS | Config object unchanged; manual economics exactly preserved |
| Regression compatibility | PASS | Same inputs at pre-intelligence commit give identical results/core artifacts |
| CLI | PASS | Collection replay, live-store queries, help, invalid inputs, empty/failure paths |
| Dashboard | PASS | Actual Streamlit runtime: term selection, stale/partial/corrupt store; freshness added |
| Tests | WARN | 1,384 passed, 2 skipped; format and type gates still fail |
| Documentation | PASS | Correct launch path, strategy meaning, current tier distinction and limits documented |
| Operational reliability | WARN | Partial canary now fails visibly; attempt ledger, approval, alerts and retention need completion |

## Architecture and scope separation

```text
official URLs in competitor_rates/collectors/{anz,asb,bnz,westpac,kiwibank}.py
  -> collectors/base.py (fetch, browser option, retries, snapshots, enrichment)
  -> competitor_rates/extractors.py + normalise.py
  -> CompetitorRateRecord (percent rates and source provenance)
  -> pricing_intelligence/validation.py
  -> service.py orchestrates atomic observations + changes
  -> store.py (SQLite distinct states, collection runs, change events)
  -> changes.py / snapshot.py / positioning.py / market_state.py
  -> strategy.py (benchmark percent -> EngineConfig decimal assumption)
  -> scenarios/runner.py -> optimization/grid_search.py
  -> application.PricingService diagnostics and governance
```

The CLI is `pricing_intelligence/cli.py` via
`python -m balance_pricing_engine.pricing_intelligence`. The market dashboard is
`src/balance_pricing_engine/dashboard/app.py`; `scripts/streamlit_dashboard.py`
is a different ledger-oriented app. `dashboard_data.py` formats service/domain
results. Configuration is split deliberately: HTTP settings in lower-level
`competitor_rates/collection_config.py`, intelligence thresholds in
`pricing_intelligence/config.py`, engine inputs in `configs/base.yaml`.
Logging uses Python logging; structured application observability and domain
events remain separate from collection. The scheduled entry point is
`scripts/collect_market_rates_workflow.py`; launchd examples and a separate
GitHub collector canary exist.

No bank scraper was found inside the optimiser. SQL remains in the store;
dashboard calculations use domain helpers. The strategy module is the sole
intelligence-to-pricing seam, protected by architectural tests. The lower-level
legacy competitor-rate ingestion/config overlay still exists deliberately.
The final changes introduce no LLM pricing or new external service dependency.

## Live bank verification

All five advertised mortgage pages were independently read during this audit.
Static fetches returned no usable rate rows for every bank. Rendered pages were
captured first on 5 September UTC and collected again through the fixed complete
pipeline at **2026-09-06 04:33:25.413251 UTC (16:33 NZST)**. This is the shared
collection timestamp, not a bank-announced effective date or exact individual
HTTP completion time. No CAPTCHA or access control was bypassed.

| Bank / official source | Live verified | Fixed terms, months | Collection status | Notes |
| --- | --- | --- | --- | --- |
| [ASB](https://www.asb.co.nz/home-loans-mortgages/interest-rates-fees.html) | YES — values/tiers | 6,12,18,24,36,48,60 | SUCCESS, 11 rows | Seven specials; variable/Orbit retained; Better Homes and closed Back My Build excluded from comparisons |
| [ANZ](https://www.anz.co.nz/rates-fees-agreements/home-loans/) | YES — values/tiers | 6,12,18,24,36,48,60 | SUCCESS, 16 rows | Specials stop at 36 months; standard curve continues; Flexible now retained; Good Energy/Tideover non-comparable |
| [BNZ](https://www.bnz.co.nz/personal-banking/home-loans/compare-bnz-home-loan-rates) | YES — values/tiers | 6,12,18,24,36,48,60 | SUCCESS, 11 rows | Current header says Standard; fixed rows excluded from default special snapshot; four distinct floating products |
| [Westpac](https://www.westpac.co.nz/home-loans-mortgages/interest-rates/) | YES — values/tiers | 6,12,18,24,36,48,60 | SUCCESS, 17 rows | Seven specials, seven standards, three floating; rate-first identity repaired |
| [Kiwibank](https://www.kiwibank.co.nz/personal-banking/home-loans/rates-and-fees/) | YES — values/tiers | 6,12,24,36,48,60 | SUCCESS, 15 rows | No advertised 18-month fixed row; less-than-20%-equity standard tier no longer inverted |

“Live verified” does not mean fully eligibility-certified. ANZ's salary-credit
condition is not represented in `package_requirement`. Kiwibank's special-scheme
exceptions, including Kāinga Ora/Whenua cases, are absent from the simplified
20%-equity fields. Source footnotes must be checked. These are confirmed coverage
limits, not evidence that the advertised numbers are wrong.

**Verified fixed mortgage rates, % p.a., at that capture:**

| Bank / tier | 6m | 12m | 18m | 24m | 36m | 48m | 60m |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ANZ special | 4.79 | 4.99 | 5.45 | 5.49 | 5.59 | — | — |
| ANZ standard | 5.39 | 5.59 | 6.05 | 6.09 | 6.19 | 6.39 | 6.49 |
| ASB special | 4.79 | 4.99 | 5.35 | 5.45 | 5.45 | 5.49 | 5.59 |
| BNZ standard | 4.79 | 4.99 | 5.29 | 5.45 | 5.45 | 5.45 | 5.49 |
| Westpac special | 4.75 | 4.99 | 5.35 | 5.45 | 5.39 | 5.39 | 5.49 |
| Westpac standard | 5.35 | 5.59 | 5.95 | 6.05 | 5.99 | 5.99 | 6.09 |
| Kiwibank special | 4.75 | 4.95 | — | 5.39 | 5.49 | 5.59 | 5.69 |
| Kiwibank standard | 5.65 | 5.85 | — | 6.29 | 6.29 | 6.29 | 6.39 |
| **Default special median** | **4.77** | **4.99** | **5.35** | **5.45** | **5.47** | **5.49** | **5.59** |
| **Default bank count** | **4** | **4** | **3** | **4** | **4** | **3** | **3** |

Rates above are supported by the five linked official pages and retained source
row evidence. A dash means no comparable advertised row, never a zero rate.
BNZ Standard is shown for transparency and is not mixed into special statistics.
The one-year special market is Kiwibank 4.95% (#1), ANZ/ASB/Westpac 4.99% (#2):
minimum 4.95%, maximum/median 4.99%, mean 4.98%, spread 4 bps.

Floating products were retained as nonfixed, with no fixed term: ANZ Floating
6.04 / Flexible 6.15; ASB Housing Variable 6.04/Orbit 6.14; BNZ Standard Floating
6.09/TotalMoney 6.19/Rapid Repay 6.19/Mortgage One 6.19; Westpac Choices
6.14/Everyday 6.24/Offset 6.14; Kiwibank Variable 6.00/Offset 6.00/Revolving 6.05.
The canonical `floating` versus `variable` labels preserve source wording and
both are excluded from fixed-special comparisons. Introductory, green, bridging
and closed products are retained as non-comparable where identified.

## Independent mathematics, validation and history

- Term conversion: 6 months→6; 1 year/12 months→12; 18 months→18; 2/3/4/5
  years→24/36/48/60. `SIX MONTHS`, `two years`, `Fixed 18 months` also passed.
  Negative, fractional numeric durations and zero terms now produce no fixed
  term, rather than a plausible positive one. Unsupported text remains unknown.
- Rates are percentage values in collector/model/SQLite/snapshot, decimal
  fractions in engine configuration, and basis points in movement/distance
  features. `bps.py` uses Decimal from decimal-string values: 4.99→4.79 = −20,
  4.79→4.89 = +10, 5.00→5.00 = 0. No float subtraction artifact appeared in
  those business outputs. General model calculations still use floats.
- The supplied independent market ANZ4.79/BNZ4.85/Westpac4.85/ASB4.89/Kiwibank4.89
  produced min4.79/max4.89/mean4.854/median4.85/spread10 and ranks1,2,2,4,4.
  Four-bank mean4.845/median4.85; two-bank mean/median4.82/spread6; one-bank
  mean/median4.79/spread0; zero observations produces no term market.
- At our supplied 4.89, distance to best is +10bps and to median +4bps: positive
  means more expensive for lending. Three competitors are below, none above,
  two tied. When our synthetic bank is additional, total bank count is six.
  Configuring an actual tracked bank excludes it before position/benchmark
  calculation; total position count then includes us plus the remaining peers.
  `MARKET_SECOND_LOWEST` excludes all tied leaders, then picks the next distinct
  competitive rate; it is not the second bank in an arbitrary tie ordering.
- Mortgage plausibility bounds are 0.5–15%; deposit bounds 0–15%. Required fields,
  absent source evidence, malformed values, invalid terms, exact duplicates,
  conflicting observations and skipped rows are covered by validation tests.
  A valid first observation can still be a plausible parser error: bounds cannot
  independently establish source truth.
- 4.89→8.49 is stored with a warning and a suspicious +360bps event. It does not
  reach a default benchmark. After the fix, repeating 8.49 remains quarantined;
  the trusted 4.89 fallback retains its original timestamp and expires normally.
  Recovery to 4.99 yields +10 from 4.89, not a fictitious −350 from 8.49. Historical
  suspicious flags no longer clear automatically. Category/eligibility changes
  in a quarantined chain do not evade that protection.
- SQLite keeps one row per distinct advertised state, updating last-seen/count
  for unchanged confirmations; changes and collection runs are separate. Two
  replays of the same official HTML gave 70 accepted, then zero new states,
  70 unchanged, zero events. First collection intentionally reports no changes.
  Observation/event writes are atomic and run IDs support idempotence. Date,
  bank, term, empty, same-time and historical queries passed. New backdated
  collections now fail before they can rewind the current state.
- Controlled change detection gave no event for unchanged, −10/+10 for cuts/
  rises, and explicit new/removed/eligibility/promotion events. Category/rate-type
  changes produce new+removed because category belongs to product identity.
  A failed bank is unknown, not withdrawn. Less-than-50% matched coverage and
  category-specific coverage guards also suppress misleading withdrawals.
  That conservative policy can delay detection of a genuine broad withdrawal.
- Injecting an ANZ failure while the other four succeeded left its last-known
  value at the old timestamp; no withdrawal was emitted. At day 8 its observation
  was excluded by the default168-hour limit while day 2 confirmations survived.
  Dashboard tests additionally checked stale 20-day data and partial collection.
  A retained last-known value must be read with its age; a successful fetch is
  not itself independent verification.
- Compact historical storage does not retain each reconfirmation's original HTML
  and timestamp as an immutable row. Some as-of freshness reconstructions use a
  state's first-seen timestamp once its last-seen lies after the query date.
  It supports observed-state analysis, not a complete event-sourced record of
  exactly what the system knew at every instant. Bank effective dates may differ
  from our collection times.

Market-state controlled outputs were LOW (0 changes), NORMAL (1 change / 1 bank),
ELEVATED (4 changes / 2 banks), HIGH (8 changes / 4 banks). Lookback is 7 days; mean absolute
movement≥15bps can escalate sparse activity; directional dominance is 70%.
Terms affected are reported, not a threshold; current spread is not used by the
classifier. These rules are documented/configured, deterministic and free of
LLM input. They are explainable heuristics without empirical calibration.
An empty fresh market now prints unknown in the CLI instead of calm/LOW; direct
domain activity still needs collection-health context to distinguish no moves
from no data.

## Optimiser proof and compatibility

Every row below ran through `application.PricingService.run_scenario`, with an
assertion that the resolved assumption equalled
`diagnostics.market.competitor_loan_rate`. Selected term: 12 months. Market-derived
universe: Kiwibank,ANZ,ASB,Westpac specials; no configured self-bank in this
synthetic run. MANUAL consults no live universe. All application decisions were
`REVIEW_REQUIRED`; the optimiser result is not an automatic approval.

| Strategy | Benchmark % | Actual decimal supplied | Optimal loan % | Expected loans NZ$m | Objective NZ$ |
| --- | ---: | ---: | ---: | ---: | ---: |
| MANUAL | 4.99 | 0.0499 | 6.19 | 509.992 | 5,920,859.37 |
| MARKET_MEDIAN | 4.99 | 0.0499 | 6.19 | 509.992 | 5,920,859.37 |
| MARKET_LEADER | 4.95 | 0.0495 | 6.15 | 537.724 | 5,826,902.96 |
| MARKET_SECOND_LOWEST | 4.99 | 0.0499 | 6.19 | 509.992 | 5,920,859.37 |
| MARKET_MEAN | 4.98 | 0.0498 | 6.18 | 516.822 | 5,882,689.72 |
| MARKET_LEADER, −5bps offset | 4.90 | 0.0490 | 6.15 | 511.192 | 5,718,760.80 |

The self-bank adversarial example ASB4.0/ANZ4.8/BNZ4.9 previously yielded median4.8
while pricing ASB. It now yields external median/mean4.85, leader4.8, second4.9.
With only ASB present the external strategy errors. The service and overlay both
exercise this exclusion; public snapshots continue to show the whole market.

**One complete source trace:** Kiwibank's linked official page had the row
`1 year fixed | 4.95 % p.a.` with its special equity header. Canonical percent
4.95, SQLite percent4.95, MARKET_LEADER4.95, config decimal0.0495, actual
diagnostics decimal0.0495, optimal own rate0.0615, objectiveNZ$5,826,902.96.
Source HTML SHA256:
`783747a564cf031a7ea3968be2b5aaecb0da64c35d4261ee9d32e9cef28c7908`.
Validation, persistence, snapshot and scenario all used the same real collection.

**Manual regression:** pre-intelligence source at `c2c30be` was extracted to an
isolated directory and run with the exact current config. Every
`OptimizationResult` field matched current code; `grid_results.csv` and
`summary.json` matched byte-for-byte (hashes in evidence). Loan rate0.0619,
deposit0.0405, loan volume509,991,691.6048304, deposit volume328,903,313.8128667;
loan interest31,568,485.710339, ECL3,438,163.90610834, deposit cost13,320,584.2094211,
wholesale cost6,700,269.97830266, capital2,141,965.10474029,
funding penalty46,643.14314492, excess income0, objective5,920,859.36862169.
Own-rate and competitor-gap elasticities remain separate. The core economics
files were unchanged versus that reference. Earlier explicit config
recalibrations (`852ca0b`, `3fca46b`, `d1f07c0`) changed loan, deposit and cash
defaults; comparing different historical YAML defaults would not test the seam.

The example script varies competitor assumptions. Its misleading “Match” labels
were corrected. To separately evaluate the requested actual customer-rate
alternatives, the existing grid evaluator was also run at fixed own rates:

| Actual own-rate alternative | Own % | Competitor % | Distance to median bps | Rank | Loans NZ$m | Interest margin NZ$m | Objective NZ$m |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No change, assumed current 4.99% | 4.99 | 4.99 | 0 | 2 | 2,518.571 | 31.934 | −22.658 |
| Match median | 4.99 | 4.99 | 0 | 2 | 2,518.571 | 31.934 | −22.658 |
| Match leader | 4.95 | 4.99 | −4 | 1 | 2,558.134 | 31.421 | −24.160 |
| Leader minus 5 bps | 4.90 | 4.99 | −9 | 1 | 2,603.211 | 30.683 | −26.027 |
| Jointly optimised | 6.19 | 4.99 | +120 | 5 | 509.992 | 11.548 | +5.921 |

There is no observed current customer rate in this synthetic project, so 4.99%
is an explicit no-change assumption. The first four rows hold deposit pricing
at 3.90%; the optimised row jointly chooses 4.05%, so that row includes both
decisions. Interest margin here means loan interest minus deposit and wholesale
cost plus excess income, before ECL/capital/funding penalty. The low loan prices
generate very large synthetic volumes/funding penalties; these are model outputs,
not market forecasts. Lower lending rates are more aggressive; higher deposit
rates are more aggressive. Existing deposit ordering uses that opposite direction.

## Confirmed issues and focused fixes

All fixes below follow reproduced behaviour → failing regression → small change
→ passing targeted/full tests. No CRITICAL issue was established.

| Severity / issue | Evidence and business impact | Root cause and actual fix | Remaining risk |
| --- | --- | --- | --- |
| HIGH — self-bank benchmark bias | ASB example above shifted external median | Only positioning excluded self; strategy/service/config overlay now exclude before statistics | `our_bank` must be configured correctly |
| HIGH — suspicious repeat becomes trusted | 4.89→8.49→8.49 silently selected8.49, generated ELEVATED activity and rewrote earlier event flags | Immediate predecessor used as validation baseline and repetition auto-confirmed history; compare trusted state, preserve quarantine, rebase clean recovery, remove automatic confirmation | Existing auto-confirmed history needs source review; no reviewed approval workflow |
| HIGH — BNZ Standard parsed as special | Current header lacked “rate”; fixed row inherited special/min20% equity and four floating columns collapsed to conflicting identities | Recognise Term header and column-specific floating names | Historical fallback defaults remain bank-specific |
| HIGH — low-equity condition inverted | Kiwibank “less than20% equity” became minimum20%/maximum80% LVR | Parse inequality direction; retain low-equity label, leave unsupported inverse bounds unknown | Scheme exceptions/page footnotes remain incomplete |
| HIGH — mutable mortgage identity | Westpac5.59→5.49 became new+removed; cheaper old states could survive higher repricing | Use term instead of percentage as product name; repair legacy numeric-only fixed names and flag associated events on store open | Migration preserves rates but cannot certify affected historical events; back up/review old stores |
| HIGH — invalid numeric assumptions | NaN position could claim leadership; nonfinite/negative manual inputs and offsets passed | Finite/nonnegative domain guards and CLI numeric validation before database open | Manual assumptions still require economic/source judgement |
| MEDIUM — backdated collection rewinds state | Older unchanged write reduced last-seen and emptied latest-rate query | Reject writes before latest per-product collection timestamp | No general late-arrival ingestion policy; historical imports need deliberate handling |
| MEDIUM — missing/malformed term products | Flexible/Revolving dropped; −1year→12,1.5years→60,0days→1 | Recognise nonfixed names and reject invalid numeric duration forms | Unrecognised future wording requires review |
| MEDIUM — stale/partial dashboard misleading | Populated stale store said empty; failed-bank age absent; corrupt DB interrupted scenario controls | Service-backed freshness table/last stored run; explicit unavailable-market warning; isolate DB read errors | Totally failed attempts leave no stored run; “fresh” is not source certification |
| MEDIUM — canary masks partial failure | Executed actual health shell with collection exit 2; it returned 0 | Preserve uploaded summary then fail final health step on every nonzero collection status; four exit-status regressions | Notification recipients and persistent daily history still require deployment configuration |
| MEDIUM — CLI errors/unknown state | Invalid terms/days/nonfinite rates accepted, missing config escaped handler, empty market reported LOW | Validate arguments, contain config errors, display unknown for no fresh market | Domain consumers must also read health/coverage |
| LOW — misleading docs/example/CI guard | Dashboard launch pointed at other app; benchmark shifts called “Match”; CI omitted explicit live exclusion | Correct launch and labels, document limits, add `-m 'not live_rates'` to CI | No live website test is used as a PR gate |

The raw-snapshot write-failure probe established that collection-wide isolation
already worked; only a regression test was added, with no unnecessary production
change. Eight existing quarantine tests expected automatic confirmation, so their
assertions were corrected to the independently established safety contract.
Small current-page structures now test BNZ headers, Kiwibank inequalities,
Westpac rate-first rows and floating product names. This addresses false
confidence from historical fixtures passing against materially different pages.

**Unresolved findings:**

| Severity | Problem / evidence / impact | Disposition and remaining work |
| --- | --- | --- |
| HIGH | ANZ/Kiwibank eligibility conditions missing from canonical fields could misstate access to a leading rate | Human source review required; extend provenance-backed footnote/exception modelling before eligibility automation |
| MEDIUM | No accepted observations returns before writing a collection run; dashboard cannot show a wholly failed attempt | Persist per-attempt and per-bank health independently of accepted observations; scheduler summaries are only a separate partial remedy |
| MEDIUM | Quarantine has no reviewed admission path; existing low-level `confirm_suspicious_changes` can mutate event history | Add authenticated/reviewed provenance and an append-only approval record; do not treat this method as a supported operator workflow |
| MEDIUM | Request timeouts/retries do not guarantee a total local browser-run deadline | Enforce supervisor wall-clock timeout and stalled-run alert; retain per-bank isolation |
| MEDIUM | Historical compacting and parser coverage heuristics limit exact as-of knowledge and broad withdrawal certainty | Use retained source/collection evidence for disputed periods; decide whether full attempt/confirmation history is required |
| LOW | Kiwibank page title includes SVG icon titles; parser version remains a coarse `competitor-rates-v1` | Improve provenance metadata/versioning; rate/term/hash evidence is unaffected |
| LOW | Repository formatter/type checks already fail | Separate cleanup: 102 format files, 109 mypy errors/41 files; not labelled passing |

## CLI, dashboard, operations and security

CLI help and scheduled-worker help ran successfully. `collect --no-snapshot`
ran through the real CLI handler with captured official HTML replay, accepted 70
and exited 0. This replay is explicitly not a new HTTP fetch. `snapshot --term12`,
`changes --days7`, `history --bank Kiwibank --term12`, strategy/example commands
queried the separate live audit database and matched stored values. Tests cover
all-bank failure, invalid/missing config and arguments, empty state, validation
rejection, product filtering, and alert formatting. A failed replay setup also
returned 1 with all five collector failures, rather than claiming success.

Actual Streamlit AppTest exercised the market table, term selector/ranking,
history/change/chart path, stale and partial freshness, and corrupt database.
Old fake-Streamlit tests were adapted only for the new caption/table placement.
This was runtime/widget verification, not a pixel-level browser design review.

HTTP defaults: 30-second timeout, 2 retries after initial attempt, 2/4-second
exponential backoff, 1-second inter-bank delay, explicit project user agent.
URLs are public official collector constants; no hard-coded user-home paths were
introduced. Playwright is optional and needed for these client-rendered pages;
launch/navigation timing is separate from the urllib inactivity timeout.
The worker has a local `fcntl` lock and exit codes 0 (success), 1 (total failure),
2 (partial/degraded), 3 (lock held). The canary has a 20-minute job timeout and
runs Sunday–Thursday 19:30 UTC, Monday–Friday 07:30 NZST / 08:30 NZDT. It uses an
ephemeral database, so it is a parser health check, not durable daily history.
Its final status now fails for partial collection, after summary upload. No
scheduler was installed and no deployed job was changed during this audit.

Only public pages and synthetic portfolios were used. No bank credentials,
customer data or personal rate profiling were added. Raw HTML snapshots have a
clear parser-debugging/provenance purpose and are git-ignored; retention is not
automated. SQLite is standard library; browser dependency is justified by live
page rendering. SQL findings involved internally constructed query fragments
with bound values, not observed user-controlled SQL injection. Security scans
are evidence within their scope, not a claim that every security property is
formally proven. No webhook or message was sent to another person.

## Automated verification

Environment: checkout's `.venv`, Python 3.12.13. Import location was verified to
this checkout, resolving the stale-path concern in older project memory.

| Check / command | Before fixes | Final |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q -m 'not live_rates'` | 1,336 passed,2 skipped,91 warnings;53.42s | Full suite below adds coverage gate |
| Same pytest plus `--cov=balance_pricing_engine --cov-report=term --cov-fail-under=95` | Not the baseline command | **1,384 passed,2 skipped,91 warnings;58.12s;95.87% coverage;exit0** |
| `.venv/bin/python -m ruff check .` | PASS,exit0 | **PASS,exit0** |
| `.venv/bin/python -m ruff format --check .` | **FAIL,exit1:102 would reformat,87 formatted** | **FAIL,exit1:102 would reformat,91 formatted** |
| `.venv/bin/python -m mypy src` | **FAIL,exit1:109 errors in41 files;101 checked** | **FAIL,exit1:109 errors in41 files;101 checked** |
| Bandit over `src scripts` | **Exit1:11 LOW,23 MEDIUM,0 HIGH** | **Exit1:same counts** |
| `pip check`; compileall of `src scripts examples` | PASS | No dependency changes |
| Gitleaks git history, redacted report | 398 commits scanned;zero leaks | Commit hook scanned staged fixes with zero leaks; report/evidence also scanned at commit |
| `git diff --check` | — | PASS |

Two skips are installed-Playwright branches that test missing-dependency
behaviour. Warnings are not suppressed. Mypy failures include missing stubs and
real project typing issues; they are not all attributed to dependency setup.
Bandit includes literal-token/“PASS” false positives, internal SQL fragments,
URL-open scheme warnings and existing server/subprocess patterns; no new
exploitable credential or injection path was established by this audit.
Four new audit test modules and focused existing-module updates cover the fixes.
Targeted red logs demonstrated the defects before changes; the final entire
suite verifies interactions with the existing engine and artifact contracts.

## Git state and production-readiness answers

Initial branch `master`, HEAD `0972522`; original intelligence feature branch
`feature/nz-pricing-intelligence` had already merged through `8e0eb16` (PR45).
Audit branch: `fix/pricing-intelligence-audit`. Fix commit: `f5c4f46`.
The report/evidence are a separate subsequent audit-documentation commit.
No push or merge was performed. The working tree deliberately retains the
pre-existing `notebooks/bank_pricing_engine_walkthrough.ipynb` modification;
it is excluded from audit commits. No temporary SQLite files, raw HTML dumps,
credentials or diagnostic scripts are committed. Graphify is updated separately
as derived local project context (5,792 nodes and 13,533 edges; AST update).

1. **Can I trust this for manual/project competitor monitoring?** Yes, after these
   fixes, as supervised monitoring of advertised values. Read the tier, term,
   age, coverage and source conditions. “SUCCESS” alone is insufficient.
2. **Can I trust it to detect competitor rate changes automatically?** For normal
   changes on supported stable page structures, with review and monitored
   collection health. No blanket unattended guarantee: plausible parser errors,
   broad-withdrawal ambiguity and incomplete failed-attempt history remain.
3. **Can I safely use its output in pricing scenario analysis?** Yes for synthetic
   portfolio scenarios after checking benchmark universe/freshness/eligibility,
   setting `our_bank` where appropriate, and reviewing quarantines. Manual
   economics are exactly preserved. A benchmark is an assumption; the pricing
   result still requires governance review.
4. **What outputs still need human verification?** Eligibility and promotional
   exceptions; suspicious moves and recoveries involving category changes;
   missing-bank/low-coverage withdrawals; fresh-looking first observations after
   page redesign; existing history written by the auto-confirming code; market
   state interpreted without health context; model recommendations.
5. **What must be fixed before daily unattended collection?** Persist every
   attempt's per-bank outcome even when zero rates are accepted; provide reviewed
   quarantine admission with immutable evidence; configure failure/partial/stall
   notifications and an external deadline for the actual scheduled worker;
   establish durable database backup and source-retention policy; review or
   separately bootstrap previously unverified history. The partial-green canary
   defect is fixed locally. Run a supervised burn-in before relying on daily
   unattended outputs. Daily unattended collection must not imply unattended
   eligibility decisions or automatic pricing approval.
