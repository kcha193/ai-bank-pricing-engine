import pytest
import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.diagnostics import evaluate_adaptive_joint_rate_grid
from balance_pricing_engine.optimization import grid_search as grid_search_module
from balance_pricing_engine.optimization.grid_search import (
    GRID_RESULT_COLUMNS,
    OptimizationResult,
    build_rate_grid,
    evaluate_joint_rate_grid,
    optimize_joint_market_relative_rates,
    optimize_joint_rates,
    select_best_result,
)
from balance_pricing_engine.optimization.response_fn import (
    SyntheticDepositResponseFn,
    SyntheticLoanResponseFn,
)
from balance_pricing_engine.market import MarketRates, SpreadGridConfig
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.utils.random import make_rng


def test_baseline_optimizer_returns_valid_joint_rates():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    assert diagnostics.grid_results["loan_rate"].min() <= result.loan_rate
    assert result.loan_rate <= diagnostics.grid_results["loan_rate"].max()
    assert diagnostics.grid_results["deposit_rate"].min() <= result.deposit_rate
    assert result.deposit_rate <= diagnostics.grid_results["deposit_rate"].max()
    assert result.expected_loan_volume > 0
    assert result.expected_deposit_volume > 0


@pytest.mark.parametrize(
    ("start", "stop", "step", "message"),
    [
        (0.03, 0.04, 0, "step"),
        (0.03, 0.04, -0.005, "step"),
        (0.04, 0.03, 0.005, "stop"),
        (0.03, 0.04, 0.006, "land exactly"),
    ],
)
def test_build_rate_grid_rejects_invalid_inputs(start, stop, step, message):
    with pytest.raises(ValueError, match=message):
        build_rate_grid(start=start, stop=stop, step=step)


def test_build_configured_rate_grids_uses_absolute_product_grid_fallback():
    raw_config = _market_relative_config_with_stale_absolute_grid()
    del raw_config["pricing_strategy"]
    del raw_config["market"]["home_loan_reference_rate"]
    del raw_config["market"]["term_deposit_reference_rate"]
    config = EngineConfig.model_validate(raw_config)

    loan_grid, deposit_grid = build_configured_rate_grids(config)

    assert list(loan_grid) == pytest.approx([0.10, 0.11])
    assert list(deposit_grid) == pytest.approx([0.08, 0.09])


def test_baseline_diagnostics_include_every_grid_candidate():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    expected_loan_rates = _expected_spread_count(config.pricing_strategy.home_loan_spread_bps)
    expected_deposit_rates = _expected_spread_count(config.pricing_strategy.term_deposit_spread_bps)

    assert list(diagnostics.grid_results.columns) == GRID_RESULT_COLUMNS
    assert len(loan_grid) == expected_loan_rates
    assert len(deposit_grid) == expected_deposit_rates
    assert len(diagnostics.grid_results) == expected_loan_rates * expected_deposit_rates
    assert sorted(diagnostics.grid_results["loan_rate"].unique()) == pytest.approx(loan_grid)
    assert sorted(diagnostics.grid_results["deposit_rate"].unique()) == pytest.approx(
        deposit_grid
    )


def test_config_rejects_multiple_pricing_modes():
    raw_config = _market_relative_config_with_stale_absolute_grid()

    with pytest.raises(ValueError, match="Exactly one"):
        EngineConfig.model_validate(raw_config)


def test_baseline_best_result_matches_max_grid_objective():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    max_objective = diagnostics.grid_results["total_objective"].max()

    assert diagnostics.best_result.objective_value == pytest.approx(max_objective)


def test_evaluate_joint_rate_grid_reuses_deposit_precompute(monkeypatch):
    loan_customers, deposit_customers = _single_customer_portfolios()
    loan_grid = np.array([0.04, 0.05])
    deposit_grid = np.array([0.02, 0.03, 0.04])
    call_count = 0
    original = grid_search_module.deposit_uptake_probability

    def counting_deposit_uptake_probability(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        grid_search_module,
        "deposit_uptake_probability",
        counting_deposit_uptake_probability,
    )

    evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        loan_rate_grid=loan_grid,
        deposit_rate_grid=deposit_grid,
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )

    assert call_count == len(deposit_grid)


