# Competitor Rate Ingestion

The competitor-rate workflow fetches official NZ bank pages, extracts advertised home-loan and
term-deposit rates, validates the extracted records, stores audit artifacts, and can apply selected
market reference rates to the pricing engine.

## Official Sources

The configured official sources cover ANZ, ASB, Westpac, BNZ, and Kiwibank for:

- Home loans and mortgages
- Ordinary retail term deposits
- PIE term deposits where they appear on the same official investment-rate pages

Third-party comparison pages are not used to override official sources.

## Commands

Fetch official pages and write artifacts:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli fetch
```

Fetch fallback rates from interest.co.nz aggregator tables:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli fetch-interest-co-nz
```

This fallback is useful for demos and local scenarios when official bank pages block automated
access or time out. It still writes the same `latest.json`, `latest.csv`, history, and validation
artifacts, so the downstream `validate`, `apply`, and scenario commands are unchanged.

If a site requires JavaScript-rendered values and Playwright is installed in the environment, use:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli fetch --browser-fallback
```

The fallback is opt-in so the default workflow does not add a browser dependency. If Playwright is
not installed, the source attempt records a clear failure instead of silently using stale or
incomplete data.

Validate an existing artifact:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli validate \
  --source-file data/processed/competitor_rates/latest.json
```

Apply configured rates without mutating the base config:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli apply --config configs/base.yaml
```

Write a generated scenario config:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli apply \
  --config configs/base.yaml \
  --write
```

Print a stored run summary:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli report
```

Run the baseline scenario with a competitor-rate file:

```bash
python3 examples/run_with_competitor_rates.py \
  --source-file data/processed/competitor_rates/latest.json
