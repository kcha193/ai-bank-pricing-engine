import json, warnings
import numpy as np
warnings.simplefilter("ignore")
from balance_pricing_engine.config import load_config, GridConfig, LoanTierConfig, DepositTierConfig, SegmentedPricingConfig, ProductConfig
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
CFG=__import__('pathlib').Path(__file__).resolve().parents[4].joinpath("configs/base.yaml").as_posix()
b=load_config(CFG)
def s(d):
    r=d.best_result; g=d.grid_results
    return dict(loan=round(r.loan_rate,4), dep=round(r.deposit_rate,4), obj=round(r.total_objective), L=round(r.expected_loan_volume), D=round(r.expected_deposit_volume), short=round(r.funding_shortfall), bw=d.boundary_diagnostics.has_warning, pb=d.policy_band_breached)
out={}
# 1. deposit pool sensitivity
for nd in (5000, 10000, 25000):
    c=b.model_copy(update={"simulation": b.simulation.model_copy(update={"n_deposit_customers": nd})})
    out[f"n_deposit_customers_{nd}"]=s(run_baseline_diagnostics(c))
# 2. no funding ratio
c=b.model_copy(update={"balance_sheet": b.balance_sheet.model_copy(update={"required_deposit_funding_ratio":0.0})})
out["required_ratio_0"]=s(run_baseline_diagnostics(c))
# 3. ECL scaled to 5bp-ish: compute baseline avg ECL rate
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng
lc=generate_loan_customers(5000, make_rng(42))
out["mean_expected_loss_rate"]=float(lc.expected_loss_rate.mean())
out["balance_weighted_expected_loss_rate"]=float((lc.expected_loss_rate*lc.loan_amount).sum()/lc.loan_amount.sum())
out["loss_rate_pctiles"]=[float(x) for x in np.percentile(lc.expected_loss_rate,[5,50,95])]
# 4. wide grid segmented vs joint (single tier each)
lg=GridConfig(start=0.03, stop=0.09, step=0.0005); dg=GridConfig(start=0.02, stop=0.06, step=0.0005)
seg=SegmentedPricingConfig(loan_tiers=[LoanTierConfig(name="all", rate_grid=lg)], deposit_tiers=[DepositTierConfig(name="all", rate_grid=dg)])
sc=b.model_copy(update={"pricing_strategy":None,"segmented_pricing":seg})
jc=b.model_copy(update={"pricing_strategy":None,"products":ProductConfig(loan_rate_grid=lg, deposit_rate_grid=dg)})
S=run_baseline_diagnostics(sc, max_boundary_expansions=0); J=run_baseline_diagnostics(jc, max_boundary_expansions=0)
out["wide_single_tier"]={"segmented":s(S),"joint":s(J),"objective_gap":J.best_result.total_objective-S.best_result.total_objective}
# 5. two deposit tiers order dependence: same tiers, same customers; swap catch-all impossible, so compare 2-tier vs 1-tier
seg2=SegmentedPricingConfig(loan_tiers=[LoanTierConfig(name="all", rate_grid=lg)], deposit_tiers=[DepositTierConfig(name="small", balance_max=100000, rate_grid=dg), DepositTierConfig(name="large", rate_grid=dg)])
S2=run_baseline_diagnostics(b.model_copy(update={"pricing_strategy":None,"segmented_pricing":seg2}), max_boundary_expansions=0)
out["wide_two_deposit_tiers"]=s(S2); out["wide_two_deposit_tiers_tiers"]=S2.segmented_tier_rates
# 6. registry ignored for segmented: show code path returns before registry
import inspect, balance_pricing_engine.scenarios.runner as R
src=inspect.getsource(R.run_baseline_diagnostics)
out["segmented_returns_before_registry"]= src.index("segmented_pricing is not None") < src.index("if registry is not None")
print(json.dumps(out, indent=1))
