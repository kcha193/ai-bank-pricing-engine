# Plan A: Pluggable Response Interface + Champion/Challenger Wiring

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a governed, human-approved trained response model (e.g. `LogisticResponseModel`) to replace the synthetic ground-truth functions inside the optimizer, with deterministic fallback to the existing synthetic functions when no model is approved.

**Architecture:** A new `optimization/response_fn.py` module defines a `ResponseFn` Protocol and two adapters — `SyntheticLoanResponseFn`/`SyntheticDepositResponseFn` (wrapping existing synthetic functions) and `MLResponseFnAdapter` (wrapping any fitted `ResponseModel`). `evaluate_joint_rate_grid` gains two optional `*_response_fn` parameters (default `None` → synthetic path unchanged). `ModelRegistry` gains `approve_model`, `promote_champion`, and `get_champion`. `ModelScoringAgent` gains `resolve_response_fn` which returns an `MLResponseFnAdapter` for the approved champion or a fallback.

**Tech Stack:** Python 3.9+, numpy, pandas, scikit-learn (already installed), pytest. No new dependencies.

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `src/balance_pricing_engine/optimization/response_fn.py` | `ResponseFn` Protocol, synthetic adapters, `MLResponseFnAdapter`, `GovernanceError`, `require_approved` |
| Modify | `src/balance_pricing_engine/optimization/grid_search.py` | Add optional `loan_response_fn`, `deposit_response_fn` to `evaluate_joint_rate_grid` and `optimize_joint_rates` |
| Modify | `src/balance_pricing_engine/modelling/agentic.py` | Add `approve_model`, `promote_champion`, `get_champion` to `ModelRegistry`; add `resolve_response_fn` to `ModelScoringAgent` |
| Create | `tests/unit/test_optimization_response_fn.py` | Tests for Protocol, adapters, governance gate |
| Modify | `tests/unit/test_optimizer.py` | Test `evaluate_joint_rate_grid` accepts custom response_fn |
| Modify | `tests/unit/test_modelling_agentic.py` | Test new `ModelRegistry` and `ModelScoringAgent` methods |

---

## Task 1: `response_fn.py` — Protocol and synthetic adapters

**Files:**
- Create: `src/balance_pricing_engine/optimization/response_fn.py`
- Create: `tests/unit/test_optimization_response_fn.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_optimization_response_fn.py`:

