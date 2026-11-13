"""Challenge case J: governed registry path (read-only w.r.t. the repo; all artifacts in a temp dir).

Run: cd <repo> && PYTHONPATH=src:<this dir> .venv/bin/python <this file>
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

warnings.filterwarnings("ignore")

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import load_config
from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    LogisticResponseModel,
    ModelRegistry,
)
from balance_pricing_engine.modelling.datasets import (
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.models import build_logistic_regression_model
from balance_pricing_engine.optimization.response_fn import GovernanceError
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng

import audit_models  # noqa: E402  (scratch helper module)

TMP = Path(tempfile.mkdtemp(prefix="mlgov_j_"))
RESULTS: dict[str, dict] = {}


def small_config(n: int = 2000):
    cfg = load_config("configs/base.yaml")
    sim = cfg.simulation.model_copy(update={"n_loan_customers": n, "n_deposit_customers": n})
    return cfg.model_copy(update={"simulation": sim})


def registry_with(name: str, record: dict) -> ModelRegistry:
    reg = ModelRegistry(TMP / f"{name}_registry.json")
    base = {
        "model_name": name,
        "champion_status": "champion",
        "governance_status": "approved",
        "feature_columns": list(LOAN_FEATURE_COLUMNS),
        "target_column": LOAN_TARGET_COLUMN,
        "version": "v-audit",
    }
    base.update(record)
    reg.register(base)
    return reg


def run_both(case: str, cfg, registry, expect: str) -> None:
    out: dict = {"expect": expect}
    for label, fn in (
        ("run_baseline_diagnostics", lambda: run_baseline_diagnostics(cfg, registry=registry)),
        (
            "PricingService.run_scenario",
            lambda: PricingService().run_scenario(PricingScenarioRequest(config=cfg, registry=registry)),
        ),
    ):
        try:
            res = fn()
            if hasattr(res, "best_result"):
                out[label] = {
                    "outcome": "returned",
                    "loan_rate": round(float(res.best_result.loan_rate), 6),
                    "deposit_rate": round(float(res.best_result.deposit_rate), 6),
                    "boundary_warning": bool(res.boundary_diagnostics.has_warning),
                    "expected_loan_volume": round(float(res.best_result.expected_loan_volume)),
                }
            else:
                out[label] = {
                    "outcome": "returned",
                    "status": res.status.value,
                    "loan_rate": round(res.best_loan_rate, 6),
                    "deposit_rate": round(res.best_deposit_rate, 6),
                    "loan_response_source": res.model_lineage["loan"].response_source,
                    "warn_gates": [g.code for g in res.governance.gates if g.status.value == "WARN"],
                    "block_gates": [g.code for g in res.governance.gates if g.status.value == "BLOCK"],
                }
        except GovernanceError as exc:
            out[label] = {"outcome": "GovernanceError", "msg": str(exc)[:160]}
        except Exception as exc:  # record the exact type
            out[label] = {"outcome": f"{type(exc).__name__}", "msg": str(exc)[:200]}
    RESULTS[case] = out
    print(case, json.dumps(out, indent=1))


def main() -> None:
    cfg = small_config()

    # J0 baseline (no registry)
    run_both("J0_no_registry", cfg, None, "synthetic baseline")
    base = RESULTS["J0_no_registry"]["run_baseline_diagnostics"]

    # J1 approved champion, artifact missing
    reg = registry_with("MissingArtifact", {"artifact_path": str(TMP / "nope.joblib")})
    run_both("J1_approved_missing_artifact", cfg, reg, "GovernanceError from both")

    # J2 approved champion, corrupt artifact (garbage bytes)
    bad = TMP / "corrupt.joblib"
    bad.write_bytes(b"this is not a joblib pickle \x00\x01\x02")
    reg = registry_with("CorruptArtifact", {"artifact_path": str(bad)})
    run_both("J2_approved_corrupt_artifact", cfg, reg, "GovernanceError from both")

    # J2b approved champion, artifact loads but is not a model (a dict)
    notmodel = TMP / "dict.joblib"
    joblib.dump({"hello": "world"}, notmodel)
    reg = registry_with("LoadableNonModel", {"artifact_path": str(notmodel)})
    run_both("J2b_approved_loadable_non_model", cfg, reg, "should fail loud (GovernanceError ideally)")

    # Build a real trained logistic champion artifact
    rng = make_rng(cfg.random_seed)
    customers = generate_loan_customers(cfg.simulation.n_loan_customers, rng)
    loan_grid, _ = build_configured_rate_grids(cfg)
    ds = build_loan_response_dataset(
        customers, loan_grid, cfg.market.competitor_loan_rate,
        cfg.response.loan_rate_elasticity, cfg.response.loan_competitor_gap_elasticity,
        rng, n_samples=cfg.simulation.n_loan_customers * 3,
    )
    lr = LogisticResponseModel(random_state=42)
    lr.fit(ds, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
    good = TMP / "logistic.joblib"
    joblib.dump(lr.fitted_model, good)

    # J3 unapproved champion with a VALID artifact -> synthetic fallback, identical rates
    reg = registry_with("Pending", {"artifact_path": str(good), "governance_status": "pending_human_approval"})
    run_both("J3_unapproved_valid_artifact", cfg, reg, "fallback; rates identical to J0")
    j3 = RESULTS["J3_unapproved_valid_artifact"]["run_baseline_diagnostics"]
    RESULTS["J3_unapproved_valid_artifact"]["identical_to_J0"] = (
        j3.get("loan_rate") == base["loan_rate"] and j3.get("deposit_rate") == base["deposit_rate"]
    )

    # J3b approved but not champion (challenger) -> fallback
    reg = registry_with("ApprovedChallenger", {"artifact_path": str(good), "champion_status": "challenger"})
    run_both("J3b_approved_noncampion", cfg, reg, "fallback; rates identical to J0")

    # J4a approved champion whose recorded feature contract differs from LOAN_FEATURE_COLUMNS
    reg = registry_with(
        "IncompatibleContract",
        {"artifact_path": str(TMP / "also_missing.joblib"), "feature_columns": LOAN_FEATURE_COLUMNS[:-1]},
    )
    run_both("J4a_approved_incompatible_record_missing_artifact", cfg, reg,
             "design says fallback; note the missing artifact is never detected")

    # J4b record claims LOAN_FEATURE_COLUMNS but artifact trained on a different column set
    sub_cols = [c for c in LOAN_FEATURE_COLUMNS if c != "risk_segment"]
    sub_model = build_logistic_regression_model(42).fit(ds[sub_cols], ds[LOAN_TARGET_COLUMN])
    sub_path = TMP / "subset_features.joblib"
    joblib.dump(sub_model, sub_path)
    reg = registry_with("ArtifactContractMismatch", {"artifact_path": str(sub_path)})
    run_both("J4b_record_matches_artifact_does_not", cfg, reg, "fail loud; ideally GovernanceError")

    # J4c artifact trained on a NumPy array with a permuted column order (no feature names)
    perm = list(reversed(LOAN_FEATURE_COLUMNS))
    perm_model = build_logistic_regression_model(42).fit(ds[perm].to_numpy(), ds[LOAN_TARGET_COLUMN])
    perm_path = TMP / "permuted_numpy.joblib"
    joblib.dump(perm_model, perm_path)
    reg = registry_with("PermutedColumns", {"artifact_path": str(perm_path)})
    run_both("J4c_permuted_columns_no_names", cfg, reg, "should be rejected; silent mispricing if not")

    # J5 ElasticityBaselineModel promoted+approved as champion
    eb_path = TMP / "elasticity_baseline.joblib"
    joblib.dump(ElasticityBaselineModel(), eb_path)
    reg = registry_with("ElasticityBaselineModel", {"artifact_path": str(eb_path)})
    run_both("J5_elasticity_baseline_as_champion", cfg, reg, "baseline is oracle-only; check flat demand")

    # J6 approved champion with inverted (wrong-sign) rate response
    inv_path = TMP / "inverted.joblib"
    joblib.dump(audit_models.InvertedLoanModel(), inv_path)
    reg = registry_with("InvertedLoanModel", {"artifact_path": str(inv_path)})
    run_both("J6_inverted_sign_champion", cfg, reg, "should not be RECOMMENDED")

    # J7 properly trained logistic champion approved
    reg = registry_with("LogisticChampion", {"artifact_path": str(good)})
    run_both("J7_trained_logistic_champion", cfg, reg, "ML-driven price; compare to J0")

    # J7b competitor loan rate +50bps: synthetic vs ML champion price response
    mk = cfg.market.model_copy(update={"competitor_loan_rate": cfg.market.competitor_loan_rate + 0.005})
    cfg_shift = cfg.model_copy(update={"market": mk})
    syn_shift = run_baseline_diagnostics(cfg_shift)
    ml_shift = run_baseline_diagnostics(cfg_shift, registry=reg)
    ml_base = run_baseline_diagnostics(cfg, registry=reg)
    RESULTS["J7b_competitor_shift_50bps"] = {
        "synthetic_loan_rate_base": base["loan_rate"],
        "synthetic_loan_rate_shift": float(syn_shift.best_result.loan_rate),
        "ml_loan_rate_base": float(ml_base.best_result.loan_rate),
        "ml_loan_rate_shift": float(ml_shift.best_result.loan_rate),
    }
    print("J7b", RESULTS["J7b_competitor_shift_50bps"])

    # J8 full-size base config: unapproved vs no registry equality
    full = load_config("configs/base.yaml")
    a = run_baseline_diagnostics(full)
    reg = registry_with("PendingFull", {"artifact_path": str(good), "governance_status": "pending_human_approval"})
    b = run_baseline_diagnostics(full, registry=reg)
    RESULTS["J8_full_config_unapproved_equals_no_registry"] = {
        "no_registry": [float(a.best_result.loan_rate), float(a.best_result.deposit_rate),
                        float(a.best_result.total_objective)],
        "unapproved": [float(b.best_result.loan_rate), float(b.best_result.deposit_rate),
                       float(b.best_result.total_objective)],
        "grid_results_equal": bool(a.grid_results.equals(b.grid_results)),
    }
    print("J8", RESULTS["J8_full_config_unapproved_equals_no_registry"])

    out = Path(__file__).with_name("challenge_j_results.json")
    out.write_text(json.dumps(RESULTS, indent=2, default=str))
    print("wrote", out, "tmp", TMP)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
