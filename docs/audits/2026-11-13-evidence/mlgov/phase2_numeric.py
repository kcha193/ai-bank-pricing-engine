"""Phase 2: independent numeric checks of the synthetic response and the ML layer (read-only)."""
from __future__ import annotations

import json
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from balance_pricing_engine.config import (
    DepositResponseCoefficientsConfig,
    LoanResponseCoefficientsConfig,
    load_config,
)
from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    HistGBMResponseModel,
    LogisticResponseModel,
    ModelRegistry,
    ModelTrainingAgent,
)
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.workflows import run_response_model_workflow
from balance_pricing_engine.optimization.response_fn import MLResponseFnAdapter, SyntheticLoanResponseFn
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import generate_deposit_customers, generate_loan_customers
from balance_pricing_engine.synthetic.response import deposit_uptake_probability, loan_acceptance_probability
from balance_pricing_engine.utils.random import make_rng

R: dict = {}
cfg = load_config("configs/base.yaml")
rsp = cfg.response
rng = make_rng(cfg.random_seed)
L = generate_loan_customers(cfg.simulation.n_loan_customers, rng)
D = generate_deposit_customers(cfg.simulation.n_deposit_customers, rng)
cL, cD = cfg.market.competitor_loan_rate, cfg.market.competitor_deposit_rate
coefL, coefD = rsp.coefficients.loan, rsp.coefficients.deposit


def pL(r, c=cL, a=rsp.loan_rate_elasticity, b=rsp.loan_competitor_gap_elasticity, coef=coefL):
    return loan_acceptance_probability(L, r, c, a, b, coefficients=coef)


def pD(r, c=cD, a=rsp.deposit_rate_elasticity, b=rsp.deposit_competitor_gap_elasticity, coef=coefD):
    return deposit_uptake_probability(D, r, c, a, b, coefficients=coef)


# 1. bounds, incl. extreme inputs
rates = np.round(np.arange(-0.02, 0.2001, 0.0025), 6)
mins, maxs, nan = 1.0, 0.0, 0
for r in rates:
    for p in (pL(r), pD(r)):
        mins, maxs, nan = min(mins, p.min()), max(maxs, p.max()), nan + int(np.isnan(p).sum())
R["bounds"] = {"rate_range": [float(rates[0]), float(rates[-1])], "min_p": float(mins), "max_p": float(maxs),
               "nan_count": nan}

# 2. monotonicity in own rate (portfolio mean and per-customer)
own = np.round(np.arange(0.03, 0.10, 0.0005), 6)
mL = np.array([pL(r).mean() for r in own])
own_d = np.round(np.arange(0.01, 0.07, 0.0005), 6)
mD = np.array([pD(r).mean() for r in own_d])
perL = np.stack([pL(r) for r in own])
perD = np.stack([pD(r) for r in own_d])
R["monotonicity_own_rate"] = {
    "loan_mean_strictly_decreasing": bool(np.all(np.diff(mL) < 0)),
    "deposit_mean_strictly_increasing": bool(np.all(np.diff(mD) > 0)),
    "loan_per_customer_violations": int((np.diff(perL, axis=0) > 0).sum()),
    "deposit_per_customer_violations": int((np.diff(perD, axis=0) < 0).sum()),
    "loan_mean_at_market(4.99%)": float(pL(0.0499).mean()),
    "deposit_mean_at_market(3.90%)": float(pD(0.039).mean()),
}

# 3. competitor move with own rate fixed
R["competitor_move_own_fixed"] = {
    "loan_mean_comp-50bps": float(pL(0.0619, c=cL - 0.005).mean()),
    "loan_mean_comp": float(pL(0.0619).mean()),
    "loan_mean_comp+50bps": float(pL(0.0619, c=cL + 0.005).mean()),
    "deposit_mean_comp-50bps": float(pD(0.0405, c=cD - 0.005).mean()),
    "deposit_mean_comp": float(pD(0.0405).mean()),
    "deposit_mean_comp+50bps": float(pD(0.0405, c=cD + 0.005).mean()),
}