```python
import numpy as np
import pandas as pd
import pytest

from balance_pricing_engine.optimization.response_fn import (
    GovernanceError,
    MLResponseFnAdapter,
    SyntheticDepositResponseFn,
    SyntheticLoanResponseFn,
    require_approved,
)


def _loan_customers() -> pd.DataFrame:
    return pd.DataFrame({
        "loan_amount": [500_000.0, 600_000.0],
        "credit_score": [700.0, 750.0],
        "lvr": [0.75, 0.80],
        "income": [120_000.0, 150_000.0],
        "risk_score": [0.2, 0.4],
        "expected_loss_rate": [0.01, 0.015],
    })


def _deposit_customers() -> pd.DataFrame:
    return pd.DataFrame({
        "deposit_amount": [50_000.0, 100_000.0],
        "relationship_years": [3.0, 7.0],
        "digital_engagement": [0.8, 0.6],
        "rate_sensitivity": [1.2, 0.9],
    })


def test_synthetic_loan_response_fn_returns_probabilities_in_range():
    fn = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    probs = fn(_loan_customers(), offered_rate=0.08, competitor_rate=0.078)
    assert probs.shape == (2,)
    assert np.all(probs >= 0) and np.all(probs <= 1)


def test_synthetic_loan_response_fn_higher_rate_lowers_acceptance():
    fn = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    customers = _loan_customers()
    probs_low = fn(customers, offered_rate=0.07, competitor_rate=0.075)
    probs_high = fn(customers, offered_rate=0.09, competitor_rate=0.075)
    assert np.all(probs_low > probs_high)


def test_synthetic_deposit_response_fn_higher_rate_raises_uptake():
    fn = SyntheticDepositResponseFn(elasticity=80.0, competitor_gap_elasticity=60.0)
    customers = _deposit_customers()
    probs_low = fn(customers, offered_rate=0.04, competitor_rate=0.045)
    probs_high = fn(customers, offered_rate=0.06, competitor_rate=0.045)
    assert np.all(probs_high > probs_low)


def test_ml_response_fn_adapter_injects_rate_columns():
    captured = {}

    class FakeModel:
        def predict_probability(self, df, feature_columns):
            captured["offered_rate"] = df["offered_rate"].iloc[0]
            captured["competitor_rate"] = df["competitor_rate"].iloc[0]
            captured["rate_gap_to_competitor"] = df["rate_gap_to_competitor"].iloc[0]
            return np.full(len(df), 0.5)

    adapter = MLResponseFnAdapter(model=FakeModel(), feature_columns=["offered_rate"])
    probs = adapter(_loan_customers(), offered_rate=0.08, competitor_rate=0.075)
    assert np.allclose(probs, 0.5)
    assert abs(captured["offered_rate"] - 0.08) < 1e-9
    assert abs(captured["rate_gap_to_competitor"] - 0.005) < 1e-9


def test_ml_response_fn_adapter_adds_risk_segment_when_missing():
    class FakeModel:
        def predict_probability(self, df, feature_columns):
            assert "risk_segment" in df.columns
            return np.zeros(len(df))

    adapter = MLResponseFnAdapter(
        model=FakeModel(),
        feature_columns=["offered_rate", "risk_segment"],
    )
    adapter(_loan_customers(), offered_rate=0.08, competitor_rate=0.075)


def test_ml_response_fn_adapter_adds_wealth_segment_when_missing():
    class FakeModel:
        def predict_probability(self, df, feature_columns):
            assert "wealth_segment" in df.columns
            return np.zeros(len(df))

    adapter = MLResponseFnAdapter(
        model=FakeModel(),
        feature_columns=["offered_rate", "wealth_segment"],
    )
    adapter(_deposit_customers(), offered_rate=0.05, competitor_rate=0.045)


def test_require_approved_raises_governance_error_when_not_approved():
    with pytest.raises(GovernanceError, match="pending_human_approval"):
        require_approved("pending_human_approval", "MyModel")


def test_require_approved_does_not_raise_when_approved():
    require_approved("approved", "MyModel")  # no exception
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_optimization_response_fn.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError` or `ImportError` — `response_fn` doesn't exist yet.

- [ ] **Step 3: Create `optimization/response_fn.py`**

```python
"""Response-function protocols and adapters for the optimizer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pandas as pd

from balance_pricing_engine.config import (
    DepositResponseCoefficientsConfig,
    LoanResponseCoefficientsConfig,
)
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


class ResponseFn(Protocol):
    """Callable interface the optimizer uses to compute response probabilities."""

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray: ...


class GovernanceError(ValueError):
    """Raised when a response function is used without human approval."""


def require_approved(governance_status: str, model_name: str) -> None:
    """Raise GovernanceError if governance_status is not 'approved'."""
    if governance_status != "approved":
        raise GovernanceError(
            f"Model '{model_name}' has governance_status='{governance_status}'; "
            "call registry.approve_model() before using this model in the optimizer."
        )


@dataclass(frozen=True)
class SyntheticLoanResponseFn:
    """Wraps the deterministic synthetic loan response function."""

    elasticity: float
    competitor_gap_elasticity: float
    coefficients: LoanResponseCoefficientsConfig | None = None

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray:
        return loan_acceptance_probability(
            customers,
            offered_rate,
            competitor_rate,
            self.elasticity,
            self.competitor_gap_elasticity,
            coefficients=self.coefficients,
        )


@dataclass(frozen=True)
class SyntheticDepositResponseFn:
    """Wraps the deterministic synthetic deposit response function."""

    elasticity: float
    competitor_gap_elasticity: float
    coefficients: DepositResponseCoefficientsConfig | None = None

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray:
        return deposit_uptake_probability(
            customers,
            offered_rate,
            competitor_rate,
            self.elasticity,
            self.competitor_gap_elasticity,
            coefficients=self.coefficients,
        )


@dataclass
class MLResponseFnAdapter:
    """Wraps a fitted ResponseModel for use inside the optimizer grid search."""

    model: Any  # duck-typed: must have predict_probability(df, feature_columns) -> np.ndarray
    feature_columns: list[str]

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray:
        df = customers.copy()
        df["offered_rate"] = offered_rate
        df["competitor_rate"] = competitor_rate
        df["rate_gap_to_competitor"] = offered_rate - competitor_rate
        if "risk_segment" in self.feature_columns and "risk_segment" not in df.columns:
            df["risk_segment"] = pd.cut(
                df["risk_score"],
                bins=[-np.inf, 0.33, 0.66, np.inf],
                labels=[0, 1, 2],
            ).astype(int)
        if "wealth_segment" in self.feature_columns and "wealth_segment" not in df.columns:
            df["wealth_segment"] = pd.cut(
                df["deposit_amount"],
                bins=[-np.inf, 50_000, 150_000, np.inf],
                labels=[0, 1, 2],
            ).astype(int)
        return self.model.predict_probability(df, self.feature_columns)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_optimization_response_fn.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/optimization/response_fn.py tests/unit/test_optimization_response_fn.py
git commit -m "feat: add ResponseFn protocol and optimizer response-function adapters"
```

