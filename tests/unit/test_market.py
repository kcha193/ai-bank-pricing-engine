import numpy as np
import pytest
from pydantic import ValidationError

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.market import (
    MarketRates,
    SpreadGridConfig,
    bps_to_rate,
    build_market_relative_grid,
)


def test_bps_to_rate_converts_basis_points_to_decimal_rate():
    assert bps_to_rate(0) == pytest.approx(0.0)
    assert bps_to_rate(25) == pytest.approx(0.0025)
    assert bps_to_rate(-40) == pytest.approx(-0.0040)
    assert bps_to_rate(100) == pytest.approx(0.0100)


def test_build_market_relative_grid_adds_spreads_to_reference_rates():
    loan_grid, deposit_grid = build_market_relative_grid(
        market_rates=MarketRates(
            home_loan_reference_rate=0.0525,
            term_deposit_reference_rate=0.0400,
        ),
        home_loan_spread_bps=SpreadGridConfig(min=-10, max=10, step=10),
        term_deposit_spread_bps=SpreadGridConfig(min=-20, max=0, step=10),
    )

    assert np.allclose(loan_grid, np.array([0.0515, 0.0525, 0.0535]))
    assert np.allclose(deposit_grid, np.array([0.0380, 0.0390, 0.0400]))


def test_market_relative_config_requires_reference_rates():
    config = _minimal_market_relative_config()
    del config["market"]["home_loan_reference_rate"]

    with pytest.raises(ValidationError, match="home_loan_reference_rate"):
        EngineConfig.model_validate(config)


def test_market_relative_config_requires_term_deposit_reference_rate():
    config = _minimal_market_relative_config()
    del config["market"]["term_deposit_reference_rate"]

    with pytest.raises(ValidationError, match="term_deposit_reference_rate"):
        EngineConfig.model_validate(config)


def test_config_requires_pricing_strategy_or_absolute_product_grid():
    config = _minimal_market_relative_config()
    del config["pricing_strategy"]

    with pytest.raises(ValidationError, match="pricing_strategy or products"):
        EngineConfig.model_validate(config)


def test_base_config_validates_successfully():
    validated = EngineConfig.model_validate(_minimal_market_relative_config())

    assert validated.pricing_strategy is not None
    assert validated.market.home_loan_reference_rate == pytest.approx(0.0525)


@pytest.mark.parametrize(
    ("product", "field", "value", "message"),
    [
        ("home_loan_spread_bps", "step", 0, "step"),
        ("term_deposit_spread_bps", "step", -5, "step"),
        ("home_loan_spread_bps", "min", 60, "max"),
    ],
)
def test_invalid_spread_grid_config_fails_clearly(product, field, value, message):
    config = _minimal_market_relative_config()
    config["pricing_strategy"][product][field] = value

    with pytest.raises(ValidationError, match=message):
        EngineConfig.model_validate(config)


def test_market_relative_config_rejects_generated_negative_rates():
    config = _minimal_market_relative_config()
    config["market"]["term_deposit_reference_rate"] = 0.001
    config["pricing_strategy"]["term_deposit_spread_bps"]["min"] = -20

    with pytest.raises(ValidationError, match="negative rates"):
        EngineConfig.model_validate(config)


def test_market_relative_config_rejects_generated_negative_home_loan_rates():
    config = _minimal_market_relative_config()
    config["market"]["home_loan_reference_rate"] = 0.001
    config["pricing_strategy"]["home_loan_spread_bps"]["min"] = -20

    with pytest.raises(ValidationError, match="negative rates"):
        EngineConfig.model_validate(config)


def test_build_market_relative_grid_rejects_invalid_spread_inputs():
    with pytest.raises(ValueError, match="step"):
        build_market_relative_grid(
            market_rates=MarketRates(0.05, 0.04),
            home_loan_spread_bps=SpreadGridConfig(min=-10, max=10, step=0),
            term_deposit_spread_bps=SpreadGridConfig(min=-10, max=10, step=5),
        )


@pytest.mark.parametrize(
    "market_rates",
    [
        MarketRates(home_loan_reference_rate=-0.001, term_deposit_reference_rate=0.0400),
        MarketRates(home_loan_reference_rate=0.0500, term_deposit_reference_rate=-0.001),
    ],
)
def test_build_market_relative_grid_rejects_negative_reference_rates(market_rates):
    with pytest.raises(ValueError, match="reference rate"):
        build_market_relative_grid(
            market_rates=market_rates,
            home_loan_spread_bps=SpreadGridConfig(min=-10, max=10, step=10),
            term_deposit_spread_bps=SpreadGridConfig(min=-10, max=10, step=10),
        )


def test_build_market_relative_grid_rejects_reversed_spread_inputs():
    with pytest.raises(ValueError, match="max"):
        build_market_relative_grid(
            market_rates=MarketRates(0.05, 0.04),
            home_loan_spread_bps=SpreadGridConfig(min=10, max=-10, step=10),
            term_deposit_spread_bps=SpreadGridConfig(min=-10, max=10, step=10),
        )


def test_build_market_relative_grid_rejects_non_divisible_spread_steps():
    with pytest.raises(ValueError, match="land exactly"):
        build_market_relative_grid(
            market_rates=MarketRates(0.05, 0.04),
            home_loan_spread_bps=SpreadGridConfig(min=-10, max=10, step=6),
            term_deposit_spread_bps=SpreadGridConfig(min=-10, max=10, step=10),
        )


def test_build_market_relative_grid_rejects_generated_negative_rates():
    with pytest.raises(ValueError, match="negative rates"):
        build_market_relative_grid(
            market_rates=MarketRates(
                home_loan_reference_rate=0.001,
                term_deposit_reference_rate=0.0400,
            ),
            home_loan_spread_bps=SpreadGridConfig(min=-20, max=0, step=10),
            term_deposit_spread_bps=SpreadGridConfig(min=-10, max=10, step=10),
        )


def test_absolute_grid_config_remains_supported():
    config = _minimal_market_relative_config()
    del config["pricing_strategy"]
    del config["market"]["home_loan_reference_rate"]
    del config["market"]["term_deposit_reference_rate"]
    config["products"] = {
        "loan_rate_grid": {"start": 0.05, "stop": 0.06, "step": 0.005},
        "deposit_rate_grid": {"start": 0.03, "stop": 0.04, "step": 0.005},
    }

    validated = EngineConfig.model_validate(config)

    assert validated.pricing_strategy is None
    assert validated.products is not None


def _minimal_market_relative_config() -> dict:
    return {
        "random_seed": 42,
        "simulation": {
            "n_loan_customers": 10,
            "n_deposit_customers": 10,
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
