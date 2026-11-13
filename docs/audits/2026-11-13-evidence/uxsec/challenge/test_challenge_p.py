"""Audit challenge case P: home-loan vs term-deposit separation (services + AppTest).

Loads the repo's own fixtures (tests/conftest.py, tests/unit/conftest.py) without editing the repo.
"""
import importlib.util, sys
from pathlib import Path

REPO = __import__('pathlib').Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "tests" / "unit"))

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

_root = _load("audit_root_conftest", REPO / "tests" / "conftest.py")
_unit = _load("audit_unit_conftest", REPO / "tests" / "unit" / "conftest.py")
make_record = _root.make_record
market_factory = _unit.market_factory
dual_market_factory = _unit.dual_market_factory

import pytest
from dashboard_harness import make_services, run_page
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.models import (
    SCENARIO_HOME_LOAN, SCENARIO_TERM_DEPOSIT, ChangeFilters, ChangeWindow, ScenarioRequest,
)

HL, TD = ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT


def test_P1_services_never_mix_products(dual_market_factory):
    s = make_services(dual_market_factory())
    hl, td = s.overview(HL).value, s.overview(TD).value
    assert (hl.product_type, td.product_type) == (HL, TD)
    assert hl.selectable_terms == (12,) and 6 in td.selectable_terms
    hl12, td12 = s.term_summary(12, HL).value, s.term_summary(12, TD).value
    assert hl12.best_pct == hl12.lowest_pct and td12.best_pct == td12.highest_pct
    assert hl12.best_pct != td12.best_pct
    # A deposit-only term is EMPTY for mortgages -- never substituted.
    r = s.term_summary(6, HL)
    assert r.status == "EMPTY" and "mortgage" in r.message
    # Changes are product-scoped.
    for product in (HL, TD):
        rows = s.changes(ChangeWindow("30d"), ChangeFilters(), product).value or ()
        assert all(row.product_type is product for row in rows)
    # Headers are per product.
    assert s.header(HL).value.product_type is HL and s.header(TD).value.product_type is TD
    # PIE/managed-fund rows never reach the deposit matrix.
    assert all(c.rate_pct not in (4.60, 6.00) for c in td.matrix.cells.values())


def test_P2_deposit_market_missing_is_empty_not_mortgage(market_factory):
    s = make_services(market_factory())  # mortgage-only database
    r = s.overview(TD)
    assert r.status == "EMPTY" and "Mortgage rates are never substituted" in r.message
    r = s.scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None, mode=SCENARIO_TERM_DEPOSIT))
    assert r.status == "EMPTY" and "term-deposit" in r.message


def test_P3_apptest_market_explorer_toggle(dual_market_factory):
    m = dual_market_factory()
    app = run_page("market_watch", m.database, m.now)
    assert not app.exception
    def metric_map(a): return {x.label: x.value for x in a.metric}
    hl = metric_map(app)
    assert "Current mortgage rate" in hl
    app.segmented_control(key="market_watch_product").set_value("term_deposit").run()
    td = metric_map(app)
    assert "Current deposit rate" in td and "Current mortgage rate" not in td
    first_rank = app.dataframe  # ranking frame
    ranking = next(d.value for d in first_rank if "Rank" in d.value.columns)
    assert ranking.iloc[0]["Bank"] == "Westpac" and ranking.iloc[0]["Rate"] == "4.25%"
    # Choose the deposit-only 6M term, then switch back: mortgages must not inherit 6M.
    sb = app.selectbox(key="market_watch_term_term_deposit")
    six = [o for o in sb.options if o.startswith("6")]
    assert six, sb.options
    sb.select(six[0]).run()
    td6 = next(d.value for d in app.dataframe if "Rank" in d.value.columns)
    assert td6.iloc[0]["Rate"] != "4.25%"  # 6M deposit market differs from 12M
    app.segmented_control(key="market_watch_product").set_value("home_loan").run()
    back = metric_map(app)
    assert "Current mortgage rate" in back
    ranking = next(d.value for d in app.dataframe if "Rank" in d.value.columns)
    assert ranking.iloc[0]["Bank"] == "ANZ" and ranking.iloc[0]["Rate"] == "4.79%"
    assert not app.exception


def test_P4_deposit_scenario_headlines_deposit_rate(dual_market_factory):
    s = make_services(dual_market_factory())
    td = s.scenarios(ScenarioRequest(12, "MARKET_LEADER", None, 0.0, None, mode=SCENARIO_TERM_DEPOSIT)).value
    hl = s.scenarios(ScenarioRequest(12, "MARKET_LEADER", None, 0.0, None, mode=SCENARIO_HOME_LOAN)).value
    assert td.selected.our_rate == td.selected.deposit_rate
    assert hl.selected.our_rate == hl.selected.loan_rate
    assert abs(td.selected.competitor_rate - 0.0425) < 1e-9  # deposit leader = highest
    assert abs(hl.selected.competitor_rate - 0.0479) < 1e-9  # mortgage leader = lowest
    print("\nP4 HL leader scenario: selected optimum", hl.selected.our_rate,
          "| 'Model optimum' marker/text", hl.model_optimum_rate)
    print("P4 TD leader scenario: selected optimum", td.selected.our_rate,
          "| 'Model optimum' marker/text", td.model_optimum_rate)