---

## Task 2: Wire response_fn params into `evaluate_joint_rate_grid`

**Files:**
- Modify: `src/balance_pricing_engine/optimization/grid_search.py:98-115` (function signature)
- Modify: `src/balance_pricing_engine/optimization/grid_search.py:121-157` (deposit loop)
- Modify: `src/balance_pricing_engine/optimization/grid_search.py:149-170` (loan loop)
- Modify: `src/balance_pricing_engine/optimization/grid_search.py:273-310` (`optimize_joint_rates`)
- Test: `tests/unit/test_optimizer.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_optimizer.py`:

```python
from balance_pricing_engine.optimization.response_fn import (
    MLResponseFnAdapter,
    SyntheticLoanResponseFn,
    SyntheticDepositResponseFn,
)


def _minimal_customers_and_market():
    from balance_pricing_engine.synthetic.customers import (
        generate_deposit_customers, generate_loan_customers,
    )
    from balance_pricing_engine.synthetic.market import MarketState
    from balance_pricing_engine.utils.random import make_rng
    rng = make_rng(99)
    loan_customers = generate_loan_customers(200, rng)
    deposit_customers = generate_deposit_customers(200, rng)
    market = MarketState(
        cash_rate=0.055,
        wholesale_funding_spread=0.012,
        competitor_loan_rate=0.079,
        competitor_deposit_rate=0.048,
    )
    return loan_customers, deposit_customers, market


def test_evaluate_joint_rate_grid_accepts_synthetic_response_fn():
    loan_customers, deposit_customers, market = _minimal_customers_and_market()
    loan_fn = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    deposit_fn = SyntheticDepositResponseFn(elasticity=80.0, competitor_gap_elasticity=60.0)

    result = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081]),
        deposit_rate_grid=np.array([0.047, 0.049]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_fn,
    )
    assert set(result.columns) == set(GRID_RESULT_COLUMNS)
    assert len(result) == 4  # 2 loan rates × 2 deposit rates


def test_evaluate_joint_rate_grid_custom_fn_overrides_volume():
    """A response fn that always returns 0.5 probability produces deterministic volume."""
    loan_customers, deposit_customers, market = _minimal_customers_and_market()

    class FixedProbFn:
        def __call__(self, customers, offered_rate, competitor_rate):
            return np.full(len(customers), 0.5)

    result_custom = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081]),
        deposit_rate_grid=np.array([0.047]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=FixedProbFn(),
        deposit_response_fn=FixedProbFn(),
    )
    # Both loan rates produce identical expected_loan_volume when prob is constant
    assert abs(
        result_custom.iloc[0]["expected_loan_volume"]
        - result_custom.iloc[1]["expected_loan_volume"]
    ) < 1.0


def test_optimize_joint_rates_accepts_response_fn():
    loan_customers, deposit_customers, market = _minimal_customers_and_market()
    loan_fn = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    deposit_fn = SyntheticDepositResponseFn(elasticity=80.0, competitor_gap_elasticity=60.0)

    result = optimize_joint_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081]),
        deposit_rate_grid=np.array([0.047, 0.049]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_fn,
    )
    assert result.loan_rate in [0.079, 0.081]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_optimizer.py::test_evaluate_joint_rate_grid_accepts_synthetic_response_fn -v
```

