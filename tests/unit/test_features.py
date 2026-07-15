"""Tests for the local feature-store boundary: schema, lineage, and provider."""

import pandas as pd
import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.features.contracts import FeatureSchema, FeatureSchemaError
from balance_pricing_engine.features.definitions import (
    DEPOSIT_RESPONSE_FEATURE_SCHEMA,
    LOAN_RESPONSE_FEATURE_SCHEMA,
    feature_schema_for_product,
)
from balance_pricing_engine.features.local_store import LocalFeatureStore
from balance_pricing_engine.modelling.datasets import LOAN_FEATURE_COLUMNS
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng


def _small_config():
    config = load_config("configs/base.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 200, "n_deposit_customers": 200}
    )
    return config.model_copy(update={"simulation": simulation})


def test_feature_schema_for_product_returns_canonical_schemas():
    assert feature_schema_for_product("loan") is LOAN_RESPONSE_FEATURE_SCHEMA
    assert feature_schema_for_product("deposit") is DEPOSIT_RESPONSE_FEATURE_SCHEMA


def test_feature_schema_for_unknown_product_raises():
    with pytest.raises(ValueError, match="Unknown feature product scope"):
        feature_schema_for_product("credit_card")


def test_feature_schema_fingerprint_is_stable_across_instances():
    first = FeatureSchema(
        name="loan_response_features",
        version="v1",
        product="loan",
        columns=tuple(LOAN_FEATURE_COLUMNS),
        target_column="accepted",
    )
    second = FeatureSchema(
        name="loan_response_features",
        version="v1",
        product="loan",
        columns=tuple(LOAN_FEATURE_COLUMNS),
        target_column="accepted",
    )

    assert first.fingerprint == second.fingerprint


def test_feature_schema_fingerprint_changes_with_version():
    base = LOAN_RESPONSE_FEATURE_SCHEMA
    bumped = FeatureSchema(
        name=base.name,
        version="v2",
        product=base.product,
        columns=base.columns,
        target_column=base.target_column,
    )

    assert base.fingerprint != bumped.fingerprint


def test_feature_schema_validate_raises_on_missing_columns():
    frame = pd.DataFrame({"offered_rate": [0.08]})

    with pytest.raises(FeatureSchemaError, match="loan_response_features"):
        LOAN_RESPONSE_FEATURE_SCHEMA.validate(frame)


def test_feature_schema_validate_passes_with_all_columns():
    frame = pd.DataFrame({column: [0.0] for column in LOAN_RESPONSE_FEATURE_SCHEMA.columns})

    LOAN_RESPONSE_FEATURE_SCHEMA.validate(frame)  # must not raise


def test_local_feature_store_training_dataset_matches_schema_and_records_lineage():
    config = _small_config()
    rng = make_rng(config.random_seed)
    store = LocalFeatureStore()

    dataset, lineage = store.training_dataset("loan", config=config, rng=rng)

    assert set(LOAN_RESPONSE_FEATURE_SCHEMA.columns) <= set(dataset.columns)
    assert LOAN_RESPONSE_FEATURE_SCHEMA.target_column in dataset.columns
    assert lineage.feature_set_name == "loan_response_features"
    assert lineage.schema_fingerprint == LOAN_RESPONSE_FEATURE_SCHEMA.fingerprint
    assert lineage.row_count == len(dataset)
    assert lineage.product == "loan"


def test_local_feature_store_serving_features_derives_rate_dependent_columns():
    config = _small_config()
    rng = make_rng(config.random_seed)
    customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    store = LocalFeatureStore()

    frame, lineage = store.serving_features(
        "loan",
        customers,
        offered_rate=0.08,
        competitor_rate=config.market.competitor_loan_rate,
    )

    assert "rate_gap_to_competitor" in frame.columns
    assert "risk_segment" in frame.columns
    assert (frame["rate_gap_to_competitor"] == 0.08 - config.market.competitor_loan_rate).all()
    assert lineage.row_count == len(customers)


def test_local_feature_store_serving_features_raises_on_incompatible_schema():
    store = LocalFeatureStore()
    incompatible = pd.DataFrame({"unrelated_column": [1, 2, 3]})

    with pytest.raises(FeatureSchemaError):
        store.serving_features("loan", incompatible)


def test_training_and_serving_snapshots_share_schema_fingerprint():
    """Training-serving consistency: the same schema/fingerprint governs both paths."""
    config = _small_config()
    rng = make_rng(config.random_seed)
    customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    store = LocalFeatureStore()

    _, training_lineage = store.training_dataset(
        "loan", config=config, rng=make_rng(config.random_seed)
    )
    _, serving_lineage = store.serving_features(
        "loan", customers, offered_rate=0.08, competitor_rate=config.market.competitor_loan_rate
    )

    assert training_lineage.schema_fingerprint == serving_lineage.schema_fingerprint
