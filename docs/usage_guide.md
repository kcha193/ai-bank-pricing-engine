# End-to-End Usage Guide

This guide explains how to use the AI Balance Sheet Pricing Engine from start to finish.
It is written for two audiences:

- Non-technical users who want to understand what the engine does and how to read the output.
- Technical users who want to run, test, configure, and extend the project.

The project currently uses synthetic data only. It is a pricing analytics prototype, not a
production pricing approval system.

## 1. What The Engine Does

The engine searches for one portfolio-level home loan rate and one portfolio-level term deposit
rate.

It estimates:

- How many home loan customers accept the offered home loan rate.
- How many term deposit customers take up the offered deposit rate.
- Loan interest income.
- Expected credit loss.
- Term deposit interest cost.
- Wholesale funding cost when deposits do not fully fund lending.
- A penalty when deposit funding falls short of the required funding ratio.

The optimizer chooses the rate pair with the highest total objective:

```text
total objective
= loan interest income
- expected credit loss
- deposit interest cost
- wholesale funding cost
- funding shortfall penalty
```

## 2. Plain-English Workflow

Use this workflow if you mainly want to understand or review the pricing output.

### Step 1: Start With The Business Question

The current business question is:

```text
What home loan rate and term deposit rate produce the best expected economic result,
given customer response, credit loss, funding costs, and funding constraints?
```

The engine does not yet do personalized pricing. It chooses one home loan rate and one term
deposit rate for the whole synthetic portfolio.

### Step 2: Review The Scenario Assumptions

The baseline assumptions live in:

```text
configs/base.yaml
```

Important fields:

- `market.cash_rate`: base cash rate assumption.
- `market.wholesale_funding_spread`: spread above cash rate for wholesale funding.
- `market.competitor_loan_rate`: competitor home loan rate.
- `market.competitor_deposit_rate`: competitor term deposit rate.
- `products.loan_rate_grid`: home loan rates the optimizer is allowed to test.
- `products.deposit_rate_grid`: deposit rates the optimizer is allowed to test.
- `balance_sheet.required_deposit_funding_ratio`: how much lending should be funded by deposits.
- `balance_sheet.funding_shortfall_penalty_rate`: penalty rate applied to deposit funding shortfall.
- `response.loan_rate_elasticity`: how strongly loan demand reacts to the offered loan rate.
- `response.deposit_rate_elasticity`: how strongly deposit uptake reacts to the offered deposit rate.

Rates are written as decimals:

- `0.08` means `8.00%`.
- `0.051` means `5.10%`.

### Step 3: Run The Baseline Report

A technical user can run:

```bash
python3 examples/run_baseline.py
```

The report prints five sections:

- Best pricing decision.
- Top 5 pricing candidates.
- Objective decomposition for the winner.
- Boundary diagnostics.
- Sensitivity summary.

### Step 4: Read The Best Pricing Decision

The current baseline result is:

```text
Home loan rate:          8.00%
Term deposit rate:       5.10%
Objective value:         about $12.5M
Expected loan volume:    about $605M
Expected deposit volume: about $384M
Funding shortfall:       about $9.1M
```

Interpretation:

- The model expects the best tested home loan rate to be `8.00%`.
- The best tested term deposit rate is `5.10%`.
- The expected economic objective is about `$12.5M`.
- The model still has a funding shortfall, but the optimizer prefers this tradeoff over paying
  a higher deposit rate or booking less lending.

### Step 5: Read The Top 5 Candidates

The top 5 candidates show whether the winner is clearly better than nearby alternatives.

Look for:

- A large gap between rank 1 and rank 2: stronger evidence for the winner.
- Very small gaps: the decision may be sensitive to assumptions.
- Similar objectives with lower funding shortfall: possible management preference even if pure
  objective value is slightly lower.

### Step 6: Read The Objective Decomposition

The winner decomposition shows why the rate pair won.

Example fields:

```text
Loan interest income
Expected credit loss
Deposit interest cost
Wholesale funding cost
Funding shortfall penalty
Total objective
```

Use this to challenge the result:

