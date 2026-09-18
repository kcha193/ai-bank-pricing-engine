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

## Validation

Every run produces a validation summary. The validator checks:

- Required bank, product type, source URL, capture timestamp, and term/product identity
- Numeric percentage rates
- Rates outside the expected 0% to 15% range
- Duplicate records for the same bank/product/term/category/frequency
- Missing official bank/product sources
- Parser failures where a page was fetched but no rates were extracted
- Large historical movements above 0.50 percentage points when a previous run is supplied
- Staleness through `max_staleness_hours`

Strict mode fails when required official sources are missing or failed. Non-strict mode reports
warnings and continues with available validated rates.

## Known Limitations

- Bank page layouts can change and may require parser updates.
- Special mortgage rates often have eligibility conditions that must be reviewed.
- PIE term deposits are not the same as ordinary retail term deposits.
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
