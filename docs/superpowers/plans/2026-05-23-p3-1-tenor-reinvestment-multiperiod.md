# P3-1: Tenor and Reinvestment Dynamics in Multi-Period

> **STATUS: ✅ COMPLETE** — tenor/reinvestment dynamics in multi-period — PR #37

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `reinvestment_spread_bps` and `loan_tenor_years` to `MultiPeriodConfig` so the back-book income calculation reflects realistic reinvestment economics and tenor-driven runoff, rather than a flat runoff rate decoupled from loan maturity.

**Architecture:** Two new optional fields on `MultiPeriodConfig`: `reinvestment_spread_bps: float = 0.0` (spread earned on back-book assets when they reinvest at market) and `loan_tenor_years: float | None = None` (when set, replaces `loan_runoff_rate` with `1 / loan_tenor_years` per period — uniform amortisation approximation). `multiperiod.py` uses both when computing the back-book income contribution each period. No change to `OptimizationResult`, `ScenarioDiagnostics`, or the grid search. `MultiPeriodDiagnostics` gains a `reinvestment_spread_bps` field for transparency. The warning about competitor rates not being drifted is extended to cover reinvestment spread drift too.

**Tech Stack:** Python 3.9+, pydantic v2 (already installed), pytest. No new dependencies.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `src/balance_pricing_engine/config.py` | Add `reinvestment_spread_bps`, `loan_tenor_years` to `MultiPeriodConfig` |
| Modify | `src/balance_pricing_engine/scenarios/multiperiod.py` | Use both new fields in back-book income and runoff |
| Modify | `tests/unit/test_multiperiod.py` | Tests for reinvestment income and tenor-driven runoff |

---

## Background: current back-book calculation

In `multiperiod.py`, the current loop (lines 52–72) tracks:

```python
back_book_loan_income *= 1 - mp.loan_runoff_rate   # surviving fraction of prior back-book
back_book_loan_income += diagnostics.best_result.loan_income_after_losses  # new book added
back_book_income = back_book_loan_income - back_book_deposit_cost           # net back-book
period_net = diagnostics.best_result.total_objective + back_book_income
discounted_contribution = period_net / (1 + mp.discount_rate) ** (period + 1)
```

The new fields affect:
1. `loan_runoff_rate` — if `loan_tenor_years` is set, effective runoff = `1 / loan_tenor_years`
2. `back_book_loan_income` — reinvesting surviving assets earns `cash_rate + reinvestment_spread_bps / 10_000` instead of the original booked rate, so the surviving back-book income is the *reinvestment income*, not the original loan income

---

## Task 1: Config changes

**Files:**

- Modify: `src/balance_pricing_engine/config.py`
- Test: `tests/unit/test_multiperiod.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_multiperiod.py`:

```python
from pydantic import ValidationError


def test_reinvestment_spread_bps_defaults_to_zero():
    cfg = MultiPeriodConfig(n_periods=2)
    assert cfg.reinvestment_spread_bps == 0.0


def test_loan_tenor_years_defaults_to_none():
    cfg = MultiPeriodConfig(n_periods=2)
    assert cfg.loan_tenor_years is None


def test_loan_tenor_years_must_be_positive():
    with pytest.raises(ValidationError):
        MultiPeriodConfig(n_periods=2, loan_tenor_years=0.0)

    with pytest.raises(ValidationError):
        MultiPeriodConfig(n_periods=2, loan_tenor_years=-1.0)


def test_loan_tenor_years_accepted_when_positive():
    cfg = MultiPeriodConfig(n_periods=2, loan_tenor_years=5.0)
    assert cfg.loan_tenor_years == 5.0


def test_reinvestment_spread_bps_negative_is_allowed():
    cfg = MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=-25.0)
    assert cfg.reinvestment_spread_bps == -25.0
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_multiperiod.py::test_reinvestment_spread_bps_defaults_to_zero -v
```

Expected: `AttributeError` — `reinvestment_spread_bps` not on `MultiPeriodConfig`.

- [ ] **Step 3: Add the two fields to `MultiPeriodConfig` in `config.py`**

Find `MultiPeriodConfig` (around line 104) and replace it with:

```python
class MultiPeriodConfig(BaseModel):
    n_periods: int = Field(default=2, ge=2)
    loan_runoff_rate: float = Field(default=0.30, ge=0, le=1)
    deposit_runoff_rate: float = Field(default=0.50, ge=0, le=1)
    discount_rate: float = Field(default=0.08, ge=0)
    market_cash_rate_drift_bps: float = 0.0
    reinvestment_spread_bps: float = 0.0
    loan_tenor_years: Optional[float] = Field(default=None, gt=0)
```

- [ ] **Step 4: Run the config tests**

```bash
python3 -m pytest tests/unit/test_multiperiod.py -v -k "reinvestment or tenor"
```