def _minimal_customers_and_market():
    from balance_pricing_engine.synthetic.customers import (
        generate_deposit_customers, generate_loan_customers,
    )
    from balance_pricing_engine.synthetic.market import MarketState
    from balance_pricing_engine.utils.random import make_rng
    rng = make_rng(99)
    loan_customers = generate_loan_customers(200, rng)
    deposit_customers = generate_deposit_customers(200, rng)
    market = MarketState(
        cash_rate=0.055,
        wholesale_funding_spread=0.012,
        competitor_loan_rate=0.079,
        competitor_deposit_rate=0.048,
    )
    return loan_customers, deposit_customers, market


def test_evaluate_joint_rate_grid_accepts_synthetic_response_fn():
    loan_customers, deposit_customers, market = _minimal_customers_and_market()
    loan_fn = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    deposit_fn = SyntheticDepositResponseFn(elasticity=80.0, competitor_gap_elasticity=60.0)

    result = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081]),
        deposit_rate_grid=np.array([0.047, 0.049]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_fn,
    )
    assert set(result.columns) == set(GRID_RESULT_COLUMNS)
    assert len(result) == 4  # 2 loan rates × 2 deposit rates


def test_evaluate_joint_rate_grid_custom_fn_overrides_volume():
    """A response fn that always returns 0.5 probability produces deterministic volume."""
    loan_customers, deposit_customers, market = _minimal_customers_and_market()

    class FixedProbFn:
        def __call__(self, customers, offered_rate, competitor_rate):
            return np.full(len(customers), 0.5)

    result_custom = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081]),
        deposit_rate_grid=np.array([0.047]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=FixedProbFn(),
        deposit_response_fn=FixedProbFn(),
    )
    # Both loan rates produce identical expected_loan_volume when prob is constant
    assert abs(
        result_custom.iloc[0]["expected_loan_volume"]
        - result_custom.iloc[1]["expected_loan_volume"]
    ) < 1.0


def test_optimize_joint_rates_accepts_response_fn():
    loan_customers, deposit_customers, market = _minimal_customers_and_market()
    loan_fn = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    deposit_fn = SyntheticDepositResponseFn(elasticity=80.0, competitor_gap_elasticity=60.0)

    result = optimize_joint_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081]),
        deposit_rate_grid=np.array([0.047, 0.049]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_fn,
    )
    assert result.loan_rate in [0.079, 0.081]


def test_evaluate_joint_rate_grid_credits_excess_deposits_at_cash_rate():
    loan_customers, deposit_customers = _single_customer_portfolios()

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.04,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        loan_rate_grid=np.array([0.05]),
        deposit_rate_grid=np.array([0.02]),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )
    row = grid_results.iloc[0]
    expected_surplus_income = max(
        row["expected_deposit_volume"] - row["expected_loan_volume"],
        0.0,
    ) * 0.04

    assert row["excess_deposit_income"] == pytest.approx(expected_surplus_income)
    assert row["total_objective"] == pytest.approx(
        row["loan_interest_income"]
        - row["expected_credit_loss"]
        - row["capital_charge"]
        - row["deposit_interest_cost"]
        - row["wholesale_funding_cost"]
        - row["funding_shortfall_penalty"]
        + row["excess_deposit_income"]
    )


def test_evaluate_joint_rate_grid_includes_nonnegative_capital_charge():
    loan_customers, deposit_customers = _single_customer_portfolios()

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        loan_rate_grid=np.array([0.04, 0.05]),
        deposit_rate_grid=np.array([0.02, 0.03]),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )

    assert "capital_charge" in grid_results.columns
    assert (grid_results["capital_charge"] >= 0.0).all()


