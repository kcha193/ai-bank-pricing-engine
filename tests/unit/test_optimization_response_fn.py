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
