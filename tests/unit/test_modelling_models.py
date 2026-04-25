import numpy as np

from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)
from balance_pricing_engine.modelling.persistence import load_model, save_model
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng


def test_logistic_regression_can_fit_and_predict_loan_probabilities():
    rng = make_rng(200)
    customers = generate_loan_customers(250, rng)
    dataset = build_loan_response_dataset(
        customers,
        offered_rates=np.array([0.06, 0.07, 0.08]),
        competitor_rate=0.0675,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=800,
    )

    model = fit_response_model(
        build_logistic_regression_model(),
        dataset,
        LOAN_FEATURE_COLUMNS,
        LOAN_TARGET_COLUMN,
    )
    probabilities = predict_response_probability(model, dataset, LOAN_FEATURE_COLUMNS)

    assert len(probabilities) == len(dataset)
    assert np.all((probabilities >= 0) & (probabilities <= 1))


def test_hist_gradient_boosting_can_fit_and_predict_deposit_probabilities():
    rng = make_rng(201)
    customers = generate_deposit_customers(250, rng)
    dataset = build_deposit_response_dataset(
        customers,
        offered_rates=np.array([0.04, 0.05, 0.06]),
        competitor_rate=0.045,
        rate_elasticity=95.0,
        competitor_gap_elasticity=75.0,
        rng=rng,
        n_samples=800,
    )

    model = fit_response_model(
        build_hist_gradient_boosting_model(),
        dataset,
        DEPOSIT_FEATURE_COLUMNS,
        DEPOSIT_TARGET_COLUMN,
    )
    probabilities = predict_response_probability(model, dataset, DEPOSIT_FEATURE_COLUMNS)

    assert len(probabilities) == len(dataset)
    assert np.all((probabilities >= 0) & (probabilities <= 1))


def test_model_persistence_round_trip_preserves_probabilities(tmp_path):
    rng = make_rng(202)
    customers = generate_loan_customers(200, rng)
    dataset = build_loan_response_dataset(
        customers,
        offered_rates=np.array([0.06, 0.07, 0.08]),
        competitor_rate=0.0675,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=600,
    )
    model = fit_response_model(
        build_logistic_regression_model(),
        dataset,
        LOAN_FEATURE_COLUMNS,
        LOAN_TARGET_COLUMN,
    )

    model_path = tmp_path / "loan_response_model.joblib"
    save_model(model, model_path)
    loaded_model = load_model(model_path)

    original_probabilities = predict_response_probability(model, dataset, LOAN_FEATURE_COLUMNS)
    loaded_probabilities = predict_response_probability(
        loaded_model,
        dataset,
        LOAN_FEATURE_COLUMNS,
    )
    assert np.allclose(original_probabilities, loaded_probabilities)