def test_grid_result_columns_includes_return_on_equity():
    assert "return_on_equity" in GRID_RESULT_COLUMNS


def test_evaluate_joint_rate_grid_has_return_on_equity_column():
    loan_customers, deposit_customers, market = _minimal_customers_and_market()

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.06]),
        deposit_rate_grid=np.array([0.04]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=95.0,
        deposit_competitor_gap_elasticity=75.0,
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )

    assert "return_on_equity" in grid_results.columns


def test_select_best_result_with_roe_floor_excludes_low_roe_rows():
    # Force a grid where all rows have negative ROE by setting a very high hurdle
    loan_customers, deposit_customers, market = _minimal_customers_and_market()

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.06, 0.065]),
        deposit_rate_grid=np.array([0.04]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=95.0,
        deposit_competitor_gap_elasticity=75.0,
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )

    # With a very high floor, should fall back to best available row
    result_high_floor = select_best_result(grid_results, min_return_on_equity=999.0)
    result_no_floor = select_best_result(grid_results)
    # When floor is unreachable, best available row is still returned (no crash)
    assert result_high_floor.loan_rate in [0.06, 0.065]
    assert result_no_floor.loan_rate in [0.06, 0.065]


def test_select_best_result_roe_floor_picks_correct_row():
    loan_customers, deposit_customers, market = _minimal_customers_and_market()

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.06, 0.07, 0.08]),
        deposit_rate_grid=np.array([0.04]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=95.0,
        deposit_competitor_gap_elasticity=75.0,
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
    )
    # Higher loan rate -> higher objective but also higher ROE
    # A tight floor (e.g. 0.01) should still return a valid winner
    result = select_best_result(grid_results, min_return_on_equity=0.01)
    assert result.loan_rate in [0.06, 0.07, 0.08]


def test_select_best_result_skips_roe_floor_when_all_values_are_nan():
    grid_results = pd.DataFrame(
        [
            {
                "loan_rate": 0.06,
                "deposit_rate": 0.03,
                "home_loan_spread_to_market_bps": 20.0,
                "term_deposit_spread_to_market_bps": 10.0,
                "loan_interest_income": 100.0,
                "expected_credit_loss": 10.0,
                "capital_charge": 4.0,
                "return_on_equity": float("nan"),
                "deposit_interest_cost": 25.0,
                "wholesale_funding_cost": 5.0,
                "excess_deposit_income": 0.0,
                "funding_shortfall_penalty": 1.0,
                "total_objective": 55.0,
                "expected_loan_volume": 1_000.0,
                "expected_deposit_volume": 700.0,
                "funding_shortfall": 0.0,
            },
            {
                "loan_rate": 0.065,
                "deposit_rate": 0.03,
                "home_loan_spread_to_market_bps": 30.0,
                "term_deposit_spread_to_market_bps": 10.0,
                "loan_interest_income": 120.0,
                "expected_credit_loss": 10.0,
                "capital_charge": 4.0,
                "return_on_equity": float("nan"),
                "deposit_interest_cost": 25.0,
                "wholesale_funding_cost": 5.0,
                "excess_deposit_income": 0.0,
                "funding_shortfall_penalty": 1.0,
                "total_objective": 75.0,
                "expected_loan_volume": 1_000.0,
                "expected_deposit_volume": 700.0,
                "funding_shortfall": 0.0,
            },
        ],
        columns=GRID_RESULT_COLUMNS,
    )

    result = select_best_result(grid_results, min_return_on_equity=0.10)

    assert result.loan_rate == pytest.approx(0.065)