- Is loan interest income doing most of the work?
- Is expected credit loss economically plausible?
- Is the deposit cost reasonable for the volume raised?
- Is wholesale funding cost large enough to matter?
- Is the shortfall penalty too small to influence the decision?

### Step 7: Review Boundary Warnings

A boundary warning appears when the winning rate is at the minimum or maximum of the search grid.

That matters because the optimizer may be saying:

```text
The best answer might be outside the range you allowed me to test.
```

Possible causes include:

- Search range too narrow.
- Weak elasticity.
- Objective over-rewarding margin.
- Funding penalty too weak or strong.
- Competitor effects too small.

The current baseline has no boundary warning. Some sensitivity cases do produce boundary warnings,
which is useful for model risk review.

### Step 8: Review Sensitivity Summary

The sensitivity summary reruns the optimizer under changed assumptions.

It varies:

- Loan price elasticity.
- Deposit price elasticity.
- Funding shortfall penalty.
- Wholesale funding rate.
- Competitor home loan rate.
- Competitor term deposit rate.

Use it to answer:

- Does the best loan rate move when customer demand is more or less elastic?
- Does the best deposit rate move when deposit customers are more or less rate-sensitive?
- Does a higher funding penalty push the model toward more deposits?
- Does a higher wholesale funding rate make deposit funding more attractive?
- Do competitor rates materially change the recommendation?

If small assumption changes produce large recommendation changes, treat the optimizer result as
sensitive and review the assumptions before using it.

## 3. Technical Setup

Use these steps if you are running the project locally.

### Step 1: Clone The Repo

```bash
git clone git@github.com:kcha193/ai-bank-pricing-engine.git
cd ai-bank-pricing-engine
```

If you already have the repo:

```bash
cd /Users/kevinchang/Documents/ai-bank-pricing-engine
git pull
```

### Step 2: Create A Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3: Install Dependencies

```bash
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -e ".[dev]"
```

The first command matters on macOS because the `pip` bundled into a new virtual environment may be
too old for editable installs from a modern `pyproject.toml` project. If you see an error mentioning
missing `setup.py` or `setup.cfg`, upgrade `pip`, `setuptools`, and `wheel`, then rerun the install.

### Step 4: Run Tests

```bash
python3 -m pytest -q
```

Expected result:

```text
19 passed
```

### Step 5: Run Linting

```bash
python3 -m ruff check .
```

Expected result:

```text
All checks passed!
```

### Step 6: Run The Baseline Report

```bash
python3 examples/run_baseline.py
```

### Step 7: Train Response Models

```bash
python3 examples/train_response_models.py
```

This trains simple ML response models on synthetic observed outcomes generated from the explicit
ground-truth response functions.

## 4. How To Read A Successful Run

This section explains the most common output from setup and from the baseline report.

### Package Install Messages

You may see output like this when running `python3 -m pip install -e ".[dev]"`:

```text
Successfully built ai-bank-pricing-engine
Installing collected packages: ai-bank-pricing-engine
  Attempting uninstall: ai-bank-pricing-engine
    Found existing installation: ai-bank-pricing-engine 0.1.0
    Uninstalling ai-bank-pricing-engine-0.1.0:
      Successfully uninstalled ai-bank-pricing-engine-0.1.0
Successfully installed ai-bank-pricing-engine-0.1.0
```

This is normal. Python has rebuilt the local project, found an older local install of the same
package, removed it, and installed the latest local version.

It does not mean anything is wrong with the pricing model.

### Best Pricing Decision

The baseline report currently starts with:

```text
Best joint pricing decision
  Home loan rate:          8.00%
  Term deposit rate:       5.10%
  Objective value:         $12,495,061
  Expected loan volume:    $604,819,814
  Expected deposit volume: $384,040,808
  Funding shortfall:       $9,092,071
```

Plain-English interpretation:

- The optimizer tested all configured home loan and term deposit rate pairs.
- The best tested pair is an `8.00%` home loan rate and a `5.10%` term deposit rate.
- The expected economic value of that decision is about `$12.5M`.
- The model expects about `$605M` of accepted home loans.
- The model expects about `$384M` of term deposits raised.
- There is still a funding shortfall of about `$9.1M`.