# 4. identifiability: with competitor fixed, (a, b) and (a+b, 0) are indistinguishable
a, b = rsp.loan_rate_elasticity, rsp.loan_competitor_gap_elasticity
coef_shift = coefL.model_copy(update={"intercept": coefL.intercept + b * (coefL.rate_anchor - cL)})
diffs = [float(np.max(np.abs(pL(r) - pL(r, a=a + b, b=0.0, coef=coef_shift)))) for r in own]
R["identifiability"] = {
    "note": "(a,b)=(-170,-130) vs (a+b,0)=(-300,0) with intercept shifted by b*(anchor-competitor)",
    "max_abs_prob_diff_over_own_grid": max(diffs),
    "own_rate_total_slope_loan": a + b,
    "own_rate_total_slope_deposit": rsp.deposit_rate_elasticity + rsp.deposit_competitor_gap_elasticity,
    "competitor_slope_loan(-b)": -b,
    "competitor_slope_deposit(-b)": -rsp.deposit_competitor_gap_elasticity,
}

# 5. training dataset collinearity
loan_grid, dep_grid = build_configured_rate_grids(cfg)
ds = build_loan_response_dataset(L, loan_grid, cL, a, b, make_rng(7), n_samples=15000)
R["training_collinearity"] = {
    "competitor_rate_std": float(ds["competitor_rate"].std()),
    "corr_offered_vs_gap": float(np.corrcoef(ds["offered_rate"], ds["rate_gap_to_competitor"])[0, 1]),
    "rank_of_[offered,competitor,gap]": int(np.linalg.matrix_rank(
        ds[["offered_rate", "competitor_rate", "rate_gap_to_competitor"]].to_numpy() - 0)),
    "n_unique_offered_rates": int(ds["offered_rate"].nunique()),
}