Expected: `TypeError: evaluate_joint_rate_grid() got an unexpected keyword argument 'loan_response_fn'`

- [ ] **Step 3: Add params to `evaluate_joint_rate_grid`**

In `src/balance_pricing_engine/optimization/grid_search.py`, update the function signature and both inner loops. The import at the top of the file needs the `ResponseFn` type:

Add import near the top of `grid_search.py` (after the existing imports):

```python
from balance_pricing_engine.optimization.response_fn import ResponseFn
```

Update function signature (add two new parameters before the closing `)`):

Old signature ending:
```python
    response_coefficients: ResponseCoefficientsConfig | None = None,
) -> pd.DataFrame:
```

New:
```python
    response_coefficients: ResponseCoefficientsConfig | None = None,
    loan_response_fn: ResponseFn | None = None,
    deposit_response_fn: ResponseFn | None = None,
) -> pd.DataFrame:
```

Update the deposit pre-compute loop (replace the `deposit_probs = deposit_uptake_probability(...)` call):

Old:
```python
        deposit_probs = deposit_uptake_probability(
            deposit_customers,
            float(deposit_rate),
            market.competitor_deposit_rate,
            deposit_rate_elasticity,
            deposit_competitor_gap_elasticity,
            coefficients=response_coefficients.deposit,
        )
```

New:
```python
        if deposit_response_fn is not None:
            deposit_probs = deposit_response_fn(
                deposit_customers,
                float(deposit_rate),
                market.competitor_deposit_rate,
            )
        else:
            deposit_probs = deposit_uptake_probability(
                deposit_customers,
                float(deposit_rate),
                market.competitor_deposit_rate,
                deposit_rate_elasticity,
                deposit_competitor_gap_elasticity,
                coefficients=response_coefficients.deposit,
            )
```

Update the loan loop (replace the `loan_probs = loan_acceptance_probability(...)` call):

Old:
```python
        loan_probs = loan_acceptance_probability(
            loan_customers,
            float(loan_rate),
            market.competitor_loan_rate,
            loan_rate_elasticity,
            loan_competitor_gap_elasticity,
            coefficients=response_coefficients.loan,
        )
```

New:
```python
        if loan_response_fn is not None:
            loan_probs = loan_response_fn(
                loan_customers,
                float(loan_rate),
                market.competitor_loan_rate,
            )
        else:
            loan_probs = loan_acceptance_probability(
                loan_customers,
                float(loan_rate),
                market.competitor_loan_rate,
                loan_rate_elasticity,
                loan_competitor_gap_elasticity,
                coefficients=response_coefficients.loan,
            )
```

Update `optimize_joint_rates` signature (add two new parameters) and its call to `evaluate_joint_rate_grid`:

New signature additions (after `response_coefficients`):
```python
    loan_response_fn: ResponseFn | None = None,
    deposit_response_fn: ResponseFn | None = None,
) -> OptimizationResult:
```

New call to `evaluate_joint_rate_grid` (add two kwargs at the end):
```python
        loan_response_fn=loan_response_fn,
        deposit_response_fn=deposit_response_fn,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_optimizer.py -v
```

Expected: all optimizer tests PASS (including the 3 new ones).

- [ ] **Step 5: Verify full suite still passes**

```bash
python3 -m pytest -q
```

