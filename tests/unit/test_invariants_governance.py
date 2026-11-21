"""TEST-002: ROE-floor selection, governance-gate policy (G01-G12) and policy-band/boundary
disclosure (P01-P10). OPT-001 regression.

Expected outcomes come from independent restatements of the documented policies:

- selection: constraint-first (finite ROE >= floor), then objective, lower shortfall,
  smaller distance to the market reference rates, then enumeration order;
- gates: any BLOCK -> BLOCKED, otherwise any WARN -> REVIEW_REQUIRED, else RECOMMENDED
  (``governance/gates.py``); only a non-finite objective blocks among the gates tested here;
- policy band: the *original* grid (or spread band) is the reference, whatever the
  adaptive expansion searched. A winner outside the band is legitimate; it must be disclosed.
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from economic_strategies import (
    budget,
    deposit_customers,
    deposit_elasticities,
    loan_customers,
    loan_elasticities,
    money_close,
    rate_grids,
)
from hypothesis import given
from hypothesis import strategies as st

from balance_pricing_engine.config import (
    EngineConfig,
    ResponseCoefficientsConfig,
    load_config,
    revalidation_payload,
)
from balance_pricing_engine.diagnostics import evaluate_adaptive_joint_rate_grid
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    GateStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.governance.lineage import synthetic_model_lineage
from balance_pricing_engine.optimization.grid_search import (
    GRID_RESULT_COLUMNS,
    build_rate_grid,
    evaluate_joint_rate_grid,
    select_best_result,
    select_best_result_with_disclosure,
)
from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.market import MarketState

pytestmark = [
    pytest.mark.economic_invariants,
    pytest.mark.filterwarnings("ignore::UserWarning:balance_pricing_engine.config"),
]

BASE = load_config("configs/base.yaml")


def _lineage():
    features = {
        product: FeatureLineage(
            feature_set_name=f"{product}_response_features",
            feature_set_version="v1",
            schema_fingerprint="fingerprint",
            product=product,
            as_of="2026-11-21T00:00:00+00:00",
            row_count=100,
            source="synthetic_local_store",
        )
        for product in ("loan", "deposit")
    }
    models = {product: synthetic_model_lineage(product) for product in ("loan", "deposit")}
    return models, features


def _gates(config, diagnostics):
    models, features = _lineage()
    gates = evaluate_gates(
        config=config, diagnostics=diagnostics, model_lineage=models, feature_lineage=features
    )
    return {gate.code: gate for gate in gates}, decision_status_from_gates(gates)


def _with_floor(config: EngineConfig, floor) -> EngineConfig:
    return config.model_copy(
        update={"balance_sheet": {**config.balance_sheet.model_dump(), "min_return_on_equity": floor}}
    )


# ------------------------------------------ G01-G03, G05 selection on generated grids

_finite = st.floats(-1e9, 1e9, allow_nan=False, allow_infinity=False)


@st.composite
def _grid_and_floor(draw):
    """Rows with unique rate pairs, coarse objectives (ties happen), and finite or NaN ROE."""
    n = draw(st.integers(1, 12))
    rows = []
    for i in range(n):
        rows.append(
            {
                "loan_rate": 0.04 + i * 0.001,
                "deposit_rate": 0.03,
                "home_loan_spread_to_market_bps": draw(st.integers(-80, 80)) * 1.0,
                "term_deposit_spread_to_market_bps": draw(st.integers(-80, 80)) * 1.0,
                "total_objective": draw(st.integers(-5, 5)) * 1e5,
                "funding_shortfall": draw(st.integers(0, 3)) * 1e6,
                "return_on_equity": draw(st.one_of(st.just(float("nan")),
                                                   st.integers(-5, 5).map(lambda x: x / 10))),
            }
        )
    frame = pd.DataFrame(rows)
    for column in GRID_RESULT_COLUMNS:
        if column not in frame:
            frame[column] = 0.0
    floor = draw(st.one_of(st.none(), st.integers(0, 6).map(lambda x: x / 10)))
    return frame[GRID_RESULT_COLUMNS], floor


def _reference_winner(grid: pd.DataFrame, floor):
    roe = grid["return_on_equity"].to_numpy()
    qualifying = np.isfinite(roe) & (roe >= floor) if floor is not None else np.ones(len(grid), bool)
    relaxed = floor is not None and not qualifying.any()
    pool = [i for i in range(len(grid)) if qualifying[i]] if qualifying.any() else range(len(grid))

    def key(i):
        row = grid.iloc[i]
        distance = abs(row.home_loan_spread_to_market_bps) + abs(row.term_deposit_spread_to_market_bps)
        if math.isnan(distance):
            distance = math.inf  # A row without both market spreads sorts last on distance.
        return (-row.total_objective, row.funding_shortfall, distance, i)

    return min(pool, key=key), relaxed


@given(case=_grid_and_floor())
@budget(300)
def test_g01_g02_g03_g05_selection_follows_constraint_first_ranking(case):
    grid, floor = case
    expected_index, expected_relaxed = _reference_winner(grid, floor)
    selection = select_best_result_with_disclosure(grid, min_return_on_equity=floor)
    winner = grid.iloc[expected_index]
    assert selection.best_result.loan_rate == winner.loan_rate  # G01 / G02
    assert selection.roe_constraint_relaxed is expected_relaxed  # G03 / G05
    if floor is not None and not expected_relaxed:
        assert selection.selected_return_on_equity >= floor
    roe = winner.return_on_equity
    assert selection.selected_return_on_equity == (roe if math.isfinite(roe) else None)
    # select_best_result is the same winner without the disclosure.
    assert select_best_result(grid, min_return_on_equity=floor) == selection.best_result


# ------------------------------------------ G02-G05, G11 through the real engine


def _small_engine_config(seed: int, loan_grid, deposit_grid, ratio: float, penalty: float):
    raw = revalidation_payload(BASE)
    raw.update(
        random_seed=seed,
        simulation={"n_loan_customers": 150, "n_deposit_customers": 150},
        pricing_strategy=None,
        products={
            "loan_rate_grid": dict(zip(("start", "stop", "step"), loan_grid)),
            "deposit_rate_grid": dict(zip(("start", "stop", "step"), deposit_grid)),
        },
    )
    raw["balance_sheet"].update(
        required_deposit_funding_ratio=ratio, funding_shortfall_penalty_rate=penalty
    )
    return EngineConfig.model_validate(raw)


@st.composite
def _engine_case(draw):
    config = _small_engine_config(
        draw(st.integers(0, 1_000)),
        draw(rate_grids(min_points=2, max_points=4, lo=0.04)),
        draw(rate_grids(min_points=2, max_points=4, lo=0.02)),
        draw(st.floats(0.0, 1.0)),
        draw(st.floats(0.0, 0.3)),
    )
    return config, draw(st.sampled_from(["observed", "infeasible"])), draw(st.floats(0.0, 1.0))


@given(case=_engine_case())
@budget(20)
def test_g02_to_g05_g11_engine_roe_floor_is_met_or_disclosed(case):
    """OPT-001 regression: on the final evaluated grid, a reachable floor is met and not
    reported relaxed; an unreachable one is reported relaxed and gates to review."""
    config, kind, quantile = case
    unconstrained = run_baseline_diagnostics(config, max_boundary_expansions=1)
    roe = np.sort(unconstrained.grid_results["return_on_equity"].to_numpy())
    floor = max(0.0, float(roe[int(quantile * (len(roe) - 1))]))
    if kind == "infeasible":
        floor = max(0.0, float(roe[-1])) + 1.0
    constrained_config = _with_floor(config, floor)
    diagnostics = run_baseline_diagnostics(constrained_config, max_boundary_expansions=1)

    final_roe = diagnostics.grid_results["return_on_equity"].to_numpy()
    reachable = bool(np.any(np.isfinite(final_roe) & (final_roe >= floor)))
    assert diagnostics.roe_constraint_relaxed is (not reachable)
    gates, status = _gates(constrained_config, diagnostics)
    roe_gate = gates["ROE_FLOOR_MET"]
    if reachable:
        assert diagnostics.selected_return_on_equity >= floor  # G02
        assert roe_gate.status is GateStatus.PASS  # G05: not falsely relaxed
    else:
        assert roe_gate.status is GateStatus.WARN  # G04
        assert status is not DecisionStatus.RECOMMENDED
    # G11: gate evidence is the scenario's own numbers.
    assert roe_gate.evidence["min_return_on_equity"] == floor
    assert roe_gate.evidence["roe_constraint_relaxed"] is diagnostics.roe_constraint_relaxed
    best = diagnostics.best_result
    winner_rows = diagnostics.grid_results[
        np.isclose(diagnostics.grid_results["loan_rate"], best.loan_rate)
        & np.isclose(diagnostics.grid_results["deposit_rate"], best.deposit_rate)
    ]
    assert money_close(float(winner_rows["return_on_equity"].iloc[0]),
                       diagnostics.selected_return_on_equity)
    assert gates["OBJECTIVE_POSITIVE"].evidence["objective_value"] == best.total_objective


# ------------------------------------------ G04, G06-G12 gate policy


_objectives = st.one_of(
    st.sampled_from([0.0, -1.0, float("nan"), float("inf"), float("-inf")]), _finite
)
_volumes = st.one_of(
    st.sampled_from([0.0, -1.0, float("nan"), float("inf"), None]), st.floats(1.0, 1e10)
)
_roe_values = st.one_of(st.sampled_from([None, float("nan"), float("inf")]), st.floats(-1, 2))


@st.composite
def _diagnostics_case(draw):
    best = SimpleNamespace(
        total_objective=draw(_objectives),
        expected_loan_volume=draw(_volumes),
        expected_deposit_volume=draw(_volumes),
        funding_shortfall=draw(st.sampled_from([0.0, 1.0, 5e6])),
    )
    warnings_flag = draw(st.booleans())
    diagnostics = SimpleNamespace(
        best_result=best,
        boundary_diagnostics=SimpleNamespace(
            has_warning=warnings_flag, warnings=("edge",) if warnings_flag else ()
        ),
        expansion_capped=draw(st.booleans()),
        expansion_rounds=draw(st.integers(0, 3)),
        policy_band_breached=draw(st.booleans()),
        roe_constraint_relaxed=draw(st.sampled_from([True, False, None])),
        selected_return_on_equity=draw(_roe_values),
    )
    floor = draw(st.one_of(st.none(), st.floats(0.0, 1.0)))
    return diagnostics, floor


def _finite(value):
    return value is not None and not isinstance(value, bool) and math.isfinite(value)


def _expected_statuses(diagnostics, floor):
    best = diagnostics.best_result
    objective = best.total_objective
    volumes = (best.expected_loan_volume, best.expected_deposit_volume)
    selected = diagnostics.selected_return_on_equity
    roe_ok = floor is None or (
        diagnostics.roe_constraint_relaxed is False and _finite(selected) and selected >= floor
    )
    return {
        "OBJECTIVE_FINITE": "BLOCK" if not math.isfinite(objective) else "PASS",
        "OBJECTIVE_POSITIVE": "PASS" if math.isfinite(objective) and objective > 0 else "WARN",
        "VOLUME_NONZERO": "PASS" if all(_finite(v) and v > 0 for v in volumes) else "WARN",
        "ROE_FLOOR_MET": "PASS" if roe_ok else "WARN",
        "BOUNDARY_WARNING": "WARN" if diagnostics.boundary_diagnostics.has_warning else "PASS",
        "EXPANSION_CAPPED": "WARN" if diagnostics.expansion_capped else "PASS",
        "POLICY_BAND_BREACH": "WARN" if diagnostics.policy_band_breached else "PASS",
        "FUNDING_SHORTFALL": "WARN" if best.funding_shortfall > 0 else "PASS",
    }


@given(case=_diagnostics_case())
@budget(300)
def test_g04_g06_to_g12_gate_statuses_follow_the_documented_policy(case):
    diagnostics, floor = case
    config = _with_floor(BASE, floor)
    gates, status = _gates(config, diagnostics)
    expected = _expected_statuses(diagnostics, floor)
    for code, expected_status in expected.items():
        assert gates[code].status.value == expected_status, code
    # G10/G12: BLOCK dominates WARN; RECOMMENDED only with every gate passing.
    statuses = [g.status for g in gates.values()]
    if GateStatus.BLOCK in statuses:
        assert status is DecisionStatus.BLOCKED
    elif GateStatus.WARN in statuses:
        assert status is DecisionStatus.REVIEW_REQUIRED
    else:
        assert status is DecisionStatus.RECOMMENDED
    # Warning-level conditions never escalate to BLOCK (no unapproved policy change).
    blocking = {code for code, gate in gates.items() if gate.status is GateStatus.BLOCK}
    assert blocking <= {"OBJECTIVE_FINITE"}
    # G11: evidence carries the scenario's own numbers.
    volume_evidence = gates["VOLUME_NONZERO"].evidence
    for product, value in (("loan", diagnostics.best_result.expected_loan_volume),
                           ("deposit", diagnostics.best_result.expected_deposit_volume)):
        recorded = volume_evidence[f"expected_{product}_volume"]
        assert recorded == (float(value) if _finite(value) else None)
    selected = diagnostics.selected_return_on_equity
    assert gates["ROE_FLOOR_MET"].evidence["selected_return_on_equity"] == (
        float(selected) if _finite(selected) else None
    )


# ------------------------------------------ P01-P09 adaptive grid disclosure


@st.composite
def _adaptive_problem(draw):
    loans = draw(loan_customers(min_size=3, max_size=12))
    deposits = draw(deposit_customers(min_size=3, max_size=12))
    competitor_loan = draw(st.integers(300, 800)) / 10_000
    competitor_deposit = draw(st.integers(100, 600)) / 10_000
    use_reference = draw(st.booleans())
    market = MarketState(
        cash_rate=draw(st.floats(0.0, 0.05)),
        wholesale_funding_spread=draw(st.floats(0.0, 0.02)),
        competitor_loan_rate=competitor_loan,
        competitor_deposit_rate=competitor_deposit,
        home_loan_reference_rate=competitor_loan if use_reference else None,
        term_deposit_reference_rate=competitor_deposit if use_reference else None,
    )
    loan_grid = build_rate_grid(*draw(rate_grids(min_points=2, max_points=4, lo=0.02)))
    deposit_grid = build_rate_grid(*draw(rate_grids(min_points=2, max_points=4, lo=0.005)))
    return {
        "loan_customers": loans,
        "deposit_customers": deposits,
        "market": market,
        "loan_rate_grid": loan_grid,
        "deposit_rate_grid": deposit_grid,
        "loan_rate_elasticity": draw(loan_elasticities),
        "loan_competitor_gap_elasticity": draw(loan_elasticities),
        "deposit_rate_elasticity": draw(deposit_elasticities),
        "deposit_competitor_gap_elasticity": draw(deposit_elasticities),
        "required_deposit_funding_ratio": draw(st.floats(0.0, 1.0)),
        "funding_shortfall_penalty_rate": draw(st.floats(0.0, 0.3)),
        "home_loan_reference_rate": market.home_loan_reference_rate,
        "term_deposit_reference_rate": market.term_deposit_reference_rate,
        "response_coefficients": ResponseCoefficientsConfig(),
    }


def _outside(rate: float, grid: np.ndarray) -> bool:
    lo, hi = float(np.min(grid)), float(np.max(grid))
    tolerance = 1e-9
    return rate < lo - tolerance or rate > hi + tolerance


def _on_edge(rate: float, grid: np.ndarray) -> bool:
    return bool(np.isclose(rate, np.min(grid)) or np.isclose(rate, np.max(grid)))


@given(
    problem=_adaptive_problem(),
    max_expansions=st.integers(0, 2),
    floor_kind=st.sampled_from(["none", "highest_initial_roe", "above_initial_roe"]),
)
@budget(60)
def test_p01_to_p09_adaptive_grid_discloses_band_breach_boundary_and_cap(
    problem, max_expansions, floor_kind
):
    original_loan, original_deposit = problem["loan_rate_grid"], problem["deposit_rate_grid"]
    original_grid = evaluate_joint_rate_grid(**problem)
    highest_roe = float(original_grid["return_on_equity"].max())
    floor = None
    if floor_kind == "highest_initial_roe":
        floor = max(0.0, highest_roe)
    elif floor_kind == "above_initial_roe":
        floor = max(0.0, highest_roe) + 1.0
    result = evaluate_adaptive_joint_rate_grid(
        **problem, max_boundary_expansions=max_expansions, min_return_on_equity=floor
    )
    best = result.best_result
    final_loan = np.unique(result.grid_results["loan_rate"].to_numpy())
    final_deposit = np.unique(result.grid_results["deposit_rate"].to_numpy())

    # P01/P02/P05/P06: breach is judged against the ORIGINAL band, never the expanded grid.
    expected_breach = _outside(best.loan_rate, original_loan) or _outside(
        best.deposit_rate, original_deposit
    )
    assert result.policy_band_breached is expected_breach
    # P03: a winner on an edge of the FINAL grid carries a boundary warning, and only then.
    on_edge = _on_edge(best.loan_rate, final_loan) or _on_edge(best.deposit_rate, final_deposit)
    assert result.boundary_diagnostics.has_warning is on_edge
    # P04: the cap is disclosed exactly when rounds ran out with a boundary still hit.
    assert result.expansion_rounds <= max_expansions
    assert result.expansion_capped is (on_edge and result.expansion_rounds >= max_expansions)
    # P08: expanded grids keep every original candidate as a benchmark.
    for rate in original_loan:
        assert np.isclose(final_loan, rate).any()
    for rate in original_deposit:
        assert np.isclose(final_deposit, rate).any()
    # P09: expansion cannot displace the incumbent with a lower-ranked candidate.
    original_index, original_relaxed = _reference_winner(original_grid, floor)
    original_best = original_grid.iloc[original_index]
    initial_edge = _on_edge(original_best.loan_rate, original_loan) or _on_edge(
        original_best.deposit_rate, original_deposit
    )
    assert result.initial_boundary_diagnostics.has_warning is initial_edge
    selected_roe = result.selected_return_on_equity
    meets_floor = floor is None or (selected_roe is not None and selected_roe >= floor)
    final_index, final_relaxed = _reference_winner(result.grid_results, floor)
    final_best = result.grid_results.iloc[final_index]
    assert (best.loan_rate, best.deposit_rate) == (final_best.loan_rate, final_best.deposit_rate)
    assert result.roe_constraint_relaxed is final_relaxed
    assert meets_floor is (not final_relaxed)
    assert original_relaxed or meets_floor
    if final_relaxed == original_relaxed:
        assert best.total_objective >= original_best.total_objective or money_close(
            best.total_objective, original_best.total_objective
        )
    # P07: governance status reflects every disclosed warning.
    gates, status = _gates(_with_floor(BASE, floor), result)
    assert (gates["POLICY_BAND_BREACH"].status is GateStatus.WARN) is expected_breach
    assert (gates["BOUNDARY_WARNING"].status is GateStatus.WARN) is on_edge
    assert (gates["EXPANSION_CAPPED"].status is GateStatus.WARN) is result.expansion_capped
    if expected_breach or on_edge:
        assert status is not DecisionStatus.RECOMMENDED


# ------------------------------------------ P10 summary.json serialisation


@given(
    seed=st.integers(0, 1_000),
    loan_grid=rate_grids(min_points=2, max_points=3, lo=0.03),
    deposit_grid=rate_grids(min_points=2, max_points=3, lo=0.01),
    max_expansions=st.integers(0, 2),
    relax_floor=st.booleans(),
)
@budget(15)
def test_p10_band_and_boundary_evidence_survive_summary_json(
    seed, loan_grid, deposit_grid, max_expansions, relax_floor
):
    config = _small_engine_config(seed, loan_grid, deposit_grid, 0.65, 0.018)
    if relax_floor:
        # Deliberately unreachable for the small synthetic portfolios and grids above.
        config = _with_floor(config, 1_000_000.0)
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=max_expansions)
    with tempfile.TemporaryDirectory() as directory:
        diagnostics.write_artifacts(directory)
        summary = json.loads((Path(directory) / "summary.json").read_text())
    best = diagnostics.best_result
    original_loan = build_rate_grid(*loan_grid)
    original_deposit = build_rate_grid(*deposit_grid)
    # Independent recomputation from the serialised rates and the configured band.
    assert summary["policy_band_breached"] is (
        _outside(summary["best_loan_rate"], original_loan)
        or _outside(summary["best_deposit_rate"], original_deposit)
    )
    assert summary["best_loan_rate"] == best.loan_rate
    assert summary["best_deposit_rate"] == best.deposit_rate
    assert summary["boundary_warning_flag"] is diagnostics.boundary_diagnostics.has_warning
    assert summary["initial_boundary_warning_flag"] is (
        diagnostics.initial_boundary_diagnostics.has_warning
    )
    assert summary["expansion_rounds"] == diagnostics.expansion_rounds
    assert summary["expansion_capped"] is diagnostics.expansion_capped
    assert diagnostics.roe_constraint_relaxed is relax_floor
    assert summary["roe_constraint_relaxed"] is relax_floor


def test_p_canonical_config_band_position_is_disclosed():
    """Roadmap 3c: the canonical config is inside its policy band, or the breach is attached
    as a review reason. Today the base config's home-loan winner sits outside the +/-50 bp
    band (FIN-001/FIN-003 calibration, Phase 4); this pins that it is disclosed, not hidden."""
    diagnostics = run_baseline_diagnostics(BASE)
    band = BASE.pricing_strategy
    loan_spread = diagnostics.best_result.home_loan_spread_to_market_bps
    deposit_spread = diagnostics.best_result.term_deposit_spread_to_market_bps
    outside = not (
        band.home_loan_spread_bps.min - 1e-6 <= loan_spread <= band.home_loan_spread_bps.max + 1e-6
        and band.term_deposit_spread_bps.min - 1e-6
        <= deposit_spread
        <= band.term_deposit_spread_bps.max + 1e-6
    )
    assert diagnostics.policy_band_breached is outside
    gates, status = _gates(BASE, diagnostics)
    if outside:
        assert gates["POLICY_BAND_BREACH"].status is GateStatus.WARN
        assert gates["POLICY_BAND_BREACH"].remediation
        assert status is not DecisionStatus.RECOMMENDED
