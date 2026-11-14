"""OPT-003: reject invalid economics before any pricing computation."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from balance_pricing_engine.api import app
from balance_pricing_engine.config import EngineConfig, load_config


@pytest.fixture
def raw():
    return yaml.safe_load(Path("configs/base.yaml").read_text())


ECONOMIC_PATHS = (
    [
        ("market", field)
        for field in (
            "cash_rate",
            "wholesale_funding_spread",
            "competitor_loan_rate",
            "competitor_deposit_rate",
            "home_loan_reference_rate",
            "term_deposit_reference_rate",
        )
    ]
    + [
        ("response", field)
        for field in (
            "loan_rate_elasticity",
            "loan_competitor_gap_elasticity",
            "deposit_rate_elasticity",
            "deposit_competitor_gap_elasticity",
        )
    ]
    + [
        ("balance_sheet", field)
        for field in (
            "required_deposit_funding_ratio",
            "funding_shortfall_penalty_rate",
            "loan_rwa_weight",
            "capital_hurdle_rate",
            "average_loan_tenor_years",
            "min_return_on_equity",
        )
    ]
    + [
        ("response", "coefficients", product, field)
        for product, fields in (
            (
                "loan",
                ("intercept", "rate_anchor", "risk_score_coef", "credit_score_coef", "lvr_coef"),
            ),
            (
                "deposit",
                (
                    "intercept",
                    "rate_anchor",
                    "rate_sensitivity_coef",
                    "relationship_coef",
                    "digital_coef",
                ),
            ),
        )
        for field in fields
    ]
    + [
        ("pricing_strategy", product, field)
        for product in ("home_loan_spread_bps", "term_deposit_spread_bps")
        for field in ("min", "max", "step")
    ]
    + [
        ("multiperiod", field)
        for field in (
            "loan_runoff_rate",
            "deposit_runoff_rate",
            "discount_rate",
            "market_cash_rate_drift_bps",
            "competitor_loan_rate_drift_bps",
            "competitor_deposit_rate_drift_bps",
            "reinvestment_spread_bps",
            "loan_tenor_years",
        )
    ]
)


@pytest.mark.parametrize("path", ECONOMIC_PATHS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_economic_fields_rejected(raw, path, bad):
    raw["multiperiod"] = {}
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(ValidationError) as error:
        EngineConfig(**raw)
    assert path in [item["loc"] for item in error.value.errors()]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("loan_rate_elasticity", 170),
        ("loan_competitor_gap_elasticity", 130),
        ("deposit_rate_elasticity", -140),
        ("deposit_competitor_gap_elasticity", -110),
    ],
)
def test_wrong_sign_elasticity_rejected(raw, field, bad):
    raw["response"][field] = bad
    with pytest.raises(ValidationError, match=field):
        EngineConfig.model_validate(raw)


def test_base_and_zero_elasticities_remain_valid(raw):
    config = load_config("configs/base.yaml")
    assert config.response.loan_rate_elasticity == -170
    assert config.response.deposit_rate_elasticity == 140
    for field in raw["response"]:
        if field != "coefficients":
            raw["response"][field] = 0
    assert EngineConfig(**raw).response.loan_rate_elasticity == 0


@pytest.mark.parametrize("field", ["start", "stop", "step"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_absolute_grid_nonfinite_rejected(raw, field, bad):
    raw.pop("pricing_strategy")
    raw["products"] = {
        "loan_rate_grid": {"start": 0.04, "stop": 0.06, "step": 0.001},
        "deposit_rate_grid": {"start": 0.03, "stop": 0.05, "step": 0.001},
    }
    raw["products"]["loan_rate_grid"][field] = bad
    with pytest.raises(ValidationError):
        EngineConfig(**raw)


def test_copy_updates_revalidate_economics(raw):
    config = EngineConfig(**raw)
    with pytest.raises(ValidationError, match="cash_rate"):
        config.market.model_copy(update={"cash_rate": float("nan")})
    market = config.market.model_dump()
    market["cash_rate"] = float("inf")
    with pytest.raises(ValidationError, match="cash_rate"):
        config.model_copy(update={"market": market})
    assert config.market.cash_rate == 0.025


@pytest.mark.parametrize("route", ["/run-scenario", "/v1/pricing/scenarios"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_api_rejects_nonfinite_config(raw, route, bad, monkeypatch):
    monkeypatch.delenv("PRICING_ENGINE_API_KEYS", raising=False)
    raw["market"]["cash_rate"] = bad
    response = TestClient(app).post(route, json={"config_yaml": yaml.safe_dump(raw)})
    assert response.status_code == 422
    assert "cash_rate" in response.text


def test_yaml_load_rejects_nonfinite(raw, tmp_path):
    raw["market"]["cash_rate"] = float("nan")
    source = tmp_path / "invalid.yaml"
    source.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="cash_rate"):
        load_config(source)


@pytest.mark.parametrize("entry", ["direct", "governed", "multiperiod"])
def test_scenario_entry_revalidates_an_existing_model(raw, entry):
    from balance_pricing_engine.application.contracts import PricingScenarioRequest
    from balance_pricing_engine.application.pricing_service import PricingService
    from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics
    from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

    raw["multiperiod"] = {}
    config = EngineConfig(**raw)
    # Even an instance altered through a trusted-code escape hatch must not reach pricing.
    config.market.cash_rate = float("nan")
    with pytest.raises(ValidationError, match="cash_rate"):
        if entry == "direct":
            run_baseline_diagnostics(config)
        elif entry == "governed":
            PricingService().run_scenario(PricingScenarioRequest(config=config))
        else:
            run_multiperiod_diagnostics(config)
