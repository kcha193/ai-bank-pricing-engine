"""Phase 2 extra: training support vs optimiser search region; HGB champion in the governed path."""
from __future__ import annotations

import json
import tempfile
import warnings
from pathlib import Path

import joblib

warnings.filterwarnings("ignore")

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import load_config
from balance_pricing_engine.modelling.agentic import HistGBMResponseModel, ModelRegistry
from balance_pricing_engine.modelling.datasets import (
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_loan_response_dataset,
)
from balance_pricing_engine.optimization.response_fn import MLResponseFnAdapter
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng

cfg = load_config("configs/base.yaml")
R = {}
lg, dg = build_configured_rate_grids(cfg)
diag = run_baseline_diagnostics(cfg)
R["training_grid_loan"] = [float(lg.min()), float(lg.max()), len(lg)]
R["training_grid_deposit"] = [float(dg.min()), float(dg.max()), len(dg)]
R["optimiser_evaluated_loan_range"] = [float(diag.grid_results.loan_rate.min()), float(diag.grid_results.loan_rate.max())]
R["optimiser_evaluated_deposit_range"] = [float(diag.grid_results.deposit_rate.min()),
                                          float(diag.grid_results.deposit_rate.max())]
R["synthetic_best"] = [float(diag.best_result.loan_rate), float(diag.best_result.deposit_rate)]
R["expansion_rounds"] = int(diag.expansion_rounds)

rng = make_rng(cfg.random_seed)
L = generate_loan_customers(cfg.simulation.n_loan_customers, rng)
ds = build_loan_response_dataset(L, lg, cfg.market.competitor_loan_rate, cfg.response.loan_rate_elasticity,
                                 cfg.response.loan_competitor_gap_elasticity, rng, n_samples=15000)
hgb = HistGBMResponseModel(42)
hgb.fit(ds, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
ad = MLResponseFnAdapter(hgb.fitted_model, LOAN_FEATURE_COLUMNS)
c = cfg.market.competitor_loan_rate
R["hgb_in_support_competitor_shift(r=5.0%)"] = {
    str(bps): round(float(ad(L, 0.05, c + bps / 1e4).mean()), 4) for bps in (-50, 0, 50)}

tmp = Path(tempfile.mkdtemp(prefix="mlgov_hgb_", dir=str(Path(__file__).with_name("tmp"))))
p = tmp / "hgb.joblib"
joblib.dump(hgb.fitted_model, p)
reg = ModelRegistry(tmp / "reg.json")
reg.register({"model_name": "HistGBMResponseModel", "champion_status": "challenger",
              "governance_status": "pending_human_approval", "feature_columns": list(LOAN_FEATURE_COLUMNS),
              "target_column": LOAN_TARGET_COLUMN, "artifact_path": str(p), "version": "v1"})
reg.promote_champion("HistGBMResponseModel", approver="audit")
d = PricingService().run_scenario(PricingScenarioRequest(config=cfg, registry=reg))
R["hgb_champion_pricing"] = {
    "status": d.status.value, "loan_rate": d.best_loan_rate, "deposit_rate": d.best_deposit_rate,
    "boundary_warning": d.boundary_warning_flag, "expansion_capped": d.expansion_capped,
    "warn_gates": [g.code for g in d.governance.gates if g.status.value == "WARN"],
    "lineage_loan": d.model_lineage["loan"].response_source,
}
out = Path(__file__).with_name("phase2_extra_results.json")
out.write_text(json.dumps(R, indent=2))
print(json.dumps(R, indent=1))
