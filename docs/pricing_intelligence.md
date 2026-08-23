# NZ Pricing Intelligence Agent

## Purpose

The Pricing Intelligence Agent monitors publicly advertised New Zealand retail
bank pricing, keeps a historical record of it, detects material competitor
changes, and turns all of that into competitor assumptions the pricing engine
can run on.

It exists to answer one question:

> What is happening in the NZ retail banking pricing market, and how should that
> information feed into pricing decisions?

It deliberately does **not** answer "what should we price?". That remains the
optimiser's job. The split matters:

| Concern | Owner | Question |
| --- | --- | --- |
| Pricing intelligence | `pricing_intelligence/` | What is the market doing? |
| Pricing optimisation | `optimization/`, `pricing/`, `scenarios/` | Given the market and our economics, what should we do? |

The only seam between them is
[`pricing_intelligence/strategy.py`](../src/balance_pricing_engine/pricing_intelligence/strategy.py).
No scraping logic reaches the optimiser, and no pricing logic lives in the
intelligence layer.

## Relationship to `competitor_rates/`

This capability was built **on top of** the existing
[`competitor_rates/`](../src/balance_pricing_engine/competitor_rates/) ingestion
module, not as a replacement. That module already owned the canonical record
model, term normalisation, run validation, JSON/CSV run artifacts, and the
config overlay. Rebuilding any of it would have created two competing systems.

What was added:

| Layer | Where | Status |
| --- | --- | --- |
| Per-bank collector adapters | `competitor_rates/collectors/` | New |
| Canonical record + provenance | `competitor_rates/models.py` | Extended with LVR/equity, cashback, promotion, package fields and `observation_key()` |
| Term/eligibility normalisation | `competitor_rates/normalise.py` | Extended with LVR, cashback, new-business and package parsing |
| Historical observation store | `pricing_intelligence/store.py` | New (SQLite) |
| Change detection | `pricing_intelligence/changes.py` | New |
| Market snapshot and ranking | `pricing_intelligence/snapshot.py` | New |
| Positioning + activity features | `pricing_intelligence/positioning.py` | New |
| Market-state classification | `pricing_intelligence/market_state.py` | New |
| Alert formatting | `pricing_intelligence/alerts.py` | New |
| Competitor-rate strategy | `pricing_intelligence/strategy.py` | New |

## Architecture

```text
Official NZ bank websites
          |
   Bank-specific collectors        competitor_rates/collectors/{anz,asb,bnz,westpac,kiwibank}.py
          |
   Canonical pricing model         competitor_rates/models.py  (CompetitorRateRecord)
          |
      Validation                   pricing_intelligence/validation.py
          |
 Historical observation store      pricing_intelligence/store.py  (SQLite)
          |
    Change detection               pricing_intelligence/changes.py  (RateChange)
          |
 Market snapshot / positioning     pricing_intelligence/{snapshot,positioning,market_state}.py
          |
 Pricing Intelligence Service      pricing_intelligence/service.py
          |
  Competitor-rate strategy         pricing_intelligence/strategy.py   <-- the only seam
          |
   Existing pricing engine         scenarios/runner.py -> optimization/grid_search.py
          |
  Pricing scenarios / recommendations
```

### Storage choice

**SQLite**, from the standard library. The repository had no persistence layer,
and SQLite adds no dependency, needs no server, and comfortably handles the
query shapes required (latest-per-key, per-key history, changes in a window) at
the scale of five banks x seven terms x daily collection. DuckDB would only pay
for itself under large analytical scans, which this workload does not have.

Business logic never sees SQL. `ObservationStore` maps rows to and from
`CompetitorRateRecord`, and everything downstream works on those objects.

### Observations vs changes

An **observation** is what a bank advertised at a point in time. A **change** is
a material difference between two observations of the same rate line. They are
stored separately, and a daily collection where nothing moved produces
observations but no changes.

The store keeps one row per *distinct advertised state*. An unchanged
re-observation bumps `last_seen_at` and `observation_count` on the existing row
instead of inserting a duplicate, so history reads as the sequence of states a
rate actually took, and the database does not grow without bound.

Records are matched across runs by `CompetitorRateRecord.observation_key()` --
bank, product type, rate category, term, LVR condition, and payment frequency.
Matching on identity is what makes a reprice read as a *movement* rather than
one product disappearing and another appearing.

### Basis points are exact

Every basis-point figure is computed in
[`bps.py`](../src/balance_pricing_engine/pricing_intelligence/bps.py) on
`Decimal`. Naive float subtraction of `4.99 -> 4.79` yields
`-19.999999999999996`; the exact path yields `-20.0`.

## Usage

### Collect

```bash
# Live collection currently requires JavaScript rendering (see Limitations).
python3 -m pip install -e ".[browser]"
python3 -m playwright install chromium

python3 -m balance_pricing_engine.pricing_intelligence collect --render-javascript
python3 -m balance_pricing_engine.pricing_intelligence collect --alerts
python3 -m balance_pricing_engine.pricing_intelligence collect --no-persist   # dry run
```

