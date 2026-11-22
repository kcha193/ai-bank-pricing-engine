"""Compare term-matched FTP scenarios on matched synthetic inputs (FIN-003, Phase 4d).

Every curve here is an illustrative construction declared before any run: shocks scale the
reference slope or the liquidity premium of ``illustrative_curve``. Nothing is fitted to a
preferred mortgage rate, and no observed market quote is used.

Run from the repository root::

    python -m examples.compare_ftp --output docs/audits/phase_4d_evidence/comparison.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.config_identity import effective_config_hash, file_sha256
from balance_pricing_engine.governance.gates import decision_status_from_gates, evaluate_gates
from balance_pricing_engine.pricing.credit_loss import illustrative_profile
from balance_pricing_engine.pricing.economic_roe import economic_roe_bridge
from balance_pricing_engine.pricing.funds_transfer_pricing import (
    FTPLiquidityBucket,
    illustrative_curve,
    illustrative_funding_profile,
    scalar_equivalent_curve,
)
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight
from balance_pricing_engine.pricing_intelligence.run_artifacts import write_json_atomically
from balance_pricing_engine.scenarios.ftp_simulation import FTPSimulationConfig, run_ftp_simulation
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from examples.compare_deposit_supply import illustrative_profiles

BASE_CONFIG = Path(__file__).resolve().parents[1] / "configs/base.yaml"
#: Fixed synthetic dates, so the evidence is reproducible byte for byte.
CURVE_AS_OF = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
VALUATION_TIME = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
LEGACY_OFFER = (0.0619, 0.0405)
SURPLUS_OFFER = (0.0750, 0.0450)


def _scenarios(engine: EngineConfig) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    curve = illustrative_curve(CURVE_AS_OF)
    profile = illustrative_funding_profile()
    shortfall_engine = engine.model_copy(
        update={
            "balance_sheet": engine.balance_sheet.model_copy(
                update={"required_deposit_funding_ratio": 0.85}
            )
        }
    )
    supply = illustrative_profiles()["fixed_market_base"]
    specs = {
        "scalar_equivalent": {
            "description": "Flat curve reproducing cash_rate + wholesale_funding_spread exactly.",
            "curve": scalar_equivalent_curve(
                cash_rate=engine.market.cash_rate,
                wholesale_funding_spread=engine.market.wholesale_funding_spread,
                as_of=CURVE_AS_OF,
            ),
        },
        "term_matched_12m": {
            "description": "Illustrative term curve; residual deficit funded at 12 months.",
        },
        "liquidity_premium_high": {
            "description": "Liquidity premium x1.5 at every tenor.",
            "curve": illustrative_curve(CURVE_AS_OF, version="1.0.0-lp150", premium_scale=1.5),
        },
        "liquidity_premium_low": {
            "description": "Liquidity premium x0.5 at every tenor.",
            "curve": illustrative_curve(CURVE_AS_OF, version="1.0.0-lp050", premium_scale=0.5),
        },
        "curve_steeper": {
            "description": "Reference term slope x2.",
            "curve": illustrative_curve(CURVE_AS_OF, version="1.0.0-steep", slope_scale=2.0),
        },
        "curve_inverted": {
            "description": "Reference term slope x-1 (longer tenors below the policy rate).",
            "curve": illustrative_curve(CURVE_AS_OF, version="1.0.0-inverted", slope_scale=-1.0),
        },
        "repricing_tenor_24m": {
            "description": "24-month fixed repricing reference. Internal attribution only.",
            "profile": profile.model_copy(update={"loan_repricing_tenor_months": 24}),
        },
        "loan_liquidity_60m": {
            "description": "All loan liquidity at 60 months. Internal attribution only.",
            "profile": profile.model_copy(
                update={
                    "loan_liquidity_buckets": (FTPLiquidityBucket(tenor_months=60, weight=1.0),)
                }
            ),
        },
        "wholesale_tenor_36m": {
            "description": "Residual deficit funded externally at 36 months.",
            "profile": profile.model_copy(update={"wholesale_funding_tenor_months": 36}),
        },
        "funding_shortfall": {
            "description": "Required deposit funding ratio raised from 65% to 85%.",
            "engine": shortfall_engine,
        },
        "surplus_deposits": {
            "description": (
                "Fixed offer at 7.50% / 4.50% leaves surplus deposits credited at the 3-month "
                "reference. Reoptimised selection uses the same request as term_matched_12m."
            ),
            "fixed_offer": SURPLUS_OFFER,
        },
        "phase_4c_supply": {
            "description": "Phase 4c fixed_market_base supply: recognised incremental funding.",
            "deposit_supply_profile": supply,
        },
        "phase_4b_credit_loss": {
            "description": "Phase 4b illustrative PD/LGD/EAD profile.",
            "credit_loss_profile": illustrative_profile(),
        },
        "joint_credit_loss_supply_ftp": {
            "description": "Phase 4b loss, Phase 4c supply and term-matched FTP together.",
            "deposit_supply_profile": supply,
            "credit_loss_profile": illustrative_profile(),
        },
    }
    return specs, {"curve": curve, "profile": profile}


def _request(engine: EngineConfig, spec: dict[str, Any], defaults: dict[str, Any]):
    return FTPSimulationConfig(
        engine_config=spec.get("engine", engine),
        curve=spec.get("curve", defaults["curve"]),
        funding_profile=spec.get("profile", defaults["profile"]),
        valuation_time=VALUATION_TIME,
        deposit_supply_profile=spec.get("deposit_supply_profile"),
        credit_loss_profile=spec.get("credit_loss_profile"),
    )


def _canonical(engine: EngineConfig) -> dict[str, Any]:
    diagnostics = run_baseline_diagnostics(engine)
    best = diagnostics.best_result
    gates = evaluate_gates(
        config=engine, diagnostics=diagnostics, model_lineage={}, feature_lineage={}
    )
    bridge = economic_roe_bridge(
        best,
        rwa_weight=maturity_adjusted_rwa_weight(
            engine.balance_sheet.loan_rwa_weight, engine.balance_sheet.average_loan_tenor_years
        ),
    )
    return {
        "config_hash": effective_config_hash(engine),
        "loan_rate": best.loan_rate,
        "deposit_rate": best.deposit_rate,
        "total_objective": best.total_objective,
        "return_on_equity": diagnostics.selected_return_on_equity,
        "policy_band_breached": diagnostics.policy_band_breached,
        "governance_status": decision_status_from_gates(gates).value,
        "economic_roe": bridge.to_dict(),
    }


def build_report(config_path: Path = BASE_CONFIG) -> dict[str, Any]:
    engine = load_config(config_path)
    specs, defaults = _scenarios(engine)
    scenarios: dict[str, Any] = {}
    for name, spec in specs.items():
        request = _request(engine, spec, defaults)
        fixed = run_ftp_simulation(
            request.model_copy(update={"fixed_rates": spec.get("fixed_offer", LEGACY_OFFER)})
        )
        reoptimised = run_ftp_simulation(request)
        scenarios[name] = {
            "description": spec["description"],
            "curve_key": reoptimised.rates.curve_key,
            "profile_key": reoptimised.rates.profile_key,
            "fixed_offer": fixed.report(),
            "reoptimised": reoptimised.report(),
        }
    runs = [
        run
        for scenario in scenarios.values()
        for run in (scenario["fixed_offer"], scenario["reoptimised"])
    ]
    canonical = _canonical(engine)
    equivalent = scenarios["scalar_equivalent"]["reoptimised"]
    return {
        "finding": "FIN-003",
        "phase": "4d",
        "classification": "SYNTHETIC / UNCALIBRATED",
        "approval_status": "NOT APPROVED FOR PRICING",
        "source_config_file_sha256": file_sha256(config_path),
        "curve_as_of": CURVE_AS_OF.isoformat(),
        "valuation_time": VALUATION_TIME.isoformat(),
        "fixed_offer": list(LEGACY_OFFER),
        "canonical": canonical,
        "scenarios": scenarios,
        "checks": {
            "scalar_equivalent_reproduces_canonical": (
                equivalent["selected_loan_rate"] == canonical["loan_rate"]
                and equivalent["selected_deposit_rate"] == canonical["deposit_rate"]
                and equivalent["components"]["N_total_consolidated_objective"]
                == canonical["total_objective"]
            ),
            "max_abs_internal_transfer_net": max(
                abs(run["ledger"]["internal_transfer_net"]) for run in runs
            ),
            "max_abs_ftp_reconciliation_difference": max(
                abs(run["ledger"]["reconciliation_difference"]) for run in runs
            ),
            "every_run_requires_review": all(
                run["governance_status"] in ("REVIEW_REQUIRED", "BLOCKED") for run in runs
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=BASE_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.config)
    write_json_atomically(args.output, json.loads(json.dumps(report, allow_nan=False)))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
