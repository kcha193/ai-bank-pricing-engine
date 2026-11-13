import warnings, tempfile, os
warnings.simplefilter("ignore")
from balance_pricing_engine.config import load_config, EngineConfig
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.application.contracts import PricingScenarioRequest
b=load_config(__import__('pathlib').Path(__file__).resolve().parents[4].joinpath("configs/base.yaml").as_posix()).model_dump()
b["market"]["competitor_loan_rate"]=float("nan"); t=tempfile.mkdtemp(); os.chdir(t)
d=PricingService().run_scenario(PricingScenarioRequest(config=EngineConfig.model_validate(b), ledger_base_dir=t))
print(d.governance.status.value, d.best_loan_rate, d.objective_value, list(d.governance.blocking_reasons))
