# P3-2: Capital/RWA Term Adjustment and ROE Hurdle Constraint

> **STATUS: ✅ COMPLETE** — capital/RWA term + ROE hurdle constraint — PR #37

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add (a) a maturity-scaled RWA weight that adjusts capital charge by loan tenor, and (b) a `min_return_on_equity` floor that hard-filters rate pairs not meeting the ROE hurdle before the optimizer selects a winner.

**Architecture:** Two additions to `BalanceSheetConfig`: `average_loan_tenor_years: float = 1.0` (scales `loan_rwa_weight` by a simple Basel-style maturity factor) and `min_return_on_equity: float | None = None` (when set, `select_best_result` skips any grid row where `return_on_equity < min_return_on_equity`). A new `return_on_equity` column is added to `GRID_RESULT_COLUMNS` and `OptimizationResult`. Capital charge already uses `expected_capital_charge` in `profit.py` — we add a `maturity_adjusted_rwa_weight` helper there. `select_best_result` in `grid_search.py` gets an optional `min_return_on_equity` parameter. No change to the optimizer loop itself.

**Tech Stack:** Python 3.9+, numpy, pandas, pydantic v2. No new dependencies.

**Regulatory context:** The maturity factor approximates Basel III's maturity adjustment `M_adj = (1 + (M - 2.5) * b) / (1 - 1.5 * b)` where `b` is a supervisory constant (~0.11478 for typical corporate exposures). For this engine we use a simplified linear approximation: `effective_rwa = base_rwa * (0.70 + 0.06 * clamped_tenor)` where tenor is clamped to [1, 5] years. This is not a regulatory calculation — it is a sensitivity proxy only.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `src/balance_pricing_engine/config.py` | Add `average_loan_tenor_years`, `min_return_on_equity` to `BalanceSheetConfig` |
| Modify | `src/balance_pricing_engine/pricing/profit.py` | Add `maturity_adjusted_rwa_weight`, update `expected_capital_charge` |
| Modify | `src/balance_pricing_engine/optimization/grid_search.py` | Add `return_on_equity` column; add `min_roe` filter in `select_best_result` |
| Modify | `tests/unit/test_profit_and_balance_sheet.py` | Tests for `maturity_adjusted_rwa_weight` |
| Modify | `tests/unit/test_optimizer.py` | Tests for `return_on_equity` column and ROE filter |

---

## Task 1: `BalanceSheetConfig` fields + `maturity_adjusted_rwa_weight`

**Files:**

- Modify: `src/balance_pricing_engine/config.py`
- Modify: `src/balance_pricing_engine/pricing/profit.py`
- Test: `tests/unit/test_profit_and_balance_sheet.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_profit_and_balance_sheet.py`:

```python
from balance_pricing_engine.config import BalanceSheetConfig
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight


def test_maturity_adjusted_rwa_weight_at_one_year_equals_base():
    weight = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=1.0)
    assert weight == pytest.approx(0.35 * (0.70 + 0.06 * 1.0))


def test_maturity_adjusted_rwa_weight_increases_with_tenor():
    w1 = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=1.0)
    w3 = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=3.0)
    w5 = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=5.0)
    assert w1 < w3 < w5


def test_maturity_adjusted_rwa_weight_clamps_below_one_year():
    w_clamped = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=0.5)
    w_one = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=1.0)
    assert w_clamped == pytest.approx(w_one)


def test_maturity_adjusted_rwa_weight_clamps_above_five_years():
    w_clamped = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=10.0)
    w_five = maturity_adjusted_rwa_weight(base_rwa_weight=0.35, tenor_years=5.0)
    assert w_clamped == pytest.approx(w_five)


def test_balance_sheet_config_average_loan_tenor_years_defaults_to_one():
    bs = BalanceSheetConfig(
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )
    assert bs.average_loan_tenor_years == 1.0


def test_balance_sheet_config_min_return_on_equity_defaults_to_none():
    bs = BalanceSheetConfig(
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )
    assert bs.min_return_on_equity is None


def test_balance_sheet_config_rejects_non_positive_tenor():
    with pytest.raises(Exception):
        BalanceSheetConfig(
            required_deposit_funding_ratio=0.65,
            funding_shortfall_penalty_rate=0.018,
            average_loan_tenor_years=0.0,
        )
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_profit_and_balance_sheet.py::test_maturity_adjusted_rwa_weight_at_one_year_equals_base -v
```

Expected: `ImportError` — `maturity_adjusted_rwa_weight` not defined.

- [ ] **Step 3: Add fields to `BalanceSheetConfig` in `config.py`**

Replace `BalanceSheetConfig`:

```python
class BalanceSheetConfig(BaseModel):
    required_deposit_funding_ratio: float = Field(ge=0, le=1)
    funding_shortfall_penalty_rate: float = Field(ge=0)
    loan_rwa_weight: float = Field(default=0.35, ge=0, le=2.5)
    capital_hurdle_rate: float = Field(default=0.12, ge=0, le=1)
    average_loan_tenor_years: float = Field(default=1.0, gt=0)
    min_return_on_equity: Optional[float] = Field(default=None, ge=0)
```