The recommendation is not saying "this is the true market price." It is saying "given the current
synthetic customers, elasticity assumptions, competitor rates, funding costs, and configured search
grid, this rate pair has the highest objective."

### Objective Decomposition

The winner decomposition currently looks like this:

```text
Loan interest income:      $48,385,585
Expected credit loss:      $4,108,330
Deposit interest cost:     $19,586,081
Wholesale funding cost:    $12,032,456
Funding shortfall penalty: $163,657
Total objective:           $12,495,061
```

The arithmetic is:

```text
$48.39M loan interest income
- $4.11M expected credit loss
- $19.59M deposit interest cost
- $12.03M wholesale funding cost
- $0.16M funding shortfall penalty
= $12.50M total objective
```

Economically, this means the model expects the `8.00%` home loan rate to generate enough margin to
outweigh lower loan demand, credit loss, deposit interest expense, wholesale funding cost, and the
funding shortfall penalty.

### Funding Shortfall

The baseline requires deposits to fund `65%` of expected loan volume.

For the winning result:

```text
Expected loan volume:    about $604.8M
Required deposit funding: about $393.1M
Expected deposit volume: about $384.0M
Funding shortfall:       about $9.1M
```

The model accepts this shortfall because paying a higher deposit rate or choosing a lower-margin
loan rate produces a lower total objective under the current assumptions.

### Top 5 Candidates

The top 5 section shows the nearest economic alternatives.

Current interpretation:

- Rank 1, `8.00%` loan and `5.10%` deposit, is best overall.
- Rank 2, `8.00%` loan and `4.90%` deposit, has a lower deposit rate but a much larger funding
  shortfall.
- Rank 3, `7.80%` loan and `5.10%` deposit, books more loan volume but gives up too much margin and
  creates a much bigger funding shortfall.
- Rank 4, `8.00%` loan and `5.30%` deposit, has zero funding shortfall, but the higher deposit cost
  reduces the objective.
- Rank 5 is close, but still worse than the winner on the current objective.

This section is useful for business review because the mathematically best answer is not always the
management-preferred answer. A lower-ranked candidate might be preferred if it has less funding risk
or better strategic fit.

### Boundary Diagnostics

The baseline currently prints:

```text
Boundary diagnostics
  No boundary warnings.
```

This is a good sign. It means the winning home loan and deposit rates are not stuck at the minimum
or maximum tested grid value.

If a boundary warning appears, it means the optimizer may be trying to move beyond the rate range it
was allowed to search. Possible causes are:

- Search range too narrow.
- Weak elasticity.
- Objective over-rewarding margin.
- Funding penalty too weak or strong.
- Competitor effects too small.

### Sensitivity Summary

The sensitivity summary reruns the optimizer after changing one assumption at a time.

Important examples from the current output:

```text
loan_elasticity_weaker: loan=8.20%, ... boundary_warning=yes
competitor_loan_rate_higher: loan=8.20%, ... boundary_warning=yes
```

These two cases push the home loan rate to the maximum tested grid value. That is a model risk
review flag. It suggests you may need to widen the loan-rate grid or challenge the elasticity and
competitor assumptions.

Other examples:

- `deposit_elasticity_stronger` reaches zero funding shortfall because more deposit customers
  respond to the offered deposit rate.
- `funding_penalty_lower` allows a much larger funding shortfall because the penalty for being
  underfunded is weaker.
- `wholesale_funding_rate_higher` lowers the objective because wholesale-funded lending becomes
  more expensive.

Big-picture interpretation:

```text
The baseline result is economically coherent, but loan pricing is somewhat sensitive to demand and
competitor assumptions.
```

That is exactly what the diagnostics layer is designed to reveal.

## 5. ML Response Modelling Workflow

The modelling layer learns customer response from synthetic observed outcomes.

It does not replace the optimizer yet. The current optimizer still uses the explicit ground-truth
response functions. The ML layer is a controlled comparison tool: it asks whether a simple learned
model can recover sensible response behaviour from synthetic observations.