Expected: all existing tests PASS (new params default to None → backward-compatible).

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/optimization/grid_search.py tests/unit/test_optimizer.py
git commit -m "feat: add optional response_fn params to evaluate_joint_rate_grid and optimize_joint_rates"
```

---

## Task 3: `ModelRegistry.approve_model`, `promote_champion`, `get_champion`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py:145-175` (`ModelRegistry` class)
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
def test_registry_approve_model_sets_governance_status(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "governance_status": "pending_human_approval",
    })

    registry.approve_model("LogisticResponseModel", approver="analyst@bank.nz")

    record = registry.latest_for_model("LogisticResponseModel")
    assert record["governance_status"] == "approved"
    assert record["approved_by"] == "analyst@bank.nz"
    assert "approved_at_utc" in record


def test_registry_approve_model_raises_when_not_found(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    with pytest.raises(ValueError, match="No registry record found"):
        registry.approve_model("UnknownModel", approver="analyst@bank.nz")


def test_registry_promote_champion_sets_champion_and_retires_previous(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "ElasticityBaselineModel",
        "governance_status": "approved",
        "champion_status": "champion",
    })
    registry.register({
        "model_name": "LogisticResponseModel",
        "governance_status": "pending_human_approval",
        "champion_status": "challenger",
    })

    registry.promote_champion("LogisticResponseModel", approver="analyst@bank.nz")

    logistic = registry.latest_for_model("LogisticResponseModel")
    elasticity = registry.latest_for_model("ElasticityBaselineModel")
    assert logistic["champion_status"] == "champion"
    assert logistic["governance_status"] == "approved"
    assert elasticity["champion_status"] == "retired_champion"


def test_registry_get_champion_returns_latest_champion(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "ElasticityBaselineModel", "champion_status": "champion"})
    registry.register({"model_name": "LogisticResponseModel", "champion_status": "challenger"})

    champion = registry.get_champion()
    assert champion["model_name"] == "ElasticityBaselineModel"


def test_registry_get_champion_returns_none_when_no_champion(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "LogisticResponseModel", "champion_status": "challenger"})
    assert registry.get_champion() is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_registry_approve_model_sets_governance_status -v
```

Expected: `AttributeError: 'ModelRegistry' object has no attribute 'approve_model'`

- [ ] **Step 3: Add methods to `ModelRegistry` in `agentic.py`**

Add after the existing `latest_for_model` method (around line 175):

```python
    def get_champion(self) -> dict[str, Any] | None:
        """Return the latest record with champion_status='champion', or None."""
        records = self.load().get("models", [])
        return next(
            (r for r in reversed(records) if r.get("champion_status") == "champion"),
            None,
        )

    def approve_model(self, model_name: str, approver: str) -> None:
        """Set governance_status='approved' on the latest record for model_name."""
        payload = self.load()
        for record in reversed(payload.get("models", [])):
            if record.get("model_name") == model_name:
                record["governance_status"] = "approved"
                record["approved_by"] = approver
                record["approved_at_utc"] = datetime.now(timezone.utc).isoformat()
                self.registry_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
                )
                return
        raise ValueError(f"No registry record found for model_name='{model_name}'")

    def promote_champion(self, model_name: str, approver: str) -> None:
        """Approve model_name and promote it to champion, retiring the previous champion."""
        self.approve_model(model_name, approver)
        payload = self.load()
        for record in payload.get("models", []):
            if record.get("model_name") == model_name:
                record["champion_status"] = "champion"
            elif record.get("champion_status") == "champion":
                record["champion_status"] = "retired_champion"
        self.registry_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add approve_model, promote_champion, get_champion to ModelRegistry"
```

---

## Task 4: `ModelScoringAgent.resolve_response_fn`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py:552-650` (`ModelScoringAgent` class)
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from balance_pricing_engine.optimization.response_fn import (
    MLResponseFnAdapter,
    SyntheticLoanResponseFn,
)


def test_scoring_agent_resolve_response_fn_returns_fallback_when_no_champion(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "LogisticResponseModel", "champion_status": "challenger"})
    agent = ModelScoringAgent(registry_path=tmp_path / "registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)

    fn = agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)
    assert fn is fallback