A collection prints a structured report:

```text
ANZ       SUCCESS  10 rates
ASB       SUCCESS  7 rates
Westpac   FAILED   ConnectionError
BNZ       SUCCESS  6 rates
Kiwibank  SUCCESS  4 rates
```

One bank failing never stops the others, and never writes partial nonsense: a
failed bank simply contributes no records that run.

### Inspect the market

```bash
python3 -m balance_pricing_engine.pricing_intelligence snapshot
python3 -m balance_pricing_engine.pricing_intelligence snapshot --term 12
```

```text
Bank               6M       1Y      18M       2Y       3Y       4Y       5Y
---------------------------------------------------------------------------
ANZ              5.29     4.79     4.89     4.95     5.19     5.59     5.69
ASB              5.35     4.89     4.95     5.05     5.29        -     5.75
BNZ              5.39     4.85     4.92     4.99     5.25        -        -
Kiwibank         5.45     4.89        -     5.15     5.35        -        -
Westpac          5.40     4.85     4.94     5.09     5.30        -        -
---------------------------------------------------------------------------
Median           5.39     4.85     4.93     5.05     5.29     5.59     5.72
Best             5.29     4.79     4.89     4.95     5.19     5.59     5.69
Spread bps         16       10        6       20       16        0        6
```

Ranking uses **standard competition ranking**: tied rates share a rank and skip
the next one, so `4.79 / 4.85 / 4.85 / 4.89 / 4.89` ranks `1, 2, 2, 4, 4`.

### Changes, history, market state, position

```bash
python3 -m balance_pricing_engine.pricing_intelligence changes --days 7
python3 -m balance_pricing_engine.pricing_intelligence changes --days 7 --alerts
python3 -m balance_pricing_engine.pricing_intelligence history --bank ANZ --term 12
python3 -m balance_pricing_engine.pricing_intelligence market-state
python3 -m balance_pricing_engine.pricing_intelligence position --rate 4.95 --term 12
```

### Market state

A deterministic classifier, never an LLM: the label drives how a recommendation
is framed, so it must be reproducible and explainable to a governance forum.

| State | Condition |
| --- | --- |
| `HIGH` | >= 8 changes across >= 4 banks in 7 days |
| `ELEVATED` | >= 4 changes across >= 2 banks, **or** a single change with mean movement >= 15 bps |
| `NORMAL` | >= 1 change |
| `LOW` | no changes |

`HIGH` and `ELEVATED` each require both a count and a breadth condition, so one
bank repricing its whole curve does not read as a market-wide repricing round.
Thresholds live in `PricingIntelligenceConfig.market_state`.

Changes flagged suspicious are excluded from the classifier. A suspicious
movement is more often a parser fault than a reprice, and one bogus 300 bps jump
would otherwise dominate the mean and fake a repricing round.

### Scenario analysis

```bash
python3 examples/run_market_intelligence_scenarios.py --term 12
```

```text
   Scenario                  Competitor   Our rate       Objective        vs A
------------------------------------------------------------------------------
A  No change (manual)             6.75%      8.10%     $10,149,221          $0
B  Match market median            4.85%      7.50%      $6,283,084 $-3,866,137
C  Match market leader            4.79%      7.54%      $6,179,713 $-3,969,508
D  Market leader -5 bps           4.74%      7.49%      $6,097,472 $-4,051,749
```

Scenario E -- the economically optimal price -- is the `Our rate` column: the
existing optimiser's own recommendation under each competitor assumption. The
script calls `run_baseline_diagnostics` once per scenario and reimplements no
pricing logic.

### Dashboard

```bash
python3 -m pip install -e ".[dashboard]"
streamlit run scripts/streamlit_dashboard.py
```

The **NZ market intelligence** section renders above the scenario runner, so the
market is visible without running an optimisation: the bank-by-term grid,
per-term summary metrics and ranking, market state, recent repricing, and a rate
history chart.

## Pricing-engine integration

Competitor pricing has always entered the optimiser through
`EngineConfig.market.competitor_loan_rate` (the gap elasticity in
`synthetic/response.py`) and `market.home_loan_reference_rate` (the anchor for
market-relative spread grids). That has not changed. What is new is where the
number comes from.

```python
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    apply_market_strategy_to_config,
)

config = apply_market_strategy_to_config(
    config,
    strategy=CompetitorRateStrategy.MARKET_MEDIAN,
    snapshot=service.get_market_snapshot(),
    term_months=12,
)
diagnostics = run_baseline_diagnostics(config)   # unchanged
```

| Strategy | Resolves to |
| --- | --- |
| `MANUAL` | the rate already in the config (**default**) |
| `MARKET_MEDIAN` | median advertised rate across tracked banks |
| `MARKET_LEADER` | most competitive advertised rate |
| `MARKET_SECOND_LOWEST` | best rate excluding banks tied at rank 1 |
| `MARKET_MEAN` | arithmetic mean across tracked banks |

`offset_bps` shifts the resolved rate, which is how "market leader minus 5 bps"
is expressed without inventing a strategy per offset.

