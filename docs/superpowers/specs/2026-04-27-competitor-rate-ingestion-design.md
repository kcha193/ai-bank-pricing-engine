# Competitor Rate Ingestion Design

## Context

The pricing engine currently uses manually configured competitor assumptions in
`configs/base.yaml` and validates them through `EngineConfig`. Market-relative pricing grids use
`market.home_loan_reference_rate` and `market.term_deposit_reference_rate`; customer response uses
`market.competitor_loan_rate` and `market.competitor_deposit_rate`.

The new workflow adds official-source-first ingestion for advertised NZ mortgage and term deposit
rates from ANZ, ASB, Westpac, BNZ, and Kiwibank. Manual competitor-rate configuration remains the
default behavior unless `competitor_rates.enabled` is true.

## Goals

- Extract advertised home-loan and term-deposit rates from official bank pages.
- Preserve audit evidence for every extracted rate: source URL, capture timestamp, raw HTML hash,
  parser version, raw text context, and validation status.
- Store stable `latest` and immutable historical artifacts.
- Resolve competitor reference rates using configured strategies and apply them to the existing
  engine without mutating base config by default.
- Keep normal tests deterministic with fixture HTML and fixture rate files.
- Provide opt-in live verification for current official pages.

## Non-Goals

- Personalised pricing or customer-level competitor matching.
- Automatically overriding official-source rates with third-party sources.
- Automatically applying low-equity mortgage margins or PIE deposit rates unless the scenario
  explicitly selects them.
- Guaranteeing that changing bank websites always parse successfully; uncertain extraction fails or
  flags manual review.

## Architecture

Create `src/balance_pricing_engine/competitor_rates/` with focused modules:

- `models.py`: Pydantic models for rate records, source attempts, validation reports, run metadata,
  strategy config, and resolved references.
- `sources.py`: official source registry for the five banks and two products.
- `normalise.py`: term, day, frequency, balance, category, and condition normalisation helpers.
- `extractors.py`: shared fetch/snapshot logic and bank/product-specific extraction functions.
- `validate.py`: record-level and run-level validation, duplicate checks, missing source checks,
  parser failure detection, stale latest checks, and day-over-day movement warnings.
- `store.py`: JSON/CSV/history/validation-report persistence.
- `apply.py`: load canonical rates, filter by scenario config, calculate reference strategies, and
  return a copied `EngineConfig` with updated competitor assumptions.
- `cli.py`: module entry point for `fetch`, `validate`, `apply`, and `report`.

The engine-facing change is deliberately small:

- Extend `EngineConfig` with optional `competitor_rates` settings.
- Keep `load_config()` behavior unchanged for existing configs.
- Add a helper such as `load_config_with_competitor_rates(path)` or
  `apply_competitor_rates_to_config(config)` so examples and CLI can opt in.

## Data Model

Canonical records represent one advertised rate row or cell:

- `bank`
- `product_type`: `home_loan` or `term_deposit`
- `product_name`
- `rate_category`
- `term_label_raw`
- `term_months_normalised`
- `term_days_raw`
- `advertised_rate_pct`
- `interest_payment_frequency`
- `min_balance_or_deposit`
- `max_balance_or_deposit`
- `lvr_condition`
- `special_conditions`
- `source_url`
- `source_bank_page_title`
- `captured_at_utc`
- `effective_date_text`
- `raw_text_context`
- `raw_html_sha256`
- `parser_version`
- `validation_status`
- `validation_warnings`

Rates are stored as advertised percentages in canonical artifacts and converted to decimal rates
only when applied to `MarketConfig`.

## Extraction Flow

`fetch` does the following:

1. Iterate over the official source registry for all bank/product combinations.
2. Fetch each page with `urllib.request` from the Python standard library.
3. Save raw HTML snapshots under `data/raw/competitor_rates/YYYY-MM-DD/`.
4. Hash the raw HTML with SHA-256.
5. Parse records using bank/product-specific extractors.
6. Mark a source attempt as failed if the page fetch succeeds but no records are extracted.
7. Validate all extracted records.
8. Write processed artifacts only when validation reaches the configured threshold.

Static HTML is the first implementation path. The extractor interface allows a future Playwright
fetcher, but Playwright is not required by default because the project currently has no browser
dependency.

## Normalisation

Term parsing preserves the bank's label and derives structured fields:

- `6 months`, `six months`, `18 months`, `1 year`, and `5 years` map to month counts.
- `30 days`, `60 days`, and `90 days` set `term_days_raw` and approximate months only where useful
  for strategy selection.
