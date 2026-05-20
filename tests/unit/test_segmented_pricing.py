import json

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.optimization.segmented import (
    optimize_segmented_rates,
    segment_deposit_customers,
    segment_loan_customers,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState


def test_segment_loan_customers_splits_lvr_tiers_without_overlap():
    customers = pd.DataFrame(
        {
            "customer_id": [1, 2, 3, 4, 5],
            "lvr": [0.50, 0.79, 0.80, 0.89, 0.92],
        }
    )
    config = load_config("configs/segmented_pricing.yaml").segmented_pricing

    cohorts = segment_loan_customers(customers, config.loan_tiers)

    assert {name: len(cohort) for name, cohort in cohorts.items()} == {
        "standard": 2,
        "high_lvr": 2,
        "very_high_lvr": 1,
    }
    assigned_ids = pd.concat(cohorts.values())["customer_id"].tolist()
    assert sorted(assigned_ids) == [1, 2, 3, 4, 5]
    assert len(assigned_ids) == len(set(assigned_ids))


def test_segment_deposit_customers_splits_balance_tiers_without_overlap():
    customers = pd.DataFrame(
        {
            "customer_id": [1, 2, 3, 4, 5],
            "deposit_amount": [10_000, 49_999, 50_000, 199_999, 250_000],
        }
    )
    config = load_config("configs/segmented_pricing.yaml").segmented_pricing

    cohorts = segment_deposit_customers(customers, config.deposit_tiers)

    assert {name: len(cohort) for name, cohort in cohorts.items()} == {
        "retail": 2,
        "mid": 2,
        "wholesale": 1,
    }
    assigned_ids = pd.concat(cohorts.values())["customer_id"].tolist()
    assert sorted(assigned_ids) == [1, 2, 3, 4, 5]
    assert len(assigned_ids) == len(set(assigned_ids))


def test_segment_loan_customers_rejects_empty_tier():
    customers = pd.DataFrame({"lvr": [0.50, 0.70, 0.95]})
    config = load_config("configs/segmented_pricing.yaml").segmented_pricing

    with pytest.raises(ValueError, match="high_lvr"):
        segment_loan_customers(customers, config.loan_tiers)


def test_optimize_segmented_rates_returns_tier_results_with_valid_rates():
    config = load_config("configs/segmented_pricing.yaml")
    rng = np.random.default_rng(0)
    loan_customers = generate_loan_customers(300, rng)
    deposit_customers = generate_deposit_customers(300, rng)
    market = MarketState(**config.market.model_dump())

    result = optimize_segmented_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        config=config.segmented_pricing,
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        response_coefficients=config.response.coefficients,
    )

    assert result.total_objective > 0
    assert len(result.loan_tiers) == 3
    assert len(result.deposit_tiers) == 3
    for tier_result, tier_config in zip(result.loan_tiers, config.segmented_pricing.loan_tiers):
        assert tier_config.rate_grid.start <= tier_result.rate <= tier_config.rate_grid.stop
    for tier_result, tier_config in zip(result.deposit_tiers, config.segmented_pricing.deposit_tiers):
        assert tier_config.rate_grid.start <= tier_result.rate <= tier_config.rate_grid.stop


def test_segmented_config_rejects_multiple_pricing_modes():
    raw_config = _segmented_config_dict()
    raw_config["pricing_strategy"] = {
        "home_loan_spread_bps": {"min": -50, "max": 50, "step": 5},
        "term_deposit_spread_bps": {"min": -40, "max": 40, "step": 5},
    }

    with pytest.raises(ValidationError, match="Exactly one"):
        EngineConfig.model_validate(raw_config)


def test_segmented_config_rejects_unsorted_tiers():
    raw_config = _segmented_config_dict()
    raw_config["segmented_pricing"]["loan_tiers"] = [
        raw_config["segmented_pricing"]["loan_tiers"][1],
        raw_config["segmented_pricing"]["loan_tiers"][0],
        raw_config["segmented_pricing"]["loan_tiers"][2],
    ]

    with pytest.raises(ValidationError, match="sorted"):
        EngineConfig.model_validate(raw_config)


def test_segmented_total_objective_matches_component_decomposition():
    config = load_config("configs/segmented_pricing.yaml")
    rng = np.random.default_rng(0)
    result = optimize_segmented_rates(
        loan_customers=generate_loan_customers(300, rng),
        deposit_customers=generate_deposit_customers(300, rng),
        market=MarketState(**config.market.model_dump()),
        config=config.segmented_pricing,
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        response_coefficients=config.response.coefficients,
    )

    assert result.total_objective == pytest.approx(
        result.total_loan_interest_income
        - result.total_credit_loss
        - result.total_deposit_interest_cost
        - result.wholesale_funding_cost
        - result.funding_shortfall_penalty
        + result.excess_deposit_income
    )


def test_segmented_scenario_writes_tier_rate_artifact(tmp_path):
    diagnostics = run_baseline_diagnostics(
        load_config("configs/segmented_pricing.yaml"),
        max_boundary_expansions=0,
    )

    diagnostics.write_artifacts(tmp_path)

    tier_rates = json.loads((tmp_path / "segmented_tier_rates.json").read_text())
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert set(tier_rates) == {"loan_tiers", "deposit_tiers"}
    assert summary["segmented_tier_rates"] == tier_rates


def _segmented_config_dict() -> dict:
    return {
        "random_seed": 42,
        "simulation": {
            "n_loan_customers": 300,
            "n_deposit_customers": 300,
        },
        "market": {
            "cash_rate": 0.0425,
            "wholesale_funding_spread": 0.012,
            "competitor_loan_rate": 0.0675,
            "competitor_deposit_rate": 0.045,
            "home_loan_reference_rate": 0.0525,
            "term_deposit_reference_rate": 0.0400,
        },
        "segmented_pricing": {
            "loan_tiers": [
                {
                    "name": "standard",
                    "lvr_max": 0.80,
                    "rate_grid": {"start": 0.060, "stop": 0.075, "step": 0.005},
                },
                {
                    "name": "high_lvr",
                    "lvr_max": 0.90,
                    "rate_grid": {"start": 0.065, "stop": 0.080, "step": 0.005},
                },
                {
                    "name": "very_high_lvr",
                    "rate_grid": {"start": 0.070, "stop": 0.090, "step": 0.005},
                },
            ],
            "deposit_tiers": [
                {
                    "name": "retail",
                    "balance_max": 50_000,
                    "rate_grid": {"start": 0.040, "stop": 0.055, "step": 0.005},
                },
                {
                    "name": "mid",
                    "balance_max": 200_000,
                    "rate_grid": {"start": 0.042, "stop": 0.057, "step": 0.005},
                },
                {
                    "name": "wholesale",
                    "rate_grid": {"start": 0.044, "stop": 0.060, "step": 0.005},
                },
            ],
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": 0.65,
            "funding_shortfall_penalty_rate": 0.018,
        },
        "response": {
            "loan_rate_elasticity": -85.0,
            "loan_competitor_gap_elasticity": -65.0,
            "deposit_rate_elasticity": 95.0,
            "deposit_competitor_gap_elasticity": 75.0,
        },
    }