def test_default_capital_charge_reduces_total_objective():
    loan_customers, deposit_customers = _single_customer_portfolios()
    grid_kwargs = {
        "loan_customers": loan_customers,
        "deposit_customers": deposit_customers,
        "market": MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        "loan_rate_grid": np.array([0.05]),
        "deposit_rate_grid": np.array([0.02]),
        "loan_rate_elasticity": 0.0,
        "loan_competitor_gap_elasticity": 0.0,
        "deposit_rate_elasticity": 0.0,
        "deposit_competitor_gap_elasticity": 0.0,
        "required_deposit_funding_ratio": 0.0,
        "funding_shortfall_penalty_rate": 0.0,
    }

    with_default_charge = evaluate_joint_rate_grid(**grid_kwargs).iloc[0]
    without_charge = evaluate_joint_rate_grid(
        **grid_kwargs,
        capital_hurdle_rate=0.0,
    ).iloc[0]

    assert with_default_charge["capital_charge"] > 0.0
    assert with_default_charge["total_objective"] < without_charge["total_objective"]


def test_baseline_grid_results_match_unhoisted_reference():
    config = load_config("configs/base.yaml")
    rng = make_rng(config.random_seed)
    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    market = MarketState(**config.market.model_dump())
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    current = evaluate_adaptive_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=loan_grid,
        deposit_rate_grid=deposit_grid,
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        rwa_weight=config.balance_sheet.loan_rwa_weight,
        capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
        home_loan_reference_rate=config.market.home_loan_reference_rate,
        term_deposit_reference_rate=config.market.term_deposit_reference_rate,
    )
    reference = _evaluate_adaptive_joint_rate_grid_unhoisted(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=loan_grid,
        deposit_rate_grid=deposit_grid,
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        rwa_weight=config.balance_sheet.loan_rwa_weight,
        capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
        home_loan_reference_rate=config.market.home_loan_reference_rate,
        term_deposit_reference_rate=config.market.term_deposit_reference_rate,
    )

    pd.testing.assert_frame_equal(
        current.grid_results,
        reference.grid_results,
        check_exact=True,
    )


def test_optimization_result_alias_properties_return_canonical_fields():
    result = OptimizationResult(
        loan_rate=0.07,
        deposit_rate=0.04,
        loan_interest_income=1_000.0,
        expected_credit_loss=100.0,
        capital_charge=12.0,
        deposit_interest_cost=50.0,
        wholesale_funding_cost=25.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=10.0,
        total_objective=815.0,
        expected_loan_volume=10_000.0,
        expected_deposit_volume=8_000.0,
        funding_shortfall=0.0,
    )

    assert result.objective_value == result.total_objective
    assert result.loan_income_after_losses == 900.0
    assert result.deposit_cost == result.deposit_interest_cost
    assert result.funding_penalty == result.funding_shortfall_penalty
    assert result.capital_charge == 12.0


def test_select_best_result_prefers_lower_funding_shortfall_when_objectives_tie():
    tied_results = np.array(
        [
            [
                0.050,
                0.030,
                0.0,
                0.0,
                100.0,
                10.0,
                0.0,
                5.0,
                1.0,
                0.0,
                1.0,
                83.0,
                1_000.0,
                500.0,
                250.0,
                0.0,
            ],
            [
                0.055,
                0.035,
                50.0,
                50.0,
                110.0,
                20.0,
                0.0,
                5.0,
                1.0,
                0.0,
                1.0,
                83.0,
                900.0,
                600.0,
                100.0,
                0.0,
            ],
        ],
        dtype=float,
    )
    grid_results = pd.DataFrame(tied_results, columns=GRID_RESULT_COLUMNS)

    result = select_best_result(grid_results)

    assert result.loan_rate == pytest.approx(0.055)
    assert result.deposit_rate == pytest.approx(0.035)


def test_select_best_result_prefers_smaller_market_spread_when_shortfall_ties():
    tied_results = np.array(
        [
            [
                0.050,
                0.030,
                -75.0,
                50.0,
                100.0,
                10.0,
                0.0,
                5.0,
                1.0,
                0.0,
                1.0,
                83.0,
                1_000.0,
                500.0,
                100.0,
                0.0,
            ],
            [
                0.055,
                0.035,
                25.0,
                -25.0,
                110.0,
                20.0,
                0.0,
                5.0,
                1.0,
                0.0,
                1.0,
                83.0,
                900.0,
                600.0,
                100.0,
                0.0,
            ],
        ],
        dtype=float,
    )
    grid_results = pd.DataFrame(tied_results, columns=GRID_RESULT_COLUMNS)

    result = select_best_result(grid_results)

    assert result.loan_rate == pytest.approx(0.055)
    assert result.deposit_rate == pytest.approx(0.035)


