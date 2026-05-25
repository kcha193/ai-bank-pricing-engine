# P3-3: LightGBM and XGBoost Optional-Dep Adapters

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `LightGBMResponseModel` and `XGBoostResponseModel` to `modelling/agentic.py`, each behind an optional `[ml]` extra, following the exact pattern of the existing `HistGBMResponseModel`.

**Architecture:** Two new classes in `agentic.py` — `LightGBMResponseModel` wraps `lightgbm.LGBMClassifier` and `XGBoostResponseModel` wraps `xgboost.XGBClassifier`. Both expose the same `ResponseModel` Protocol interface (`fit`, `predict_probability`, `feature_importances`). Both guard the import with a clear `ImportError` message if the optional dep is missing. A new `[ml]` optional extra in `pyproject.toml` declares both deps. Tests monkeypatch the libraries to avoid requiring real installs in CI.

**Tech Stack:** Python 3.9+, `lightgbm>=4.0` (optional), `xgboost>=2.0` (optional), pytest. Both are behind `[ml]` extra — never required by default.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `pyproject.toml` | Add `ml = ["lightgbm>=4.0", "xgboost>=2.0"]` optional extra |
| Modify | `src/balance_pricing_engine/modelling/agentic.py` | Add `LightGBMResponseModel`, `XGBoostResponseModel` |
| Modify | `tests/unit/test_modelling_agentic.py` | Tests for both new classes (monkeypatched) |

---

## Background: `HistGBMResponseModel` pattern to follow

From `agentic.py` lines 154–232:

```python
class HistGBMResponseModel:
    name = "HistGBMResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None): ...
    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None: ...
    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray: ...
    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None: ...
    @property
    def fitted_model(self) -> Any: ...
```

`LightGBMResponseModel` and `XGBoostResponseModel` must follow this exact interface.

---

## Task 1: `pyproject.toml` and import guards

**Files:**

- Modify: `pyproject.toml`

- [ ] **Step 1: Add the `[ml]` extra to `pyproject.toml`**

In `pyproject.toml`, find:

```toml
llm = ["anthropic>=0.30"]
```

Replace with:

```toml
llm = ["anthropic>=0.30"]
ml = ["lightgbm>=4.0", "xgboost>=2.0"]
```

- [ ] **Step 2: Verify the project installs cleanly without the extra**

```bash
python3 -m pip install -e ".[dev]" --quiet
python3 -c "import balance_pricing_engine; print('ok')"
```

Expected: `ok`. Neither `lightgbm` nor `xgboost` is imported at package level.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add [ml] optional extra for lightgbm and xgboost"
```

---

## Task 2: `LightGBMResponseModel`

**Files:**

- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
import sys
from unittest.mock import MagicMock, patch

from balance_pricing_engine.modelling.agentic import LightGBMResponseModel
from balance_pricing_engine.modelling.datasets import LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN


def _make_fake_lgbm_classifier():
    """Build a mock LGBMClassifier that behaves like the real one."""
    clf = MagicMock()
    clf.fit.return_value = clf
    clf.predict_proba.return_value = np.column_stack(
        [np.full(100, 0.4), np.full(100, 0.6)]
    )
    clf.feature_importances_ = np.ones(len(LOAN_FEATURE_COLUMNS))
    return clf


def _make_fake_lgbm_module(clf):
    lgbm = MagicMock()
    lgbm.LGBMClassifier.return_value = clf
    return lgbm


def test_lightgbm_response_model_name():
    assert LightGBMResponseModel.name == "LightGBMResponseModel"


def test_lightgbm_response_model_raises_before_fit():
    model = LightGBMResponseModel()
    df = pd.DataFrame({"offered_rate": [0.08]})
    with pytest.raises(ValueError, match="fitted"):
        model.predict_probability(df, ["offered_rate"])


def test_lightgbm_response_model_raises_import_error_when_dep_missing():
    with patch.dict(sys.modules, {"lightgbm": None}):
        model = LightGBMResponseModel()
        df = pd.DataFrame({col: [0.0] for col in LOAN_FEATURE_COLUMNS})
        train = pd.DataFrame({col: [0.0] * 50 for col in LOAN_FEATURE_COLUMNS})
        train[LOAN_TARGET_COLUMN] = [0] * 25 + [1] * 25
        with pytest.raises(ImportError, match="lightgbm"):
            model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)


def test_lightgbm_response_model_fits_and_predicts():
    clf = _make_fake_lgbm_classifier()
    fake_lgbm = _make_fake_lgbm_module(clf)

    with patch.dict(sys.modules, {"lightgbm": fake_lgbm}):
        model = LightGBMResponseModel(random_state=42)
        train = pd.DataFrame({col: np.random.rand(100) for col in LOAN_FEATURE_COLUMNS})
        train[LOAN_TARGET_COLUMN] = np.random.randint(0, 2, 100)

        model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
        probs = model.predict_probability(train, LOAN_FEATURE_COLUMNS)

    assert probs.shape == (100,)
    assert np.all(probs >= 0) and np.all(probs <= 1)


def test_lightgbm_response_model_feature_importances():
    clf = _make_fake_lgbm_classifier()
    fake_lgbm = _make_fake_lgbm_module(clf)

    with patch.dict(sys.modules, {"lightgbm": fake_lgbm}):
        model = LightGBMResponseModel(random_state=42)
        train = pd.DataFrame({col: np.random.rand(100) for col in LOAN_FEATURE_COLUMNS})
        train[LOAN_TARGET_COLUMN] = np.random.randint(0, 2, 100)
        model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)

        importances = model.feature_importances(LOAN_FEATURE_COLUMNS)

    assert importances is not None
    assert set(importances.keys()) == set(LOAN_FEATURE_COLUMNS)
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_lightgbm_response_model_name -v
```

