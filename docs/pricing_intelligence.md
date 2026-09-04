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
| Alert formatting and webhook delivery | `pricing_intelligence/alerts.py` | New |
| Competitor-rate and cash-rate strategy | `pricing_intelligence/strategy.py` | New |
| RBNZ Official Cash Rate intelligence | `pricing_intelligence/rbnz.py` | New |

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

### Scheduled collection

Observation history **cannot be backfilled** -- a day not collected is gone --
and everything time-based (change detection, market state, the 7d/30d features,
history charts) is inert until collections accumulate. A single snapshot reports
`LOW - no rate changes detected` regardless of what the market is doing.

[`scripts/collect_market_rates_workflow.py`](../scripts/collect_market_rates_workflow.py)
is the unattended entry point. It adds what a scheduler needs and an interactive
session does not: a single-instance lock (an overrunning browser-rendered
collection must not interleave with the next schedule and write two runs at
once), JSON logging via `observability.logging`, a machine-readable run summary,
and exit codes worth alerting on.

| Exit | Meaning |
| --- | --- |
| 0 | Every bank fetched, and every accepted row passed validation with adequate coverage |
| 1 | Nothing usable was stored -- either no bank could be fetched, or every fetched row was rejected by validation (`rate_count == 0`) |
| 2 | Partial: at least one bank failed to fetch, at least one row was rejected by validation (`validation.rejected`), or a bank matched too few of its previously known rate lines to trust (`low_coverage_banks`, below `min_bank_coverage_ratio`) |
| 3 | Skipped, another collection was already running |

Exit codes are not solely about fetch success: a bank whose page fetches and
parses cleanly can still push the run to exit 1 or 2 if what it returned fails
validation or covers too little of its known range -- see
`exit_code_for()` and `build_summary()`'s `banks_failed`/`failures` fields in
[`scripts/collect_market_rates_workflow.py`](../scripts/collect_market_rates_workflow.py)
for exactly which case applied. Exit 2 is deliberately distinct from 0: an
incomplete or unreliable market view produces a median over fewer (or less
trustworthy) banks than it reports, which is exactly the failure mode live
verification uncovered.

```bash
python3 scripts/collect_market_rates_workflow.py \
    --config configs/base.yaml \
    --render-javascript \
    --summary-file data/processed/pricing_intelligence/last_run.json
```

`--config` loads the `pricing_intelligence` block (selected banks, validation
thresholds, collection delays, database path) from an EngineConfig YAML, same
as the interactive CLI. Without it, this falls back to
`PricingIntelligenceConfig()` defaults and silently ignores anything
configured there.

`--product` selects which market this invocation collects (default:
`home_loan`). **Term deposits are a separate market and need their own
scheduled invocation** with `--product term_deposit` -- one schedule alone
never refreshes both. Give each invocation its own `--lock-file` and
`--summary-file`: sharing either between the two products makes the second
one either get skipped (`EXIT_ALREADY_RUNNING`) or silently overwrite the
other product's summary.

```bash
python3 scripts/collect_market_rates_workflow.py \
    --config configs/base.yaml \
    --product term_deposit \
    --render-javascript \
    --lock-file data/processed/pricing_intelligence/collection-term-deposit.lock \
    --summary-file data/processed/pricing_intelligence/last_run_term_deposit.json
```

Ready-made schedules are in [`deploy/scheduling/`](../deploy/scheduling/):

- `nz.pricing-intelligence.collect.plist` -- macOS launchd agent for home
  loans, daily at 07:30 local, with `RunAtLoad` so a machine asleep at the
  scheduled time collects on next wake rather than skipping the day.
- `nz.pricing-intelligence.collect-term-deposit.plist` -- the sibling agent
  for term deposits, daily at 07:35 local. Install both to keep either
  market fresh.
- `crontab.example` -- the equivalent cron entries for both products.

All three need their `REPLACE_ME` / `/path/to` paths edited before use.

### Where history lives

**Durable observation history is local**, in the SQLite store the scheduled job
writes to. It is deliberately not committed: `data/processed/pricing_intelligence/`
and `*.db` are git-ignored, and a binary database in git would churn badly and
diff meaninglessly.

The [collector canary](../.github/workflows/collector-canary.yml) workflow is a
*different thing* and is easy to confuse with a history store. It runs on a
schedule, collects into a throwaway database, and reports only whether each
collector still parses its live page. Its purpose is to notice a bank redesign
breaking a parser -- because live verification is point-in-time, not a standing
guarantee. It is kept out of `ci.yml` on purpose: it reaches live bank websites,
so it must never gate a pull request.

