"""Phase 1d: evidence survives presentation without changing synthetic economics."""

from dataclasses import asdict, replace
from unittest.mock import patch

import pytest
from dashboard_harness import make_services, run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.models import (
    SCENARIO_HOME_LOAN,
    SCENARIO_TERM_DEPOSIT,
    SCENARIO_JOINT,
    ScenarioRequest,
)
from balance_pricing_engine.dashboard.scenario_text import build_decision_text
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    apply_market_strategy_to_config,
)
from balance_pricing_engine.pricing_scenario_trigger import (
    ScenarioComparison,
    ScenarioRow,
    format_scenario_block,
    run_triggered_scenarios,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from test_dashboard_services_scenarios import base_config, backend_snapshot
from test_pricing_scenario_trigger import _small_config, _snapshot, _deposit_snapshot


@pytest.mark.parametrize("snapshot", [_snapshot, _deposit_snapshot])
def test_each_row_preserves_own_diagnostics_and_economics(snapshot):
    """Dropping or copying median evidence into other rows must fail."""
    real = run_baseline_diagnostics(_small_config(), max_boundary_expansions=0)
    runs = []
    for i in range(4):
        runs.append(
            replace(
                real,
                best_result=replace(
                    real.best_result,
                    funding_shortfall=1000.0 * i,
                    expected_loan_volume=1e6 + i,
                    expected_deposit_volume=2e6 + i,
                ),
                policy_band_breached=i == 0,
                expansion_capped=i == 1,
                expansion_rounds=i,
                roe_constraint_relaxed=i == 2,
                boundary_diagnostics=replace(
                    real.boundary_diagnostics, warnings=("edge",) if i == 3 else ()
                ),
                initial_boundary_diagnostics=replace(
                    real.initial_boundary_diagnostics, warnings=("initial edge",)
                ),
            )
        )
    with patch(
        "balance_pricing_engine.pricing_scenario_trigger.run_baseline_diagnostics", side_effect=runs
    ):
        comparison = run_triggered_scenarios(_small_config(), term_months=12, snapshot=snapshot())
    assert len(comparison.rows) == 4  # reference is not a fifth strategy
    for row, diagnostics, stored in zip(comparison.rows, runs, comparison.as_dicts()):
        for name in (
            "policy_band_breached",
            "expansion_capped",
            "expansion_rounds",
            "roe_constraint_relaxed",
        ):
            assert getattr(row, name) == getattr(diagnostics, name)
            assert stored[name] == getattr(diagnostics, name)
        assert row.boundary_warning == diagnostics.boundary_diagnostics.has_warning
        assert row.initial_boundary_warning is True
        for name in ("funding_shortfall", "expected_loan_volume", "expected_deposit_volume"):
            assert getattr(row, name) == getattr(diagnostics.best_result, name)
        best = diagnostics.best_result
        rate = best.deposit_rate if snapshot is _deposit_snapshot else best.loan_rate
        assert row.our_rate == rate
        assert row.objective == best.total_objective
    assert comparison.median_reference is comparison.rows[1]
    assert comparison.model_optimum is comparison.median_reference  # legacy accessor


@pytest.mark.parametrize(
    "product,title",
    [(ProductType.HOME_LOAN, "HOME LOANS"), (ProductType.TERM_DEPOSIT, "TERM DEPOSITS")],
)
def test_alert_product_and_combined_diagnostic_warnings(product, title):
    row = ScenarioRow(
        "Match median",
        "MARKET_MEDIAN",
        0.05,
        0.0619,
        5e6,
        policy_band_breached=True,
        boundary_warning=True,
        initial_boundary_warning=True,
        expansion_capped=True,
        expansion_rounds=2,
        roe_constraint_relaxed=True,
        funding_shortfall=12345,
        expected_loan_volume=1e8,
        expected_deposit_volume=8e7,
    )
    block = format_scenario_block(ScenarioComparison(12, (row,), product_type=product))
    for text in (
        title,
        "Model-best",
        "6.19%",
        "REVIEW REQUIRED",
        "Outside policy band",
        "Grid-boundary warning",
        "Expansion cap",
        "ROE floor relaxed",
        "Funding shortfall: $12,345",
        "Median-benchmark optimum (reference)",
        "Synthetic decision support only",
        "No automatic pricing action",
    ):
        assert text in block
    assert "BLOCKED" not in block


def test_legacy_metadata_never_claims_clear_or_approval():
    row = ScenarioRow("Match leader", "MARKET_LEADER", 0.05, 0.06, 5e6)
    block = format_scenario_block(ScenarioComparison(12, (row,)))
    assert "REVIEW REQUIRED" in block and "metadata incomplete" in block
    assert "flags clear" not in block and "APPROVED" not in block


def test_alert_checked_flags_clear_is_not_approval():
    row = ScenarioRow(
        "Match median",
        "MARKET_MEDIAN",
        0.05,
        0.06,
        5e6,
        policy_band_breached=False,
        boundary_warning=False,
        initial_boundary_warning=True,
        expansion_capped=False,
        expansion_rounds=1,
        roe_constraint_relaxed=False,
        funding_shortfall=0,
        expected_loan_volume=1e8,
        expected_deposit_volume=1e8,
    )
    block = format_scenario_block(ScenarioComparison(12, (row,)))
    assert "Checked diagnostic flags clear" in block
    assert "Outside policy band" not in block and "Grid-boundary warning" not in block
    assert "APPROVED" not in block


@pytest.mark.parametrize("mode", [SCENARIO_HOME_LOAN, SCENARIO_TERM_DEPOSIT, SCENARIO_JOINT])
def test_service_rationale_names_product_direction(dual_market_factory, mode):
    services = make_services(dual_market_factory())
    request = ScenarioRequest(
        12,
        "MARKET_SECOND_LOWEST",
        None,
        0,
        None,
        mode=mode,
        deposit_term_months=6,
        deposit_strategy="MARKET_SECOND_LOWEST",
    )
    view = services.scenarios(request).value
    assert view is not None
    rationale = view.selected.rationale
    assert "MARKET_SECOND_LOWEST" not in rationale
    if mode != SCENARIO_TERM_DEPOSIT:
        assert "home loan Second-lowest rate" in rationale
    if mode != SCENARIO_HOME_LOAN:
        assert "term deposit Second-highest rate" in rationale
    if mode == SCENARIO_TERM_DEPOSIT:
        assert "Second-lowest" not in rationale


def test_service_selected_reference_and_evidence_remain_independent(market_factory):
    market = market_factory()
    services = make_services(market)
    view = services.scenarios(ScenarioRequest(12, "MARKET_LEADER", None, 0, None)).value
    config = apply_market_strategy_to_config(
        base_config(services),
        strategy=CompetitorRateStrategy.MARKET_LEADER,
        snapshot=backend_snapshot(market),
        term_months=12,
    )
    truth = run_baseline_diagnostics(config)
    assert view.selected.our_rate == truth.best_result.loan_rate
    assert view.selected.objective == truth.best_result.total_objective
    assert view.selected.our_rate != view.median_reference_rate
    for name in ("roe_constraint_relaxed", "expansion_capped", "expansion_rounds"):
        assert getattr(view.selected, name) == getattr(truth, name)
    assert view.selected.initial_boundary_warning == truth.initial_boundary_diagnostics.has_warning
    comparison = run_triggered_scenarios(
        base_config(services), term_months=12, snapshot=backend_snapshot(market)
    )
    for row, source in zip(view.rows, comparison.rows):
        assert asdict(row) == asdict(source)
    text = build_decision_text(view)
    assert f"Selected scenario home-loan model-best rate: {view.selected.our_rate:.2%}" in text
    assert (
        f"Median-benchmark optimum (reference) home-loan rate: {view.median_reference_rate:.2%}"
        in text
    )
    assert "Under current assumptions, the model optimum" not in text


@pytest.mark.parametrize("breach", [False, True])
@pytest.mark.parametrize("mode", [SCENARIO_HOME_LOAN, SCENARIO_TERM_DEPOSIT, SCENARIO_JOINT])
def test_apptest_headline_and_warnings_agree(dual_market_factory, breach, mode):
    market = dual_market_factory()
    request = ScenarioRequest(
        12,
        "MARKET_LEADER",
        None,
        0,
        None,
        mode=mode,
        deposit_term_months=6,
        deposit_strategy="MARKET_SECOND_LOWEST",
    )
    view = make_services(market).scenarios(request).value
    selected = replace(
        view.selected,
        policy_band_breached=breach,
        boundary_warning=True,
        initial_boundary_warning=True,
        expansion_capped=True,
        expansion_rounds=2,
        roe_constraint_relaxed=True,
        funding_shortfall=12345,
    )
    # Replace only the service boundary; render the real UI with real view models.
    from balance_pricing_engine.dashboard.results import Result

    with patch(
        "balance_pricing_engine.dashboard.services.DashboardServices.scenarios",
        return_value=Result.ok(replace(view, selected=selected)),
    ):
        app = run_page("scenarios", market.database, market.now)
        app.segmented_control(key="scenario_mode").set_value(mode).run()
        key = "scenario_run" if mode == SCENARIO_HOME_LOAN else f"scenario_run_{mode}"
        app.button(key=key).click().run()
    assert not app.exception
    headline = (
        "Model-best — outside policy band (review)"
        if breach
        else "Model-best rate (within policy band)"
    )
    assert any(headline in m.label for m in app.metric)
    warning = " ".join(w.value for w in app.warning)
    for text in (
        "REVIEW REQUIRED",
        "Grid-boundary warning",
        "Expansion cap",
        "ROE floor relaxed",
        "Funding shortfall",
    ):
        assert text in warning
    assert ("Outside policy band" in warning) is breach
    assert not app.error  # these conditions are WARN, never a hard BLOCK


def test_partial_selected_metadata_does_not_assert_within_policy():
    from balance_pricing_engine.dashboard.models import SelectedScenario

    selected = SelectedScenario(0.06, 5e6, 1e8, 0, 0.05, "legacy caller")
    assert "within policy band" not in selected.headline_label
    assert "unavailable" in selected.headline_label
    assert "metadata incomplete" in selected.decision_quality_summary


@pytest.mark.parametrize("snapshot", [_snapshot, _deposit_snapshot])
def test_real_trigger_rates_and_objectives_equal_direct_runs(snapshot):
    config, market = _small_config(), snapshot()
    comparison = run_triggered_scenarios(
        config, term_months=12, snapshot=market, max_boundary_expansions=0
    )
    for row, strategy, offset in zip(
        comparison.rows,
        (
            CompetitorRateStrategy.MANUAL,
            CompetitorRateStrategy.MARKET_MEDIAN,
            CompetitorRateStrategy.MARKET_LEADER,
            CompetitorRateStrategy.MARKET_LEADER,
        ),
        (0, 0, 0, -5),
    ):
        deposit = snapshot is _deposit_snapshot
        kwargs = (
            dict(
                deposit_strategy=strategy,
                deposit_snapshot=market,
                deposit_term_months=12,
                deposit_offset_bps=offset,
            )
            if deposit
            else dict(strategy=strategy, snapshot=market, term_months=12, offset_bps=offset)
        )
        diagnostics = run_baseline_diagnostics(
            apply_market_strategy_to_config(config, **kwargs), max_boundary_expansions=0
        )
        best = diagnostics.best_result
        assert row.our_rate == (best.deposit_rate if deposit else best.loan_rate)
        assert row.objective == best.total_objective
        assert row.roe_constraint_relaxed == diagnostics.roe_constraint_relaxed


def test_canonical_summary_contract_and_values_are_unchanged(tmp_path):
    import json
    from balance_pricing_engine.config import load_config

    diagnostics = run_baseline_diagnostics(load_config("configs/base.yaml"))
    diagnostics.write_artifacts(tmp_path)
    # Captured from master 68d79dd before any Phase 1d implementation.
    expected = {
        "best_deposit_rate": 0.0405,
        "best_loan_rate": 0.0619,
        "boundary_warning_flag": False,
        "capital_charge": 2141965.1047402876,
        "excess_deposit_income": 0.0,
        "expansion_capped": False,
        "expansion_rounds": 1,
        "expected_deposit_volume": 328903313.8128667,
        "expected_loan_volume": 509991691.6048304,
        "funding_shortfall": 2591285.730273068,
        "initial_boundary_warning_flag": True,
        "objective_value": 5920859.368621694,
        "policy_band_breached": True,
        "roe_constraint_relaxed": False,
    }

    actual = json.loads((tmp_path / "summary.json").read_text())
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        if isinstance(value, bool):
            assert actual[key] is value
        else:
            assert actual[key] == pytest.approx(value, rel=1e-12, abs=1e-9)


@pytest.mark.parametrize(
    "product,expected",
    [
        (ProductType.HOME_LOAN, "Second-lowest rate"),
        (ProductType.TERM_DEPOSIT, "Second-highest rate"),
    ],
)
def test_alert_second_best_strategy_follows_product(product, expected):
    row = ScenarioRow("Second best", "MARKET_SECOND_LOWEST", 0.05, 0.06, 1e6)
    text = format_scenario_block(ScenarioComparison(12, (row,), product_type=product))
    assert expected in text
    assert "MARKET_SECOND_LOWEST" not in text


@pytest.mark.parametrize("objective", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_objective_preserves_existing_block_severity(objective):
    from balance_pricing_engine.config import load_config
    from balance_pricing_engine.governance.gates import evaluate_gates, GateStatus

    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config)
    diagnostics = replace(
        diagnostics, best_result=replace(diagnostics.best_result, total_objective=objective)
    )
    gates = evaluate_gates(
        config=config, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    assert next(g.status for g in gates if g.code == "OBJECTIVE_FINITE") is GateStatus.BLOCK
    row = ScenarioRow("Match median", "MARKET_MEDIAN", 0.05, 0.06, objective)
    block = format_scenario_block(ScenarioComparison(12, (row,)))
    assert "Status: BLOCKED" in block
    assert "do not use this result" in block


def test_apptest_nonfinite_objective_is_blocked(market_factory):
    from balance_pricing_engine.dashboard.results import Result

    market = market_factory()
    view = (
        make_services(market).scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0, None)).value
    )
    view = replace(view, selected=replace(view.selected, objective=-float("inf")))
    with patch(
        "balance_pricing_engine.dashboard.services.DashboardServices.scenarios",
        return_value=Result.ok(view),
    ):
        app = run_page("scenarios", market.database, market.now)
        app.button(key="scenario_run").click().run()
    assert not app.exception
    assert any("BLOCKED" in e.value and "Objective" in e.value for e in app.error)
