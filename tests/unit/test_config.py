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


# ------------------------------------------------- revalidation keeps every effective value
#
# Regression for PR #74 review r4583163702: a field edited in place inside a nested model that
# was created from its default (base.yaml minus ``response.coefficients``) is not in the
# parent's ``model_fields_set``. Revalidation must still price -- and validate -- the edited
# value, never silently restore the default.


def _without_coefficients():
    import yaml

    raw = yaml.safe_load(open("configs/base.yaml"))
    del raw["response"]["coefficients"]
    raw["simulation"] = {"n_loan_customers": 200, "n_deposit_customers": 200}
    return raw


def _edited_in_place(intercept):
    config = EngineConfig.model_validate(_without_coefficients())
    assert "coefficients" not in config.response.model_fields_set
    config.response.coefficients.loan.intercept = intercept
    return config


def _config_seams():
    from types import SimpleNamespace

    from balance_pricing_engine.competitor_rates.apply import revalidate_config
    from balance_pricing_engine.llm.scenario_interface import ConfigPatch, _patched_config
    from balance_pricing_engine.pricing_intelligence.strategy import (
        apply_manual_competitor_rate_to_config,
    )

    patch = SimpleNamespace(patches=[ConfigPatch(path=("market", "cash_rate"), value=0.04)])
    return {
        "model_copy": lambda c: c.model_copy(update={"random_seed": 7}),
        "revalidate_config": revalidate_config,
        "manual_overlay": lambda c: apply_manual_competitor_rate_to_config(c, 0.05),
        "llm_patch": lambda c: _patched_config(c, patch),
    }


@pytest.mark.parametrize("seam", ["model_copy", "revalidate_config", "manual_overlay", "llm_patch"])
def test_revalidation_keeps_an_in_place_edit_to_a_default_created_section(seam):
    revalidated = _config_seams()[seam](_edited_in_place(9.0))

    assert revalidated.response.coefficients.loan.intercept == 9.0


@pytest.mark.parametrize("seam", ["model_copy", "revalidate_config", "manual_overlay", "llm_patch"])
def test_revalidation_rejects_a_non_finite_in_place_edit(seam):
    with pytest.raises(ValueError, match="finite"):
        _config_seams()[seam](_edited_in_place(float("nan")))


def test_scenario_entry_points_price_an_in_place_edit():
    from balance_pricing_engine.application import PricingScenarioRequest, PricingService
    from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

    explicit_raw = _without_coefficients()
    explicit_raw["response"]["coefficients"] = _edited_in_place(9.0).response.coefficients.model_dump()
    explicit = EngineConfig.model_validate(explicit_raw)
    default = EngineConfig.model_validate(_without_coefficients())

    expected = run_baseline_diagnostics(explicit, max_boundary_expansions=0).best_result
    baseline = run_baseline_diagnostics(default, max_boundary_expansions=0).best_result
    assert expected.total_objective != baseline.total_objective  # the edit moves the price

    edited = run_baseline_diagnostics(_edited_in_place(9.0), max_boundary_expansions=0)
    assert edited.best_result.total_objective == expected.total_objective
    decision = PricingService().run_scenario(
        PricingScenarioRequest(config=_edited_in_place(9.0), max_boundary_expansions=0)
    )
    assert decision.decision_record.config_hash != PricingService().run_scenario(
        PricingScenarioRequest(config=default, max_boundary_expansions=0)
    ).decision_record.config_hash