If durable history ever needs to live somewhere shared, that is a real design
decision (a committed text export, an object store, or a hosted database) and
should be made explicitly rather than by letting CI accrete state.

### Alert delivery

A detected change sitting in the SQLite store is not the same as anyone
knowing about it. Set an env var and the scheduled job pushes every rate move
to a webhook:

```bash
export PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL="https://hooks.slack.com/services/T000/B000/xxxx"
python3 scripts/collect_market_rates_workflow.py --config configs/base.yaml --render-javascript
```

The payload's message goes in a `"text"` field by default, which **Slack
incoming webhooks** and the legacy **Microsoft Teams Office 365 connector**
both render directly with no further setup. Pass `--webhook-text-field content`
for a **Discord** webhook instead. Structured fields (`bank`, `change_type`,
`change_bps`, `suspicious`, `detected_at`) ride alongside the message for any
consumer that parses JSON rather than displaying it.

The env var is unset by default -- **no delivery channel is the default
state**, matching how `ANTHROPIC_API_KEY` and `PRICING_ENGINE_API_KEYS` are
handled elsewhere in this repo (`secrets.SecretsProvider`, read once, absent
means the feature is off). Nothing else changes when it is unset: the run
summary and structured logs are unaffected either way.

**A broken or unreachable webhook never fails the collection run.**
`alerts.dispatch_alerts_safely()` mirrors
`events.publisher.publish_safely()`: each alert is attempted independently, a
failure is logged and the run continues, and the exit code reflects
collection success or failure exactly as it did before any webhook was
configured. `summary["alerts_sent"]` reports how many actually got through, so
a silently-broken webhook is visible in the run summary even though it never
turns into a failing exit code.

A rate-limited webhook (HTTP 429) is retried after honoring its `Retry-After`
header, up to three attempts, so a multi-term repricing burst is not silently
truncated after the first message. `run_collection()` calls
`dispatch_alerts_safely()` synchronously while holding its single-instance
lock, before writing the run summary, so a misbehaving endpoint must not be
able to stall that indefinitely: the per-wait time is capped
(`MAX_RETRY_AFTER_SECONDS`), and each alert's `sink.send()` runs on its own
thread joined against whatever remains of the overall
`DEFAULT_ALERT_DISPATCH_DEADLINE_SECONDS` budget -- a per-alert check alone
would not be enough, since one alert's own retries could otherwise exhaust
the whole budget before control returned to the dispatch loop. A send that is
still running when its slice of the budget runs out is left to finish (or
not) in the background rather than waited on further, the same trade-off
`events.publisher.publish_safely()` makes for a hung publisher.

Only rate moves are pushed -- a new or withdrawn product is a real market
event but is not a reprice, and the CLI's own `--alerts` path (`collect
--alerts`, `changes --alerts`) makes the same choice. Console output there
stays console-only; a human running the CLI interactively is already looking
at the terminal, so a duplicate webhook push would be noise. Webhook delivery
is scheduled-job-only by design.

To use a delivery channel with a different payload shape entirely (email, a
paging system, the in-process event publisher), implement the two-method
`AlertSink` protocol in
[`pricing_intelligence/alerts.py`](../src/balance_pricing_engine/pricing_intelligence/alerts.py)
and pass it to `dispatch_alerts_safely()` -- `WebhookAlertSink` is one concrete
sink, not the only extension point.

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
A  No change (manual)             4.99%      6.19%      $5,920,859          $0
B  Match market median            4.99%      6.19%      $5,920,859          $0
C  Match market leader            4.95%      6.15%      $5,826,903    $-93,956
D  Market leader -5 bps           4.90%      6.15%      $5,718,761   $-202,099
```

Scenario E -- the economically optimal price -- is the `Our rate` column: the
existing optimiser's own recommendation under each competitor assumption. The
script calls `run_baseline_diagnostics` once per scenario and reimplements no
pricing logic.

A and B coincide here because `configs/base.yaml` was recalibrated on
2026-08-26 so its manual competitor default *is* the verified market median.
Before that recalibration the manual default was 6.75% -- 176 bps above the real
market -- and A diverged wildly from every market-derived scenario. That the two
now agree is the point: the manual assumption is no longer silently stale.

The recommended rate sits above the market because the optimiser maximises
unconstrained risk-adjusted profit. It models no competitive reaction and no
market-share or volume constraint, so a premium over market is a coherent
optimum *for the model*. Real banks price nearer the market because of
strategic constraints this engine does not represent. See
[modelling_assumptions.md](modelling_assumptions.md).

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

## Bank coverage

Verified live on 2026-08-25 with `--render-javascript`. Rate counts are total
records parsed, across all categories.