Expected: `ImportError` — `LightGBMResponseModel` not defined.

- [ ] **Step 3: Implement `LightGBMResponseModel` in `agentic.py`**

Add after `HistGBMResponseModel` (around line 232):

```python
class LightGBMResponseModel:
    """Governed LightGBM response model. Requires the [ml] optional extra."""

    name = "LightGBMResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise ImportError(
                "LightGBMResponseModel requires lightgbm. "
                "Install it with: pip install 'balance_pricing_engine[ml]'"
            ) from exc
        clf = lgb.LGBMClassifier(
            random_state=self._random_state,
            n_estimators=100,
            verbose=-1,
        )
        clf.fit(train[feature_columns], train[target_column])
        self._model = clf

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("LightGBMResponseModel must be fitted before scoring.")
        proba = self._model.predict_proba(data[feature_columns])
        return proba[:, 1].astype(float)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None:
        if self._model is None:
            raise ValueError("LightGBMResponseModel must be fitted before retrieving importances.")
        importances = getattr(self._model, "feature_importances_", None)
        if importances is None:
            return None
        return dict(zip(feature_columns, importances.tolist()))

    @property
    def fitted_model(self) -> Any:
        return self._model
```

- [ ] **Step 4: Run the LightGBM tests**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v -k "lightgbm"
```

Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add LightGBMResponseModel adapter behind [ml] optional extra"
```

---

## Task 3: `XGBoostResponseModel`

**Files:**

- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from balance_pricing_engine.modelling.agentic import XGBoostResponseModel


def _make_fake_xgb_classifier():
    clf = MagicMock()
    clf.fit.return_value = clf
    clf.predict_proba.return_value = np.column_stack(
        [np.full(100, 0.35), np.full(100, 0.65)]
    )
    clf.feature_importances_ = np.ones(len(LOAN_FEATURE_COLUMNS))
    return clf


def _make_fake_xgb_module(clf):
    xgb = MagicMock()
    xgb.XGBClassifier.return_value = clf
    return xgb


def test_xgboost_response_model_name():
    assert XGBoostResponseModel.name == "XGBoostResponseModel"


def test_xgboost_response_model_raises_before_fit():
    model = XGBoostResponseModel()
    df = pd.DataFrame({"offered_rate": [0.08]})
    with pytest.raises(ValueError, match="fitted"):
        model.predict_probability(df, ["offered_rate"])


