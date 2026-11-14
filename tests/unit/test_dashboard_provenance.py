"""Phase 1f (UX-004): modelled and observed numbers are labelled, in words, wherever they appear.

MODELLED · synthetic  -- optimiser output from synthetic customers (Pricing Studio, Scenario Lab).
OBSERVED · market rates -- bank-advertised rates collected from public pages.
"""

import pytest
from dashboard_harness import badge_labels, badges, frame_with, make_services, run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.formatting import fmt_rate_pct
from balance_pricing_engine.dashboard.models import (
    SCENARIO_HOME_LOAN,
    SCENARIO_JOINT,
    SCENARIO_TERM_DEPOSIT,
    ScenarioRequest,
)

MODELLED = "MODELLED · synthetic"
OBSERVED = "OBSERVED · market rates"
HL, TD = ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT
ASSUMPTION = "ASSUMPTION · user-entered"


def count(app, label: str) -> int:
    return badge_labels(app).count(label)


def input_provenance(app, side: str) -> list[str]:
    """Read the selected input's explanation, separate from comparison/output captions."""
    return [c.value for c in app.caption if c.value.startswith(f"{side} competitor input:")]


@pytest.mark.parametrize(
    "mode,side,suffix,rate",
    [
        (SCENARIO_HOME_LOAN, "Home-loan", "", 5.09),
        (SCENARIO_TERM_DEPOSIT, "Term-deposit", "_term_deposit", 4.09),
    ],
)
def test_manual_input_and_result_have_user_entered_provenance(
    dual_market_factory, mode, side, suffix, rate
):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(mode).run()
    app.selectbox(key=f"scenario_strategy{suffix}").select("MANUAL").run()
    app.number_input(key=f"scenario_manual_rate{suffix}").set_value(rate).run()
    assert not app.exception
    assert count(app, ASSUMPTION) == 1 and count(app, OBSERVED) == 0
    notes = input_provenance(app, side)
    assert len(notes) == 1 and "user-entered" in notes[0]
    assert "not an observed bank quote" in notes[0]
    app.button(key=f"scenario_run{suffix}").click().run()
    assert not app.exception
    notes = input_provenance(app, side)
    assert len(notes) == 2 and notes[0] == notes[1]  # inputs and stored result agree
    assert count(app, ASSUMPTION) == 2 and count(app, OBSERVED) == 0
    assert count(app, MODELLED) >= 2
    assert any("comparison" in c.value.lower() and "observed" in c.value for c in app.caption)
    assert not any("Competitor assumptions are reference rates derived" in c.value for c in app.caption)


@pytest.mark.parametrize(
    "strategy,loan_label,deposit_label",
    [
        ("MARKET_MEDIAN", "Market median", "Market median"),
        ("MARKET_LEADER", "Market leader (lowest rate)", "Market leader (highest rate)"),
        ("MARKET_SECOND_LOWEST", "Second-lowest rate", "Second-highest rate"),
        ("MARKET_MEAN", "Market mean", "Market mean"),
    ],
)
@pytest.mark.parametrize("mode", [SCENARIO_HOME_LOAN, SCENARIO_TERM_DEPOSIT])
def test_market_source_and_effective_assumption_remain_distinct(
    dual_market_factory, strategy, loan_label, deposit_label, mode
):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(mode).run()
    suffix = "_term_deposit" if mode == SCENARIO_TERM_DEPOSIT else ""
    side = "Term-deposit" if suffix else "Home-loan"
    app.selectbox(key=f"scenario_strategy{suffix}").select(strategy).run()
    for offset in (0.0, 12.5, -5.0):
        app.number_input(key=f"scenario_offset{suffix}").set_value(offset).run()
        assert not app.exception
        notes = input_provenance(app, side)
        assert len(notes) == 1 and "observed market rates" in notes[0]
        assert (deposit_label if suffix else loan_label) in notes[0]
        assert "not an observed bank quote" in notes[0]
        assert count(app, OBSERVED) == 1 and count(app, ASSUMPTION) == 0
        if offset:
            assert "hypothetical" in notes[0] and f"{offset:+g} bps" in notes[0]
        else:
            assert "offset" not in notes[0].lower()


