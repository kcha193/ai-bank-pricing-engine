"""Phase 8: gates, publisher deadline, decision-record additivity/config-hash, LLM patch path (mock client)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings("ignore")

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import load_config
from balance_pricing_engine.events.models import pricing_scenario_requested
from balance_pricing_engine.events.publisher import NoOpEventPublisher, publish_safely
from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.governance.gates import decision_status_from_gates, evaluate_gates
from balance_pricing_engine.governance.lineage import synthetic_model_lineage
from balance_pricing_engine.llm.scenario_interface import NLScenarioInterface
from balance_pricing_engine.run_ledger import write_run_ledger
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

HERE = Path(__file__).parent
TMP = Path(tempfile.mkdtemp(prefix="mlgov_p8_", dir=str(HERE / "tmp")))
R: dict = {}
cfg = load_config("configs/base.yaml")


def small(c, n=1500):
    return c.model_copy(update={"simulation": c.simulation.model_copy(
        update={"n_loan_customers": n, "n_deposit_customers": n})})


# ---- 1. gate combinations with stub diagnostics (gates are pure functions)
def stub(obj=1e6, shortfall=0.0, boundary=False, capped=False, band=False, loan_vol=1e8, roe=0.15):
    return SimpleNamespace(
        best_result=SimpleNamespace(total_objective=obj, funding_shortfall=shortfall,
                                    expected_loan_volume=loan_vol, return_on_equity=roe),
        boundary_diagnostics=SimpleNamespace(has_warning=boundary, warnings=("x",) if boundary else ()),
        expansion_capped=capped, expansion_rounds=0, policy_band_breached=band)


fl = FeatureLineage("f", "v1", "abc", "loan", "now", 1, "synthetic_local_store")
ml = {"loan": synthetic_model_lineage("loan"), "deposit": synthetic_model_lineage("deposit")}
fll = {"loan": fl, "deposit": fl}
cases = {
    "clean_interior": stub(),
    "negative_objective_loss_making": stub(obj=-5e6),
    "zero_loan_volume": stub(loan_vol=0.0),
    "roe_below_floor(0.01)": stub(roe=0.01),
    "boundary_warning": stub(boundary=True),
    "policy_band_breach": stub(band=True),
    "funding_shortfall": stub(shortfall=1.0),
    "objective_nan": stub(obj=float("nan")),
    "objective_inf": stub(obj=float("inf")),
}
R["gate_combinations"] = {
    k: decision_status_from_gates(evaluate_gates(config=cfg, diagnostics=v, model_lineage=ml,
                                                 feature_lineage=fll)).value
    for k, v in cases.items()}
R["gate_missing_lineage"] = decision_status_from_gates(
    evaluate_gates(config=cfg, diagnostics=stub(), model_lineage={}, feature_lineage={})).value

# ---- 2. real run: infeasible ROE floor
bs = cfg.balance_sheet.model_copy(update={"min_return_on_equity": 5.0})
cfg_roe = small(cfg).model_copy(update={"balance_sheet": bs})
d = PricingService().run_scenario(PricingScenarioRequest(config=cfg_roe))
gr = d.diagnostics.grid_results
R["infeasible_roe_floor_500pct"] = {
    "rows_meeting_floor": int((gr["return_on_equity"] >= 5.0).sum()),
    "best_roe": float(gr.loc[(gr.loan_rate == d.best_loan_rate) & (gr.deposit_rate == d.best_deposit_rate), "return_on_equity"].iloc[0]), "max_roe_in_grid": float(gr["return_on_equity"].max()),
    "status": d.status.value,
    "warn_gates": [g.code for g in d.governance.gates if g.status.value == "WARN"],
    "any_gate_mentions_roe": any("ROE" in g.code or "EQUITY" in g.code for g in d.governance.gates),
}


# ---- 3. publish_safely deadlines
release = threading.Event()


class Hung:
    calls = 0

    def publish(self, event):
        Hung.calls += 1
        release.wait(60)


class Boom:
    def publish(self, event):
        raise RuntimeError("broker down")


ev = pricing_scenario_requested("cid", scenario_name="s")
t0 = time.monotonic(); publish_safely(Hung(), ev, deadline_seconds=0.2); t_hung = time.monotonic() - t0
t0 = time.monotonic(); publish_safely(Boom(), ev, deadline_seconds=0.2); t_boom = time.monotonic() - t0
# saturate the 16-slot cap
for _ in range(20):
    publish_safely(Hung(), ev, deadline_seconds=0.01)
t0 = time.monotonic(); publish_safely(Hung(), ev, deadline_seconds=1.0); t_capped = time.monotonic() - t0
R["publish_safely"] = {"hung_deadline_0.2s_elapsed": round(t_hung, 3), "raising_elapsed": round(t_boom, 3),
                       "after_cap_saturated_elapsed(deadline 1s)": round(t_capped, 3),
                       "hung_publish_calls_started": Hung.calls}
release.set(); time.sleep(0.3)

release2 = threading.Event()


class Hung2:
    def publish(self, event):
        release2.wait(60)


sc = small(cfg)
t0 = time.monotonic(); d_ok = PricingService().run_scenario(PricingScenarioRequest(config=sc)); t_ok = time.monotonic() - t0
t0 = time.monotonic(); d_h = PricingService().run_scenario(PricingScenarioRequest(config=sc, event_publisher=Hung2()))
t_h = time.monotonic() - t0
release2.set()
R["pricing_service_hung_publisher"] = {"noop_seconds": round(t_ok, 2), "hung_seconds": round(t_h, 2),
                                       "overhead_seconds": round(t_h - t_ok, 2),
                                       "same_rates": (d_ok.best_loan_rate, d_ok.best_deposit_rate)
                                       == (d_h.best_loan_rate, d_h.best_deposit_rate),
                                       "same_status": d_ok.status == d_h.status}

# ---- 4. decision_record additivity + config_hash fidelity
led1 = TMP / "ledger_service"; led2 = TMP / "ledger_direct"
d1 = PricingService().run_scenario(PricingScenarioRequest(config=cfg, config_path="configs/base.yaml",
                                                         persist_ledger=True, ledger_base_dir=led1))
diag = run_baseline_diagnostics(cfg)
rd2 = write_run_ledger(scenario_name="scenario", config=cfg, config_path="configs/base.yaml",
                       diagnostics=diag, base_dir=led2)
s1 = json.loads((d1.run_dir / "summary.json").read_text()); s2 = json.loads((rd2 / "summary.json").read_text())
m1 = json.loads((d1.run_dir / "metadata.json").read_text()); m2 = json.loads((rd2 / "metadata.json").read_text())
R["decision_record_additivity"] = {
    "service_files": sorted(p.name for p in d1.run_dir.iterdir()),
    "direct_files": sorted(p.name for p in rd2.iterdir()),
    "summary_equal": s1 == s2,
    "metadata_keys_equal": sorted(m1) == sorted(m2),
    "metadata_values_differ_only_in": sorted(k for k in m1 if m1[k] != m2.get(k)),
}
# mutated in-memory config + config_path pointing at the unmodified YAML
resp = cfg.response.model_copy(update={"loan_rate_elasticity": -60.0})
cfg_mut = cfg.model_copy(update={"response": resp})
led3 = TMP / "ledger_mutated"
d3 = PricingService().run_scenario(PricingScenarioRequest(config=cfg_mut, config_path="configs/base.yaml",
                                                         persist_ledger=True, ledger_base_dir=led3))
rec3 = json.loads((d3.run_dir / "decision_record.json").read_text())
snap3 = (d3.run_dir / "config_snapshot.yaml").read_text()
R["config_hash_fidelity"] = {
    "priced_loan_elasticity": -60.0,
    "priced_loan_rate": d3.best_loan_rate, "base_loan_rate": d1.best_loan_rate,
    "decision_record_config_hash_equals_sha256(base.yaml)":
        rec3["config_hash"] == hashlib.sha256(Path("configs/base.yaml").read_bytes()).hexdigest(),
    "snapshot_contains_-60": "-60" in snap3,
    "snapshot_loan_rate_elasticity_line": [ln.strip() for ln in snap3.splitlines() if "loan_rate_elasticity" in ln],
    "decision_record_keys": sorted(rec3),
}

# ---- 5. LLM patch path with a mock client (no network)


class _Msg:
    def __init__(self, text):
        self.content = [SimpleNamespace(text=text)]


class MockClient:
    class messages:  # noqa: N801
        @staticmethod
        def create(**kw):
            return _Msg(json.dumps({"patches": [
                {"path": ["response", "loan_rate_elasticity"], "value": 170.0},
                {"path": ["response", "loan_competitor_gap_elasticity"], "value": 130.0},
                {"path": ["balance_sheet", "funding_shortfall_penalty_rate"], "value": 0.0},
            ]}))


audit = TMP / "nl_audit.jsonl"
iface = NLScenarioInterface(client=MockClient(), audit_log_path=audit)
small_cfg = small(cfg)
prop = iface.propose(small_cfg, "make loans more attractive")
patched = iface.apply(small_cfg, prop, approve=True)
dd = run_baseline_diagnostics(patched)
R["llm_patch_path"] = {
    "diff_yaml_tagged": "[DECISION-SUPPORT: requires human review]" in prop.diff_yaml,
    "patched_loan_rate_elasticity": patched.response.loan_rate_elasticity,
    "config_validation_accepted_wrong_sign": True,
    "resulting_loan_rate": float(dd.best_result.loan_rate),
    "boundary_warning": bool(dd.boundary_diagnostics.has_warning),
    "audit_log_entry": json.loads(audit.read_text().splitlines()[-1]),
}

out = HERE / "phase8_results.json"
out.write_text(json.dumps(R, indent=2, default=str))
print(json.dumps(R, indent=1, default=str))
