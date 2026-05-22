# Plan B: Tier 2 — HistGBM Challenger in Governed Workflow

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote `HistGradientBoosting` to a first-class governed challenger by adding `HistGBMResponseModel` to `agentic.py` (parallel to `LogisticResponseModel`), exposing feature importances in model cards, and adding it as a third candidate in `train_response_models.py`.

**Architecture:** One new class in `modelling/agentic.py` following the exact pattern of `LogisticResponseModel`. `write_model_card` gains a `feature_importances` optional parameter. The existing `build_hist_gradient_boosting_model` in `models.py` is unchanged. No new dependencies — `sklearn` is already installed. This plan is independent of Plan A but works best when Plan A's `MLResponseFnAdapter` is available for end-use.

**Note on P1 gap status:** All P1 gaps identified in the 2026-05-18 review (`excess_deposit_income` in `SENSITIVITY_RESULT_COLUMNS`, multi-seed column drift, boundary expansion test, CLI validate flags, rate response range) have been closed in PRs #27–#29. Plan B covers the highest-value remaining P2 item (Tier 2 model support) that was not addressed in `2026-05-20-p2-ml-diagnostics-and-dashboard.md`.

**Tech Stack:** Python 3.9+, scikit-learn `HistGradientBoostingClassifier` (already in `models.py`), pytest.

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Modify | `src/balance_pricing_engine/modelling/agentic.py` | Add `HistGBMResponseModel`; update `write_model_card` for feature importances |
| Modify | `src/balance_pricing_engine/modelling/models.py` | Add `get_feature_importances` helper |
| Modify | `examples/train_response_models.py` | Add `HistGBMResponseModel` as third candidate |
| Modify | `tests/unit/test_modelling_agentic.py` | Tests for new model class and updated model card |

---

## Task 1: `HistGBMResponseModel` class

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from balance_pricing_engine.modelling.agentic import HistGBMResponseModel


def test_histgbm_response_model_fits_and_predicts(tmp_path):
    dataset = _loan_dataset()
    model = HistGBMResponseModel(random_state=42)
    train = dataset.iloc[:800]
    test_data = dataset.iloc[800:]

    model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
    probs = model.predict_probability(test_data, LOAN_FEATURE_COLUMNS)

    assert probs.shape == (len(test_data),)
    assert np.all(probs >= 0) and np.all(probs <= 1)


def test_histgbm_response_model_raises_before_fit():
    model = HistGBMResponseModel(random_state=42)
    df = pd.DataFrame({"offered_rate": [0.08]})
    with pytest.raises(ValueError, match="fitted"):
        model.predict_probability(df, ["offered_rate"])


def test_histgbm_response_model_name():
    assert HistGBMResponseModel.name == "HistGBMResponseModel"


def test_histgbm_response_model_feature_importances_available_after_fit():
    dataset = _loan_dataset()
    model = HistGBMResponseModel(random_state=42)
    model.fit(dataset.iloc[:800], LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
    importances = model.feature_importances(LOAN_FEATURE_COLUMNS)
    assert isinstance(importances, dict)
    assert set(importances.keys()) == set(LOAN_FEATURE_COLUMNS)
    assert all(isinstance(v, float) for v in importances.values())


def test_histgbm_response_model_in_training_agent(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    result = agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=7),
            "histgbm": HistGBMResponseModel(random_state=7),
        },
        registry=agent.registry,
        run_name="loan_three_candidates",
    )
    model_names = {c["model_name"] for c in result.report["candidates"]}
    assert "HistGBMResponseModel" in model_names
    assert len(result.report["candidates"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_histgbm_response_model_fits_and_predicts -v
```

Expected: `ImportError: cannot import name 'HistGBMResponseModel'`

- [ ] **Step 3: Add `HistGBMResponseModel` to `agentic.py`**

In `src/balance_pricing_engine/modelling/agentic.py`, add the following class after `LogisticResponseModel` (after line ~138):

```python
class HistGBMResponseModel:
    """Governed HistGradientBoosting response model backed by sklearn."""

    name = "HistGBMResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        self._model = fit_response_model(
            build_hist_gradient_boosting_model(self._random_state),
            train,
            feature_columns,
            target_column,
        )

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("Model must be fitted before scoring.")
        return predict_response_probability(self._model, data, feature_columns)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float]:
        """Return per-feature importance scores. Requires a fitted model."""
        if self._model is None:
            raise ValueError("Model must be fitted before retrieving feature importances.")
        importances = getattr(self._model, "feature_importances_", None)
        if importances is None:
            return {col: float("nan") for col in feature_columns}
        return dict(zip(feature_columns, importances.tolist()))

    @property
    def fitted_model(self) -> Any:
        return self._model
