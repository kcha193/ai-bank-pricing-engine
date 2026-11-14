"""Phase 1f (UX-004): modelled and observed numbers are labelled, in words, wherever they appear.

MODELLED · synthetic  -- optimiser output from synthetic customers (Pricing Studio, Scenario Lab).
OBSERVED · market rates -- bank-advertised rates collected from public pages.
"""

import pytest
from dashboard_harness import badge_labels, badges, frame_with, make_services, run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.formatting import fmt_rate_pct
from balance_pricing_engine.dashboard.models import SCENARIO_JOINT, SCENARIO_TERM_DEPOSIT

MODELLED = "MODELLED · synthetic"
OBSERVED = "OBSERVED · market rates"
HL, TD = ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT


def count(app, label: str) -> int:
    return badge_labels(app).count(label)


def test_the_labels_are_exported_constants_of_the_shared_ui_helper():
    assert ui.MODELLED_LABEL == MODELLED and ui.OBSERVED_LABEL == OBSERVED


# ------------------------------------------------------------------ Pricing Studio


def test_t15_pricing_studio_labels_results_modelled_synthetic(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    assert count(app, MODELLED) == 0  # nothing modelled is on screen before a run
    app.button(key="scenario_run").click().run()
    assert not app.exception
    assert count(app, MODELLED) >= 2  # headline results and the comparison charts/table
    text = " ".join(m.value for m in app.markdown) + " ".join(c.value for c in app.caption)
    assert "model-best" in text.lower() and "observed" in text.lower()


def test_pricing_studio_names_the_observed_inputs_before_a_run(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    assert count(app, OBSERVED) == 1 and count(app, MODELLED) == 0


@pytest.mark.parametrize("mode", [SCENARIO_TERM_DEPOSIT, SCENARIO_JOINT])
def test_every_scenario_mode_is_labelled_modelled(dual_market_factory, mode):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(mode).run()
    key = "scenario_run_term_deposit" if mode == SCENARIO_TERM_DEPOSIT else "scenario_run_joint"
    app.button(key=key).click().run()
    assert not app.exception
    assert count(app, MODELLED) >= 1


def test_the_joint_scenario_names_both_modelled_outputs_by_product(dual_market_factory):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_JOINT).run()
    app.button(key="scenario_run_joint").click().run()
    labels = [m.label for m in app.metric]
    assert any(label.startswith("⌂ Home-loan") for label in labels)
    assert any(label.startswith("◎ Term-deposit") for label in labels)


# ----------------------------------------------------------------- observed pages


@pytest.mark.parametrize("product", [HL, TD])
def test_t16_t17_market_explorer_labels_observed_for_each_product(dual_market_factory, product):
    market = dual_market_factory()
    app = run_page("market_watch", market.database, market.now)
    if product is TD:
        app.segmented_control(key="market_watch_product").set_value("term_deposit").run()
    assert not app.exception
    assert count(app, OBSERVED) >= 1 and count(app, MODELLED) == 0


def test_t18_command_centre_labels_both_market_panels_and_the_detail(dual_market_factory):
    market = dual_market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    assert count(app, OBSERVED) >= 3  # home-loan panel, term-deposit panel, detailed analysis
    assert count(app, MODELLED) == 0


@pytest.mark.parametrize("page", ["trends", "rate_changes"])
def test_t18_historical_observation_pages_are_labelled_observed(dual_market_factory, page):
    market = dual_market_factory()
    app = run_page(page, market.database, market.now)
    assert not app.exception
    assert count(app, OBSERVED) >= 1 and count(app, MODELLED) == 0


def test_scenario_lab_results_are_labelled_modelled(tmp_path):
    from datetime import datetime, timezone

    app = run_page("scenario_lab", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert count(app, MODELLED) == 0  # nothing modelled until the lab is run
    app.button(key="lab_run").click().run()
    assert not app.exception
    assert count(app, MODELLED) == 1 and count(app, OBSERVED) == 0


def test_an_observed_label_never_claims_a_quarantined_value_is_a_new_price(
    quarantine_market_factory,
):
    market = quarantine_market_factory(mortgage=True, deposit=False)
    app = run_page("market_watch", market.database, market.now)
    assert not app.exception
    frame = app.dataframe[0].value.set_index("Bank")
    assert "QUARANTINED" in frame.loc["ANZ", "Freshness"]  # T19: existing marking is kept
    assert not any("new price" in label.lower() for label in badge_labels(app))
    help_text = " ".join(text for _, text in badges(app))
    assert "quarantined" in help_text.lower() and "last confirmed" in help_text.lower()


def test_stale_banks_keep_their_exclusion_notices_beside_the_label(dual_market_factory):
    market = dual_market_factory(stale_banks=("Westpac",))
    app = run_page("overview", market.database, market.now)
    assert any("Westpac" in w.value for w in app.warning) or any(
        "Westpac" in c.value for c in app.caption
    )
    assert count(app, OBSERVED) >= 1


def test_t20_labels_do_not_change_observed_or_modelled_numbers(market_factory):
    market = market_factory()
    truth = {p.bank: fmt_rate_pct(p.rate_pct) for p in make_services(market).bank_positions(12).value}
    app = run_page("market_watch", market.database, market.now)
    table = frame_with(app, "Rate").set_index("Bank")
    assert {bank: table.loc[bank, "Rate"] for bank in truth} == truth


@pytest.mark.parametrize("product", ["home_loan", "term_deposit"])
def test_t21_pages_render_in_both_product_modes(dual_market_factory, product):
    market = dual_market_factory()
    for page, key in (
        ("market_watch", "market_watch_product"),
        ("trends", "trends_product"),
        ("rate_changes", "rate_changes_product"),
        ("overview", "overview_product"),
    ):
        app = run_page(page, market.database, market.now)
        app.segmented_control(key=key).set_value(product).run()
        assert not app.exception, page
        assert count(app, OBSERVED) >= 1, page
