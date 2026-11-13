"""UX experiments on Pricing Studio (AppTest, offline, real optimiser on fixture DB)."""
import importlib.util, sys
from pathlib import Path
REPO = __import__('pathlib').Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "tests" / "unit"))
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
_root = _load("audit_root_conftest2", REPO / "tests" / "conftest.py")
_unit = _load("audit_unit_conftest2", REPO / "tests" / "unit" / "conftest.py")
make_record = _root.make_record; market_factory = _unit.market_factory
dual_market_factory = _unit.dual_market_factory
from dashboard_harness import run_page


def _metrics(app): return {m.label: m.value for m in app.metric}
def _text(app): return " ".join(m.value for m in app.markdown)


def test_UX_conflicting_optimum_numbers(market_factory):
    m = market_factory()
    app = run_page("scenarios", m.database, m.now)
    app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
    app.button(key="scenario_run").click().run()
    assert not app.exception
    headline = _metrics(app)["Optimal home-loan rate"]
    text = _text(app)
    import re
    found = re.search(r"Under current assumptions, the model optimum home-loan rate is ([0-9.]+%)", text)
    print("\nUX headline 'Optimal home-loan rate':", headline)
    print("UX decision text model optimum:", found.group(1) if found else None)
    print("UX rationale:", [c.value for c in app.caption if "Selected assumption" in c.value][:1])
    assert found and found.group(1) != headline  # demonstrates the conflict


def test_UX_stale_result_still_rendered_after_input_change(market_factory):
    m = market_factory()
    app = run_page("scenarios", m.database, m.now)
    app.button(key="scenario_run").click().run()
    before = _metrics(app)["Optimal home-loan rate"]
    app.number_input(key="scenario_offset").set_value(-100.0).run()  # no rerun of optimiser
    after = _metrics(app).get("Optimal home-loan rate")
    caps = [c.value for c in app.caption if "Inputs changed" in c.value]
    warns = [w.value for w in app.warning if "Inputs changed" in w.value]
    print("\nUX before:", before, "after input change (no rerun):", after,
          "| caption:", caps, "| warning:", warns)
    assert after == before and caps and not warns


def test_UX_deposit_mode_objective_label(dual_market_factory):
    m = dual_market_factory()
    app = run_page("scenarios", m.database, m.now)
    app.segmented_control(key="scenario_mode").set_value("term_deposit").run()
    app.button(key="scenario_run_term_deposit").click().run()
    assert not app.exception
    mt = _metrics(app)
    print("\nUX deposit metrics:", mt)
    assert "Optimal term-deposit rate" in mt and "Optimal home-loan rate" not in mt