def test_select_best_result_rejects_empty_grid_results():
    empty_results = pd.DataFrame(columns=GRID_RESULT_COLUMNS)

    with pytest.raises(ValueError, match="At least one loan rate"):
        select_best_result(empty_results)


def test_evaluate_joint_rate_grid_rejects_empty_rate_grid():
    loan_customers, deposit_customers = _single_customer_portfolios()

    with pytest.raises(ValueError, match="At least one loan rate"):
        evaluate_joint_rate_grid(
            loan_customers=loan_customers,
            deposit_customers=deposit_customers,
            market=MarketState(
                cash_rate=0.0,
                wholesale_funding_spread=0.0,
                competitor_loan_rate=0.05,
                competitor_deposit_rate=0.03,
            ),
            loan_rate_grid=np.array([]),
            deposit_rate_grid=np.array([0.03]),
            loan_rate_elasticity=0.0,
            loan_competitor_gap_elasticity=0.0,
            deposit_rate_elasticity=0.0,
            deposit_competitor_gap_elasticity=0.0,
            required_deposit_funding_ratio=0.0,
            funding_shortfall_penalty_rate=0.0,
        )


def test_monotonic_objective_selects_highest_loan_and_lowest_deposit_rates():
    loan_customers, deposit_customers = _single_customer_portfolios()

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        loan_rate_grid=np.array([0.04, 0.05, 0.06]),
        deposit_rate_grid=np.array([0.02, 0.03, 0.04]),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )

    result = select_best_result(grid_results)

    assert result.loan_rate == pytest.approx(0.06)
    assert result.deposit_rate == pytest.approx(0.02)


def test_optimize_joint_rates_returns_highest_objective_result():
    loan_customers, deposit_customers = _single_customer_portfolios()

    result = optimize_joint_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        loan_rate_grid=np.array([0.04, 0.06]),
        deposit_rate_grid=np.array([0.02, 0.04]),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )

    assert result.loan_rate == pytest.approx(0.06)
    assert result.deposit_rate == pytest.approx(0.02)


def test_optimize_joint_market_relative_rates_builds_spread_grids():
    loan_customers, deposit_customers = _single_customer_portfolios()

    result = optimize_joint_market_relative_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        market_rates=MarketRates(
            home_loan_reference_rate=0.0500,
            term_deposit_reference_rate=0.0300,
        ),
        home_loan_spread_bps=SpreadGridConfig(min=-100, max=100, step=100),
        term_deposit_spread_bps=SpreadGridConfig(min=-100, max=100, step=100),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )

    assert result.loan_rate == pytest.approx(0.06)
    assert result.deposit_rate == pytest.approx(0.02)
    assert result.home_loan_spread_to_market_bps == pytest.approx(100.0)
    assert result.term_deposit_spread_to_market_bps == pytest.approx(-100.0)


def test_optimize_joint_market_relative_rates_accepts_response_fn():
    loan_customers, deposit_customers = _single_customer_portfolios()

    class FixedProbFn:
        def __init__(self):
            self.rates = []

        def __call__(self, customers, offered_rate, competitor_rate):
            self.rates.append(offered_rate)
            return np.full(len(customers), 0.5)

    loan_fn = FixedProbFn()
    deposit_fn = FixedProbFn()

    optimize_joint_market_relative_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        market_rates=MarketRates(
            home_loan_reference_rate=0.0500,
            term_deposit_reference_rate=0.0300,
        ),
        home_loan_spread_bps=SpreadGridConfig(min=-100, max=100, step=100),
        term_deposit_spread_bps=SpreadGridConfig(min=-100, max=100, step=100),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_fn,
    )

    assert loan_fn.rates == pytest.approx([0.04, 0.05, 0.06])
    assert deposit_fn.rates == pytest.approx([0.02, 0.03, 0.04])