```

## Artifacts

Fetch writes:

- `data/raw/competitor_rates/YYYY-MM-DD/<bank>_<product_type>.html`
- `data/processed/competitor_rates/latest.json`
- `data/processed/competitor_rates/latest.csv`
- `data/processed/competitor_rates/history/YYYY-MM-DDTHH-MM-SSZ.json`
- `data/processed/competitor_rates/validation_report_latest.json`

Raw and processed live artifacts are gitignored. Tests use sanitised fixtures under
`tests/fixtures/competitor_rates/`.

## Normalisation

The parser preserves each advertised term label and derives structured fields where possible.
Examples:

- `6 months` and `six months` become 6 normalised months.
- `1 year` becomes 12 normalised months.
- `30 days`, `60 days`, and `90 days` retain `term_days_raw` and approximate months for strategy
  selection.
- `floating` and `variable` remain special labels without a month term.

Interest payment frequency maps to `at_maturity`, `monthly`, `quarterly`, `annually`, or
`unknown`.

## Application

Manual config remains supported. If `competitor_rates.enabled=false`, the engine uses the existing
`market.competitor_loan_rate`, `market.competitor_deposit_rate`, `market.home_loan_reference_rate`,
and `market.term_deposit_reference_rate`.

When enabled, rates are filtered by selected banks, product, term, mortgage conditions, and deposit
frequency preference. Supported strategies are:

- Mortgages: `bank_specific`, `market_average`, `market_min`, `market_median`
- Term deposits: `bank_specific`, `market_average`, `market_max`, `market_median`

Resolved advertised percentages are converted to decimal rates before being applied to the engine.

### Shared policy with pricing intelligence (Phase 2d)

The legacy path is **retained, with its validation and selection delegated to the trusted
pricing-intelligence components** (DATA-006, DATA-018). It no longer keeps its own definitions:

| Concern | Shared component |
|---|---|
| Term-deposit categorisation | `BankPricingCollector.enrich_term_deposit_record()` -- the collector path's fence. Fund, KiwiSaver, PIE effective-rate, PIR and savings rows become `non_comparable`. `fetch` applies it, and so does `apply`, so a `latest.json` written before Phase 2d is fenced too. A bank without a collector gets the base-class vocabulary. |
| PIE return tables | Already `pie_term_deposit` from the shared extractor; they never enter the ordinary (`term_deposit`) benchmark. |
| Term-deposit specials | `pricing_intelligence.deposit_specials` (Phase 2b), with the configured `pricing_intelligence.deposit_specials` vocabulary and balance-tier edges. A `comparable` special is eligible and keeps `rate_category="special"`. It represents its bank only when it beats the bank's ordinary representative at the same payment frequency. A `not_comparable` special (restricted, PIE, different tier or frequency) is an understood exclusion. An `uncertain` special is excluded **and** raised as a warning ("degraded, needs review"). Specials are not assessed for a `pie_term_deposit` benchmark. |
| Coverage | `pricing_intelligence.min_banks` (Phase 2c, default 3) distinct banks behind any `market_*` strategy, in strict and non-strict mode. Below it: `CompetitorRateResolutionError` ("Insufficient competitor coverage ..."). `bank_specific` needs only its own bank. A direct caller can pass `min_banks=` explicitly. |
| Untrusted readings | A record whose `validation_status` is `warning` (quarantined/suspicious) or `failed` (rejected) never contributes, and the exclusion is warned. |
| Duplicate identity | `CompetitorRateRecord.observation_key()` -- bank, product, category, term, frequency, product name, balance tier and eligibility -- plus the row text for `non_comparable` rows. See *Validation*. |

**Equal bank weighting (DATA-018).** Before any aggregate each bank is reduced to one
representative: its most competitive eligible rate (lowest for home loans, highest for term
deposits), the trusted market snapshot's rule. A tie is broken by `observation_key()`, so the
choice never depends on record order. `market_average` and `market_median` therefore weight banks
equally. Another LVR tier or package row from the same bank changes the result only if it changes
that bank's representative. `market_min`/`market_max` use the representatives, and `bank_specific`
uses the named bank's representative. The aggregate is summed in `selected_banks` order. For input
with one qualifying row per bank the resolved rates are bit-identical to the previous
implementation.

Term-deposit representatives are still chosen per bank by payment-frequency preference. The
reference balance-tier policy is Phase 2f, not this change.

If a bank has only comparable specials at the configured term, frequency preference and
exact-before-combined matching still take precedence. Within that group, the highest advertised
rate wins, with `observation_key()` breaking equal-rate ties. If no preferred frequency matches,
the highest comparable special wins across the fallback group. Ordinary-deposit selection is
unchanged; a special replaces an ordinary representative only at a higher rate at the same frequency.

### Staleness and warnings

`load_and_resolve_competitor_rates()` returns `ResolvedCompetitorRates` with additive evidence:
`source_file_name`, `source_sha256`, `captured_at_utc`, `evaluated_at_utc`, `source_age_hours`,
`max_staleness_hours`, `stale`, `strict`, `source_validation_status`, contributing
`mortgage_banks`/`term_deposit_banks`, `mortgage_bank_representatives`, `min_banks`,
`resolution_warnings`, `deposit_special_assessments` and `reduced_assurance`. `provenance()` is
the JSON-safe form. It never carries an absolute path.

- **Strict mode** rejects a stale or failed source with `CompetitorRateResolutionError`.
- **Non-strict mode** resolves, reports the age, the threshold, the validation status and every
  warning, and labels the source `stale`. It is meant for local exploration only.
- `apply_competitor_rates_with_resolution(config)` returns `(config, resolution)`.
  `apply_competitor_rates_to_config(config)` is kept and logs any warnings at WARNING level.
  The overlay changes only the four numeric market fields. No warning text enters the
  configuration, so the Phase 1g effective-config hash is unaffected.
- `PricingService` resolves once and passes the resolution to the `COMPETITOR_RATE_FRESHNESS` gate.
  Resolution warnings (uncertain specials, excluded untrusted readings) make it WARN
  (`REVIEW_REQUIRED`). Its evidence carries `competitor_source` (the provenance) whatever its
  status, so it reaches `decision_record.json`.
- `competitor_rates.cli apply` prints the age, threshold, `STALE` label and contributing banks. When
  the source is stale or carries warnings, it prints a disclosure. `--write` puts that disclosure as
  `# WARNING` comments at the top of the generated YAML.

### Reduced assurance of a legacy source