- `floating` and `variable` remain special labels with no month count.

Frequency parsing maps common labels to `at_maturity`, `monthly`, `quarterly`, `annually`, or
`unknown`.

Mortgage conditions such as minimum equity, standard/floating, package/customer conditions, and
low-equity margin disclosures are stored as categories/conditions. Low-equity margins are not added
to base rates by default.

## Validation

Validation has record-level and run-level outputs.

Record rules:

- Require bank, product type, source URL, captured timestamp, and a term label or product name.
- Require numeric advertised percentage.
- Warn or fail for rates below 0% or above 15%, depending on strictness.
- Preserve parser warnings on the record.

Run rules:

- Detect duplicates by bank, product type, term, category, frequency, and product name.
- Detect missing expected bank/product pages.
- Detect successful fetches with zero extracted rates.
- Detect stale latest files using `max_staleness_hours`.
- Compare against the previous historical run when available and flag movements over 0.50
  percentage points for manual review.

Strict mode fails the command if any official source is missing or parser failures occur. Non-strict
mode writes artifacts with warnings if at least one validated rate is available for each product
needed by the scenario.

## Storage

`store.py` writes:

- `data/processed/competitor_rates/latest.json`
- `data/processed/competitor_rates/latest.csv`
- `data/processed/competitor_rates/history/YYYY-MM-DDTHH-MM-SSZ.json`
- `data/processed/competitor_rates/validation_report_latest.json`

`latest.json` includes run metadata:

- `run_id`
- `captured_at_utc`
- `parser_version`
- source URLs attempted
- extraction success/failure by bank/product
- validation summary
- count of rates by bank/product type
- canonical records

Raw HTML and generated processed data are local artifacts and should be gitignored. Tests use small
fixtures under `tests/fixtures/competitor_rates/`.

## Application Strategy

When `competitor_rates.enabled=false` or absent, the engine uses existing manual market values
unchanged.

When enabled, `apply.py`:

1. Loads `source_file`.
2. Validates freshness and validation status.
3. Filters to `selected_banks`, product, term, ordinary term deposit vs PIE, frequency preference,
   and mortgage special/LVR options.
4. Calculates the configured strategy.
5. Returns a new `EngineConfig` whose `market.competitor_loan_rate`,
   `market.competitor_deposit_rate`, `market.home_loan_reference_rate`, and
   `market.term_deposit_reference_rate` reflect the resolved rates.

Mortgage strategies:

- `bank_specific`
- `market_average`
- `market_min`
- `market_median`

Term deposit strategies:

- `bank_specific`
- `market_average`
- `market_max`
- `market_median`

## CLI

The module entry point supports:

- `python3 -m balance_pricing_engine.competitor_rates.cli fetch`
- `python3 -m balance_pricing_engine.competitor_rates.cli validate`
- `python3 -m balance_pricing_engine.competitor_rates.cli apply --config configs/base.yaml`
- `python3 -m balance_pricing_engine.competitor_rates.cli report`

`apply` prints the exact assumptions being applied and does not mutate `configs/base.yaml` unless
`--write` is supplied. With `--write`, it creates a generated scenario config under
`configs/generated/`.

## Testing

Default tests are offline:

- term normalisation tests
- parser tests against sanitised fixture HTML snippets
- validation tests
- strategy tests for all required strategy names
- config compatibility tests proving manual behavior is unchanged when disabled
- integration-style test using a fixture `latest.json` to run the engine with competitor rates

Live tests are opt-in with `pytest -m live_rates` and may call official bank pages. They assert that
the fetch pipeline either extracts validated records or fails clearly with source-level errors.

## Documentation

Add `docs/competitor_rate_ingestion.md` and link it from `README.md`. The document explains source
pages, commands, normalisation, application strategy, validation reports, and limitations including
website layout changes, special-rate conditions, PIE deposits, low-equity margins, and the need for
human verification before business use.

## Acceptance Criteria

- Existing `python3 -m pytest -q` passes.
- `python3 -m ruff check .` passes.
- `python3 -m balance_pricing_engine.competitor_rates.cli validate --source-file tests/fixtures/competitor_rates/latest.json` succeeds.
- `python3 examples/run_with_competitor_rates.py --source-file tests/fixtures/competitor_rates/latest.json` runs and prints competitor references.
- `python3 -m balance_pricing_engine.competitor_rates.cli fetch --strict false` can attempt all official source pages and reports failures clearly if a parser cannot confidently extract rates.