def test_scoring_agent_resolve_response_fn_returns_fallback_when_artifact_missing(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "champion_status": "champion",
        "governance_status": "approved",
        "artifact_path": str(tmp_path / "missing.joblib"),
    })
    agent = ModelScoringAgent(registry_path=tmp_path / "registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)

    fn = agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)
    assert fn is fallback


def test_scoring_agent_resolve_response_fn_returns_ml_adapter_for_approved_champion(tmp_path):
    from balance_pricing_engine.modelling.agentic import (
        ElasticityBaselineModel, ModelTrainingAgent, TrainingConfig,
    )

    dataset = _loan_dataset()
    agent_trainer = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    registry = agent_trainer.registry
    agent_trainer.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=7),
        },
        registry=registry,
        run_name="loan",
    )
    registry.promote_champion("LogisticResponseModel", approver="analyst@bank.nz")

    scoring_agent = ModelScoringAgent(registry_path=tmp_path / "model_registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    fn = scoring_agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)

    assert isinstance(fn, MLResponseFnAdapter)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_scoring_agent_resolve_response_fn_returns_fallback_when_no_champion -v
```

Expected: `AttributeError: 'ModelScoringAgent' object has no attribute 'resolve_response_fn'`

- [ ] **Step 3: Add `resolve_response_fn` to `ModelScoringAgent`**

Add after the existing `_validate_scoring_schema` static method in `ModelScoringAgent` (around line 645):

```python
    def resolve_response_fn(
        self,
        feature_columns: list[str],
        fallback: Any,
    ) -> Any:
        """Return MLResponseFnAdapter for the approved champion, or fallback."""
        from balance_pricing_engine.optimization.response_fn import MLResponseFnAdapter

        champion = self.registry.get_champion()
        if champion is None:
            return fallback
        if champion.get("governance_status") != "approved":
            return fallback
        artifact_path = champion.get("artifact_path")
        if artifact_path is None or not Path(str(artifact_path)).exists():
            return fallback
        model = load_model(str(artifact_path))
        return MLResponseFnAdapter(model=model, feature_columns=feature_columns)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add resolve_response_fn to ModelScoringAgent"
```

---

## Task 5: Integration test — train → approve → use in optimizer

**Files:**
- Modify: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write the integration test**

Add to `tests/unit/test_modelling_agentic.py`:

```python
def test_end_to_end_train_approve_and_use_in_optimizer(tmp_path):
    """
    Full Tier 1 governance flow:
    1. Train logistic challenger on synthetic data.
    2. Approve and promote it to champion in the registry.
    3. Resolve it as a ResponseFn via ModelScoringAgent.
    4. Run evaluate_joint_rate_grid with the resolved fn.
    5. Verify results are valid (same shape as synthetic path).
    """
    from balance_pricing_engine.modelling.agentic import (
        ElasticityBaselineModel, ModelTrainingAgent, TrainingConfig,
    )
    from balance_pricing_engine.optimization.grid_search import evaluate_joint_rate_grid
    from balance_pricing_engine.optimization.response_fn import SyntheticLoanResponseFn
    from balance_pricing_engine.synthetic.customers import (
        generate_deposit_customers, generate_loan_customers,
    )
    from balance_pricing_engine.synthetic.market import MarketState
    from balance_pricing_engine.utils.random import make_rng

    rng = make_rng(42)
    loan_customers = generate_loan_customers(300, rng)
    deposit_customers = generate_deposit_customers(300, rng)
    market = MarketState(
        cash_rate=0.055,
        wholesale_funding_spread=0.012,
        competitor_loan_rate=0.079,
        competitor_deposit_rate=0.048,
    )

    # Step 1: Train
    dataset = _loan_dataset()
    trainer = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=42,
        ),
        artifacts_dir=tmp_path,
    )
    registry = trainer.registry
    trainer.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=42),
        },
        registry=registry,
        run_name="loan_integration",
    )

    # Step 2: Approve and promote
    registry.promote_champion("LogisticResponseModel", approver="integration_test")

    # Step 3: Resolve ResponseFn
    scoring_agent = ModelScoringAgent(registry_path=tmp_path / "model_registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    loan_fn = scoring_agent.resolve_response_fn(
        feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback
    )
    deposit_fn = SyntheticDepositResponseFn = __import__(
        "balance_pricing_engine.optimization.response_fn",
        fromlist=["SyntheticDepositResponseFn"],
    ).SyntheticDepositResponseFn
    deposit_response_fn = deposit_fn(elasticity=80.0, competitor_gap_elasticity=60.0)

    # Step 4: Run optimizer with ML loan fn
    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081, 0.083]),
        deposit_rate_grid=np.array([0.047, 0.049]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_response_fn,
    )

    # Step 5: Validate
    assert len(grid_results) == 6  # 3 loan × 2 deposit
    assert grid_results["expected_loan_volume"].gt(0).all()
    assert grid_results["total_objective"].notna().all()