```

Also add `build_hist_gradient_boosting_model` to the existing import from `models.py` near the top of `agentic.py`:

Old:
```python
from balance_pricing_engine.modelling.models import (
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)
```

New:
```python
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v
```

Expected: all tests PASS (including the 5 new HistGBM tests).

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add HistGBMResponseModel to governed agentic workflow"
```

---

## Task 2: Feature importances in model cards

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py:178-222` (`write_model_card`)
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from balance_pricing_engine.modelling.agentic import write_model_card


def test_write_model_card_includes_feature_importances(tmp_path):
    record = ModelRecord(
        model_name="HistGBMResponseModel",
        model_type="HistGBMResponseModel",
        version="20260521000000000000",
        trained_at_utc="2026-05-21T00:00:00+00:00",
        artifact_path=None,
        metrics={"auc": 0.72, "log_loss": 0.64, "brier_score": 0.21},
        calibration_mae=0.04,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="abc123",
    )
    importances = {"offered_rate": 0.45, "credit_score": 0.30, "loan_amount": 0.25}

    card_path = write_model_card(
        record=record,
        output_dir=tmp_path,
        feature_columns=list(importances.keys()),
        target_column="accepted",
        feature_importances=importances,
    )

    content = card_path.read_text()
    assert "## Feature Importances" in content
    assert "offered_rate" in content
    assert "0.4500" in content


def test_write_model_card_omits_importances_section_when_none(tmp_path):
    record = ModelRecord(
        model_name="LogisticResponseModel",
        model_type="LogisticResponseModel",
        version="20260521000000000001",
        trained_at_utc="2026-05-21T00:00:00+00:00",
        artifact_path=None,
        metrics={"auc": 0.70, "log_loss": 0.66, "brier_score": 0.22},
        calibration_mae=0.05,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="def456",
    )
    card_path = write_model_card(
        record=record,
        output_dir=tmp_path,
        feature_columns=["offered_rate"],
        target_column="accepted",
    )
    content = card_path.read_text()
    assert "## Feature Importances" not in content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_write_model_card_includes_feature_importances -v
```

Expected: `TypeError: write_model_card() got an unexpected keyword argument 'feature_importances'`

- [ ] **Step 3: Update `write_model_card` in `agentic.py`**

Update the function signature:

Old:
```python
def write_model_card(
    record: "ModelRecord",
    output_dir: Path,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
) -> Path:
```

New:
```python
def write_model_card(
    record: "ModelRecord",
    output_dir: Path,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
    feature_importances: dict[str, float] | None = None,
) -> Path:
```

Add the importances section to the card content string, just before `## Governance`. Find the line where `content` is built and insert:

Old (excerpt near end of content string):
```python
        f"## Governance\n"
```

New:
```python
        + (
            "## Feature Importances\n"
            "| Feature | Importance |\n"
            "|---------|------------|\n"
            + "".join(
                f"| {feat} | {imp:.4f} |\n"
                for feat, imp in sorted(
                    feature_importances.items(), key=lambda x: x[1], reverse=True
                )
            )
            + "\n"
            if feature_importances
            else ""
        )
        + f"## Governance\n"
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add optional feature_importances section to model cards"
```

---

## Task 3: Wire importances into `ModelTrainingAgent.train()`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py:425-437` (model card generation block in `train`)

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_modelling_agentic.py`:

```python
def test_training_agent_writes_model_card_with_importances_for_histgbm(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "histgbm": HistGBMResponseModel(random_state=7),
        },
        registry=agent.registry,
        run_name="loan_importances",
    )
    cards_dir = tmp_path / "model_cards"
    card_files = list(cards_dir.glob("HistGBMResponseModel_*.md"))
    assert len(card_files) == 1
    content = card_files[0].read_text()
    assert "## Feature Importances" in content
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_training_agent_writes_model_card_with_importances_for_histgbm -v
```

Expected: FAIL — card exists but has no `## Feature Importances` section.