A legacy `latest.json` cannot carry evidence that exists only on the SQLite pipeline:

- operator quarantine decisions (approve/reject/void);
- peer-plausibility assessments;
- the operational collection ledger (per-bank latest attempt). Its age is the file's single
  capture time.

None of this is invented. Every resolution lists it under `reduced_assurance`, which is recorded in
the decision record. A legacy source is never treated as more trustworthy than the trusted pipeline.

### CSV export safety (SEC-010)

`latest.csv` is a presentation copy for spreadsheets. Bank page text is untrusted. A string cell is
prefixed with `'` when it starts with a control character (tab, carriage return, newline, any C0
control) or when its first non-whitespace character is `=`, `+`, `-`, `@` or a full-width form
(`＝＋－＠`). Plain signed numbers stored as text (`-0.84`) and non-string cells (rates, negatives
included) are unchanged. A value already starting with `'` is unchanged, so the transform never
double-escapes. `latest.json`, the history JSON and the record objects keep the original source
text (`store.neutralise_csv_cell`).

## Validation

Every run produces a validation summary. The validator checks:

- Required bank, product type, source URL, capture timestamp, and term/product identity
- Numeric percentage rates
- Rates outside the expected 0% to 15% range
- Records claiming one economic identity (`validate.duplicate_identity`: the shared
  `observation_key()`, plus row text for `non_comparable` rows). Two simultaneous balance tiers,
  payment-frequency options, eligibility conditions or products are distinct. Two different rates for
  one identity is a **conflict**. An exact repeat is a **duplicate**. Both are errors in every mode.
- Missing official bank/product sources
- Parser failures where a page was fetched but no rates were extracted
- Large historical movements above 0.50 percentage points when a previous run is supplied
- Staleness through `max_staleness_hours`

Strict mode fails when required official sources are missing or failed. Non-strict mode reports
warnings and continues with available validated rates. With Phase 2d categorisation, a complete
five-bank home-loan plus term-deposit fixture run passes strict validation. Before, BNZ fund
returns and PIE tax-band columns collided as "duplicates" (verified offline on fixtures only; no
live-page validation is claimed).

## Known Limitations

- Bank page layouts can change and may require parser updates.
- Special mortgage rates often have eligibility conditions that must be reviewed.
- PIE term deposits are not the same as ordinary retail term deposits; they never enter the
  ordinary benchmark.
- Low-equity mortgage margins are stored separately and are not applied unless a scenario explicitly
  models them.
- This project uses synthetic data and is not financial advice. Rates must be verified before
  business use.
- **GitHub-hosted collector-canary failures are not necessarily parser bugs** (investigated
  2026-09-18). ASB has failed with `net::ERR_HTTP2_PROTOCOL_ERROR` on literally every canary run
  since 2026-09-08, but the same URL, same Playwright `headless=True` launch, and the same
  collector user agent fetch successfully from every other network tested, including a live
  re-check the same day. Westpac, Kiwibank, and (intermittently) BNZ show a second, non-deterministic
  pattern: the page loads (200) but the parser finds no rate content, and which bank fails changes
  from run to run a few minutes apart with no code change in between. Kiwibank's rates page loads an
  Akamai bot-manager pixel (`/akam/13/pixel_*`); the pattern across all four is consistent with
  Akamai-style bot mitigation reacting to GitHub Actions' shared/datacenter IP ranges, not with a
  parsing defect in this repo. Re-running the canary repeatedly to "confirm" a fix risks making this
  worse, not better -- repeated bursts from the same runner IP are exactly the signal these systems
  escalate on. A code change cannot fix this; it needs an infrastructure decision (e.g. a
  self-hosted runner on a non-datacenter IP) if a live-green canary is required. Until then, canary
  red from these four banks should be triaged against this note before assuming a parser regression.
  BNZ's own async-rendered-table timing bug (`wait_until="load"` racing its
  `api.bnz.co.nz/v1/ratesfeed/home/xml` XHR) was a genuine, separate, and now-fixed parser bug --
  see `collectors/base.py`'s `build_browser_fetcher`.