### What Gets Trained

The example trains two logistic regression models:

- Home loan acceptance model.
- Term deposit uptake model.

The project also includes a small `HistGradientBoostingClassifier` factory for later comparison,
but the example report keeps the visible workflow to logistic regression first.

### Dataset Features

Home loan response datasets include:

```text
offered_rate
competitor_rate
rate_gap_to_competitor
loan_amount
credit_score
lvr
income
risk_score
expected_loss_rate
risk_segment
accepted
```

Term deposit response datasets include:

```text
offered_rate
competitor_rate
rate_gap_to_competitor
deposit_amount
relationship_years
digital_engagement
rate_sensitivity
wealth_segment
opened
```

The target columns are:

- `accepted` for home loans.
- `opened` for term deposits.

### How Outcomes Are Created

For each sampled customer-offer row:

1. The code assigns an offered rate from the configured rate grid.
2. It calculates the ground-truth response probability with the existing explicit response
   function.
3. It samples a binary observed outcome from that probability.
4. The ML model learns from those observed outcomes.

This makes the setup useful for model risk review because the true response mechanism is known.

### Metrics

The training script prints:

- `auc`: ranking power. Higher is better.
- `log_loss`: probability quality. Lower is better.
- `brier_score`: squared probability error. Lower is better.
- calibration table: compares predicted probabilities with observed response rates.
- rate response sanity check: confirms direction of predicted response across offered rates.

Current example output is roughly:

```text
Home loan acceptance model
  auc: 0.7893
  log_loss: 0.5543
  brier_score: 0.1870
  Rate response sanity check: pass

Term deposit uptake model
  auc: 0.8170
  log_loss: 0.5255
  brier_score: 0.1737
  Rate response sanity check: pass
```

### Sanity Checks

The most important checks are directional:

- Home loan predicted acceptance should generally decrease as the offered loan rate increases.
- Term deposit predicted uptake should generally increase as the offered deposit rate increases.

The current logistic models pass both checks.

If either check fails, do not use that model for pricing analysis until you understand why. Possible
causes include noisy training data, weak feature design, a model that is too flexible, or training
data that does not cover the pricing range well.

## 6. Technical Project Map

Main files:

```text
configs/base.yaml
examples/run_baseline.py
examples/train_response_models.py
src/balance_pricing_engine/config.py
src/balance_pricing_engine/synthetic/customers.py
src/balance_pricing_engine/synthetic/response.py
src/balance_pricing_engine/pricing/profit.py
src/balance_pricing_engine/pricing/balance_sheet.py
src/balance_pricing_engine/optimization/grid_search.py
src/balance_pricing_engine/diagnostics.py
src/balance_pricing_engine/modelling/datasets.py
src/balance_pricing_engine/modelling/evaluation.py
src/balance_pricing_engine/modelling/models.py
src/balance_pricing_engine/modelling/persistence.py
src/balance_pricing_engine/scenarios/runner.py
tests/unit/
```

Responsibilities:

- `config.py`: loads and validates YAML config.
- `synthetic/customers.py`: generates synthetic home loan and deposit customers.
- `synthetic/response.py`: calculates customer response probabilities.
- `pricing/profit.py`: calculates income, costs, credit loss, and funding costs.
- `pricing/balance_sheet.py`: calculates funding requirement, shortfall, and penalty.
- `optimization/grid_search.py`: evaluates the full joint rate grid and selects the best pair.
- `diagnostics.py`: objective decomposition, boundary checks, and sensitivity analysis.
- `modelling/datasets.py`: builds synthetic observed response datasets.
- `modelling/models.py`: builds and fits sklearn response models.
- `modelling/evaluation.py`: evaluates metrics, calibration, and rate response sanity.
- `modelling/persistence.py`: saves and loads fitted models with joblib.
- `scenarios/runner.py`: orchestrates the baseline scenario.
- `examples/run_baseline.py`: user-facing report script.
- `examples/train_response_models.py`: trains and evaluates ML response models.