- [ ] **Step 3: Update the model card generation block in `train()`**

In `ModelTrainingAgent.train()`, find the model card writing loop (around line 428):

Old:
```python
        for record in records:
            write_model_card(
                replace(
                    record,
                    champion_status=self._champion_status(record.model_name, recommendation),
                ),
                self.artifacts_dir / "model_cards",
                card_feature_columns,
                card_target_column,
            )
```

New:
```python
        fitted_candidates: dict[str, Any] = dict(zip(candidates.keys(), candidates.values()))
        for record in records:
            model_key = next(
                (k for k, v in fitted_candidates.items() if _model_name(v) == record.model_name),
                None,
            )
            fitted_model = fitted_candidates.get(model_key) if model_key else None
            importances = (
                fitted_model.feature_importances(card_feature_columns)
                if fitted_model is not None and hasattr(fitted_model, "feature_importances")
                else None
            )
            write_model_card(
                replace(
                    record,
                    champion_status=self._champion_status(record.model_name, recommendation),
                ),
                self.artifacts_dir / "model_cards",
                card_feature_columns,
                card_target_column,
                feature_importances=importances,
            )
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: wire feature importances from HistGBMResponseModel into auto-generated model cards"
```

---

## Task 4: Update `train_response_models.py` example

**Files:**
- Modify: `examples/train_response_models.py`

- [ ] **Step 1: Add `HistGBMResponseModel` as a third candidate**

In `examples/train_response_models.py`, find the imports block and add:

```python
from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    HistGBMResponseModel,
    LogisticResponseModel,
    ModelRegistry,
    ModelTrainingAgent,
    TrainingConfig,
)
```

Find the `candidates` dict (wherever `ElasticityBaselineModel` and `LogisticResponseModel` are passed in) and add the third candidate:

Old pattern (wherever `candidates` is constructed):
```python
candidates={
    "elasticity_baseline": ElasticityBaselineModel(),
    "logistic": LogisticResponseModel(random_state=config.random_seed),
},
```

New:
```python
candidates={
    "elasticity_baseline": ElasticityBaselineModel(),
    "logistic": LogisticResponseModel(random_state=config.random_seed),
    "histgbm": HistGBMResponseModel(random_state=config.random_seed),
},
```

- [ ] **Step 2: Run the example to verify it executes without error**

```bash
python3 examples/train_response_models.py
```

Expected: prints training results for all three candidates including a `HistGBMResponseModel` row, no exceptions.

- [ ] **Step 3: Run full test suite**

```bash
python3 -m pytest -q
python3 -m ruff check .
```

Expected: all tests PASS, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add examples/train_response_models.py
git commit -m "feat: add HistGBMResponseModel as third candidate in train_response_models example"
```

---

## Task 5: Open PR

- [ ] **Step 1: Push and open PR**

```bash
git push -u origin HEAD
gh pr create \
  --title "feat: Tier 2 HistGBM challenger in governed workflow with feature importances" \
  --body "$(cat <<'EOF'
## Summary
- Adds `HistGBMResponseModel` to `agentic.py` (parallel to `LogisticResponseModel`), backed by `build_hist_gradient_boosting_model` from `models.py`
- `write_model_card` gains optional `feature_importances` parameter; `ModelTrainingAgent.train()` auto-populates this from any model implementing `feature_importances()`
- `train_response_models.py` now trains three candidates: elasticity baseline, logistic, and HistGBM

## Test plan
- [ ] `test_histgbm_response_model_fits_and_predicts` — shape, range, raises-before-fit
- [ ] `test_histgbm_response_model_in_training_agent` — three-candidate training run
- [ ] `test_write_model_card_includes_feature_importances` — card section present
- [ ] `test_training_agent_writes_model_card_with_importances_for_histgbm` — auto-wired
- [ ] All prior tests unchanged

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**
- `HistGBMResponseModel` (fit, predict, name, feature_importances) → Task 1 ✓
- `write_model_card` importances section → Task 2 ✓
- `train()` auto-wires importances → Task 3 ✓
- Example updated → Task 4 ✓

**Placeholder scan:** None found.

**Type consistency:** `feature_importances` is `dict[str, float] | None` throughout. `HistGBMResponseModel.feature_importances()` always returns `dict[str, float]` (uses `nan` when pre-fit somehow reached — guarded by the early raise). `train()` passes `None` for models without the method.
