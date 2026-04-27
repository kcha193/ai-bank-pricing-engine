import pytest

from balance_pricing_engine.competitor_rates.apply import (
    apply_competitor_rates_to_config,
    load_and_resolve_competitor_rates,
)
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.scenarios.runner import run_baseline_scenario


FIXTURE = "tests/fixtures/competitor_rates/latest.json"


def test_disabled_competitor_rates_leave_manual_config_unchanged():
    config = EngineConfig.model_validate(_base_config(enabled=False))

    applied = apply_competitor_rates_to_config(config)

    assert applied is config
    assert applied.market.competitor_loan_rate == pytest.approx(0.0675)
    assert applied.market.competitor_deposit_rate == pytest.approx(0.045)


def test_market_average_strategy_resolves_decimal_reference_rates():
    config = EngineConfig.model_validate(_base_config())

    resolved = load_and_resolve_competitor_rates(config.competitor_rates)

    assert resolved.mortgage_reference_rate == pytest.approx((5.5 + 5.7 + 5.6 + 5.8) / 4 / 100)
    assert resolved.term_deposit_reference_rate == pytest.approx((4.1 + 4.3 + 4.4 + 4.2 + 4.5) / 5 / 100)


def test_market_min_and_market_max_strategies_use_required_direction():
    raw = _base_config()
    raw["competitor_rates"]["mortgage"]["strategy"] = "market_min"
    raw["competitor_rates"]["term_deposit"]["strategy"] = "market_max"
    config = EngineConfig.model_validate(raw)

    applied = apply_competitor_rates_to_config(config)

    assert applied.market.competitor_loan_rate == pytest.approx(0.055)
    assert applied.market.competitor_deposit_rate == pytest.approx(0.045)


def test_bank_specific_strategy_requires_selected_bank_and_uses_named_records():
    raw = _base_config()
    raw["competitor_rates"]["mortgage"] = {
        "strategy": "bank_specific",
        "bank": "ASB",
        "term_months": 12,
        "include_specials": True,
        "require_min_20_percent_equity": True,
    }
    raw["competitor_rates"]["term_deposit"] = {
        "strategy": "bank_specific",
        "bank": "Kiwibank",
        "term_months": 12,
        "product_type": "term_deposit",
    }
    config = EngineConfig.model_validate(raw)

    applied = apply_competitor_rates_to_config(config)

    assert applied.market.home_loan_reference_rate == pytest.approx(0.057)
    assert applied.market.term_deposit_reference_rate == pytest.approx(0.045)


def test_market_median_strategy_calculates_median_rates():
    raw = _base_config()
    raw["competitor_rates"]["mortgage"]["strategy"] = "market_median"
    raw["competitor_rates"]["term_deposit"]["strategy"] = "market_median"
    config = EngineConfig.model_validate(raw)

    applied = apply_competitor_rates_to_config(config)

    assert applied.market.home_loan_reference_rate == pytest.approx((0.056 + 0.057) / 2)
    assert applied.market.term_deposit_reference_rate == pytest.approx(0.043)


def test_engine_runs_with_fixture_competitor_rates_applied():
    config = EngineConfig.model_validate(_base_config())
    applied = apply_competitor_rates_to_config(config)

    result = run_baseline_scenario(applied)

    assert result.loan_rate > 0
    assert result.deposit_rate > 0


def _base_config(enabled: bool = True) -> dict:
    return {
        "random_seed": 42,
        "simulation": {
            "n_loan_customers": 50,
            "n_deposit_customers": 50,
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
            "home_loan_spread_bps": {"min": -10, "max": 10, "step": 10},
            "term_deposit_spread_bps": {"min": -10, "max": 10, "step": 10},
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
        "competitor_rates": {
            "enabled": enabled,
            "source_file": FIXTURE,
            "selected_banks": ["ANZ", "ASB", "Westpac", "BNZ", "Kiwibank"],
            "max_staleness_hours": 10_000,
            "strict": True,
            "mortgage": {
                "strategy": "market_average",
                "term_months": 12,
                "include_specials": True,
                "require_min_20_percent_equity": True,
            },
            "term_deposit": {
                "strategy": "market_average",
                "term_months": 12,
                "product_type": "term_deposit",
                "interest_payment_frequency_preference": [
                    "at_maturity",
                    "annually",
                    "quarterly",
                    "monthly"
                ],
            },
        },
    }
