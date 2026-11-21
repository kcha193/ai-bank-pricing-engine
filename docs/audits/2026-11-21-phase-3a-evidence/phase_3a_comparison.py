"""Phase 3a (OPT-002) before/after comparison for the segmented optimiser.

Run from the repository root against any checkout (master or the fix branch)::

    PYTHONPATH=<checkout>/src python docs/audits/2026-11-21-phase-3a-evidence/phase_3a_comparison.py

Every scenario is synthetic and seeded. It prints one JSON document; nothing is written.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

from balance_pricing_engine.config import (
    DepositTierConfig,
    GridConfig,
    LoanTierConfig,
    ProductConfig,
    SegmentedPricingConfig,
    load_config,
)
from balance_pricing_engine.governance.gates import decision_status_from_gates, evaluate_gates
from balance_pricing_engine.governance.lineage import synthetic_model_lineage
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[3]
BASE = load_config(ROOT / "configs/base.yaml")
SEGMENTED = load_config(ROOT / "configs/segmented_pricing.yaml")


def _grid(start: float, stop: float, step: float) -> GridConfig:
    return GridConfig(start=start, stop=stop, step=step)


def _with(config, section: str, **values):
    return config.model_copy(
        update={section: getattr(config, section).model_copy(update=values)}
    )


def _summarise(config, diagnostics) -> dict:
    best = diagnostics.best_result
    gates = evaluate_gates(
        config=config,
        diagnostics=diagnostics,
        model_lineage={p: synthetic_model_lineage(p) for p in ("loan", "deposit")},
        feature_lineage={},
        competitor_rate_issues=None,
    )
    summary = {
        "tier_rates": diagnostics.segmented_tier_rates,
        "loan_rate": best.loan_rate,
        "deposit_rate": best.deposit_rate,
        "loan_volume": round(best.expected_loan_volume),
        "deposit_volume": round(best.expected_deposit_volume),
        "loan_interest_income": round(best.loan_interest_income),
        "credit_loss": round(best.expected_credit_loss),
        "capital_charge": round(best.capital_charge),
        "deposit_interest_expense": round(best.deposit_interest_cost),
        "wholesale_funding_cost": round(best.wholesale_funding_cost),
        "funding_shortfall": round(best.funding_shortfall),
        "funding_shortfall_penalty": round(best.funding_shortfall_penalty),
        "excess_deposit_income": round(best.excess_deposit_income),
        "total_objective": round(best.total_objective),
        "roe": diagnostics.selected_return_on_equity,
        "roe_constraint_relaxed": diagnostics.roe_constraint_relaxed,
        "expansion_rounds": diagnostics.expansion_rounds,
        "boundary_warning": diagnostics.boundary_diagnostics.has_warning,
        "policy_band_breached": diagnostics.policy_band_breached,
        "governance_status": decision_status_from_gates(gates).value,
        "warn_gates": sorted(g.code for g in gates if g.status.value != "PASS"),
    }
    solver = getattr(diagnostics, "segmented_solver", None)
    if solver is not None:
        summary["solver"] = solver
    return summary


def _run(config, expansions: int = 0) -> dict:
    return _summarise(config, run_baseline_diagnostics(config, max_boundary_expansions=expansions))


def scenarios() -> dict:
    out: dict = {}

    # A. Original November 13 audit reproduction (followup.py case 4), plus the joint optimiser.
    loan_grid, deposit_grid = _grid(0.03, 0.09, 0.0005), _grid(0.02, 0.06, 0.0005)
    one_tier = BASE.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[LoanTierConfig(name="all", rate_grid=loan_grid)],
                deposit_tiers=[DepositTierConfig(name="all", rate_grid=deposit_grid)],
            ),
        }
    )
    joint = BASE.model_copy(
        update={
            "pricing_strategy": None,
            "products": ProductConfig(loan_rate_grid=loan_grid, deposit_rate_grid=deposit_grid),
        }
    )
    out["A_audit_one_tier"] = _run(one_tier)
    joint_diagnostics = run_baseline_diagnostics(joint, max_boundary_expansions=0)
    out["A_audit_joint_reference"] = {
        "loan_rate": joint_diagnostics.best_result.loan_rate,
        "deposit_rate": joint_diagnostics.best_result.deposit_rate,
        "total_objective": round(joint_diagnostics.best_result.total_objective),
        "funding_shortfall": round(joint_diagnostics.best_result.funding_shortfall),
    }

    # B. Small two-by-two tier example: 3 rates per tier, 81 combinations.
    two_by_two = BASE.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[
                    LoanTierConfig(name="standard", lvr_max=0.80, rate_grid=_grid(0.055, 0.065, 0.005)),
                    LoanTierConfig(name="high_lvr", rate_grid=_grid(0.060, 0.070, 0.005)),
                ],
                deposit_tiers=[
                    DepositTierConfig(name="small", balance_max=100_000, rate_grid=_grid(0.035, 0.045, 0.005)),
                    DepositTierConfig(name="large", rate_grid=_grid(0.038, 0.048, 0.005)),
                ],
            ),
        }
    )
    out["B_two_by_two"] = _run(two_by_two)

    # C. Production-sized synthetic fixture: configs/segmented_pricing.yaml (3x3 tiers).
    out["C_production_fixture"] = _run(SEGMENTED)

    # D. High shortfall-penalty stress.
    out["D_high_penalty"] = _run(_with(SEGMENTED, "balance_sheet", funding_shortfall_penalty_rate=0.20))

    # E. Low deposit supply: 1,000 deposit prospects against 5,000 loan prospects.
    out["E_low_deposit_supply"] = _run(_with(SEGMENTED, "simulation", n_deposit_customers=1000))

    # F. Excess deposits: a large deposit pool and a ratio-free balance sheet.
    out["F_excess_deposits"] = _run(
        _with(
            _with(SEGMENTED, "simulation", n_deposit_customers=40_000),
            "balance_sheet",
            required_deposit_funding_ratio=0.0,
        )
    )

    # G. Adaptive grid expansion on the production fixture (default two rounds).
    out["G_adaptive_expansion"] = _run(SEGMENTED, expansions=2)
    return out


if __name__ == "__main__":
    print(json.dumps(scenarios(), indent=1, sort_keys=True, default=str))