def test_joint_inputs_and_results_keep_each_products_source_and_offset(dual_market_factory):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_JOINT).run()
    for key in ("scenario_strategy_joint", "scenario_strategy_deposit_joint"):
        assert "Manual competitor rate" not in app.selectbox(key=key).options
    app.selectbox(key="scenario_strategy_joint").select("MARKET_MEAN")
    app.selectbox(key="scenario_strategy_deposit_joint").select("MARKET_SECOND_LOWEST")
    app.number_input(key="scenario_offset_joint").set_value(12.5)
    app.number_input(key="scenario_offset_deposit_joint").set_value(-5.0)
    app.button(key="scenario_run_joint").click().run()
    assert not app.exception
    for side, strategy, offset in (
        ("Home-loan", "Market mean", "+12.5 bps"),
        ("Term-deposit", "Second-highest rate", "-5 bps"),
    ):
        notes = input_provenance(app, side)
        assert len(notes) == 2 and notes[0] == notes[1]
        assert strategy in notes[0] and offset in notes[0]
        assert "hypothetical" in notes[0] and "not an observed bank quote" in notes[0]
    assert count(app, OBSERVED) == 4 and count(app, MODELLED) >= 1
    assert count(app, ASSUMPTION) == 0


def test_missing_market_never_receives_observed_input_provenance(tmp_path):
    from datetime import datetime, timezone

    app = run_page("scenarios", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert count(app, OBSERVED) == count(app, ASSUMPTION) == count(app, MODELLED) == 0
    assert not input_provenance(app, "Home-loan")


def test_manual_provenance_ignores_unused_offset():
    from balance_pricing_engine.dashboard.scenario_text import benchmark_provenance

    entries = benchmark_provenance(ScenarioRequest(12, "MANUAL", 5.09, 99.0, None))
    assert len(entries) == 1 and not entries[0].observed
    assert "user-entered" in entries[0].text and "5.09%" in entries[0].text
    assert "offset" not in entries[0].text.lower() and "99" not in entries[0].text


def test_joint_provenance_uses_backend_deposit_strategy_fallback():
    from balance_pricing_engine.dashboard.scenario_text import benchmark_provenance

    request = ScenarioRequest(
        12, "MARKET_LEADER", None, 0.0, None, mode=SCENARIO_JOINT,
        deposit_term_months=6, deposit_offset_bps=-5.0,
    )
    loan, deposit = benchmark_provenance(request)
    assert loan.observed and deposit.observed
    assert "12 months" in loan.text and "lowest" in loan.text
    assert "6 months" in deposit.text and "highest" in deposit.text
    assert "-5 bps" in deposit.text and "offset" not in loan.text.lower()


@pytest.mark.parametrize(
    "scenario_request",
    [
        None,
        ScenarioRequest(12, "MANUAL", None, 0.0, None),
        ScenarioRequest(12, "MANUAL", float("nan"), 0.0, None),
        ScenarioRequest(12, "MANUAL", -1.0, 0.0, None),
        ScenarioRequest(12, "MARKET_MEDIAN", None, float("inf"), None),
        ScenarioRequest(12, "UNKNOWN", None, 0.0, None),
        ScenarioRequest(0, "MARKET_MEDIAN", None, 0.0, None),
        ScenarioRequest(12, "MANUAL", 5.0, 0.0, None, mode=SCENARIO_JOINT),
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None, mode=SCENARIO_JOINT),
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None, mode="invalid"),
    ],
)
def test_invalid_requests_do_not_receive_a_provenance_claim(scenario_request):
    from balance_pricing_engine.dashboard.scenario_text import benchmark_provenance

    assert benchmark_provenance(scenario_request) == ()


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