**Manual assumptions still work exactly as before.** Under `MANUAL` the config
object is returned unchanged -- not copied, not revalidated -- so every existing
scenario, config file, and test is unaffected. This is asserted directly by
`test_manual_leaves_the_config_completely_untouched`.

Only the loan side is overlaid. Term-deposit intelligence is phase 2, so
`market.competitor_deposit_rate` keeps whatever the config holds.

## Adding a sixth bank

1. Create `competitor_rates/collectors/<bank>.py` with a subclass of
   `BankPricingCollector` declaring `bank`, `home_loan_url`, and optionally
   `term_deposit_url`.
2. Set the product markers if the bank brands its discounted rates its own way
   (BNZ calls them "Classic"):

   ```python
   class RabobankCollector(BankPricingCollector):
       bank = "Rabobank"
       home_loan_url = "https://www.rabobank.co.nz/..."
       special_product_markers = ("special",)
       min_equity_product_markers = ("special",)
   ```
3. Register it in `COLLECTOR_CLASSES` in `collectors/__init__.py`.
4. Save a fixture page at `tests/fixtures/pricing_intelligence/rabobank_home_loans.html`.
5. Override `parse_mortgage_html` only if the shared table parser cannot read
   the page.

Nothing in validation, storage, change detection, market statistics, or the
pricing strategy needs to change.

## Troubleshooting a broken parser

A collector reporting `FAILED  ParserError` fetched the page but recognised no
rates. Usually the bank restructured its markup.

1. Reproduce and capture the page:
   ```bash
   python3 -m balance_pricing_engine.pricing_intelligence --verbose collect
   ```
   (`--verbose` is a parent-parser option and must come before the `collect`
   subcommand, not after -- `collect --verbose` fails with
   `unrecognized arguments: --verbose`.)
   Snapshots land in `data/raw/competitor_rates/<date>/<bank>_home_loan.html`.
2. Check whether the rates are in the static HTML at all. If the file contains
   the page furniture but no rate numbers, the table is client-rendered and
   needs `--render-javascript`.
3. Diff the snapshot against the saved fixture to see what moved.
4. Fix the bank's collector, refresh its fixture, and confirm the test suite.

A `SUSPICIOUS` flag on a change is the other signal worth chasing: it usually
means the parser has started reading a different column (a standard rate where a
special used to be), not that the bank moved 300 bps overnight. The change is
kept and flagged rather than dropped, because a genuine large reprice must not
be silently discarded. Verify against `source_url` and `raw_text_context`, both
retained on every record.

## Data provenance

Every observation retains `bank`, `source_url`, `captured_at_utc`,
`raw_text_context` (the exact row text it was parsed from),
`raw_html_sha256`, and `parser_version`. Derived eligibility fields
(`max_lvr`, `min_equity`, `cashback_text`, `package_requirement`) are parsed
only from that retained text, so each one can be audited back to its evidence.

Raw HTML snapshots are written per collection under `data/raw/`, which is
git-ignored. They are diagnostic material, not a permanent archive.

## Limitations

- **Advertised rates are not negotiated rates.** Every NZ bank discounts off
  carded rates for individual customers. Nothing here observes what customers
  actually pay.
- **Collectors are fixture-verified, not live-verified.** All five parse their
  saved fixtures correctly. Against live pages they currently return
  `ParserError`: the pages fetch successfully, but every tracked bank builds its
  rate table client-side, so the static HTML contains no rates. Live collection
  requires `--render-javascript` with the `browser` extra, and the resulting
  output has not yet been checked against the live pages field by field.
- **Bank pages change.** A collector will eventually break. It will report
  `FAILED`, not invent numbers.
- **Promotional eligibility matters.** A market-leading rate may require 20%
  equity, a salary credit, or new lending only. These are captured where
  published, but a rate comparison is not an eligibility comparison.
- **Rate-category comparisons must be like for like.** Snapshots default to
  `special`, since front-book competition is fought there. Comparing a special
  against a standard rate is meaningless.
- **A first collection reports no changes.** Everything being new is not market
  intelligence.
- **Term deposits are phase 2.** The domain model, storage, and collector
  plumbing support them, and `collect_term_deposit_rates` exists, but the
  per-bank tuning and market logic target mortgages.
- **This is decision-support/research software.** Public market data does not
  replace internal bank pricing governance, model risk review, or approval.

## Configuration

All thresholds live in `PricingIntelligenceConfig`
([`config.py`](../src/balance_pricing_engine/pricing_intelligence/config.py)):
request timeout, retries, backoff, inter-request delay, user agent, rate bounds,
suspicious-movement threshold, database path, tracked terms, selected banks, and
market-state cut-offs. There are no magic numbers scattered through the
pipeline.

## Testing

```bash
python3 -m pytest tests/unit/test_pricing_intelligence_*.py -q
```

No test touches a live bank website. Collection is exercised through saved HTML
fixtures via the `fixture_fetcher` conftest fixture, which can inject network
failures, page mutations, and unparseable pages.
