import pytest

from balance_pricing_engine.competitor_rates.apply import (
    apply_competitor_rates_to_config,
    load_and_resolve_competitor_rates,
)
from balance_pricing_engine.competitor_rates.store import load_rate_run
from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def test_enabled_competitor_rates_rebuild_engine_market_state_and_grids(tmp_path):
    run = load_rate_run("tests/fixtures/competitor_rates/latest.json")
    source_file = tmp_path / "latest.json"
    source_file.write_text(run.model_dump_json(), encoding="utf-8")
    raw_config = load_config("configs/base.yaml").model_dump()
    raw_config["simulation"] = {
        "n_loan_customers": 1000,
        "n_deposit_customers": 1000,
    }
    raw_config["competitor_rates"]["enabled"] = True
    raw_config["competitor_rates"]["source_file"] = str(source_file)
    raw_config["competitor_rates"]["max_staleness_hours"] = 10_000
    config = EngineConfig.model_validate(raw_config)

    resolved = load_and_resolve_competitor_rates(config.competitor_rates)
    applied = apply_competitor_rates_to_config(config)
    loan_grid, deposit_grid = build_configured_rate_grids(applied)
    diagnostics = run_baseline_diagnostics(applied, max_boundary_expansions=0)

    assert applied.market.competitor_loan_rate == pytest.approx(
        resolved.mortgage_reference_rate
    )
    assert applied.market.competitor_deposit_rate == pytest.approx(
        resolved.term_deposit_reference_rate
    )
    assert applied.market.home_loan_reference_rate == pytest.approx(
        resolved.mortgage_reference_rate
    )
    assert applied.market.term_deposit_reference_rate == pytest.approx(
        resolved.term_deposit_reference_rate
    )
    assert diagnostics.grid_results["loan_rate"].min() == pytest.approx(loan_grid.min())
    assert diagnostics.grid_results["loan_rate"].max() == pytest.approx(loan_grid.max())
    assert diagnostics.grid_results["deposit_rate"].min() == pytest.approx(
        deposit_grid.min()
    )
    assert diagnostics.grid_results["deposit_rate"].max() == pytest.approx(
        deposit_grid.max()
    )
    assert diagnostics.grid_results["home_loan_spread_to_market_bps"].min() == pytest.approx(
        applied.pricing_strategy.home_loan_spread_bps.min
    )
    assert diagnostics.grid_results["term_deposit_spread_to_market_bps"].max() == pytest.approx(
        applied.pricing_strategy.term_deposit_spread_bps.max
    )
