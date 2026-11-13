import json, warnings, tempfile, os
warnings.simplefilter("ignore")
from balance_pricing_engine.config import load_config, GridConfig, LoanTierConfig, DepositTierConfig, SegmentedPricingConfig
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.application.contracts import PricingScenarioRequest
b=load_config(__import__('pathlib').Path(__file__).resolve().parents[4].joinpath("configs/base.yaml").as_posix())
tmp=tempfile.mkdtemp()
os.chdir(tmp)  # any relative writes land in temp
def run(cfg, name):
    d=PricingService().run_scenario(PricingScenarioRequest(config=cfg, scenario_name=name, ledger_base_dir=tmp))
    return {"status": d.governance.status.value, "loan": d.best_loan_rate, "dep": d.best_deposit_rate,
            "obj": round(d.objective_value), "gates": {g.code: g.status.value for g in d.governance.gates},
            "review_reasons": list(d.governance.review_reasons)}
out={}
out["baseline"]=run(b,"baseline")
out["roe_floor_1000pct"]=run(b.model_copy(update={"balance_sheet": b.balance_sheet.model_copy(update={"min_return_on_equity":10.0})}),"roe")
nan=b.model_dump(); nan["market"]["competitor_loan_rate"]=float("nan")
from balance_pricing_engine.config import EngineConfig
try: out["nan_competitor"]=run(EngineConfig.model_validate(nan),"nan")
except Exception as e: out["nan_competitor_error"]=f"{type(e).__name__}: {e}"[:300]
lg=GridConfig(start=0.03, stop=0.09, step=0.0005); dg=GridConfig(start=0.02, stop=0.06, step=0.0005)
seg=SegmentedPricingConfig(loan_tiers=[LoanTierConfig(name="all", rate_grid=lg)], deposit_tiers=[DepositTierConfig(name="all", rate_grid=dg)])
out["segmented_wide_single_tier"]=run(b.model_copy(update={"pricing_strategy":None,"segmented_pricing":seg}),"seg")
# within-policy-band-only (no expansion)
d=PricingService().run_scenario(PricingScenarioRequest(config=b, scenario_name="noexp", max_boundary_expansions=0, ledger_base_dir=tmp))
out["baseline_no_expansion"]={"status": d.governance.status.value, "loan": d.best_loan_rate, "obj": round(d.objective_value)}
print(json.dumps(out, indent=1, default=str)); print("tmp", tmp)