Expected: all 5 new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/config.py tests/unit/test_multiperiod.py
git commit -m "feat: add reinvestment_spread_bps and loan_tenor_years to MultiPeriodConfig"
```

---

## Task 2: Wire into `multiperiod.py`

**Files:**

- Modify: `src/balance_pricing_engine/scenarios/multiperiod.py`
- Test: `tests/unit/test_multiperiod.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_multiperiod.py`:

```python
def test_tenor_years_overrides_loan_runoff_rate(monkeypatch):
    period_results = [
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
    ]

    def fake_run(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)

    # loan_tenor_years=2 → effective runoff = 1/2 = 0.50
    # loan_runoff_rate is set differently to confirm it is ignored
    config = make_config(
        MultiPeriodConfig(n_periods=2, loan_runoff_rate=0.10, loan_tenor_years=2.0)
    )
    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    # After period 0: back_book_loan_income = 10.0
    # Period 1 opens with: 10.0 * (1 - 0.50) = 5.0  (not 10.0 * (1-0.10) = 9.0)
    # Expected period 1 back_book_income = 5.0 - (back_book_deposit_cost surviving)
    assert result.loan_runoff_rate == pytest.approx(0.50)


def test_reinvestment_spread_raises_back_book_income(monkeypatch):
    # When reinvestment_spread_bps=100, surviving back-book earns cash_rate + 1%
    # Period 0: loan_income = 10.0 added to back-book
    # Period 1: back-book survives at (1 - runoff) and earns reinvestment_rate
    #   reinvestment_rate = cash_rate(0.04) + 0.01 = 0.05
    #   surviving back-book VOLUME (from period 0) = best_result.expected_loan_volume * (1-runoff)
    # This test checks that period 1 discounted contribution differs from the zero-spread baseline.

    results_with_spread = [
        _fake_period_result_with_volume(
            total_objective=100.0, loan_income_after_losses=10.0,
            deposit_cost=2.0, loan_volume=200.0, deposit_volume=150.0
        ),
        _fake_period_result_with_volume(
            total_objective=100.0, loan_income_after_losses=10.0,
            deposit_cost=2.0, loan_volume=200.0, deposit_volume=150.0
        ),
    ]
    results_zero_spread = [
        _fake_period_result_with_volume(
            total_objective=100.0, loan_income_after_losses=10.0,
            deposit_cost=2.0, loan_volume=200.0, deposit_volume=150.0
        ),
        _fake_period_result_with_volume(
            total_objective=100.0, loan_income_after_losses=10.0,
            deposit_cost=2.0, loan_volume=200.0, deposit_volume=150.0
        ),
    ]

    def fake_with(config, max_boundary_expansions):
        return results_with_spread.pop(0)

    def fake_zero(config, max_boundary_expansions):
        return results_zero_spread.pop(0)

    config_with = make_config(
        MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=100.0, loan_runoff_rate=0.30)
    )
    config_zero = make_config(
        MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=0.0, loan_runoff_rate=0.30)
    )

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_with)
    result_with = run_multiperiod_diagnostics(config_with, max_boundary_expansions=0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_zero)
    result_zero = run_multiperiod_diagnostics(config_zero, max_boundary_expansions=0)

    assert result_with.npv > result_zero.npv


def test_reinvestment_spread_bps_exposed_on_diagnostics(monkeypatch):
    period_results = [
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
    ]

    def fake_run(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)
    config = make_config(MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=50.0))

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.reinvestment_spread_bps == 50.0
```

Also add this helper alongside `_fake_period_result`:

```python
def _fake_period_result_with_volume(
    total_objective: float,
    loan_income_after_losses: float,
    deposit_cost: float,
    loan_volume: float,
    deposit_volume: float,
) -> ScenarioDiagnostics:
    result = OptimizationResult(
        loan_rate=0.06,
        deposit_rate=0.04,
        loan_interest_income=loan_income_after_losses,
        expected_credit_loss=0.0,
        deposit_interest_cost=deposit_cost,
        wholesale_funding_cost=0.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=total_objective,
        expected_loan_volume=loan_volume,
        expected_deposit_volume=deposit_volume,
        funding_shortfall=0.0,
    )
    return ScenarioDiagnostics(
        best_result=result,
        grid_results=pd.DataFrame(),
        boundary_diagnostics=None,
        initial_boundary_diagnostics=None,
    )
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_multiperiod.py::test_tenor_years_overrides_loan_runoff_rate -v
```

Expected: `AttributeError` or assertion error — logic not yet in `multiperiod.py`.

- [ ] **Step 3: Update `MultiPeriodDiagnostics` to expose `reinvestment_spread_bps`**

In `multiperiod.py`, replace `MultiPeriodDiagnostics`:

```python
@dataclass(frozen=True)
class MultiPeriodDiagnostics:
    period_diagnostics: list[ScenarioDiagnostics]
    period_discounted_contributions: list[float]
    npv: float
    n_periods: int
    loan_runoff_rate: float
    deposit_runoff_rate: float
    discount_rate: float
    reinvestment_spread_bps: float = 0.0

    def period_summary(self) -> pd.DataFrame:
        """Return one row per period with headline pricing and objective values."""
        return pd.DataFrame(
            [
                {
                    "period": period,
                    "best_loan_rate": diagnostics.best_result.loan_rate,
                    "best_deposit_rate": diagnostics.best_result.deposit_rate,
                    "total_objective": diagnostics.best_result.total_objective,
                    "discounted_contribution": discounted_contribution,
                }
                for period, (diagnostics, discounted_contribution) in enumerate(
                    zip(self.period_diagnostics, self.period_discounted_contributions)
                )
            ]
        )