# 6. ML-implied competitor sensitivity vs truth (fit on ds, score on L at r=6.19%)
lr = LogisticResponseModel(42); lr.fit(ds, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
hgb = HistGBMResponseModel(42); hgb.fit(ds, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
clf = lr.fitted_model.named_steps["classifier"]; sc = lr.fitted_model.named_steps["scaler"]
raw_coef = dict(zip(LOAN_FEATURE_COLUMNS, (clf.coef_[0] / sc.scale_).round(2)))
R["logistic_raw_coefficients_rate_terms"] = {k: float(raw_coef[k]) for k in
                                             ("offered_rate", "competitor_rate", "rate_gap_to_competitor")}
rows = []
for name, m in (("truth", None), ("logistic", lr), ("hist_gbm", hgb)):
    for dc in (-0.005, 0.0, 0.005):
        if m is None:
            p = pL(0.0619, c=cL + dc).mean()
        else:
            p = MLResponseFnAdapter(m, LOAN_FEATURE_COLUMNS)(L, 0.0619, cL + dc).mean()
        rows.append({"model": name, "competitor_shift_bps": int(round(dc * 1e4)), "mean_p": round(float(p), 4)})
R["competitor_sensitivity_table"] = rows
# own-rate curve truth vs ML at fixed competitor, including off-grid extrapolation
own_rows = []
for r in (0.040, 0.0499, 0.0619, 0.075, 0.09, 0.11):
    own_rows.append({
        "offered_rate": r,
        "truth": round(float(pL(r).mean()), 4),
        "logistic": round(float(MLResponseFnAdapter(lr, LOAN_FEATURE_COLUMNS)(L, r, cL).mean()), 4),
        "hist_gbm": round(float(MLResponseFnAdapter(hgb, LOAN_FEATURE_COLUMNS)(L, r, cL).mean()), 4),
    })
R["own_rate_curve_truth_vs_ml"] = own_rows
R["training_offered_rate_range"] = [float(ds["offered_rate"].min()), float(ds["offered_rate"].max())]

# 7. leakage: customer overlap between train and test in the standard workflow split
from sklearn.model_selection import train_test_split  # noqa: E402
tr, te = train_test_split(ds, test_size=0.3, random_state=42, stratify=ds[LOAN_TARGET_COLUMN])
R["leakage"] = {
    "share_test_rows_whose_customer_in_train": float(te["customer_id"].isin(set(tr["customer_id"])).mean()),
    "scoring_population_equals_training_population":
        "workflow + optimiser both call make_rng(config.random_seed) then generate_loan_customers -> identical"
        " customers (see check below)",
}
wf = run_response_model_workflow(cfg)
rng2 = make_rng(cfg.random_seed)
L2 = generate_loan_customers(cfg.simulation.n_loan_customers, rng2)
R["leakage"]["workflow_training_customers_subset_of_optimiser_customers"] = bool(
    set(wf.loan_dataset["customer_id"]).issubset(set(L2["customer_id"]))
    and np.allclose(L2.set_index("customer_id").loc[wf.loan_dataset["customer_id"].iloc[:50], "credit_score"],
                    wf.loan_dataset["credit_score"].iloc[:50]))
R["workflow_reports"] = {
    k: {"metrics": {m: round(v, 4) for m, v in rep["metrics"].items()},
        "rate_check": bool(rep["response_check"].is_directionally_sane),
        "lift_monotone": bool(rep["lift_monotone"])}
    for k, rep in (("loan", wf.loan_model_report), ("deposit", wf.deposit_model_report))
}
wf2 = run_response_model_workflow(cfg)
R["reproducibility_workflow_metrics_equal"] = (
    wf.loan_model_report["metrics"] == wf2.loan_model_report["metrics"]
    and wf.deposit_model_report["metrics"] == wf2.deposit_model_report["metrics"]
)

# 8. training generator ignores config.response.coefficients
alt = coefL.model_copy(update={"intercept": 0.5})
cfg_alt = cfg.model_copy(update={"response": rsp.model_copy(update={
    "coefficients": rsp.coefficients.model_copy(update={"loan": alt})})})
ds_alt = build_loan_response_dataset(L, loan_grid, cL, a, b, make_rng(7), n_samples=15000)
truth_alt = SyntheticLoanResponseFn(a, b, cfg_alt.response.coefficients.loan)
gt_from_optimiser_path = np.concatenate([truth_alt(ds_alt.iloc[[i]], float(ds_alt["offered_rate"].iloc[i]), cL)
                                         for i in range(200)])
R["generator_coefficient_mismatch"] = {
    "config_loan_intercept": 0.5,
    "dataset_ground_truth_mean_first200": float(ds_alt["ground_truth_probability"].iloc[:200].mean()),
    "optimiser_synthetic_truth_mean_first200": float(gt_from_optimiser_path.mean()),
    "max_abs_diff": float(np.max(np.abs(ds_alt["ground_truth_probability"].iloc[:200].to_numpy()
                                        - gt_from_optimiser_path))),
}

# 9. ModelTrainingAgent: oracle baseline, test split unused
tmp = Path(tempfile.mkdtemp(prefix="mlgov_p2_", dir=str(Path(__file__).with_name("tmp"))))
agent = ModelTrainingAgent(artifacts_dir=tmp)
reg = ModelRegistry(tmp / "reg.json")
res = agent.train(dataset=ds, candidates={"elasticity_baseline": ElasticityBaselineModel(),
                                          "logistic": LogisticResponseModel(42),
                                          "hist_gbm": HistGBMResponseModel(42)},
                  registry=reg, feature_columns=LOAN_FEATURE_COLUMNS, target_column=LOAN_TARGET_COLUMN)
R["training_agent"] = {
    "models": [{"name": r["name"], "log_loss": round(r["metrics"]["log_loss"], 5),
                "auc": round(r["metrics"]["auc"], 4), "cal_mae": round(r["calibration_mae"], 4)}
               for r in res.report["models"]],
    "recommended": res.recommended_model_name,
    "test_rows_reported": res.report["test_rows"],
    "test_metrics_present_in_report": any("test" in k for k in json.dumps(res.report["models"])),
    "champion_status_values": sorted({r["champion_status"] for r in reg.load()["models"]}),
    "registry_config_hash_inputs": "features+target+seed only (agentic.py _stable_hash call in train())",
}
# baseline scored WITHOUT ground_truth column at constant offered rate -> flat
eb = ElasticityBaselineModel()
flat = [float(MLResponseFnAdapter(eb, LOAN_FEATURE_COLUMNS)(L, r, cL).mean()) for r in (0.04, 0.06, 0.08)]
R["elasticity_baseline_scored_via_adapter"] = {"rates": [0.04, 0.06, 0.08], "mean_p": flat}

# 10. registry approval controls
reg2 = ModelRegistry(tmp / "reg2.json")
reg2.register({"model_name": "Failed", "governance_status": "pending_human_approval",
               "champion_status": "challenger", "feature_columns": LOAN_FEATURE_COLUMNS,
               "metrics": {"log_loss": 9.9}, "calibration_mae": 0.9})
reg2.promote_champion("Failed", approver="")
rec = reg2.load()["models"][0]
R["approval_controls"] = {"promote_failed_model_with_empty_approver": {
    "governance_status": rec["governance_status"], "champion_status": rec["champion_status"],
    "approved_by": rec["approved_by"]}}

out = Path(__file__).with_name("phase2_results.json")
out.write_text(json.dumps(R, indent=2, default=str))
print(json.dumps(R, indent=1, default=str))