## 7. Programmatic Usage

Use this from Python if you want the result objects or pandas DataFrames.

```python
from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

config = load_config("configs/base.yaml")
diagnostics = run_baseline_diagnostics(config)

best = diagnostics.best_result
print(best.loan_rate)
print(best.deposit_rate)
print(best.total_objective)

top_5 = diagnostics.top_results(5)
print(top_5)

decomposition = diagnostics.winner_decomposition()
print(decomposition.reconciled_total_objective)

boundary = diagnostics.boundary_diagnostics
print(boundary.has_warning)
print(boundary.warnings)

sensitivity = run_sensitivity_analysis(config)
print(sensitivity)
```

Important columns in `diagnostics.grid_results`:

```text
loan_rate
deposit_rate
loan_interest_income
expected_credit_loss
deposit_interest_cost
wholesale_funding_cost
funding_shortfall_penalty
total_objective
expected_loan_volume
expected_deposit_volume
funding_shortfall
```

Important columns in the sensitivity DataFrame:

```text
scenario_name
best_loan_rate
best_deposit_rate
objective_value
expected_loan_volume
expected_deposit_volume
funding_shortfall
boundary_warning_flag
```

### Response Model Usage

Use this from Python if you want to build and evaluate ML response datasets directly.

```python
from balance_pricing_engine.modelling.datasets import (
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.evaluation import evaluate_binary_classifier
from balance_pricing_engine.modelling.models import (
    build_logistic_regression_model,
    fit_response_model,
)

dataset = build_loan_response_dataset(...)
model = fit_response_model(
    build_logistic_regression_model(),
    dataset,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
)
metrics = evaluate_binary_classifier(model, dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
```

## 8. How To Change A Scenario

For simple scenario changes, edit `configs/base.yaml`.

Common changes:

- Change competitor rates:

```yaml
market:
  competitor_loan_rate: 0.0675
  competitor_deposit_rate: 0.045
```

- Change the search grid:

```yaml
products:
  loan_rate_grid:
    start: 0.058
    stop: 0.082
    step: 0.002
  deposit_rate_grid:
    start: 0.035
    stop: 0.058
    step: 0.002
```

- Change funding constraints:

```yaml
balance_sheet:
  required_deposit_funding_ratio: 0.65
  funding_shortfall_penalty_rate: 0.018
```

- Change response assumptions:

```yaml
response:
  loan_rate_elasticity: -85.0
  deposit_rate_elasticity: 95.0
```

After changes, rerun:

```bash
python3 examples/run_baseline.py
python3 -m pytest -q
```

## 9. Model Risk Review Checklist

Before relying on a result, review:

- Are the tested rate ranges wide enough?
- Did any winning rate hit a grid boundary?
- Are elasticities directionally correct and economically plausible?
- Is the credit loss assumption material enough for lending risk?
- Is the funding shortfall penalty strong enough to influence balance-sheet behavior?
- Do sensitivity cases produce stable recommendations?
- Are top candidate objectives very close together?
- Are there business reasons to prefer a lower-risk candidate over the mathematical winner?
- Do learned response models recover the expected loan and deposit rate-response directions?
- Are model metrics and calibration stable enough for the intended diagnostic use?

## 10. Current Limitations

The engine intentionally does not yet include:

- Personalized pricing.
- FastAPI.
- Streamlit or dashboard UI.
- Capital modeling.
- Prepayment modeling.
- Deposit runoff.
- Behavioral maturity modeling.
- Real customer data.

Those should come later, after the economics and diagnostics are easy to inspect.

The ML layer is currently diagnostic only. It does not yet feed the optimizer.

## 11. Troubleshooting

If `python` is not found, use:

```bash
python3
```

If imports fail, reinstall the project in editable mode:

```bash
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -e ".[dev]"
```

If tests are not found or imports fail, run commands from the repo root:

```bash
cd /Users/kevinchang/Documents/ai-bank-pricing-engine
```

If the optimizer keeps choosing a grid boundary, widen the relevant rate grid in
`configs/base.yaml` and rerun the report.