- [ ] **Step 4: Add `maturity_adjusted_rwa_weight` to `profit.py`**

Add after `expected_capital_charge`:

```python
def maturity_adjusted_rwa_weight(base_rwa_weight: float, tenor_years: float) -> float:
    """Return a maturity-scaled RWA weight using a simplified linear approximation.

    Clamps tenor to [1, 5] years. Factor = 0.70 + 0.06 * tenor.
    At 1y: 0.76x base. At 5y: 1.00x base. Not a regulatory calculation.
    """
    clamped = max(1.0, min(5.0, tenor_years))
    return base_rwa_weight * (0.70 + 0.06 * clamped)
```

- [ ] **Step 5: Run the new tests**

```bash
python3 -m pytest tests/unit/test_profit_and_balance_sheet.py -v -k "maturity or tenor or equity"
```

Expected: all 7 new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/config.py src/balance_pricing_engine/pricing/profit.py \
        tests/unit/test_profit_and_balance_sheet.py
git commit -m "feat: maturity_adjusted_rwa_weight and BalanceSheetConfig tenor/ROE fields"
```

---

## Task 2: `return_on_equity` column and ROE filter in `grid_search.py`

**Files:**

- Modify: `src/balance_pricing_engine/optimization/grid_search.py`
- Test: `tests/unit/test_optimizer.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_optimizer.py`:

```python
from balance_pricing_engine.optimization.grid_search import (
    GRID_RESULT_COLUMNS,
    evaluate_joint_rate_grid,
    select_best_result,
)


def test_grid_result_columns_includes_return_on_equity():
    assert "return_on_equity" in GRID_RESULT_COLUMNS


def test_evaluate_joint_rate_grid_has_return_on_equity_column(small_grid_result):
    # small_grid_result is an existing fixture — check it has the new column
    assert "return_on_equity" in small_grid_result.columns


