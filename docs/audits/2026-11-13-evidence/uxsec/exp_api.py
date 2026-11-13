"""SEC experiments against the FastAPI app via TestClient (in-process, no network bind)."""
import os, copy, yaml
os.environ.pop("PRICING_ENGINE_API_KEYS", None)
REPO = str(__import__('pathlib').Path(__file__).resolve().parents[4])
os.chdir(REPO)  # app reads configs/base.yaml relative
from fastapi.testclient import TestClient
from balance_pricing_engine.api.app import app
from balance_pricing_engine.config import EngineConfig
import sys; api_app = sys.modules["balance_pricing_engine.api.app"]

c = TestClient(app)
base = yaml.safe_load(open("configs/base.yaml"))

# 1. Auth default: is a protected route reachable without a key when env var unset?
#    Use a config that fails validation so no compute runs: expect 422 (not 401) => auth off.
r = c.post("/run-scenario", json={"config_yaml": "random_seed: 1"})
print("1 unauth /run-scenario (invalid cfg) status:", r.status_code, "(401 would mean auth on)")
r = c.post("/v1/pricing/scenarios", json={"config_yaml": "random_seed: 1"})
print("1 unauth /v1 (invalid cfg) status:", r.status_code)

# 2. Resource bounds: does validation accept absurd sizes? (validate only, no compute)
big = copy.deepcopy(base)
big["simulation"]["n_loan_customers"] = 10**10
big["simulation"]["n_deposit_customers"] = 10**10
try:
    EngineConfig(**big); print("2 n_customers=1e10 accepted by EngineConfig: True")
except Exception as e:
    print("2 n_customers=1e10 rejected:", type(e).__name__)
tiny = copy.deepcopy(base)
ps = tiny.get("pricing_strategy") or {}
if ps:
    ps["home_loan_spread_bps"]["step"] = 1e-6
    ps["term_deposit_spread_bps"]["step"] = 1e-6
try:
    EngineConfig(**tiny); print("2 spread step=1e-6 bps accepted by EngineConfig: True")
except Exception as e:
    print("2 tiny step rejected:", type(e).__name__, str(e)[:120])
print("2 config_yaml max_length constraint:",
      api_app.RunScenarioRequest.model_fields["config_yaml"].metadata)

# 3. File-existence oracle through competitor_rates.source_file (no compute reached if file missing)
probe = copy.deepcopy(base)
probe["competitor_rates"] = {"enabled": True, "source_file": "/etc/definitely_missing_audit.json"}
r = c.post("/v1/pricing/scenarios", json={"config_yaml": yaml.safe_dump(probe)})
print("3 missing file:", r.status_code, r.json().get("error_code"), "|", r.json().get("message")[:120])
probe["competitor_rates"]["source_file"] = "/etc/hosts"
r = c.post("/v1/pricing/scenarios", json={"config_yaml": yaml.safe_dump(probe)})
print("3 existing non-JSON file:", r.status_code, r.json().get("error_code"), "|", (r.json().get("message") or "")[:160])

# 4. /ready error leakage with a bad config path
os.environ["PRICING_ENGINE_CONFIG_PATH"] = "/nonexistent/audit/base.yaml"
r = c.get("/ready")
print("4 /ready bad path:", r.status_code, r.json())
os.environ.pop("PRICING_ENGINE_CONFIG_PATH")

# 5. CORS / security headers
r = c.options("/v1/pricing/scenarios", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
print("5 CORS preflight:", r.status_code, {k: v for k, v in r.headers.items() if k.lower().startswith("access-control")})
r = c.get("/health")
print("5 /health headers:", dict(r.headers))
print("5 docs exposed:", c.get("/docs").status_code, c.get("/openapi.json").status_code)

# 6. Validation error echo (422 includes str(ValidationError) with input values)
r = c.post("/v1/pricing/scenarios", json={"config_yaml": "random_seed: 1\nsimulation: {n_loan_customers: -5, n_deposit_customers: 'SECRET_INPUT_ECHO'}"})
print("6 422 echoes input:", "SECRET_INPUT_ECHO" in r.text, r.status_code)