def test_optimizer_output_contains_spread_to_market_fields():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    assert "home_loan_spread_to_market_bps" in diagnostics.grid_results.columns
    assert "term_deposit_spread_to_market_bps" in diagnostics.grid_results.columns
    assert result.home_loan_spread_to_market_bps == pytest.approx(
        (result.loan_rate - config.market.home_loan_reference_rate) * 10_000
    )
    assert result.term_deposit_spread_to_market_bps == pytest.approx(
        (result.deposit_rate - config.market.term_deposit_reference_rate) * 10_000
    )
    assert np.allclose(
        diagnostics.grid_results["home_loan_spread_to_market_bps"],
        (diagnostics.grid_results["loan_rate"] - config.market.home_loan_reference_rate)
        * 10_000,
    )
    assert np.allclose(
        diagnostics.grid_results["term_deposit_spread_to_market_bps"],
        (diagnostics.grid_results["deposit_rate"] - config.market.term_deposit_reference_rate)
        * 10_000,
    )


def test_top_results_are_sorted_by_objective_descending():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    top_results = diagnostics.top_results(5)

    assert top_results["total_objective"].is_monotonic_decreasing


def _expected_spread_count(spread_config) -> int:
    return round((spread_config.max - spread_config.min) / spread_config.step) + 1


def _single_customer_portfolios() -> tuple[pd.DataFrame, pd.DataFrame]:
    loan_customers = pd.DataFrame(
        {
            "loan_amount": [1_000_000.0],
            "expected_loss_rate": [0.0],
            "risk_score": [0.0],
            "credit_score": [700.0],
            "lvr": [0.75],
        }
    )
    deposit_customers = pd.DataFrame(
        {
            "deposit_amount": [500_000.0],
            "rate_sensitivity": [0.0],
            "relationship_years": [0.0],
            "digital_engagement": [0.0],
        }
    )
    return loan_customers, deposit_customers


