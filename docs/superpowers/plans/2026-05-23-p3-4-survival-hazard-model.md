# P3-4: Survival/Hazard Response Model (Interface + Synthetic Stub)

> **STATUS: ✅ COMPLETE** — SurvivalResponseModel + synthetic attrition dataset — PR #37

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define a `SurvivalResponseModel` class in `modelling/agentic.py` that implements the `ResponseModel` Protocol using a Weibull AFT model (via `lifelines`), plus a `SyntheticAttritionDataset` builder in `modelling/datasets.py` that generates survival data from the synthetic engine so the model can be trained and tested without real data.

**Architecture:** `SurvivalResponseModel` wraps `lifelines.WeibullAFTFitter`. Its `fit` method expects a dataset with a `duration` column (time to churn/maturity) and an `event_observed` column (1 = churned/matured, 0 = censored). `predict_probability` returns the probability of surviving (not churning) to the end of a standard hold period (default 12 months). `build_attrition_dataset` in `datasets.py` generates synthetic duration data by sampling from a Weibull distribution parameterised by the existing synthetic response probabilities — high-acceptance customers get longer modelled tenors. This gives a concrete, testable stub until real attrition data exists. `lifelines` is added to the `[ml]` optional extra.

**Governance note:** This model is decision-support only. Its predictions must not replace the optimizer's synthetic response functions until the model is trained on real attrition data and approved through the governance workflow.

**Tech Stack:** Python 3.9+, `lifelines>=0.28` (optional `[ml]` extra), numpy, pandas, pytest. Tests monkeypatch `lifelines` to avoid CI installs.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `pyproject.toml` | Add `lifelines>=0.28` to `[ml]` optional extra |
| Modify | `src/balance_pricing_engine/modelling/datasets.py` | Add `build_attrition_dataset` |
| Modify | `src/balance_pricing_engine/modelling/agentic.py` | Add `SurvivalResponseModel` |
| Modify | `tests/unit/test_modelling_agentic.py` | Tests for `SurvivalResponseModel` |
| Modify | `tests/unit/test_modelling_datasets.py` | Tests for `build_attrition_dataset` |

---

## Background: `ResponseModel` Protocol

From `agentic.py` line 32:

```python
class ResponseModel(Protocol):
    name: str
    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None: ...
    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray: ...
```

`SurvivalResponseModel.fit` accepts the same signature but uses `duration_col` and `event_col` attributes (set at construction) to extract the survival columns from `train`. The `feature_columns` passed to `fit` and `predict_probability` are the covariates used by the Weibull AFT model.

---

## Task 1: `build_attrition_dataset` in `datasets.py`

**Files:**

- Modify: `src/balance_pricing_engine/modelling/datasets.py`
- Test: `tests/unit/test_modelling_datasets.py`

- [ ] **Step 1: Read the existing dataset builder signatures**

```bash
grep -n "def build_loan\|def build_deposit\|LOAN_FEATURE_COLUMNS\|DEPOSIT_FEATURE_COLUMNS" \
  src/balance_pricing_engine/modelling/datasets.py | head -20
```

- [ ] **Step 2: Write the failing test**

Add to `tests/unit/test_modelling_datasets.py`:

```python
from balance_pricing_engine.modelling.datasets import build_attrition_dataset


def test_build_attrition_dataset_returns_dataframe_with_required_columns():
    rng = np.random.default_rng(42)
    n = 200
    acceptance_probs = rng.uniform(0.2, 0.8, n)

    frame = build_attrition_dataset(
        acceptance_probabilities=acceptance_probs,
        n_rows=n,
        hold_period_months=12,
        rng=rng,
    )

    assert "duration" in frame.columns
    assert "event_observed" in frame.columns
    assert len(frame) == n
    assert frame["duration"].min() >= 1
    assert frame["event_observed"].isin([0, 1]).all()


def test_build_attrition_dataset_high_acceptance_implies_longer_duration():
    rng = np.random.default_rng(0)
    n = 1000
    high_probs = np.full(n, 0.9)
    low_probs = np.full(n, 0.1)

    high_frame = build_attrition_dataset(high_probs, n_rows=n, rng=rng)
    rng2 = np.random.default_rng(0)
    low_frame = build_attrition_dataset(low_probs, n_rows=n, rng=rng2)

    assert high_frame["duration"].mean() > low_frame["duration"].mean()
```

