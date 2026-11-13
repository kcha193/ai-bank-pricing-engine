import importlib.util, sys
from pathlib import Path
REPO = __import__('pathlib').Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "tests" / "unit"))
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
_root = _load("audit_root_conftest3", REPO / "tests" / "conftest.py")
_unit = _load("audit_unit_conftest3", REPO / "tests" / "unit" / "conftest.py")
make_record = _root.make_record; market_factory = _unit.market_factory
from dashboard_harness import make_services
from balance_pricing_engine.dashboard.insights import build_insights
from balance_pricing_engine.competitor_rates.models import ProductType as PT


def test_quarantine_insight_truncated(market_factory):
    m = market_factory(stale_banks=("BNZ",), anz_new_status="warning")
    s = make_services(m)
    ov = s.overview(PT.HOME_LOAN).value
    summ = s.term_summary(12, PT.HOME_LOAN).value
    pos = s.bank_positions(12, PT.HOME_LOAN).value
    quarantined = [p.bank for p in pos if p.quarantined]
    full = build_insights(ov, summ, pos, "ASB")
    print("\nquarantined banks:", quarantined, "| exclusions:", [e.bank for e in ov.exclusions],
          "| changes_7d:", ov.changes_7d)
    for line in full: print("  -", line)
    assert quarantined, "fixture should quarantine ANZ"
    assert not any("quarantined" in line for line in full)