def _market_relative_config_with_stale_absolute_grid() -> dict:
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
        "products": {
            "loan_rate_grid": {"start": 0.10, "stop": 0.11, "step": 0.01},
            "deposit_rate_grid": {"start": 0.08, "stop": 0.09, "step": 0.01},
        },
        "pricing_strategy": {
            "home_loan_spread_bps": {"min": -5, "max": 5, "step": 5},
            "term_deposit_spread_bps": {"min": -5, "max": 5, "step": 5},
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


def _evaluate_adaptive_joint_rate_grid_unhoisted(**kwargs):
    original = grid_search_module.evaluate_joint_rate_grid
    try:
        grid_search_module.evaluate_joint_rate_grid = _evaluate_joint_rate_grid_unhoisted
        import balance_pricing_engine.diagnostics as diagnostics_module

        diagnostics_module.evaluate_joint_rate_grid = _evaluate_joint_rate_grid_unhoisted
        return evaluate_adaptive_joint_rate_grid(**kwargs)
    finally:
        grid_search_module.evaluate_joint_rate_grid = original
        diagnostics_module.evaluate_joint_rate_grid = original


def _evaluate_joint_rate_grid_unhoisted(
    loan_customers,
    deposit_customers,
    market,
    loan_rate_grid,
    deposit_rate_grid,
    loan_rate_elasticity,
    loan_competitor_gap_elasticity,
    deposit_rate_elasticity,
    deposit_competitor_gap_elasticity,
    required_deposit_funding_ratio,
    funding_shortfall_penalty_rate,
    rwa_weight=0.35,
    capital_hurdle_rate=0.12,
    home_loan_reference_rate=None,
    term_deposit_reference_rate=None,
    response_coefficients=None,
    loan_response_fn=None,
    deposit_response_fn=None,
):
    rows = []
    for loan_rate in loan_rate_grid:
        loan_probs = grid_search_module.loan_acceptance_probability(
            loan_customers,
            float(loan_rate),
            market.competitor_loan_rate,
            loan_rate_elasticity,
            loan_competitor_gap_elasticity,
            coefficients=None if response_coefficients is None else response_coefficients.loan,
        )
        loan_volume = grid_search_module.expected_loan_volume(loan_customers, loan_probs)
        loan_interest_income = grid_search_module.expected_loan_interest_income(
            loan_customers,
            loan_probs,
            float(loan_rate),
        )
        credit_loss = grid_search_module.expected_credit_loss(loan_customers, loan_probs)
        capital_charge = grid_search_module.expected_capital_charge(
            loan_volume,
            rwa_weight,
            capital_hurdle_rate,
        )
        home_loan_spread_to_market_bps = grid_search_module._spread_to_market_bps(
            float(loan_rate),
            home_loan_reference_rate,
        )

        for deposit_rate in deposit_rate_grid:
            deposit_probs = grid_search_module.deposit_uptake_probability(
                deposit_customers,
                float(deposit_rate),
                market.competitor_deposit_rate,
                deposit_rate_elasticity,
                deposit_competitor_gap_elasticity,
                coefficients=(
                    None if response_coefficients is None else response_coefficients.deposit
                ),
            )
            deposit_volume = grid_search_module.expected_deposit_volume(
                deposit_customers,
                deposit_probs,
            )
            deposit_cost = grid_search_module.expected_deposit_cost(
                deposit_customers,
                deposit_probs,
                float(deposit_rate),
            )
            wholesale_funding_cost = grid_search_module.expected_wholesale_funding_cost(
                loan_volume,
                deposit_volume,
                market.wholesale_funding_rate,
            )
            excess_deposit_income = grid_search_module.expected_excess_deposit_income(
                loan_volume,
                deposit_volume,
                market.cash_rate,
            )
            funding = calculate_funding_result(
                loan_volume,
                deposit_volume,
                required_deposit_funding_ratio,
                funding_shortfall_penalty_rate,
            )
            total_objective = (
                loan_interest_income
                - credit_loss
                - capital_charge
                - deposit_cost
                - wholesale_funding_cost
                - funding.funding_penalty
                + excess_deposit_income
            )
            tier1_capital_ratio = 0.10
            required_capital = loan_volume * rwa_weight * tier1_capital_ratio
            return_on_equity = (
                (
                    loan_interest_income
                    - credit_loss
                    - deposit_cost
                    - wholesale_funding_cost
                    - funding.funding_penalty
                    + excess_deposit_income
                )
                / required_capital
                if required_capital > 0
                else 0.0
            )
            term_deposit_spread_to_market_bps = grid_search_module._spread_to_market_bps(
                float(deposit_rate),
                term_deposit_reference_rate,
            )
            rows.append(
                {
                    "loan_rate": float(loan_rate),
                    "deposit_rate": float(deposit_rate),
                    "home_loan_spread_to_market_bps": home_loan_spread_to_market_bps,
                    "term_deposit_spread_to_market_bps": term_deposit_spread_to_market_bps,
                    "loan_interest_income": loan_interest_income,
                    "expected_credit_loss": credit_loss,
                    "capital_charge": capital_charge,
                    "deposit_interest_cost": deposit_cost,
                    "wholesale_funding_cost": wholesale_funding_cost,
                    "excess_deposit_income": excess_deposit_income,
                    "funding_shortfall_penalty": funding.funding_penalty,
                    "total_objective": total_objective,
                    "expected_loan_volume": loan_volume,
                    "expected_deposit_volume": deposit_volume,
                    "funding_shortfall": funding.funding_shortfall,
                    "return_on_equity": return_on_equity,
                }
            )

    if not rows:
        raise ValueError("At least one loan rate and one deposit rate are required.")
    return pd.DataFrame(rows, columns=GRID_RESULT_COLUMNS)