```

- [ ] **Step 4: Update `run_multiperiod_diagnostics` to use both new fields**

Replace `run_multiperiod_diagnostics` in `multiperiod.py` with:

```python
def run_multiperiod_diagnostics(
    config: EngineConfig,
    max_boundary_expansions: int = 2,
) -> MultiPeriodDiagnostics:
    mp = config.multiperiod
    if mp is None:
        raise ValueError("config.multiperiod is required for multi-period diagnostics.")

    # Determine effective per-period loan runoff rate
    effective_loan_runoff = (
        1.0 / mp.loan_tenor_years if mp.loan_tenor_years is not None else mp.loan_runoff_rate
    )

    reinvestment_rate = (
        config.market.cash_rate + mp.reinvestment_spread_bps / 10_000
    )

    if mp.market_cash_rate_drift_bps != 0:
        warnings.warn(
            "market_cash_rate_drift_bps is set but competitor rates are not drifted. "
            "Competitor gaps will shift mechanically, distorting elasticity calculations.",
            stacklevel=2,
        )

    period_diagnostics: list[ScenarioDiagnostics] = []
    period_discounted_contributions: list[float] = []
    back_book_loan_volume = 0.0   # surviving loan balances from prior periods
    back_book_deposit_volume = 0.0

    for period in range(mp.n_periods):
        # Surviving back-book earns reinvestment_rate on remaining balances
        back_book_loan_volume *= 1 - effective_loan_runoff
        back_book_deposit_volume *= 1 - mp.deposit_runoff_rate

        back_book_loan_income = back_book_loan_volume * reinvestment_rate
        back_book_deposit_cost = back_book_deposit_volume * config.market.cash_rate

        scenario_config = _build_period_config(config, period)
        diagnostics = run_baseline_diagnostics(
            scenario_config,
            max_boundary_expansions=max_boundary_expansions,
        )
        back_book_income = back_book_loan_income - back_book_deposit_cost
        period_net = diagnostics.best_result.total_objective + back_book_income
        discounted_contribution = period_net / (1 + mp.discount_rate) ** (period + 1)

        period_diagnostics.append(diagnostics)
        period_discounted_contributions.append(discounted_contribution)

        back_book_loan_volume += diagnostics.best_result.expected_loan_volume
        back_book_deposit_volume += diagnostics.best_result.expected_deposit_volume

    return MultiPeriodDiagnostics(
        period_diagnostics=period_diagnostics,
        period_discounted_contributions=period_discounted_contributions,
        npv=sum(period_discounted_contributions),
        n_periods=mp.n_periods,
        loan_runoff_rate=effective_loan_runoff,
        deposit_runoff_rate=mp.deposit_runoff_rate,
        discount_rate=mp.discount_rate,
        reinvestment_spread_bps=mp.reinvestment_spread_bps,
    )
```

Note the semantic shift: back-book tracking is now done in **volumes** (balances outstanding) rather than income, so that the reinvestment rate can be applied correctly. The income is computed each period as `volume × rate`.

- [ ] **Step 5: Run all multiperiod tests**

```bash
python3 -m pytest tests/unit/test_multiperiod.py -v
```

Expected: all tests pass, including the three new ones. The existing `test_discounting_and_back_book_runoff_are_applied_by_period` test uses monkeypatched fake results with zero volumes — verify it still passes (back-book volume is 0 throughout when `expected_loan_volume=0` in the fake).

- [ ] **Step 6: Run the full suite**

```bash
python3 -m pytest -q
```

Expected: same or higher pass count.

- [ ] **Step 7: Commit**

```bash
git add src/balance_pricing_engine/scenarios/multiperiod.py tests/unit/test_multiperiod.py
git commit -m "feat: tenor-driven runoff and reinvestment spread in multi-period diagnostics"
```

---

## Self-review

**Spec coverage:**

- `loan_tenor_years` replaces `loan_runoff_rate` when set (effective runoff = 1/tenor) ✓
- `reinvestment_spread_bps` controls income earned on surviving back-book ✓
- `MultiPeriodDiagnostics.reinvestment_spread_bps` exposed for transparency ✓
- Existing tests still pass (fake results use `expected_loan_volume=0` so back-book stays zero) ✓
- Config validation: `loan_tenor_years > 0` ✓

**Placeholder scan:** None found.

**Type consistency:** `effective_loan_runoff: float` derived once and used throughout. `reinvestment_rate: float` computed from config. `back_book_loan_volume` and `back_book_deposit_volume` are plain `float` scalars matching `expected_loan_volume` / `expected_deposit_volume` types on `OptimizationResult`.