| Bank | Collector | Live verified | Rates | Unmarked fixed row means |
| --- | --- | --- | --- | --- |
| ANZ | `collectors/anz.py` | 2026-08-25 | 15 | labelled in-row (no default) |
| ASB | `collectors/asb.py` | 2026-08-25 | 11 | `special` |
| BNZ | `collectors/bnz.py` | 2026-08-25 | 9 | `special` |
| Westpac | `collectors/westpac.py` | 2026-08-25 | 17 | `standard` |
| Kiwibank | `collectors/kiwibank.py` | 2026-08-25 | 14 | labelled in-row (no default) |

Observed market at that time (advertised specials):

```text
Bank               6M       1Y      18M       2Y       3Y       4Y       5Y
---------------------------------------------------------------------------
ANZ              4.79     4.99     5.45     5.49     5.59        -        -
ASB              4.79     4.99     5.35     5.45     5.45     5.49     5.59
BNZ              4.79     4.99     5.29     5.45     5.45     5.45     5.49
Kiwibank         4.75     4.95        -     5.39     5.49     5.59     5.69
Westpac          4.75     4.99     5.35     5.45     5.39     5.39     5.49
---------------------------------------------------------------------------
Median           4.79     4.99     5.35     5.45     5.45     5.47     5.54
Best             4.75     4.95     5.29     5.39     5.39     5.39     5.49
Spread bps          4        4       16       10       20       20       20
```

## Term deposit market

```bash
python3 -m balance_pricing_engine.pricing_intelligence --product term_deposit collect --render-javascript
python3 -m balance_pricing_engine.pricing_intelligence --product term_deposit snapshot
```

Observed 2026-08-27 (advertised term-deposit tier, PIE excluded):

```text
Bank               6M       1Y      18M       2Y       3Y       5Y
------------------------------------------------------------------
ANZ              3.55     3.90     4.20     4.30     4.40     4.70
ASB              3.55     3.90     4.15     4.20     4.40     4.75
BNZ              3.55     4.05     4.20     4.20     4.40     4.70
Kiwibank         3.50     4.00        -     4.30     4.40     4.70
Westpac          3.45     3.90     4.20     4.30     4.40     4.60
------------------------------------------------------------------
Median           3.55     3.90     4.20     4.30     4.40     4.70
Best             3.55     4.05     4.20     4.30     4.40     4.75
Spread bps         10       15        5       10        0       15
```

Deposit spreads (0-15 bps) are far tighter than mortgage spreads (4-20 bps at
short terms, widening to 20 at five years), which is what a less
broker-intermediated product looks like.

Each product keeps its own baseline, change history, and bootstrap state, so
collecting one is never read as the other being withdrawn.

### Feeding deposits into the optimiser

```python
config = apply_market_strategy_to_config(
    config,
    strategy=CompetitorRateStrategy.MARKET_MEDIAN,
    snapshot=loan_snapshot,
    deposit_snapshot=deposit_snapshot,
    deposit_strategy=CompetitorRateStrategy.MARKET_LEADER,
)
```

Each side resolves independently: a market-derived loan rate with a manual
deposit assumption is a legitimate scenario. Omitting `deposit_snapshot` leaves
`market.competitor_deposit_rate` exactly as the config holds it, and `MANUAL` on
both sides still returns the identical config object.

## RBNZ Official Cash Rate

