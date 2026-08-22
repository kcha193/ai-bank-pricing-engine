import pytest

from balance_pricing_engine.config import EngineConfig


def test_engine_config_warns_for_negative_wholesale_funding_spread():
    with pytest.warns(UserWarning, match="negative wholesale_funding_spread"):
        EngineConfig.model_validate(
            _base_config(
                market={
                    "wholesale_funding_spread": -0.001,
                }
            )
        )


def test_engine_config_warns_when_shortfall_penalty_is_dominated_by_wholesale_spread():
    with pytest.warns(UserWarning, match="penalty dominated"):
        EngineConfig.model_validate(
            _base_config(
                balance_sheet={
                    "funding_shortfall_penalty_rate": 0.005,
                }
            )
        )


def test_engine_config_warns_for_small_customer_cohorts():
    with pytest.warns(UserWarning, match="high MC variance"):
        EngineConfig.model_validate(
            _base_config(
                simulation={
                    "n_loan_customers": 999,
                    "n_deposit_customers": 999,
                }
            )
        )


def test_engine_config_warns_for_narrow_spread_policy_bands():
    with pytest.warns(UserWarning, match="likely to expand past policy"):
        EngineConfig.model_validate(
            _base_config(
                pricing_strategy={
                    "home_loan_spread_bps": {"min": -10, "max": 10, "step": 5},
                    "term_deposit_spread_bps": {"min": -20, "max": 20, "step": 5},
                }
            )
        )


def test_engine_config_accepts_a_pricing_intelligence_block():
    raw = _base_config()
    raw["pricing_intelligence"] = {
        "selected_banks": ["ANZ", "ASB"],
        "max_observation_age_hours": 48,
    }

    config = EngineConfig.model_validate(raw)

    assert config.pricing_intelligence is not None
    assert config.pricing_intelligence.selected_banks == ["ANZ", "ASB"]
    assert config.pricing_intelligence.max_observation_age_hours == 48


def test_engine_config_pricing_intelligence_defaults_to_none():
    config = EngineConfig.model_validate(_base_config())

    assert config.pricing_intelligence is None


def _base_config(**overrides):
    config = {
        "random_seed": 42,
        "simulation": {
            "n_loan_customers": 5000,
            "n_deposit_customers": 5000,
        },
        "market": {
            "cash_rate": 0.0425,
            "wholesale_funding_spread": 0.012,
            "competitor_loan_rate": 0.0675,
            "competitor_deposit_rate": 0.045,
            "home_loan_reference_rate": 0.0525,
            "term_deposit_reference_rate": 0.0400,
        },
        "pricing_strategy": {
            "home_loan_spread_bps": {"min": -50, "max": 50, "step": 5},
            "term_deposit_spread_bps": {"min": -40, "max": 40, "step": 5},
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
    for section, values in overrides.items():
        config[section].update(values)
    return config
