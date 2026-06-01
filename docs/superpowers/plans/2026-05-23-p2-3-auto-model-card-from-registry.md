# P2-3: Auto-generate Model Cards from ModelRegistry

> **STATUS: ✅ COMPLETE** — auto-generate model cards from registry — PR #36

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `generate_model_cards_from_registry(registry, output_dir)` function to `modelling/agentic.py` that iterates all records in a `ModelRegistry` and writes a markdown model card for each one that has an artifact path. Also add a `generate_card_for_champion(registry, output_dir, ...)` convenience wrapper. Both are callable after `approve_model` / `promote_champion` as a post-approval hook.

**Architecture:** Two new public functions sit alongside `write_model_card` in `agentic.py`. They read the registry JSON, load feature importances from the persisted artifact when available (via `_optional_feature_importances`), and call `write_model_card` per record. No changes to `ModelTrainingAgent` internals — these are standalone utilities. No new dependencies.

**Tech Stack:** Python 3.9+, joblib (already installed), pytest.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `src/balance_pricing_engine/modelling/agentic.py` | Add `generate_model_cards_from_registry`, `generate_card_for_champion` |
| Modify | `tests/unit/test_modelling_agentic.py` | Tests for both new functions |

---

## Task 1: `generate_model_cards_from_registry`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Understand the existing `write_model_card` signature**

Read the function signature at line ~430 of `src/balance_pricing_engine/modelling/agentic.py`:

```python
def write_model_card(
    record: "ModelRecord",
    output_dir: Path,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
    feature_importances: dict[str, float] | None = None,
) -> Path:
```

And `ModelRecord` fields (lines ~69–88):
```python
@dataclass(frozen=True)
class ModelRecord:
    model_name: str
    model_type: str
    version: str
    trained_at_utc: str
    config_hash: str
    metrics: dict[str, float]
    calibration_mae: float
    artifact_path: str | None
    governance_status: str
    champion_status: str
    feature_columns: list[str] | None = None
    target_column: str | None = None
    run_name: str | None = None
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from balance_pricing_engine.modelling.agentic import (
    ModelRecord,
    ModelRegistry,
    generate_card_for_champion,
    generate_model_cards_from_registry,
)


def _make_record(model_name: str = "logistic", artifact_path: str | None = None) -> ModelRecord:
    return ModelRecord(
        model_name=model_name,
        model_type="LogisticResponseModel",
        version="v1",
        trained_at_utc="2026-05-23T00:00:00+00:00",
        config_hash="abc123",
        metrics={"auc": 0.71, "log_loss": 0.64, "brier_score": 0.22},
        calibration_mae=0.03,
        artifact_path=artifact_path,
        governance_status="trained",
        champion_status="challenger",
        feature_columns=["offered_rate", "competitor_rate"],
        target_column="accepted",
    )


def test_generate_model_cards_from_registry_writes_one_card_per_record(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    registry.append(_make_record("logistic"))
    registry.append(_make_record("histgbm"))

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")

    assert len(cards) == 2
    assert all(p.exists() for p in cards)


def test_generate_model_cards_from_registry_skips_no_records(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")

    assert cards == []


def test_generate_model_cards_from_registry_card_contains_model_name(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    registry.append(_make_record("logistic"))

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")
    content = cards[0].read_text()

    assert "logistic" in content
    assert "LogisticResponseModel" in content


def test_generate_card_for_champion_returns_none_when_no_champion(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    registry.append(_make_record("logistic"))

    result = generate_card_for_champion(registry, tmp_path / "cards")

    assert result is None


def test_generate_card_for_champion_writes_card_for_champion(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    record = _make_record("logistic")
    registry.append(record)
    registry.approve_model("logistic", approver="test-user")
    registry.promote_champion("logistic", approver="test-user")

    card_path = generate_card_for_champion(registry, tmp_path / "cards")

    assert card_path is not None
    assert card_path.exists()
    content = card_path.read_text()
    assert "champion" in content.lower()
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_generate_model_cards_from_registry_writes_one_card_per_record -v
```

Expected: `ImportError` — `generate_model_cards_from_registry` not defined.

- [ ] **Step 4: Implement the two functions in `agentic.py`**

Add the following two functions immediately after `write_model_card` (around line 493):

```python
def generate_model_cards_from_registry(
    registry: "ModelRegistry",
    output_dir: Path | str,
) -> list[Path]:
    """Write a model card for every record in the registry. Returns list of written paths."""
    output_dir = Path(output_dir)
    records = registry.load().get("models", [])
    paths: list[Path] = []
    for raw in records:
        record = ModelRecord(**{k: raw[k] for k in ModelRecord.__dataclass_fields__ if k in raw})
        importances: dict[str, float] | None = None
        if record.artifact_path:
            try:
                from balance_pricing_engine.modelling.persistence import load_model

                model = load_model(record.artifact_path)
                feature_cols = record.feature_columns or []
                importances = _optional_feature_importances(model, feature_cols)
            except Exception:
                importances = None
        card_path = write_model_card(
            record=record,
            output_dir=output_dir,
            feature_columns=record.feature_columns,
            target_column=record.target_column,
            feature_importances=importances,
        )
        paths.append(card_path)
    return paths


def generate_card_for_champion(
    registry: "ModelRegistry",
    output_dir: Path | str,
    *,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
    config_hash: str | None = None,
    run_name: str | None = None,
) -> Path | None:
    """Write a model card for the current champion, or return None if none exists."""
    champion = registry.get_champion(
        feature_columns=feature_columns,
        target_column=target_column,
        config_hash=config_hash,
        run_name=run_name,
    )
    if champion is None:
        return None
    record = ModelRecord(
        **{k: champion[k] for k in ModelRecord.__dataclass_fields__ if k in champion}
    )
    importances: dict[str, float] | None = None
    if record.artifact_path:
        try:
            from balance_pricing_engine.modelling.persistence import load_model

            model = load_model(record.artifact_path)
            feature_cols = record.feature_columns or []
            importances = _optional_feature_importances(model, feature_cols)
        except Exception:
            importances = None
    return write_model_card(
        record=record,
        output_dir=Path(output_dir),
        feature_columns=record.feature_columns,
        target_column=record.target_column,
        feature_importances=importances,
    )
```

Also add `generate_model_cards_from_registry` and `generate_card_for_champion` to the module-level `__all__` if one exists, or add at the top of the file:

```python
__all__ = [
    "ElasticityBaselineModel",
    "ElasticityResponseModel",
    "LogisticResponseModel",
    "HistGBMResponseModel",
    "ModelTrainingAgent",
    "ModelScoringAgent",
    "ModelRegistry",
    "ModelRecord",
    "TrainingConfig",
    "TrainingResult",
    "write_model_card",
    "generate_model_cards_from_registry",
    "generate_card_for_champion",
]
```

- [ ] **Step 5: Run the tests**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v -k "generate"
```

Expected: all 5 new tests PASS.

- [ ] **Step 6: Run the full suite to check for regressions**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add generate_model_cards_from_registry and generate_card_for_champion"
```

---

## Self-review

**Spec coverage:**
- `generate_model_cards_from_registry` iterates all registry records and writes a card per record ✓
- `generate_card_for_champion` is the convenience post-approval hook ✓
- Both handle the no-records / no-champion case gracefully ✓
- Feature importances loaded from artifact when available; silently skipped on failure ✓

**Placeholder scan:** None found.

**Type consistency:** Both functions accept `ModelRegistry` and `Path | str`. Return types are `list[Path]` and `Path | None`. `ModelRecord.__dataclass_fields__` iteration used consistently in both.