A separate data source from the five bank collectors above: the RBNZ's own
published series for `market.cash_rate`, the one funding-cost input this engine
had never verified until 2026-09-03. Live-verified against
[the RBNZ's exchange-rates-and-wholesale-interest-rates page](https://www.rbnz.govt.nz/statistics/key-statistics/exchange-rates-and-wholesale-interest-rates):
the current OCR is **2.50%**, alongside the 90-day bank bill rate (2.98% as at
Aug-2026).

```bash
python3 -m balance_pricing_engine.pricing_intelligence ocr
```

```text
RBNZ OFFICIAL CASH RATE

  OCR                    2.50%
  90-day bank bill rate  2.98%
  as at                  Aug-2026
  change over 1 month    +0 bps
  change over 3 months   +25 bps
  change over 12 months  -50 bps
  source                 https://www.rbnz.govt.nz/statistics/key-statistics/exchange-rates-and-wholesale-interest-rates
```

**Shape difference from the bank collectors, on purpose.** RBNZ's own chart
already carries the full monthly OCR history back to 1999 in a single page
load, so unlike bank rates -- a fresh number that must be accumulated over many
collections to build history -- there is nothing to accumulate here. Each call
to `fetch_ocr_series()` returns the whole series. There is no local
observation store, no change detection, and no scheduled collection for this
module: a single point-in-time read is already as complete as the source
itself.

**Parsing anchor, and why it matters.** The RBNZ page renders several charts
with an identical accessible-table shape (`Date | series | series`) as a
sighted-user-equivalent companion to each visual chart: one for the OCR and
90-day bank bill rate, others for exchange rates and the trade-weighted index.
All of them look alike by column shape alone. An early version of this
collector grabbed the first `Date`-headed table on the page and silently
returned NZD/USD exchange-rate values (around 0.55-0.60) as if they were the
OCR -- a plausible-looking float, and one a numeric range check would not have
caught. The parser now anchors on the "Official Cash Rate" heading text and
takes the table that follows it, never the first table matching the expected
shape. The same category of defect as the PIE-effective-rate and managed-fund
rows found during term-deposit live verification: two data series sharing a
table shape are not the same series, and only a semantic anchor tells them
apart.

**Fetch strategy is dedicated, not shared.** The bank collectors'
`build_browser_fetcher()` waits for the page to reach `networkidle`, which
works for bank rate pages but never fires on this one -- the RBNZ statistics
page keeps background network activity going indefinitely (chart telemetry,
related-content widgets). `rbnz.build_dashboard_browser_fetcher()` waits only
for the DOM, then gives the chart widget a fixed settle window to render;
confirmed against the live page to be enough for the accessible data table to
appear.

### Feeding the OCR into the optimiser

```python
from balance_pricing_engine.pricing_intelligence.rbnz import fetch_ocr_series
from balance_pricing_engine.pricing_intelligence.strategy import (
    CashRateStrategy,
    apply_cash_rate_strategy_to_config,
)

series = fetch_ocr_series()
config = apply_cash_rate_strategy_to_config(
    config, strategy=CashRateStrategy.RBNZ_OCR, ocr_series=series
)
```

A second, independent seam from `apply_market_strategy_to_config`: the OCR
comes from RBNZ, not the bank collectors, and feeds a different `MarketConfig`
field. `CashRateStrategy.MANUAL` is the default and returns the config object
completely unchanged, the same invariant the competitor-rate strategy holds.
Only `cash_rate` moves -- `wholesale_funding_spread` is a separate, still
unverified assumption (see
[modelling_assumptions.md](modelling_assumptions.md#cash-rate-correction-2026-09-03))
and is deliberately left alone rather than guessed at.

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
- **Collectors are live-verified as of 2026-08-25, and require JavaScript
  rendering.** All five were run against the live official pages with
  `--render-javascript` and their parsed values checked field by field. The
  static-HTML path returns `ParserError` for every bank: the rate tables are
  built client-side, so live collection needs the `browser` extra. A live run is
  a point-in-time check, not a standing guarantee -- re-verify after any bank
  redesign.
- **Rate-tier categorisation is per-bank knowledge.** Live pages put "Special"
  in the table *heading*, not in each row, so rows arrive as bare
  `6 month | 4.79% p.a.` with no tier word. What an unmarked row means differs
  by bank -- for ASB and BNZ it is the special tier, for Westpac it is the
  carded rate -- so each collector declares `default_fixed_category`. This
  cannot be inferred generically, and getting it wrong silently drops banks from
  the market comparison.
- **Not all advertised mortgage rates are comparable.** Subsidised green/top-up
  lending (ANZ Good Energy, ASB Better Homes, both at 1.00%), bridging finance
  (ANZ Tideover), and products closed to new applications (ASB Back My Build)
  are advertised rates but not front-book fixed lending. They are captured under
  the `non_comparable` category -- retained for audit, fenced out of every
  market statistic, since a subsidised 1.00% rate would wreck any median it
  reached.
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
- **Term deposits are live-verified as of 2026-08-27, with caveats.** All five
  banks collect (ANZ 75, ASB 13, BNZ 46, Westpac 56, Kiwibank 34 raw rows).
  Deposit pages are messier than mortgage pages, and two classes of row are
  fenced into `non_comparable`:
  - **Managed-fund and KiwiSaver returns.** BNZ lists these in the same tables;
    a "1 year | 12.47% | Growth Fund" row was being read as a 12.47% one-year
    term deposit.
  - **PIE "effective rate of return" columns.** ANZ and Westpac publish
    tax-adjusted returns beside the advertised rate, one per marginal tax band.
    Because deposits rank highest-is-best, including these made both banks look
    like market leaders by 70 bps when their advertised 1-year rate (3.90%) is
    at parity with ASB.

  Deposit elasticities and `rate_anchor` were recalibrated against this data on
  2026-09-02; see
  [modelling_assumptions.md](modelling_assumptions.md#deposit-response-calibration-2026-09-02).
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
