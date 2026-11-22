"""FIN-003 (Phase 4d): opt-in FTP simulation, optimiser parity, governance and composition."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
from itertools import product
import json
from pathlib import Path

import numpy as np
import pytest

from balance_pricing_engine.application.contracts import PricingScenarioRequest
from balance_pricing_engine.config import (
    DepositTierConfig,
    EngineConfig,
    GridConfig,
    LoanTierConfig,
    SegmentedPricingConfig,
    load_config,
)
from balance_pricing_engine.config_identity import effective_config_hash
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    GateStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.pricing.credit_loss import illustrative_profile
from balance_pricing_engine.pricing.deposit_supply import (
    ConcentrationMode,
    DepositConcentrationPolicy,
    DepositFlow,
    DepositMarketProfile,
    DepositOpportunitySegment,
    DepositSupply,
)
from balance_pricing_engine.pricing.funds_transfer_pricing import (
    FTPLiquidityBucket,
    ftp_market,
    illustrative_curve,
    illustrative_funding_profile,
    scalar_equivalent_curve,
)
from balance_pricing_engine.pricing.profit import TIER1_CAPITAL_RATIO
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.ftp_simulation import (
    FTPSimulationConfig,
    run_ftp_simulation,
)
from balance_pricing_engine.scenarios.runner import (
    _evaluate_pricing_population,
    _generate_pricing_population,
    run_baseline_diagnostics,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)

AS_OF = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
VALUATION = AS_OF + timedelta(hours=16)
LEGACY_OFFER = (0.0619, 0.0405)
FUNDING_LINES = {"wholesale_funding_cost", "excess_deposit_income", "total_objective"}
SRC = Path(__file__).resolve().parents[2] / "src" / "balance_pricing_engine"


@pytest.fixture(scope="module")
def engine() -> EngineConfig:
    return load_config("configs/base.yaml")


@pytest.fixture
def request_(engine) -> FTPSimulationConfig:
    # Wholesale funding at 36 months so the external rate differs from the legacy scalar.
    return FTPSimulationConfig(
        engine_config=engine,
        curve=illustrative_curve(AS_OF),
        funding_profile=illustrative_funding_profile(wholesale_funding_tenor_months=36),
        valuation_time=VALUATION,
        max_boundary_expansions=0,
    )


def _grid(values):
    return GridConfig(start=values[0], stop=values[-1], step=values[1] - values[0])


def _supply_profile(**update) -> DepositMarketProfile:
    data = dict(
        profile_id="ftp-compat",
        version="1",
        opportunity_basis="FLOW",
        review_notes="Illustrative test only.",
        segments=(DepositOpportunitySegment(segment_id="external", opportunity_nzd=500e6),),
    )
    data.update(update)
    return DepositMarketProfile(**data)


# -- Canonical preservation (T53) ---------------------------------------------------------


def test_t53_canonical_baseline_unchanged(engine):
    best = run_baseline_diagnostics(engine).best_result
    assert (best.loan_rate, best.deposit_rate) == LEGACY_OFFER
    assert best.total_objective == pytest.approx(5_920_859.368621694, abs=1e-6)
    assert effective_config_hash(engine).startswith("cf725ba5")


def test_scalar_equivalent_curve_reproduces_the_canonical_result_exactly(engine):
    canonical = run_baseline_diagnostics(engine)
    equivalent = run_ftp_simulation(
        FTPSimulationConfig(
            engine_config=engine,
            curve=scalar_equivalent_curve(
                cash_rate=engine.market.cash_rate,
                wholesale_funding_spread=engine.market.wholesale_funding_spread,
                as_of=AS_OF,
            ),
            funding_profile=illustrative_funding_profile(),
            valuation_time=VALUATION,
        )
    )
    assert asdict(equivalent.diagnostics.best_result) == asdict(canonical.best_result)
    assert equivalent.diagnostics.grid_results.equals(canonical.grid_results)
    assert equivalent.roe_bridge.model_return_on_equity == canonical.selected_return_on_equity
    assert equivalent.diagnostics.policy_band_breached is canonical.policy_band_breached
    assert equivalent.governance_status is DecisionStatus.REVIEW_REQUIRED


# -- Optimisation (T36-T41) ---------------------------------------------------------------


def test_t36_t40_joint_search_reconciles_on_the_full_objective(request_):
    result = run_ftp_simulation(request_)
    rates, grid = result.rates, result.diagnostics.grid_results
    deficit = np.maximum(grid["expected_loan_volume"] - grid["expected_deposit_volume"], 0)
    surplus = np.maximum(grid["expected_deposit_volume"] - grid["expected_loan_volume"], 0)
    np.testing.assert_allclose(
        grid["wholesale_funding_cost"], deficit * rates.wholesale_funding_rate
    )
    np.testing.assert_allclose(
        grid["excess_deposit_income"], surplus * rates.surplus_reinvestment_rate
    )
    recomputed = (
        grid["loan_interest_income"]
        - grid["expected_credit_loss"]
        - grid["capital_charge"]
        - grid["deposit_interest_cost"]
        - grid["wholesale_funding_cost"]
        - grid["funding_shortfall_penalty"]
        + grid["excess_deposit_income"]
    )
    np.testing.assert_allclose(grid["total_objective"], recomputed, rtol=1e-12, atol=1e-6)
    best = result.diagnostics.best_result
    assert best.total_objective == grid["total_objective"].max()
    assert result.diagnostics.winner_decomposition().reconciled_total_objective == pytest.approx(
        best.total_objective
    )
    assert result.ledger.reconciliation_difference == pytest.approx(0, abs=1e-6)
    assert result.ledger.internal_transfer_net == 0.0
    assert result.roe_bridge.model_objective == pytest.approx(best.total_objective, abs=1e-6)


def test_t37_one_tier_segmented_matches_joint_under_ftp(request_):
    loan_rates, deposit_rates = build_configured_rate_grids(request_.engine_config)
    segmented = request_.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[LoanTierConfig(name="all", rate_grid=_grid(loan_rates))],
                deposit_tiers=[DepositTierConfig(name="all", rate_grid=_grid(deposit_rates))],
            ),
        }
    )
    joint = run_ftp_simulation(request_)
    tiered = run_ftp_simulation(request_.model_copy(update={"engine_config": segmented}))
    joint_best, tier_best = joint.diagnostics.best_result, tiered.diagnostics.best_result
    assert (tier_best.loan_rate, tier_best.deposit_rate) == (
        joint_best.loan_rate,
        joint_best.deposit_rate,
    )
    for name in (
        "total_objective",
        "wholesale_funding_cost",
        "excess_deposit_income",
        "funding_shortfall_penalty",
        "capital_charge",
    ):
        assert getattr(tier_best, name) == pytest.approx(getattr(joint_best, name), rel=1e-12)
    assert tiered.ledger.to_dict() == pytest.approx(joint.ledger.to_dict(), rel=1e-9, abs=1e-6)
    assert tiered.roe_bridge.economic_return_on_equity == pytest.approx(
        joint.roe_bridge.economic_return_on_equity
    )
    assert "FTP_SIMULATION_ONLY" in {gate.code for gate in tiered.gates}


def test_t38_small_multi_tier_exact_search_matches_independent_enumeration(request_):
    low_loan, high_loan = (0.058, 0.062), (0.060, 0.064)
    low_deposit, high_deposit = (0.037, 0.042), (0.038, 0.043)
    engine = request_.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[
                    LoanTierConfig(name="low", lvr_max=0.8, rate_grid=_grid(low_loan)),
                    LoanTierConfig(name="high", rate_grid=_grid(high_loan)),
                ],
                deposit_tiers=[
                    DepositTierConfig(
                        name="low", balance_max=100_000, rate_grid=_grid(low_deposit)
                    ),
                    DepositTierConfig(name="high", rate_grid=_grid(high_deposit)),
                ],
            ),
        }
    )
    result = run_ftp_simulation(request_.model_copy(update={"engine_config": engine}))
    rates = result.rates
    loans, deposits = _generate_pricing_population(engine)
    loan_low = loans["lvr"] < 0.8
    deposit_low = deposits["deposit_amount"] < 100_000
    market, response, sheet = engine.market, engine.response, engine.balance_sheet
    candidates = []
    for offer in product(low_loan, high_loan, low_deposit, high_deposit):
        loan_volume = loan_income = credit_loss = deposit_volume = deposit_cost = 0.0
        for rate, mask in zip(offer[:2], (loan_low, ~loan_low)):
            cohort = loans.loc[mask]
            booked = (
                loan_acceptance_probability(
                    cohort,
                    rate,
                    market.competitor_loan_rate,
                    response.loan_rate_elasticity,
                    response.loan_competitor_gap_elasticity,
                    coefficients=response.coefficients.loan,
                )
                * cohort["loan_amount"]
            )
            loan_volume += booked.sum()
            loan_income += booked.sum() * rate
            credit_loss += (booked * cohort["expected_loss_rate"]).sum()
        for rate, mask in zip(offer[2:], (deposit_low, ~deposit_low)):
            cohort = deposits.loc[mask]
            booked = (
                deposit_uptake_probability(
                    cohort,
                    rate,
                    market.competitor_deposit_rate,
                    response.deposit_rate_elasticity,
                    response.deposit_competitor_gap_elasticity,
                    coefficients=response.coefficients.deposit,
                )
                * cohort["deposit_amount"]
            )
            deposit_volume += booked.sum()
            deposit_cost += booked.sum() * rate
        objective = (
            loan_income
            - credit_loss
            - deposit_cost
            - loan_volume * sheet.loan_rwa_weight * TIER1_CAPITAL_RATIO * sheet.capital_hurdle_rate
            - max(loan_volume - deposit_volume, 0) * rates.wholesale_funding_rate
            + max(deposit_volume - loan_volume, 0) * rates.surplus_reinvestment_rate
            - max(loan_volume * sheet.required_deposit_funding_ratio - deposit_volume, 0)
            * sheet.funding_shortfall_penalty_rate
        )
        candidates.append((objective, offer))
    tiers = result.diagnostics.segmented_tier_rates
    selected = tuple(
        tiers[side][tier] for side in ("loan_tiers", "deposit_tiers") for tier in ("low", "high")
    )
    best_objective, best_offer = max(candidates)
    assert selected == best_offer
    assert result.diagnostics.best_result.total_objective == pytest.approx(best_objective)
    assert result.diagnostics.segmented_solver["optimality_proven"] is True


def test_t39_fixed_offer_curve_change_moves_only_funding_lines(request_):
    fixed = request_.model_copy(update={"fixed_rates": LEGACY_OFFER})
    base = run_ftp_simulation(fixed)
    steep = run_ftp_simulation(
        fixed.model_copy(
            update={"curve": illustrative_curve(AS_OF, slope_scale=2.0, premium_scale=1.5)}
        )
    )
    before, after = base.diagnostics.best_result, steep.diagnostics.best_result
    for name in (field.name for field in fields(before)):
        if name not in FUNDING_LINES:
            assert getattr(after, name) == getattr(before, name), name
    assert after.wholesale_funding_cost > before.wholesale_funding_cost
    delta = (after.wholesale_funding_cost - before.wholesale_funding_cost) - (
        after.excess_deposit_income - before.excess_deposit_income
    )
    assert before.total_objective - after.total_objective == pytest.approx(delta)
    assert steep.ledger.loan_ftp_charge != base.ledger.loan_ftp_charge
    assert steep.roe_bridge.capital_charge == base.roe_bridge.capital_charge


def test_t41_boundary_and_policy_band_flags_follow_the_ftp_search(request_):
    narrow = request_.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "products": {
                "loan_rate_grid": GridConfig(start=0.0520, stop=0.0540, step=0.001),
                "deposit_rate_grid": GridConfig(start=0.0380, stop=0.0400, step=0.001),
            },
        }
    )
    result = run_ftp_simulation(request_.model_copy(update={"engine_config": narrow}))
    diagnostics = result.diagnostics
    best = diagnostics.best_result
    at_edge = best.loan_rate in (0.052, 0.054) or best.deposit_rate in (0.038, 0.040)
    assert diagnostics.boundary_diagnostics.has_warning is at_edge
    statuses = {gate.code: gate.status for gate in result.gates}
    assert statuses["BOUNDARY_WARNING"] is (GateStatus.WARN if at_edge else GateStatus.PASS)
    assert result.report()["boundary_warning_flag"] is at_edge


# -- Fixed-offer funding cases ---------------------------------------------------------


def test_surplus_case_credits_the_term_reinvestment_rate(request_):
    result = run_ftp_simulation(request_.model_copy(update={"fixed_rates": (0.0750, 0.0450)}))
    ledger = result.ledger
    assert ledger.surplus_funds > 0 and ledger.wholesale_deficit == 0
    assert ledger.surplus_investment_income == pytest.approx(
        ledger.surplus_funds * result.rates.surplus_reinvestment_rate
    )
    assert (
        result.rates.surplus_reinvestment_rate == illustrative_curve(AS_OF).quote(3).reference_rate
    )


# -- Governance and lineage (T48-T52) -------------------------------------------------------


def test_t48_internal_seam_cannot_skip_the_simulation_gates(engine):
    loans, deposits = _generate_pricing_population(engine)
    rates = illustrative_funding_profile().rates(illustrative_curve(AS_OF))
    market = ftp_market(MarketState(**engine.market.model_dump()), rates)
    supply = DepositSupply(_supply_profile(), deposits)
    diagnostics = _evaluate_pricing_population(
        engine, loans, deposits, max_boundary_expansions=0, deposit_supply=supply, market=market
    )
    assert diagnostics.ftp_market is market
    assert diagnostics.deposit_supply_profile == supply.profile
    gates = evaluate_gates(
        config=engine, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    codes = {gate.code: gate for gate in gates}
    assert codes["FTP_SIMULATION_ONLY"].status is GateStatus.WARN
    assert codes["FTP_SIMULATION_ONLY"].evidence["curve_key"] == rates.curve_key
    assert codes["DEPOSIT_SUPPLY_SIMULATION_ONLY"].status is GateStatus.WARN
    assert decision_status_from_gates(gates) is not DecisionStatus.RECOMMENDED


def test_t49_block_gates_override_the_ftp_warning(request_):
    result = run_ftp_simulation(request_)
    diagnostics = replace(
        result.diagnostics,
        best_result=replace(result.diagnostics.best_result, total_objective=float("nan")),
    )
    gates = evaluate_gates(
        config=request_.engine_config, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    assert "FTP_SIMULATION_ONLY" in {gate.code for gate in gates}
    assert decision_status_from_gates(gates) is DecisionStatus.BLOCKED


def test_t50_governed_schema_cannot_carry_ftp_inputs(request_):
    governed = request_.engine_config
    payload = governed.model_dump(mode="json")
    payload["ftp"] = request_.curve.model_dump(mode="json")
    smuggled = EngineConfig.model_validate(payload)
    assert "ftp" not in EngineConfig.model_fields
    assert "ftp" not in smuggled.model_dump()
    assert effective_config_hash(smuggled) == effective_config_hash(governed)
    assert not {name for name in (f.name for f in fields(PricingScenarioRequest)) if "ftp" in name}
    assert run_baseline_diagnostics(smuggled).ftp_market is None
    with pytest.raises(ValueError, match="stale"):
        request_.model_copy(update={"valuation_time": AS_OF + timedelta(days=30)})
    with pytest.raises(ValueError, match="look-ahead"):
        request_.model_copy(update={"valuation_time": AS_OF - timedelta(days=1)})


def test_t51_effective_hash_changes_with_curve_and_conventions(request_):
    base = run_ftp_simulation(request_).config_hash
    variants = (
        {"curve": illustrative_curve(AS_OF, premium_scale=1.01)},
        {"curve": illustrative_curve(AS_OF, version="1.0.1")},
        {"valuation_time": VALUATION + timedelta(hours=1)},
        {
            "funding_profile": request_.funding_profile.model_copy(
                update={"loan_repricing_tenor_months": 24}
            )
        },
        {
            "funding_profile": request_.funding_profile.model_copy(
                update={"surplus_reinvestment_spread": -0.001}
            )
        },
    )
    hashes = {
        run_ftp_simulation(request_.model_copy(update=update)).config_hash for update in variants
    }
    assert base not in hashes and len(hashes) == len(variants)


def test_t52_recorded_evidence_resists_caller_mutation(engine):
    caller_engine = engine.model_copy()
    config = FTPSimulationConfig(
        engine_config=caller_engine,
        curve=illustrative_curve(AS_OF),
        funding_profile=illustrative_funding_profile(),
        valuation_time=VALUATION,
        max_boundary_expansions=0,
    )
    result = run_ftp_simulation(config)
    recorded = result.effective_config_json
    caller_engine.market.cash_rate = 0.09
    config.engine_config.market.cash_rate = 0.08
    view = result.effective_config
    view.engine_config.market.cash_rate = 0.07
    assert result.effective_config_json == recorded
    assert result.effective_config.engine_config.market.cash_rate == engine.market.cash_rate
    assert effective_config_hash(result.effective_config) == result.config_hash
    with pytest.raises(FrozenInstanceError):
        result.config_hash = "x"
    with pytest.raises(FrozenInstanceError):
        result.rates.loan_ftp_rate = 0.0


def test_scope_validation(request_):
    live = request_.engine_config.competitor_rates.model_copy(update={"enabled": True})
    with pytest.raises(ValueError, match="static market"):
        request_.model_copy(
            update={
                "engine_config": request_.engine_config.model_copy(
                    update={"competitor_rates": live}
                )
            }
        )
    with pytest.raises(ValueError, match="single-horizon"):
        request_.model_copy(
            update={"engine_config": request_.engine_config.model_copy(update={"multiperiod": {}})}
        )
    with pytest.raises(ValueError, match="non-negative"):
        request_.model_copy(update={"fixed_rates": (0.0619, -0.01)})
    tiered = request_.engine_config.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[LoanTierConfig(name="all", rate_grid=_grid((0.06, 0.065)))],
                deposit_tiers=[DepositTierConfig(name="all", rate_grid=_grid((0.04, 0.045)))],
            ),
        }
    )
    with pytest.raises(ValueError, match="joint pricing"):
        request_.model_copy(update={"engine_config": tiered, "fixed_rates": LEGACY_OFFER})
    with pytest.raises(ValueError, match="60"):
        request_.model_copy(
            update={
                "funding_profile": request_.funding_profile.model_copy(
                    update={
                        "loan_liquidity_buckets": (
                            FTPLiquidityBucket(tenor_months=120, weight=1.0),
                        )
                    }
                )
            }
        )


def test_artifacts_disclose_the_ftp_simulation(request_, tmp_path):
    result = run_ftp_simulation(request_)
    result.diagnostics.write_artifacts(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ftp_simulation"]["review_required"] is True
    assert summary["ftp_simulation"]["curve_key"] == result.rates.curve_key
    report = result.report()
    assert report["governance_status"] == "REVIEW_REQUIRED"
    assert report["gates"]["FTP_SIMULATION_ONLY"] == "WARN"
    json.dumps(report, allow_nan=False)


# -- Phase 4c compatibility (T20-T22) -------------------------------------------------------


def test_t20_opening_deposits_do_not_fund_new_lending(request_):
    def run(opening):
        profile = _supply_profile(
            opening_funding_nzd=opening, opening_unallocated_interest_rate=0.027
        )
        return run_ftp_simulation(request_.model_copy(update={"deposit_supply_profile": profile}))

    without, with_book = run(0.0), run(600e6)
    assert with_book.diagnostics.best_result == without.diagnostics.best_result
    assert with_book.ledger == without.ledger
    assert with_book.eligible_funding_basis == "phase_4c_recognised_incremental_funding"
    assert (
        with_book.ledger.usable_deposit_funding == with_book.deposit_supply_estimate.usable_funding
    )
    assert with_book.deposit_supply_estimate.reference_funding == 600e6


def test_t21_internal_migrations_do_not_add_consolidated_funding(request_):
    fixed = request_.model_copy(update={"fixed_rates": LEGACY_OFFER})
    migration = DepositOpportunitySegment(
        segment_id="migration",
        opportunity_nzd=200e6,
        flow=DepositFlow.INTERNAL_MIGRATION,
        source_interest_rate=0.027,
    )
    plain = run_ftp_simulation(
        fixed.model_copy(update={"deposit_supply_profile": _supply_profile()})
    )
    migrated = run_ftp_simulation(
        fixed.model_copy(
            update={
                "deposit_supply_profile": _supply_profile(
                    opening_funding_nzd=200e6,
                    segments=(*_supply_profile().segments, migration),
                )
            }
        )
    )
    assert (
        migrated.deposit_supply_estimate.gross_bookings
        > plain.deposit_supply_estimate.gross_bookings
    )
    assert migrated.ledger.usable_deposit_funding == plain.ledger.usable_deposit_funding
    assert (
        migrated.ledger.external_wholesale_funding_cost
        == plain.ledger.external_wholesale_funding_cost
    )
    assert migrated.ledger.deposit_ftp_credit == plain.ledger.deposit_ftp_credit


def test_t22_eligible_funding_caps_remain_enforced(request_):
    cap = 100e6
    profile = _supply_profile(
        concentration=DepositConcentrationPolicy(mode=ConcentrationMode.HARD_LIMIT, limit_nzd=cap)
    )
    result = run_ftp_simulation(
        request_.model_copy(update={"deposit_supply_profile": profile, "fixed_rates": LEGACY_OFFER})
    )
    assert result.ledger.usable_deposit_funding <= cap
    assert result.deposit_supply_estimate.constrained_funding > 0
    assert result.ledger.deposit_ftp_credit == pytest.approx(cap * result.rates.deposit_ftp_rate)


def test_incompatible_supply_basis_is_rejected(request_):
    with pytest.raises(ValueError, match="12-month supply horizon"):
        request_.model_copy(update={"deposit_supply_profile": _supply_profile(horizon_months=24)})
    with pytest.raises(ValueError, match="incompatible"):
        request_.model_copy(
            update={
                "deposit_supply_profile": _supply_profile(),
                "funding_profile": request_.funding_profile.model_copy(
                    update={"deposit_contractual_tenor_months": 6}
                ),
            }
        )


# -- Phase 4b compatibility --------------------------------------------------------------


def test_ftp_changes_funding_not_credit_loss_estimates(request_):
    fixed = request_.model_copy(
        update={"fixed_rates": LEGACY_OFFER, "credit_loss_profile": illustrative_profile()}
    )
    base = run_ftp_simulation(fixed)
    shocked = run_ftp_simulation(
        fixed.model_copy(update={"curve": illustrative_curve(AS_OF, premium_scale=2.0)})
    )
    assert (
        shocked.diagnostics.best_result.expected_credit_loss
        == base.diagnostics.best_result.expected_credit_loss
    )
    plain = run_ftp_simulation(fixed.model_copy(update={"credit_loss_profile": None}))
    assert (
        base.diagnostics.best_result.total_objective - plain.diagnostics.best_result.total_objective
        == pytest.approx(
            plain.diagnostics.best_result.expected_credit_loss
            - base.diagnostics.best_result.expected_credit_loss
        )
    )
    assert (
        base.ledger.external_wholesale_funding_cost == plain.ledger.external_wholesale_funding_cost
    )
    codes = {gate.code for gate in base.gates}
    assert {"CREDIT_LOSS_SIMULATION_ONLY", "FTP_SIMULATION_ONLY"} <= codes


def test_joint_credit_loss_supply_and_ftp_composition_keeps_every_warning(request_):
    result = run_ftp_simulation(
        request_.model_copy(
            update={
                "credit_loss_profile": illustrative_profile(),
                "deposit_supply_profile": _supply_profile(),
            }
        )
    )
    codes = {gate.code: gate.status for gate in result.gates}
    for code in (
        "CREDIT_LOSS_SIMULATION_ONLY",
        "DEPOSIT_SUPPLY_SIMULATION_ONLY",
        "FTP_SIMULATION_ONLY",
    ):
        assert codes[code] is GateStatus.WARN
    assert result.governance_status is DecisionStatus.REVIEW_REQUIRED
    assert result.ledger.reconciliation_difference == pytest.approx(0, abs=1e-6)
    assert result.roe_bridge.model_objective == pytest.approx(
        result.diagnostics.best_result.total_objective, abs=1e-6
    )


# -- Boundaries (T57-T58) ------------------------------------------------------------------


def test_t57_no_production_api_or_collector_uses_ftp():
    for package in ("api", "pricing_intelligence", "competitor_rates", "application", "dashboard"):
        for path in (SRC / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "funds_transfer_pricing" not in text and "ftp_simulation" not in text, path


def test_t58_ftp_modules_have_no_network_or_live_data_access():
    forbidden = {
        "requests",
        "urllib",
        "socket",
        "httpx",
        "playwright",
        "sqlite3",
        "os",
        "subprocess",
    }
    for name in (
        "pricing/funds_transfer_pricing.py",
        "pricing/economic_roe.py",
        "scenarios/ftp_simulation.py",
    ):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not imported & forbidden, (name, imported & forbidden)
        assert "pricing_intelligence" not in {
            part
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for part in node.module.split(".")
        }