def test_xgboost_response_model_raises_import_error_when_dep_missing():
    with patch.dict(sys.modules, {"xgboost": None}):
        model = XGBoostResponseModel()
        train = pd.DataFrame({col: [0.0] * 50 for col in LOAN_FEATURE_COLUMNS})
        train[LOAN_TARGET_COLUMN] = [0] * 25 + [1] * 25
        with pytest.raises(ImportError, match="xgboost"):
            model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)


def test_xgboost_response_model_fits_and_predicts():
    clf = _make_fake_xgb_classifier()
    fake_xgb = _make_fake_xgb_module(clf)

    with patch.dict(sys.modules, {"xgboost": fake_xgb}):
        model = XGBoostResponseModel(random_state=42)
        train = pd.DataFrame({col: np.random.rand(100) for col in LOAN_FEATURE_COLUMNS})
        train[LOAN_TARGET_COLUMN] = np.random.randint(0, 2, 100)
        model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
        probs = model.predict_probability(train, LOAN_FEATURE_COLUMNS)

    assert probs.shape == (100,)
    assert np.all(probs >= 0) and np.all(probs <= 1)


def test_xgboost_response_model_feature_importances():
    clf = _make_fake_xgb_classifier()
    fake_xgb = _make_fake_xgb_module(clf)

    with patch.dict(sys.modules, {"xgboost": fake_xgb}):
        model = XGBoostResponseModel(random_state=42)
        train = pd.DataFrame({col: np.random.rand(100) for col in LOAN_FEATURE_COLUMNS})
        train[LOAN_TARGET_COLUMN] = np.random.randint(0, 2, 100)
        model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
        importances = model.feature_importances(LOAN_FEATURE_COLUMNS)

    assert importances is not None
    assert set(importances.keys()) == set(LOAN_FEATURE_COLUMNS)
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_xgboost_response_model_name -v
```

Expected: `ImportError` — `XGBoostResponseModel` not defined.

- [ ] **Step 3: Implement `XGBoostResponseModel` in `agentic.py`**

Add immediately after `LightGBMResponseModel`:

```python
class XGBoostResponseModel:
    """Governed XGBoost response model. Requires the [ml] optional extra."""

    name = "XGBoostResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        try:
            import xgboost as xgb
        except ImportError as exc:
            raise ImportError(
                "XGBoostResponseModel requires xgboost. "
                "Install it with: pip install 'balance_pricing_engine[ml]'"
            ) from exc
        clf = xgb.XGBClassifier(
            random_state=self._random_state,
            n_estimators=100,
            eval_metric="logloss",
            verbosity=0,
        )
        clf.fit(train[feature_columns], train[target_column])
        self._model = clf

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("XGBoostResponseModel must be fitted before scoring.")
        proba = self._model.predict_proba(data[feature_columns])
        return proba[:, 1].astype(float)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None:
        if self._model is None:
            raise ValueError("XGBoostResponseModel must be fitted before retrieving importances.")
        importances = getattr(self._model, "feature_importances_", None)
        if importances is None:
            return None
        return dict(zip(feature_columns, importances.tolist()))

    @property
    def fitted_model(self) -> Any:
        return self._model
```

- [ ] **Step 4: Run all new agentic tests**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v -k "lightgbm or xgboost"
```

Expected: all 10 new tests PASS.

- [ ] **Step 5: Run the full suite**

```bash
python3 -m pytest -q
```

Expected: same or higher pass count.

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add XGBoostResponseModel adapter behind [ml] optional extra"
```

---

## Self-review

**Spec coverage:**

- `LightGBMResponseModel` implements `ResponseModel` Protocol ✓
- `XGBoostResponseModel` implements `ResponseModel` Protocol ✓
- Both raise `ImportError` with install instructions if dep missing ✓
- Both raise `ValueError` if scored before fit ✓
- Both expose `feature_importances` ✓
- `[ml]` extra declared in `pyproject.toml` ✓
- Tests use monkeypatching — no real installs required in CI ✓

**Placeholder scan:** None found.

**Type consistency:** Both classes have identical public interface: `name: str`, `fit(train, feature_columns, target_column)`, `predict_probability(data, feature_columns) -> np.ndarray`, `feature_importances(feature_columns) -> dict[str, float] | None`, `fitted_model: Any`. Matches `HistGBMResponseModel` exactly.
