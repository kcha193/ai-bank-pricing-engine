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

### Telegram delivery

Independent of, and stackable with, the webhook above -- both can be
configured at once, and each dedups and delivers separately.

```bash
export TELEGRAM_BOT_TOKEN="123456:AAAA-your-bot-token"
export TELEGRAM_CHAT_ID="-1001234567890"
python3 -m balance_pricing_engine.pricing_intelligence telegram-test
```

Create a bot via [@BotFather](https://t.me/BotFather) to get a token, then
message the bot (or add it to a group and send any message there) to learn
the chat id -- `https://api.telegram.org/bot<token>/getUpdates` echoes it
back. Both variables must be set; either alone is treated as "not
configured", same as the webhook's "unset means off" default. Plain text
only -- no `parse_mode` -- so a bank or product name containing `_`/`*`/`[`
can never break Telegram's Markdown/HTML parser and drop the message.

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

`LOW` means "collected, and nothing moved" — it never means "we have no data".
With no fresh market the command prints `No fresh market data; market activity
is unknown.` and **exits 1** rather than reporting a calm market. Any external
scheduler wrapping `market-state` should expect that nonzero exit. The collect
worker (`scripts/collect_market_rates_workflow.py`) does not invoke this
command and keeps its own 0/1/2/3 exit codes.

### Scenario analysis

```bash
python3 examples/run_market_intelligence_scenarios.py --term 12
```

```text
   Scenario                  Competitor   Our rate       Objective        vs A
------------------------------------------------------------------------------
A  No change (manual)             4.99%      6.19%      $5,920,859          $0
B  Median assumption              4.99%      6.19%      $5,920,859          $0
C  Leader assumption              4.95%      6.15%      $5,826,903    $-93,956
D  Leader -5 assumption           4.90%      6.15%      $5,718,761   $-202,099
```

Scenario E -- the economically optimal price -- is the `Our rate` column: the
existing optimiser's own recommendation under each competitor assumption. The
script calls `run_baseline_diagnostics` once per scenario and reimplements no
pricing logic.

A and B coincide here because `configs/base.yaml` was recalibrated on
2026-08-26 so its manual competitor default *is* the verified market median.
Before that recalibration the manual default was 6.75% -- 176 bps above the real
market -- and A diverged wildly from every market-derived scenario. That the two
agree in this dated example does not keep the manual assumption current.
Recheck it against a fresh snapshot before each analysis. These scenarios shift
the competitor assumption; they do not force our customer rate to match it.

The recommended rate sits above the market because the optimiser maximises
unconstrained risk-adjusted profit. It models no competitive reaction and no
market-share or volume constraint, so a premium over market is a coherent
optimum *for the model*. Real banks price nearer the market because of
strategic constraints this engine does not represent. See
[modelling_assumptions.md](modelling_assumptions.md).

### Dashboard

```bash
python3 -m pip install -e ".[dashboard]"
streamlit run src/balance_pricing_engine/dashboard/app.py
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
- **Mortgage advertised values and tiers were independently reverified on
  2026-09-06; eligibility extraction remains incomplete. They require JavaScript
  rendering.** All five were run against the live official pages with
  `--render-javascript` and their rates, terms and current table headers checked.
  See the [independent audit](audits/2026-09-06-pricing-intelligence-audit.md). The
  static-HTML path returns `ParserError` for every bank: the rate tables are
  built client-side, so live collection needs the `browser` extra. A live run is
  a point-in-time check, not a standing guarantee -- re-verify after any bank
  redesign.
- **Rate-tier categorisation is per-bank knowledge.** Live pages put "Special"
  in the table *heading*, not in each row, so rows arrive as bare
  `6 month | 4.79% p.a.` with no tier word. What an unmarked row means differs
  by bank. ASB uses a special heading; Westpac unmarked rows are carded.
  BNZ's current explicit Standard header overrides its historical Classic/special
  fallback. Therefore BNZ is collected but excluded from the default special
  snapshot. Each collector declares `default_fixed_category`; header changes
  require review because a plausible rate alone does not establish its tier.
- **Not all advertised mortgage rates are comparable.** Subsidised green/top-up
  lending (ANZ Good Energy, ASB Better Homes, both at 1.00%), bridging finance
  (ANZ Tideover), and products closed to new applications (ASB Back My Build)
  are advertised rates but not front-book fixed lending. They are captured under
  the `non_comparable` category -- retained for audit, fenced out of every
  market statistic, since a subsidised 1.00% rate would wreck any median it
  reached.
- **Bank pages change.** Zero recognised rates produces `FAILED`, but a parser
  can also return plausible, wrongly classified rates with `SUCCESS`. Review
  coverage, table headers and source evidence after any redesign.
- **Promotional eligibility matters.** A market-leading rate may require 20%
  equity, a salary credit, or new lending only. Row/header conditions are
  captured where recognised; page footnotes can be missed. The audit found
  ANZ salary-credit conditions and Kiwibank special-scheme exceptions absent
  from canonical eligibility. Verify the full source conditions before use.
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
pipeline. `health.warning_after_failures`/`health.alert_after_failures`
control collector-failure escalation (see Operations below).

## Pricing Intelligence Operations

This section covers the v0.2.0 operational layer: scheduling, Telegram/webhook
alerting, health monitoring, briefings, and auto-triggered pricing scenarios.
The distinction to keep in mind throughout:

| Layer | Answers | Where |
| --- | --- | --- |
| Pricing intelligence | "What is happening?" | `pricing_intelligence/` (collection, snapshots, changes) |
| Pricing engine | "What are the economic consequences?" | `optimization/`, `pricing/`, `scenarios/` |
| Alerts / briefing | "What needs my attention?" | `orchestration.py`, `briefing.py`, `alerts.py` |

### First-time setup

```bash
python3 -m pip install -e ".[browser]"      # collectors need headless-browser rendering
python3 -m playwright install chromium
cp .env.example .env                        # then fill in whichever channels you want
```

Nothing above is required to run the engine's synthetic pricing scenarios --
only for live NZ market collection.

### Environment variables

See [`.env.example`](../.env.example) for the full list with placeholders.
Every one is optional; each feature it gates is off until set.

| Variable | Gates |
| --- | --- |
| `PRICING_INTELLIGENCE_DB_PATH` | Fallback database path for the CLI/scripts when `--database`/`--config` don't set one |
| `PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL` | Slack/Teams/Discord-compatible webhook alert delivery |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram alert delivery (both required) |
| `ANTHROPIC_API_KEY` | The separate LLM decision-support layer (`llm/`), unrelated to alerting |
| `PRICING_ENGINE_API_KEYS` | The optional FastAPI serving layer's auth |

### Manual run

```bash
python3 -m balance_pricing_engine.pricing_intelligence run --render-javascript
```

One command: collect → validate → persist → detect changes → classify market
state → update collector health → dispatch alerts (webhook/Telegram, whichever
configured) → persist a `RunRecord`. Prints the same collection report
`collect` does, plus status/alerts-sent/run id. Takes the same single-instance
lock the scheduled workflow uses (`--lock-file`, default
`data/processed/pricing_intelligence/collection.lock`), so a manual and a
scheduled run cannot interleave against the same database.

Does **not** auto-trigger pricing scenarios (see below) -- that requires the
optimiser, which `pricing_intelligence` (this CLI included) is architecturally
forbidden from importing. Use the scheduled workflow script for
scenario-triggered runs, or run `collect`/`brief` and inspect the scenario
manually via `examples/run_market_intelligence_scenarios.py`.

The lower-level commands (`collect`, `snapshot`, `changes`, `history`,
`market-state`, `position`, `strategy`) remain available for debugging and
exploration, unchanged.

### Scheduled run

Unchanged mechanics from before v0.2.0 (see "Scheduled collection" above) --
[`scripts/collect_market_rates_workflow.py`](../scripts/collect_market_rates_workflow.py)
now additionally does everything `run` does (Telegram, collector health,
`RunRecord` persistence, and -- when `--config` is given -- auto-triggered
scenarios), through the same `orchestration.run_operational_cycle()` the
interactive `run` command uses. Nothing about the lock/exit-code/summary-file
contract changed.

```bash
python3 scripts/collect_market_rates_workflow.py \
    --config configs/base.yaml \
    --render-javascript \
    --lock-file data/processed/pricing_intelligence/collection.lock \
    --summary-file data/processed/pricing_intelligence/last_run.json
```

`--config` is what enables scenario-triggering here (see "How pricing
scenarios are triggered" below) -- without it, this behaves exactly as a
`--database`-only invocation always has: collection, alerting, and health
tracking, no scenario runs.

| Time | Timezone | Command | Deployment |
| --- | --- | --- | --- |
| 07:30 | Pacific/Auckland (explicit in the `.plist`/crontab, never machine-local) | home-loan collection | `deploy/scheduling/nz.pricing-intelligence.collect.plist` (launchd) or `crontab.example` |
| 07:35 | Pacific/Auckland | term-deposit collection | sibling `.plist` / second crontab line |

Both ready-made schedules in [`deploy/scheduling/`](../deploy/scheduling/)
document Telegram configuration alongside the existing webhook guidance --
launchd needs the variables added to the `.plist`'s `EnvironmentVariables`
block (it does not inherit your shell's exported variables); cron inherits a
plain `VAR=value` line placed above the job in the same crontab.

### Daily workflow

```bash
python3 scripts/collect_market_rates_workflow.py --config configs/base.yaml --render-javascript
python3 -m balance_pricing_engine.pricing_intelligence brief --send
```

Or, in one scheduled line: `... workflow.py ... && ... brief --send`. The
brief is a separate command by design (see the layer table above) -- `run`/the
scheduled workflow answers "what happened", `brief` answers "what needs your
attention", and chaining them is an operator choice, not a hidden coupling.

### Weekly workflow

```bash
python3 -m balance_pricing_engine.pricing_intelligence brief --weekly --send
```

Run this any time (Sunday evening / Monday morning is a reasonable schedule);
it summarises the last `market_state.lookback_days` (7, by default) of
activity from whatever is already stored -- no separate weekly collection
needed.

### Health checks

```bash
python3 -m balance_pricing_engine.pricing_intelligence health
```

Reports: `PRAGMA integrity_check` plus a few lightweight data-quality checks
(`ObservationStore.check_integrity()` -- duplicate current keys, implausible
rates, unparseable timestamps), the most recent `RunRecord` and its status,
per-bank collector health (`FRESH` / `FAILING (Nx)` / `UNKNOWN` for a bank
never yet collected), whether Telegram/webhook are configured, and whether the
latest market snapshot is `VALID` or `EMPTY`. Exits non-zero when the
integrity check finds an issue -- suitable for a monitoring check, not just
human reading.

### Database backup

```bash
python3 -m balance_pricing_engine.pricing_intelligence backup --destination ~/pricing-intelligence-backups
```

Writes a timestamped, transactionally-consistent copy
(`sqlite3.Connection.backup()`, not a raw file copy -- safe even if the
database is open elsewhere) and prunes down to `--keep` (default 30) newest
backups. Store the destination **outside the repository** -- there is no
retention policy beyond the count-based pruning above, so pick a destination
your own backup/off-site strategy already covers. Run it on whatever cadence
matches how much history you can afford to lose (daily, alongside the morning
collection, is a reasonable default for a personal deployment).

### Telegram testing

```bash
python3 -m balance_pricing_engine.pricing_intelligence telegram-test
```

Sends a fixed `✅ NZ Pricing Intelligence Telegram test successful.` message.
Does not require a real market event or a populated database -- exactly for
verifying delivery before relying on it during a scheduled run.

### Dry run

```bash
python3 -m balance_pricing_engine.pricing_intelligence run --dry-run
```

Collects, validates, and classifies exactly as a real run does, but persists
no observations, writes no `RunRecord`, updates no collector health, and sends
no real alerts -- every alert (including a scenario-enriched one, if scenario-
triggering were wired in at this call site) is printed to the console instead.
Safe to run against a real Telegram/webhook configuration: nothing reaches
those channels. Useful when iterating on a collector or a threshold change
without touching production history.

`collect --no-persist` (unchanged from before v0.2.0) is the equivalent for
the lower-level `collect` command specifically.

### Collector troubleshooting

See the Runbook below ("Bank collector fails").

### Stale-data behaviour

`max_observation_age_hours` (default 168h / 7 days) is the single freshness
threshold: a bank's last-known rate stops feeding market-derived pricing
benchmarks (median, leader, rankings) once its most recent confirmation is
older than that, and the dashboard's freshness table / `health` command both
surface exactly which bank and how old. An unreachable bank is "unknown, not
withdrawn" for change-detection purposes (it stays in the baseline
indefinitely, so it does not falsely read as every one of its products being
withdrawn), but a stale value is not allowed to silently keep influencing a
*live* pricing view forever -- those are two different questions with two
different, deliberately separate answers. (v0.2.0 kept this single-threshold
design rather than adding a second `FRESH_MAX_AGE_HOURS`/`STALE_MAX_AGE_HOURS`
tier -- the existing Fresh/Stale/Missing states, plus the "quarantined" and
"not fully confirmed in latest run" states already covered above, give
conservative, explicit freshness without more config surface.)

### How pricing scenarios are triggered

A scheduled/manual `run` invoked **with `--config`** (so a full pricing
`EngineConfig`, not just the `pricing_intelligence` block, is loaded) checks
every tracked, comparable-category term for a genuine (non-suspicious) rate
move detected that run -- any bank, not just a configured `our_bank`. Each
triggered term runs the fixed scenario set from
[`pricing_scenario_trigger.py`](../src/balance_pricing_engine/pricing_scenario_trigger.py),
reusing exactly the seam `examples/run_market_intelligence_scenarios.py`
already established (`strategy.apply_market_strategy_to_config` +
the optimiser's baseline-diagnostics entry point -- no optimiser logic is
duplicated):

| Scenario | Competitor assumption |
| --- | --- |
| No change | The config's existing manual rate |
| Match median | `MARKET_MEDIAN` |
| Match leader | `MARKET_LEADER` |
| Leader -5bps | `MARKET_LEADER`, offset -5bps |
| Model optimum | Match median's own optimiser answer, relabelled |

Results are persisted to the `scenario_runs` table (keyed by `run_id` +
`term_months`, queryable via `ObservationStore.get_scenario_runs`/
`get_recent_scenario_runs`, and visible in the dashboard's "Pricing scenarios"
section) and a compact block is appended to the triggering rate-move alert:

```text
PRICING ENGINE

Current rate:     4.89%
Match median:     4.85%
Match leader:     4.79%
Model optimum:    4.84%

Decision support only -- no automatic pricing action taken.
```

This is decision support only. Nothing here writes a price -- see
`pricing_scenario_trigger.py`'s own docstring for why this deliberately lives
outside `pricing_intelligence/` (that package is
architecturally forbidden from importing the optimiser; see
`tests/unit/test_pricing_intelligence_boundaries.py`).

### How to disable alerts

Unset (or never set) `PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL`,
`TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` -- no delivery channel is the
default state, and collection/classification/health-tracking/`RunRecord`
persistence all continue unaffected. To keep a channel configured but silence
one run, use `run --dry-run` or `collect --no-persist`.

### How to disable scheduling

Remove/unload the launchd agent(s) (`launchctl unload
~/Library/LaunchAgents/nz.pricing-intelligence.collect*.plist`) or delete the
crontab line(s). Collection, `health`, `brief`, and manual `run` all keep
working on demand either way -- nothing about the CLI depends on a scheduler
being installed.

## Runbook

### Bank collector fails

1. `python3 -m balance_pricing_engine.pricing_intelligence health` -- confirm
   which bank, and how many consecutive failures.
2. Run that collector manually: `collect --render-javascript` and read the
   `FAILED` reason in the report.
3. Open the bank's official page and compare its current table structure
   against what the collector expects.
4. Run that collector's fixture tests: `pytest tests/unit/test_pricing_intelligence_collectors.py -k <bank>`.
5. If the page structure changed, update the collector in
   `competitor_rates/collectors/` (see "Adding a sixth bank" for the pattern;
   fixing one is a smaller version of the same work) and refresh its saved
   HTML fixture.
6. `run --dry-run` to confirm the fix without touching production history.
7. Deploy (redeploy/restart whatever runs the scheduled job).

Escalation is automatic and configurable
(`health.warning_after_failures`/`health.alert_after_failures`, defaults 2/3):
the 1st failure logs only, the 2nd logs a structured warning (no Telegram
send), the 3rd+ sends a Telegram/webhook alert, and a subsequent success sends
a recovery notice.

### Suspicious rate detected

1. Compare the flagged value against the bank's official page directly.
2. Determine whether it is a genuine reprice or a parser fault (mis-parsed
   digit, wrong table row, standard/special mix-up).
3. Follow the existing quarantine workflow (see "Audit hardening and
   operating limits" below) -- there is no automated approve/reject command;
   review against source evidence and correct the parser if it is a fault.
4. Re-run collection/`snapshot` once resolved.

Suspicious observations are excluded from market benchmarks and market-state
classification by construction (`compute_market_activity` skips them by
default) -- a parser fault cannot silently reach the optimiser through
scenario-triggering either, since `_scenario_trigger_candidates` only
considers non-suspicious changes.

### Telegram fails

1. `telegram-test` -- read the reported error (invalid token, wrong chat id,
   bot not a member of the target chat/group, network failure).
2. Confirm both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the
   *process* actually running the scheduled job -- launchd needs them in the
   `.plist`'s `EnvironmentVariables` block, not just your shell.
3. A broken Telegram channel never fails a collection run or changes its exit
   code -- `health` still reports "not configured"/delivery failures are only
   in the structured logs, not the exit code, by design (mirrors webhook
   delivery's existing safety guarantee).

### Database problem

1. `health` -- read which integrity check failed.
2. Restore from the most recent backup:
   `cp ~/pricing-intelligence-backups/<latest>.db data/processed/pricing_intelligence/observations.db`
   (adjust the destination to match your configured `database_path`).
3. Re-run `health` to confirm the restored copy passes.
4. If no backup exists, `check_integrity()`'s issues list names exactly what
   is wrong (schema, implausible rates, broken timestamps) -- a targeted SQL
   fix may be possible for a narrow corruption; a full rebuild from scratch
   loses history that cannot be backfilled.

## Testing

```bash
python3 -m pytest tests/unit/test_pricing_intelligence_*.py -q
```

No test touches a live bank website. Collection is exercised through saved HTML
fixtures via the `fixture_fetcher` conftest fixture, which can inject network
failures, page mutations, and unparseable pages.


## Audit hardening and operating limits (2026-09-06)

- `our_bank` excludes that bank from market-derived pricing benchmarks and
  competitive-position features. The public market grid still includes it.
  With no external peers, market strategies error. `MANUAL` still returns the
  original config object unchanged.
- Repeating a suspicious rate is not independent confirmation. Readings remain
  quarantined against the last trusted state; their historical change events
  remain flagged. A clean recovery compares against the trusted rate, not the
  parser spike. There is no reviewed operator approval workflow yet. Use an
  explicitly verified manual assumption when a genuine large move needs review.
- Existing stores may contain values auto-confirmed by earlier code. This fix
  prevents new automatic confirmations; it cannot certify old history. Review
  suspicious periods against source evidence before relying on them.
- New collections must be chronological per product. Existing numeric-only
  mortgage product names (a former rate-first parsing defect) are repaired on
  store opening; associated historical events remain available but are flagged
  suspicious. Back up an existing database before its first opening after upgrade.
- The dashboard exposes observation age, freshness, quarantine and incomplete
  confirmation in the latest stored run. A completely failed collection creates
  no observation run, so this view cannot reveal every failed attempt. Monitor
  scheduled-run summaries and all nonzero exit codes independently.
- **Resolved in v0.2.0** (see "Pricing Intelligence Operations" above):
  persistent per-bank collector health with configurable failure escalation
  and recovery notifications, Telegram delivery alongside the existing
  webhook, a persisted `RunRecord` operational ledger, a `health` command, and
  a documented/implemented backup command. Still open: a reviewed quarantine
  *approval workflow* (the quarantine mechanism itself is enforced; there is
  no operator approve/reject command) and a whole-job wall-clock timeout --
  HTTP retry limits alone are not a wall-clock guarantee for a browser run.
- Raw HTML is retained for parser diagnostics, under ignored `data/raw/` paths;
  no automatic retention policy is implemented. Live evidence is a dated check,
  not an assurance that tomorrow's page has the same meaning.