```

- [ ] **Step 2: Run the integration test**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py::test_end_to_end_train_approve_and_use_in_optimizer -v
```

Expected: PASS.

- [ ] **Step 3: Run full suite**

```bash
python3 -m pytest -q
python3 -m ruff check .
```

Expected: all tests PASS, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_modelling_agentic.py
git commit -m "test: end-to-end integration test for Tier 1 champion promotion and optimizer wiring"
```

---

## Task 6: Open PR

- [ ] **Step 1: Push branch and open PR**

```bash
git push -u origin HEAD
gh pr create \
  --title "feat: pluggable response interface + champion/challenger wiring (Tier 1)" \
  --body "$(cat <<'EOF'
## Summary
- Adds `optimization/response_fn.py` with `ResponseFn` Protocol, synthetic adapters, `MLResponseFnAdapter`, and governance gate
- `evaluate_joint_rate_grid` and `optimize_joint_rates` accept optional `loan_response_fn` / `deposit_response_fn` (backwards-compatible; default None uses existing synthetic path)
- `ModelRegistry` gains `approve_model`, `promote_champion`, `get_champion`
- `ModelScoringAgent` gains `resolve_response_fn` which returns an `MLResponseFnAdapter` for the approved champion or a fallback
- End-to-end integration test: train → approve → use in optimizer

## Test plan
- [ ] All existing tests pass unchanged (new params default to None)
- [ ] New test file `test_optimization_response_fn.py` covers Protocol, adapters, governance gate
- [ ] New registry tests cover approve/promote/get_champion lifecycle
- [ ] Integration test covers full Tier 1 flow

## Governance note
No ML model is wired into the optimizer by default. The synthetic ground-truth path is unchanged unless `resolve_response_fn` is called and an approved champion artifact exists in the registry. Human approval via `registry.promote_champion()` is required.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**
- `ResponseFn` Protocol → Task 1 ✓
- Synthetic adapters → Task 1 ✓
- `MLResponseFnAdapter` (injects rate columns, handles missing segments) → Task 1 ✓
- Governance gate (`require_approved`, `GovernanceError`) → Task 1 ✓
- Optimizer wiring (loan + deposit loops, `evaluate_joint_rate_grid`, `optimize_joint_rates`) → Task 2 ✓
- `ModelRegistry.approve_model` → Task 3 ✓
- `ModelRegistry.promote_champion` → Task 3 ✓
- `ModelRegistry.get_champion` → Task 3 ✓
- `ModelScoringAgent.resolve_response_fn` → Task 4 ✓
- Integration test → Task 5 ✓

**Gaps addressed:** `optimize_joint_market_relative_rates` calls `optimize_joint_rates` so it picks up the new params transitively. `evaluate_adaptive_joint_rate_grid` in `diagnostics.py` calls `evaluate_joint_rate_grid` directly with keyword args — the new params default to `None` so no change needed there.

**Placeholder scan:** None found.

**Type consistency:** `ResponseFn | None` used consistently throughout. `MLResponseFnAdapter` constructor uses `model: Any` to avoid circular import from `modelling/`. `resolve_response_fn` returns `Any` for the same reason.
