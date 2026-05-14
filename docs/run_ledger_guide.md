# Pricing Run Ledger — Usage Guide

---

## Background — what this is and why it exists

### What does this engine do?

This is a retail bank pricing engine. Its job is to recommend two interest rates:

- **Home loan rate** — the rate the bank charges customers who borrow money to buy a house. The
  higher this rate, the more interest income the bank earns, but fewer customers will take the
  loan (they'll go to a competitor instead).

- **Term deposit rate** — the rate the bank pays customers who park money in a fixed-term savings
  account. The higher this rate, the more customers deposit with the bank (providing cheap
  funding), but the more it costs to service those deposits.

The engine finds the combination of these two rates that maximises the bank's expected profit
after accounting for credit losses, funding costs, and the risk of not raising enough deposit
funding to cover the loan book.

### What is a "run"?

A run is one execution of the engine — loading a config, simulating customer responses across a
grid of possible rate combinations, and selecting the best-performing pair. A single run takes
a few seconds and produces a recommendation like:

> Set the home loan rate to 8.00% (+275 bps above the market reference rate) and the term
> deposit rate to 5.15% (+115 bps above the market reference rate). Expected profit: $12.5M.

### What is the ledger?

The ledger is the audit trail for that recommendation. Every time a run produces a result that
might inform a real pricing decision, the ledger captures a complete, reproducible package:

- **What** the engine recommended (rates, volumes, profit breakdown)
- **Why** it recommended it (objective decomposition, sensitivity to assumptions)
- **Under what assumptions** (the exact config file used, hashed for tamper detection)
- **What to review before acting** (a pre-populated governance checklist)
- **Who approved it** (space for a human sign-off)

Without the ledger, a pricing decision made six months ago is impossible to reconstruct: you
cannot tell which config was used, whether assumptions have since changed, or who approved the
recommendation. The ledger solves this.

### Key banking concepts used in this guide

**Basis points (bps):** A unit of measurement for interest rates. 1 basis point = 0.01%.
So 275 bps = 2.75%. Pricing discussions typically use basis points because changes of less than
1% are common and saying "the rate moved 25 basis points" is clearer than "the rate moved 0.25%".

**OCR (Official Cash Rate):** The Reserve Bank of New Zealand sets this rate to control
inflation. It is the benchmark from which most lending and deposit rates are derived. When the
OCR rises, banks generally raise both home loan and term deposit rates. The Reserve Bank reviews
the OCR eight times per year, roughly every six weeks. Each review is a natural trigger for a
pricing run.

**Market reference rate:** The rate against which the engine measures the bank's spread. For
home loans, this is typically a swap rate or the average competitor rate. For term deposits it
is a similar benchmark. The engine can be configured to optimise in "spread" terms (how many
basis points above or below the market rate) rather than in absolute rate terms.

**Funding shortfall:** Banks use deposit money to fund home loans. If the deposit book does not
cover enough of the loan book, the bank must borrow the shortfall at wholesale rates (more
expensive). The engine penalises rate combinations that create a large shortfall.

**Elasticity:** A measure of how sensitive customers are to rate changes. A loan rate elasticity
of -85 means that if the home loan rate increases by 100 bps, expected loan acceptance falls by
roughly 85%. The engine's recommendations are only as good as these elasticity assumptions —
which is why the governance checklist requires them to be reviewed before acting on results.

**Pricing committee:** The internal group (typically including the CFO, Treasurer, and Head of
Retail) that reviews and approves rate changes before they are published. The `pricing_memo.md`
and `governance_checklist.md` are the primary documents for this committee.

---

## When to run the ledger

### The core principle

Run the ledger **whenever a pricing decision is being considered** — not on a fixed schedule.
The ledger exists to support and record decisions. Running it without a decision attached
produces noise.

### Specific triggers

**Run the ledger when any of the following occur:**

| Trigger | Why |
| --- | --- |
| Reserve Bank OCR announcement | The benchmark rate has changed; the engine's spread-based recommendation may now imply a different absolute rate |
| A competitor visibly moves their rates | The competitor rate inputs in the config may be stale; re-fetch with the `competitor_rates` CLI and re-run |
| Config assumptions are updated | Elasticities, funding ratio, penalty rate, or spread bands have been revised — the prior ledger is now based on different assumptions |
| A scenario is being compared to the baseline | Run both scenarios with the ledger to produce side-by-side audit-ready outputs |
| A pricing committee meeting is scheduled | The committee needs a current memo and checklist to review |

**Do not run the ledger:**

- On a cron job or automated schedule without a human review cycle attached. The governance
  checklist is meaningless if no one reads it.
- As a "let's see what it says" exploration. Use `python3 examples/run_baseline.py` directly for
  casual exploration; only write the ledger when the result will go to a committee or be
  archived.
- Immediately after changing the config without first verifying the config change is intentional
  and understood. The ledger archives what the config says — garbage in, garbage out.

### Minimum cadence for a NZ retail bank context

At minimum, produce one ledger per month. More practically:

- **8 times per year** — after each OCR review (whether or not the OCR moves; the review is a
  natural prompt to verify assumptions are still current)
- **Ad hoc** — any time a major competitor (ANZ, ASB, Westpac, BNZ, Kiwibank) publishes a rate
  change visible on interest.co.nz
- **Before any actual rate change** — if the bank is about to publish new rates, there must be
  a ledger run with a completed checklist on record first

---

## 1. Prerequisites

Install the package in the project root:

```bash
python3 -m pip install -e ".[dev]"
```

Confirm a config file is present:

```bash
ls configs/base.yaml
```

---

## 2. Quickstart — run the baseline example

The fastest path is the built-in example, which runs the full baseline scenario and writes the
ledger automatically:

```bash
python3 examples/run_baseline.py
```

The terminal will print the recommended rates, sensitivity results, and then end with:

```text
Run ledger written to: data/outputs/runs/20260514_013815_123456_baseline
```

Open that directory to find all ledger artifacts. The two files to read first are:

- `pricing_memo.md` — the recommendation document to share with the pricing committee
- `governance_checklist.md` — the sign-off checklist to complete before acting

---

## 3. Add the ledger to your own scenario

Import `write_run_ledger` and call it after `run_baseline_diagnostics` (or any function that
returns a `ScenarioDiagnostics` object):

```python
from pathlib import Path

from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.run_ledger import write_run_ledger
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

CONFIG_PATH = Path("configs/base.yaml")
config = load_config(CONFIG_PATH)

diagnostics = run_baseline_diagnostics(config)
sensitivity = run_sensitivity_analysis(config)   # optional but strongly recommended

run_dir = write_run_ledger(
    scenario_name="my_scenario",       # used in directory name and memo title
    config=config,
    config_path=CONFIG_PATH,
    diagnostics=diagnostics,
    sensitivity=sensitivity,           # omit to skip sensitivity section in memo
)

print(f"Ledger written to: {run_dir}")
```

### Full parameter reference

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `scenario_name` | `str` | required | Label for the run; appears in directory name, memo, and checklist. Use a short descriptive name like `"baseline"`, `"post_ocr_may_2026"`, or `"high_penalty_stress"` |
| `config` | `EngineConfig` | required | Loaded engine config object |
| `config_path` | `str \| Path` | required | Path to the YAML config file (copied verbatim into the ledger) |
| `diagnostics` | `ScenarioDiagnostics` | required | Completed scenario result |
| `sensitivity` | `pd.DataFrame \| None` | `None` | Output of `run_sensitivity_analysis()`; adds a sensitivity table to the memo. Omitting this means the committee cannot see how robust the recommendation is to assumption changes |
| `competitor_rate_source` | `str \| None` | `None` | Free-text note explaining where competitor rates came from, e.g. `"interest.co.nz 2026-05-14"`. Appears in the memo and checklist. Important for audit trail |
| `base_dir` | `str \| Path` | `"data/outputs/runs"` | Parent directory for all run ledgers |

---

## 4. Ledger directory structure

Each run creates a directory named `{YYYYMMDD_HHMMSS_microseconds}_{scenario_name}`:

```
data/outputs/runs/
└── 20260514_013815_123456_baseline/
    ├── metadata.json           # run identity and governance flags
    ├── config_snapshot.yaml    # exact copy of the config used
    ├── summary.json            # accepted recommendation and boundary metadata
    ├── grid_results.csv        # every evaluated rate pair
    ├── top_results.csv         # top candidates by objective value
    ├── sensitivity.csv         # sensitivity sweep results (if provided)
    ├── counterfactuals.csv     # one-axis decompositions for top candidates
    ├── cohort_attribution.csv  # expected volumes by synthetic customer segment
    ├── pricing_memo.md         # business-facing recommendation document
    └── governance_checklist.md # manual sign-off checklist
```

The directory name starts with a timestamp so runs sort chronologically in any file browser.
If two runs complete within the same microsecond (rare), the second directory gets a numeric
suffix (`_001`, `_002`, …) to guarantee uniqueness.

---

## 5. Artifact reference

### `metadata.json`

Machine-readable run identity. This is the fingerprint of the run — it records exactly when it
happened, which code version produced it, and whether any governance flags were raised.

| Field | Meaning |
| --- | --- |
| `run_id` | Globally unique identifier (UUID4) — use this to refer to a specific run in committee notes or audit logs |
| `scenario_name` | The label passed to `write_run_ledger` |
| `timestamp` | UTC time when the ledger was written (ISO-8601 format) |
| `config_hash` | SHA-256 fingerprint of the config file — if the hash matches a prior run, the assumptions were identical |
| `git_commit` | Short hash of the code version used (or `null` if git is unavailable) — confirms the exact engine logic |
| `package_version` | Engine package version |
| `python_version` | Python version used |
| `boundary_warning_flag` | `true` if the engine could not find a clearly superior interior solution — the optimal rate may lie outside the searched range. See "Boundary flags explained" below |
| `initial_boundary_warning_flag` | `true` if the first grid search hit a boundary, triggering adaptive expansion |
| `expansion_rounds` | How many times the grid was expanded to chase the boundary |
| `policy_band_breached` | `true` if the recommended rate exceeds the configured policy band — requires explicit committee discussion |
| `competitor_rate_source` | Provenance note for competitor rates |

Example:

```json
{
  "boundary_warning_flag": false,
  "config_hash": "ab1574f3b9b39027...",
  "config_path": "configs/base.yaml",
  "expansion_rounds": 2,
  "git_commit": "d8bd121",
  "initial_boundary_warning_flag": true,
  "package_version": "0.1.0",
  "policy_band_breached": true,
  "python_version": "3.9.6",
  "run_id": "9554ef90-2355-47fb-a255-affac29ac233",
  "scenario_name": "baseline",
  "timestamp": "2026-05-14T01:38:15.810615"
}
```

#### Boundary flags explained

The engine searches a grid of rate combinations. If the best result sits at the edge of that
grid, it means the true optimum might be even further outside the range searched — like reaching
the edge of a map and not knowing what lies beyond.

When this happens, the engine automatically expands the grid and searches again
(`expansion_rounds` counts these attempts). If `boundary_warning_flag` is still `true` after
expansion, the engine has not found a clear interior optimum and the recommendation should be
treated with extra caution. The checklist requires this to be reviewed.

If `boundary_warning_flag` is `false` (even if `initial_boundary_warning_flag` was `true`),
the expansion resolved the issue and the result is reliable.

### `config_snapshot.yaml`

An exact copy of the YAML config file used for the run. Combined with `config_hash` in
`metadata.json`, this allows any past run to be reproduced exactly, even after the live config
has been updated.

**This is the most important reproducibility artifact.** If a regulator or internal auditor
asks "what assumptions did you use when you set rates on 14 May 2026?", the answer is in this
file.

To reproduce a past run:

```bash
python3 - <<'EOF'
from balance_pricing_engine.config import load_config
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

config = load_config("data/outputs/runs/<run_dir>/config_snapshot.yaml")
diagnostics = run_baseline_diagnostics(config)
print(diagnostics.best_result.loan_rate)
EOF
```

### `summary.json`

The accepted recommendation in machine-readable form. Contains:
`best_loan_rate`, `best_deposit_rate`, `objective_value`, `expected_loan_volume`,
`expected_deposit_volume`, `funding_shortfall`, plus all boundary and expansion flags.

Use this for programmatic comparisons between runs (e.g. "did the recommended rate change
between the March and April runs?").

### `grid_results.csv`

One row per rate pair evaluated across the entire final search grid. Columns include the two
rates, the total objective, all income and cost components, expected volumes, and funding
shortfall. There are typically hundreds of rows.

This file is primarily for data analysts who want to plot the full profit surface — seeing which
combinations are almost as good as the winner, and how profit falls off as rates move away from
the optimum.

### `top_results.csv`

The top-5 candidates ranked by objective value (a subset of `grid_results.csv`). Use this to
answer the question: "is the winning rate pair clearly better than its neighbours, or is the
profit landscape flat and any nearby rate would do just as well?"

If the top-5 objective values are very close together, the committee has more flexibility. If
they are far apart, the recommended rates are uniquely important.

### `sensitivity.csv`

One row per sensitivity scenario. The default sensitivity cases each change one elasticity or
customer-feature assumption by ±25% and rerun the optimisation. Columns include:

| Column | Meaning |
| --- | --- |
| `scenario_name` | Name of the perturbed assumption (e.g. `loan_elasticity_weaker`) |
| `best_loan_rate` | Recommended home loan rate under this scenario |
| `best_deposit_rate` | Recommended term deposit rate under this scenario |
| `delta_loan_rate_bps` | How much the loan rate moved vs. baseline, in basis points |
| `delta_deposit_rate_bps` | How much the deposit rate moved vs. baseline, in basis points |
| `delta_objective_pct` | How much profit changed vs. baseline, as a percentage |
| `boundary_warning_flag` | Whether a boundary warning was triggered in this scenario |

A large `delta_loan_rate_bps` (say, more than 15–20 bps) means the recommendation is sensitive
to that assumption. If you're not confident in that assumption, the committee should discuss it
before acting.

### `counterfactuals.csv`

For each top candidate: what would the objective have been if only the loan rate had been set
to the winner's level (holding deposit at the candidate's level), and vice versa. This
decomposes how much of the profit gain comes from the loan rate decision vs. the deposit rate
decision.

### `cohort_attribution.csv`

Expected volumes broken down by synthetic customer segments (e.g. high-LVR borrowers,
low-income depositors). Useful for understanding which customer groups are driving the
recommendation — for example, whether the optimal rate is being pulled up by large borrowers
or down by rate-sensitive depositors.

### `pricing_memo.md`

The primary document for the pricing committee. Written in plain English. Sections:

| Section | What it contains |
| --- | --- |
| **Header** | Run ID, timestamp, config hash, git commit, competitor rate source |
| **Recommendation** | The two recommended rates, their spread above market, and the expected profit |
| **Objective Decomposition** | Line-by-line breakdown of where the $12.5M (or whatever the number is) comes from: loan interest income, minus credit losses, minus deposit cost, minus wholesale funding cost, plus any excess deposit income, minus any funding shortfall penalty |
| **Expected Volumes and Funding Position** | How many dollars of loans and deposits are expected, and whether there is a funding shortfall |
| **Sensitivity Summary** | Table showing how the recommendation changes if key assumptions are wrong |
| **Boundary Diagnostics** | Whether the grid search found a clean interior solution |
| **Model-Risk Notes** | Fixed caveats: the model uses synthetic data, elasticities are point estimates, there are no confidence intervals |
| **Questions for Pricing Committee** | Pre-populated questions specific to the current config values — the committee should be able to answer all of these before approving |
| **Synthetic Data Limitation** | Mandatory disclaimer that this is a prototype, not a production pricing system |

Share `pricing_memo.md` with the pricing committee as the primary discussion document. It is
designed to be readable without any technical background.

### `governance_checklist.md`

A pre-populated sign-off checklist. Every item is an unchecked box (`- [ ]`). A reviewer works
through each item and checks it off (`- [x]`) as the committee satisfies themselves on each
point.

Checklist sections and what each one is asking:

**Optimizer Diagnostics**
- *Boundary result reviewed* — Was the search well-behaved, or did it hit the edge of the grid?
  If `boundary_warning_flag` is `true`, this cannot be ticked off without discussion.
- *Sensitivity reviewed* — Does any single-factor scenario reverse the recommendation? If so,
  why is the committee comfortable with that scenario being unlikely?

**Market Assumptions**
- *Competitor-rate freshness reviewed* — Are the competitor rates in the config still current?
  Rates go stale quickly. If the config was last updated two weeks ago and competitors have
  moved since, the recommendation may be based on wrong inputs.
- *Funding assumptions reviewed* — Is the required deposit funding ratio and the penalty rate
  still aligned with the bank's current risk appetite?

**Model Assumptions**
- *Elasticity assumptions reviewed* — Do the elasticity numbers in the config reflect current
  market intelligence? These are the most important inputs in the model.
- *Conduct and fairness limitations noted* — The engine optimises portfolio-level profit. It
  does not check whether rates are fair to individual customer segments. Someone on the committee
  must acknowledge this limitation before rates are moved.

**Approval**
- *Human approval required* — A named, authorised person must sign off. The engine recommends;
  a human decides.
- *Synthetic-data limitation acknowledged* — The model has never been calibrated to real
  customer behaviour. The committee must acknowledge this each time.

---

## 6. Governance workflow — step by step

This is the full process from "someone thinks rates need to change" to "rates are approved to change".

### Step 1 — Identify the trigger

Something has prompted a pricing review: an OCR announcement, a competitor move, a scheduled
monthly review, or a request from the committee. Note the reason — it should appear in the
`scenario_name` or be referenced in the committee discussion.

### Step 2 — Update config assumptions if needed

If competitor rates have changed, fetch fresh data first:

```bash
python3 -m balance_pricing_engine.competitor_rates.cli fetch \
    --output-dir data/processed/competitor_rates

python3 -m balance_pricing_engine.competitor_rates.cli apply \
    --config configs/base.yaml
```

If the OCR has moved, update `market.cash_rate` and `market.wholesale_funding_spread` in
`configs/base.yaml` to reflect current market conditions. Document why you changed each value.

If elasticity assumptions are being revised, update them in `configs/base.yaml` and record the
rationale (e.g. "updated based on March customer research").

### Step 3 — Run the engine and write the ledger

```bash
python3 examples/run_baseline.py
```

Or, for a named scenario (e.g. after an OCR cut):

```python
run_dir = write_run_ledger(
    scenario_name="post_ocr_cut_may_2026",
    config=config,
    config_path=CONFIG_PATH,
    diagnostics=diagnostics,
    sensitivity=sensitivity,
    competitor_rate_source="interest.co.nz 2026-05-14",
)
```

Note the run directory path.

### Step 4 — Check boundary flags before sharing

Before distributing the memo, open `metadata.json` and check:

- `boundary_warning_flag: false` — the engine found a clean interior solution. Proceed.
- `boundary_warning_flag: true` — the engine may not have found the true optimum. Consider
  widening the spread bands in the config (the `pricing_strategy` section) and re-running before
  distributing.

Also check `policy_band_breached`. If `true`, the recommended rate exceeds the configured policy
band. This is not a blocker, but the committee must explicitly discuss it.

### Step 5 — Distribute the pricing memo

Send `pricing_memo.md` to the pricing committee ahead of the meeting. Ask them to review:

1. The **Recommendation** section — do the rates and spreads look reasonable?
2. The **Sensitivity Summary** — are there any scenarios where the recommendation flips
   significantly?
3. The **Questions for Pricing Committee** — can everyone answer these questions?

### Step 6 — Committee review meeting

In the meeting, work through `governance_checklist.md` item by item. For each item:

- If satisfied: the reviewer changes `- [ ]` to `- [x]` in the file.
- If not satisfied: the item stays unchecked and the reason is noted inline. The run cannot be
  approved until all items are resolved (either checked or explicitly overridden with a written
  rationale).

Do not skip items. The checklist is the governance record.

### Step 7 — Sign off

At the bottom of `governance_checklist.md`, add a manual sign-off block:

```markdown
---

## Sign-off

- Approved by: [Name], [Role]
- Date: [YYYY-MM-DD]
- Notes: [Any caveats or conditions on this approval]
```

### Step 8 — Archive the ledger

The run directory is the audit record for this pricing decision. Store it:

- In version control (a separate governance repository, not the main codebase), or
- In a shared drive accessible to compliance and audit teams, with the directory name and run ID
  recorded in a decision log.

The `run_id` from `metadata.json` is the canonical reference. When someone asks in six months
"what did the engine recommend in May 2026 and what were the assumptions?", you search the
decision log by `run_id` and open the corresponding directory.

---

## 7. Tips

**Name scenarios meaningfully.** Use names like `"post_ocr_cut_may_2026"` or
`"high_penalty_stress"` rather than `"test"` or `"run1"`. The scenario name appears in the
directory name, memo title, and checklist heading — it is the primary label in the audit archive.

**Always include sensitivity.** The `sensitivity` parameter is optional, but a memo without a
sensitivity section gives the committee no way to assess how robust the recommendation is. Omit
it only if time is genuinely unavailable.

**Attach the competitor rate source.** When live competitor rates have been fetched, pass the
source string so it appears in the memo and checklist:

```python
run_dir = write_run_ledger(
    ...,
    competitor_rate_source="interest.co.nz 2026-05-14",
)
```

**Use a custom output location.** Override `base_dir` to write ledgers to a shared drive or
governance repository:

```python
run_dir = write_run_ledger(..., base_dir="/mnt/shared/pricing-governance/runs")
```

**Reproduce a past run.** Load the config snapshot from the ledger directory and re-run the
engine. The `config_hash` in `metadata.json` lets you verify the snapshot has not been modified:

```python
import hashlib
from pathlib import Path

snapshot = Path("data/outputs/runs/<run_dir>/config_snapshot.yaml")
actual_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
print(actual_hash)  # should match config_hash in metadata.json
```

**Do not edit ledger artifacts after the fact.** The `pricing_memo.md` and
`governance_checklist.md` are generated from the engine state at run time. Post-hoc edits to
these files (other than filling in the sign-off block) break the audit trail. If an error is
discovered, re-run the engine and produce a new ledger — do not patch the old one.

---

## 8. Limitations

- **No digital signature or approval lock.** The checklist sign-off is by convention (editing
  the markdown file). There is no mechanism that prevents someone from ticking boxes without
  actually reviewing the items, or from editing the file after the fact. Trust in the process
  depends on organisational discipline, not technical enforcement.

- **Synthetic data only.** All customer populations and response probabilities in this engine
  are generated from assumed elasticity parameters. The ledger does not validate that these
  assumptions match real customer behaviour. The model has never been calibrated to empirical
  data. Every ledger must carry the synthetic data disclaimer, and the committee must acknowledge
  it at each review.

- **Elasticities are point estimates.** The sensitivity analysis perturbs assumptions by ±25%,
  but does not produce confidence intervals or probability distributions over outcomes. A result
  that looks robust under ±25% perturbation may still be sensitive to larger shocks.

- **Git commit may be null.** If the engine is run outside a git repository or in an air-gapped
  environment, `git_commit` in `metadata.json` will be `null`. The `config_hash` still provides
  reproducibility for the inputs; only the code version is unverified.

- **Sensitivity table shape.** The sensitivity section in `pricing_memo.md` expects a DataFrame
  produced by `run_sensitivity_analysis()`. Passing a differently-shaped DataFrame may render
  incorrectly or silently produce an empty table.

---

## Appendix — glossary

| Term | Definition |
| --- | --- |
| **Basis point (bps)** | 1/100th of 1%. 100 bps = 1%. Rate changes are typically described in bps |
| **OCR** | Official Cash Rate — set by the Reserve Bank of NZ 8 times per year. The primary lever for monetary policy |
| **Home loan rate** | The interest rate charged to mortgage borrowers. A bank asset (generates income) |
| **Term deposit rate** | The interest rate paid to savers who lock up funds for a fixed term. A bank liability (costs money) |
| **Spread** | The difference between a rate and a reference benchmark, in bps. E.g. "275 bps above the market reference rate" |
| **Funding shortfall** | When deposit volume is insufficient to fund the loan book; the bank must borrow the gap at higher wholesale rates |
| **Funding penalty** | A penalty term added to the objective function when there is a funding shortfall. Discourages the engine from recommending rates that leave the bank underfunded |
| **Elasticity** | How much customer behaviour changes in response to a 1% change in rate. Negative for loans (higher rate = fewer takers), positive for deposits (higher rate = more savers) |
| **Grid search** | The engine evaluates every combination of loan rate and deposit rate across a defined grid, computing expected profit for each. The best combination is the recommendation |
| **Adaptive boundary expansion** | If the best result sits at the edge of the grid, the engine automatically extends the grid and searches again |
| **Boundary warning** | Raised when the engine cannot confirm the true optimum lies within the searched range, even after expansion |
| **Policy band** | A configured rate range outside which the bank's policy requires explicit committee discussion |
| **Objective decomposition** | The breakdown of total profit into its components: loan income, credit losses, deposit cost, wholesale funding cost, excess deposit income, and funding penalty |
| **Sensitivity analysis** | Re-running the engine with each key assumption perturbed by ±25% to test how much the recommendation changes |
| **Pricing committee** | The internal governance body (typically CFO, Treasurer, Head of Retail) that reviews and approves rate changes |
| **Run ledger** | The self-contained directory of artifacts produced by `write_run_ledger()` for a single pricing run |
| **Config hash** | SHA-256 fingerprint of the config YAML file — uniquely identifies the exact set of assumptions used |
| **Run ID** | UUID4 assigned to each run — used as the canonical reference in decision logs and audit records |