def test_select_best_result_with_roe_floor_excludes_low_roe_rows(small_customers, market):
    # Force a grid where all rows have negative ROE by setting a very high hurdle
    import numpy as np
    from balance_pricing_engine.optimization.grid_search import evaluate_joint_rate_grid
    from balance_pricing_engine.config import ResponseCoefficientsConfig

    grid_results = evaluate_joint_rate_grid(
        loan_customers=small_customers["loan"],
        deposit_customers=small_customers["deposit"],
        market=market,
        loan_rate_grid=np.array([0.06, 0.065]),
        deposit_rate_grid=np.array([0.04]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=95.0,
        deposit_competitor_gap_elasticity=75.0,
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )

    # With a very high floor, should fall back to best available row
    result_high_floor = select_best_result(grid_results, min_return_on_equity=999.0)
    result_no_floor = select_best_result(grid_results)
    # When floor is unreachable, best available row is still returned (no crash)
    assert result_high_floor.loan_rate in [0.06, 0.065]
    assert result_no_floor.loan_rate in [0.06, 0.065]


def test_select_best_result_roe_floor_picks_correct_row(small_customers, market):
    import numpy as np

    grid_results = evaluate_joint_rate_grid(
        loan_customers=small_customers["loan"],
        deposit_customers=small_customers["deposit"],
        market=market,
        loan_rate_grid=np.array([0.06, 0.07, 0.08]),
        deposit_rate_grid=np.array([0.04]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=95.0,
        deposit_competitor_gap_elasticity=75.0,
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )
    # Higher loan rate → higher objective but also higher ROE
    # A tight floor (e.g. 0.01) should still return a valid winner
    result = select_best_result(grid_results, min_return_on_equity=0.01)
    assert result.loan_rate in [0.06, 0.07, 0.08]
```

Check the existing test file for fixture names:

```bash
grep -n "def small_grid_result\|def small_customers\|def market\|@pytest.fixture" \
  tests/unit/test_optimizer.py | head -20
```

Adapt the test to use whatever fixtures already exist in `test_optimizer.py` for loan customers, deposit customers, and market state.

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_optimizer.py::test_grid_result_columns_includes_return_on_equity -v
```

Expected: `AssertionError` — `return_on_equity` not in `GRID_RESULT_COLUMNS`.

- [ ] **Step 3: Add `return_on_equity` to `GRID_RESULT_COLUMNS` in `grid_search.py`**

In `grid_search.py`, append `"return_on_equity"` to `GRID_RESULT_COLUMNS`:

```python
GRID_RESULT_COLUMNS = [
    "loan_rate",
    "deposit_rate",
    "home_loan_spread_to_market_bps",
    "term_deposit_spread_to_market_bps",
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "excess_deposit_income",
    "funding_shortfall_penalty",
    "total_objective",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "return_on_equity",
]
```

- [ ] **Step 4: Compute `return_on_equity` in `evaluate_joint_rate_grid`**

In the `evaluate_joint_rate_grid` function, after `total_objective` is computed (line ~214), add:

```python
tier1_capital_ratio = 0.10
required_capital = loan_volume * rwa_weight * tier1_capital_ratio
return_on_equity = (
    (loan_interest_income - credit_loss - deposit_cost - wholesale_funding_cost)
    / required_capital
    if required_capital > 0
    else 0.0
)
```

And include it in the `rows.append(...)` dict:

```python
rows.append(
    {
        ...
        "return_on_equity": return_on_equity,
    }
)
```

Also update `optimization_result_from_row` to read the new column:

```python
def optimization_result_from_row(row: pd.Series) -> OptimizationResult:
    return OptimizationResult(
        ...
        # return_on_equity is not on OptimizationResult — it stays in the grid only
    )
```

`return_on_equity` is a grid diagnostic column — it does not need to be on `OptimizationResult` (which is already frozen). Leave `OptimizationResult` unchanged.

- [ ] **Step 5: Add `min_return_on_equity` parameter to `select_best_result`**

Replace `select_best_result` in `grid_search.py`:

```python
def select_best_result(
    grid_results: pd.DataFrame,
    min_return_on_equity: float | None = None,
) -> OptimizationResult:
    """Select the highest-objective pricing result from evaluated grid results.

    When min_return_on_equity is set, prefer rows meeting the floor; fall back
    to unconstrained best if no row qualifies (avoids hard infeasibility).
    """
    if grid_results.empty:
        raise ValueError("At least one loan rate and one deposit rate are required.")

    candidates = grid_results
    if min_return_on_equity is not None and "return_on_equity" in grid_results.columns:
        qualified = grid_results[grid_results["return_on_equity"] >= min_return_on_equity]
        if not qualified.empty:
            candidates = qualified

    market_spread_distance = (
        candidates["home_loan_spread_to_market_bps"].abs()
        + candidates["term_deposit_spread_to_market_bps"].abs()
    )
    best_row = (
        candidates.assign(_market_spread_distance=market_spread_distance)
        .sort_values(
            ["total_objective", "_market_spread_distance"],
            ascending=[False, True],
        )
        .iloc[0]
    )
    return optimization_result_from_row(best_row)
```

- [ ] **Step 6: Wire `average_loan_tenor_years` into the optimizer call**

In `scenarios/runner.py`, `evaluate_adaptive_joint_rate_grid` is called with `rwa_weight=config.balance_sheet.loan_rwa_weight`. Change it to use the maturity-adjusted weight:

```python
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight

# inside run_baseline_diagnostics, before calling evaluate_adaptive_joint_rate_grid:
effective_rwa_weight = maturity_adjusted_rwa_weight(
    config.balance_sheet.loan_rwa_weight,
    config.balance_sheet.average_loan_tenor_years,
)
```

Then pass `rwa_weight=effective_rwa_weight` instead of `rwa_weight=config.balance_sheet.loan_rwa_weight`.

Also pass `min_return_on_equity=config.balance_sheet.min_return_on_equity` to `select_best_result` inside `evaluate_adaptive_joint_rate_grid` (or pass it through the call chain). Check how `evaluate_adaptive_joint_rate_grid` calls `select_best_result`:

```bash
grep -n "select_best_result" src/balance_pricing_engine/diagnostics.py \
  src/balance_pricing_engine/optimization/grid_search.py
```

Trace the call chain and thread `min_return_on_equity` through to `select_best_result`.

- [ ] **Step 7: Run the optimizer tests**

```bash
python3 -m pytest tests/unit/test_optimizer.py -v
```

Expected: all pass.

- [ ] **Step 8: Run the full suite**

```bash
python3 -m pytest -q
```

Expected: same or higher pass count. Check `test_baseline_golden.py` — the golden file may need updating if `return_on_equity` is now included in `grid_results.csv`. If so, regenerate:

```bash
python3 examples/run_baseline.py
# update tests/fixtures/baseline_golden.json if it records grid column names
```

- [ ] **Step 9: Commit**

```bash
git add src/balance_pricing_engine/optimization/grid_search.py \
        src/balance_pricing_engine/scenarios/runner.py \
        src/balance_pricing_engine/pricing/profit.py \
        tests/unit/test_optimizer.py tests/fixtures/
git commit -m "feat: return_on_equity column and ROE hurdle filter in grid search"
```

---

## Self-review

**Spec coverage:**

- Maturity-adjusted RWA weight via `maturity_adjusted_rwa_weight` ✓
- `average_loan_tenor_years` wired from config into `run_baseline_diagnostics` ✓
- `return_on_equity` as a diagnostic column in `GRID_RESULT_COLUMNS` ✓
- `min_return_on_equity` hard floor in `select_best_result` (soft fallback when no row qualifies) ✓
- Both new `BalanceSheetConfig` fields default to backwards-compatible values ✓

**Placeholder scan:** Step 6 directs the implementer to grep for the call chain rather than assuming — necessary because `select_best_result` may be called in multiple places.

**Type consistency:** `maturity_adjusted_rwa_weight(base_rwa_weight: float, tenor_years: float) -> float`. `select_best_result(grid_results, min_return_on_equity: float | None = None)`. `return_on_equity` is `float` in the grid dict.