- [ ] **Step 3: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_datasets.py::test_build_attrition_dataset_returns_dataframe_with_required_columns -v
```

Expected: `ImportError` — `build_attrition_dataset` not defined.

- [ ] **Step 4: Implement `build_attrition_dataset` in `datasets.py`**

Add at the end of `src/balance_pricing_engine/modelling/datasets.py`:

```python
def build_attrition_dataset(
    acceptance_probabilities: np.ndarray,
    n_rows: int | None = None,
    hold_period_months: int = 12,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Build a synthetic survival dataset from acceptance probabilities.

    Higher acceptance probability → longer modelled hold duration (customers
    who accepted are more engaged and churn later). Weibull shape parameter
    is fixed at 1.5; scale is proportional to acceptance probability.

    Columns:
      duration        — time in months until churn or censoring (>=1)
      event_observed  — 1 = churned, 0 = censored (survived to hold_period_months)
    """
    if rng is None:
        rng = np.random.default_rng(42)
    n = n_rows if n_rows is not None else len(acceptance_probabilities)
    if len(acceptance_probabilities) != n:
        raise ValueError("acceptance_probabilities length must equal n_rows.")

    weibull_shape = 1.5
    # Scale: customers with prob=1.0 get mean duration = 1.5 * hold_period
    scale = acceptance_probabilities * hold_period_months * 1.5 + 1.0

    # Sample from Weibull: duration = scale * (-ln(u))^(1/shape)
    u = rng.uniform(0.0, 1.0, n)
    raw_duration = scale * (-np.log(np.clip(u, 1e-9, 1.0))) ** (1.0 / weibull_shape)
    duration = np.clip(np.round(raw_duration).astype(int), 1, None)

    event_observed = (duration <= hold_period_months).astype(int)
    duration = np.minimum(duration, hold_period_months)

    return pd.DataFrame({"duration": duration, "event_observed": event_observed})
```

- [ ] **Step 5: Run the dataset tests**

```bash
python3 -m pytest tests/unit/test_modelling_datasets.py -v -k "attrition"
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/modelling/datasets.py tests/unit/test_modelling_datasets.py
git commit -m "feat: add build_attrition_dataset for synthetic survival data"
```

---

## Task 2: `SurvivalResponseModel` in `agentic.py`

**Files:**

- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Update `pyproject.toml` to add `lifelines` to `[ml]`**

Find:

```toml
ml = ["lightgbm>=4.0", "xgboost>=2.0"]
```

Replace with:

```toml
ml = ["lightgbm>=4.0", "xgboost>=2.0", "lifelines>=0.28"]
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from balance_pricing_engine.modelling.agentic import SurvivalResponseModel


def _make_fake_lifelines_module():
    """Mock lifelines.WeibullAFTFitter with plausible behavior."""
    fitter = MagicMock()
    fitter.fit.return_value = fitter
    fitter.predict_survival_function.return_value = pd.DataFrame(
        {i: [0.7] for i in range(100)},
        index=[12],
    )
    ll = MagicMock()
    ll.WeibullAFTFitter.return_value = fitter
    return ll


def test_survival_response_model_name():
    assert SurvivalResponseModel.name == "SurvivalResponseModel"


def test_survival_response_model_raises_before_fit():
    model = SurvivalResponseModel()
    df = pd.DataFrame({"offered_rate": [0.08], "duration": [12], "event_observed": [1]})
    with pytest.raises(ValueError, match="fitted"):
        model.predict_probability(df, ["offered_rate"])


def test_survival_response_model_raises_import_error_when_lifelines_missing():
    with patch.dict(sys.modules, {"lifelines": None}):
        model = SurvivalResponseModel()
        train = pd.DataFrame({
            "offered_rate": [0.08] * 50,
            "duration": list(range(1, 51)),
            "event_observed": [1] * 50,
        })
        with pytest.raises(ImportError, match="lifelines"):
            model.fit(train, ["offered_rate"], "event_observed")


def test_survival_response_model_fits_and_predicts():
    fake_ll = _make_fake_lifelines_module()

    with patch.dict(sys.modules, {"lifelines": fake_ll}):
        model = SurvivalResponseModel(hold_period_months=12)
        train = pd.DataFrame({
            "offered_rate": np.random.rand(100),
            "duration": np.random.randint(1, 13, 100),
            "event_observed": np.random.randint(0, 2, 100),
        })
        model.fit(train, ["offered_rate"], "event_observed")
        probs = model.predict_probability(train, ["offered_rate"])

    assert probs.shape == (100,)
    assert np.all(probs >= 0) and np.all(probs <= 1)


def test_survival_response_model_predicts_survival_probability():
    fake_ll = _make_fake_lifelines_module()

    with patch.dict(sys.modules, {"lifelines": fake_ll}):
        model = SurvivalResponseModel(hold_period_months=12)
        train = pd.DataFrame({
            "offered_rate": np.random.rand(50),
            "duration": np.random.randint(1, 13, 50),
            "event_observed": [1] * 50,
        })
        model.fit(train, ["offered_rate"], "event_observed")
        probs = model.predict_probability(train, ["offered_rate"])

    # mock returns 0.7 for all rows at t=12
    assert probs == pytest.approx(np.full(50, 0.7))
```

- [ ] **Step 3: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_survival_response_model_name -v
```

Expected: `ImportError` — `SurvivalResponseModel` not defined.

- [ ] **Step 4: Implement `SurvivalResponseModel` in `agentic.py`**

Add after `XGBoostResponseModel`:

```python
class SurvivalResponseModel:
    """Governed Weibull AFT survival model for churn/rollover prediction.

    Requires the [ml] optional extra (lifelines). Predicts the probability of
    a customer surviving (not churning) to `hold_period_months`.

    Governance note: decision-support only. Must not replace the synthetic
    response functions in the optimizer until trained on real attrition data
    and approved through the ModelRegistry governance workflow.
    """

    name = "SurvivalResponseModel"

    def __init__(
        self,
        hold_period_months: int = 12,
        duration_col: str = "duration",
        event_col: str = "event_observed",
        model: Any | None = None,
    ):
        self._hold_period_months = hold_period_months
        self._duration_col = duration_col
        self._event_col = event_col
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        try:
            import lifelines
        except ImportError as exc:
            raise ImportError(
                "SurvivalResponseModel requires lifelines. "
                "Install it with: pip install 'balance_pricing_engine[ml]'"
            ) from exc
        fitter = lifelines.WeibullAFTFitter()
        fit_frame = train[feature_columns + [self._duration_col, self._event_col]].copy()
        fitter.fit(
            fit_frame,
            duration_col=self._duration_col,
            event_col=self._event_col,
        )
        self._model = fitter

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        """Return probability of surviving to hold_period_months for each row."""
        if self._model is None:
            raise ValueError("SurvivalResponseModel must be fitted before scoring.")
        survival = self._model.predict_survival_function(
            data[feature_columns], times=[self._hold_period_months]
        )
        return survival.loc[self._hold_period_months].to_numpy(dtype=float)

    @property
    def fitted_model(self) -> Any:
        return self._model
```

- [ ] **Step 5: Run the survival model tests**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v -k "survival"
```

Expected: all 5 PASS.

- [ ] **Step 6: Run the full suite**

```bash
python3 -m pytest -q
```

Expected: same or higher pass count.

- [ ] **Step 7: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py \
        src/balance_pricing_engine/modelling/datasets.py \
        pyproject.toml \
        tests/unit/test_modelling_agentic.py \
        tests/unit/test_modelling_datasets.py
git commit -m "feat: SurvivalResponseModel stub and build_attrition_dataset for synthetic survival data"
```

---

## Self-review

**Spec coverage:**

- `SurvivalResponseModel` implements `ResponseModel` Protocol ✓
- `fit` uses `lifelines.WeibullAFTFitter` ✓
- `predict_probability` returns survival probability to `hold_period_months` ✓
- Raises `ImportError` with install instructions if `lifelines` missing ✓
- Raises `ValueError` if scored before fit ✓
- `build_attrition_dataset` generates testable synthetic survival data ✓
- `lifelines` added to `[ml]` extra ✓
- Governance warning documented in class docstring ✓

**Placeholder scan:** None found.

**Type consistency:** `SurvivalResponseModel.predict_probability` returns `np.ndarray` as required by the `ResponseModel` Protocol. `build_attrition_dataset` returns `pd.DataFrame` with `duration: int` and `event_observed: int` columns — consistent with what `lifelines.WeibullAFTFitter.fit` expects.
