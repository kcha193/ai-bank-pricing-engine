"""Phase 1f (UX-002): a stored Pricing Studio result is shown only while it is still current.

Pure-logic tests for ``dashboard.freshness`` plus AppTests that assert the stale output is
*absent* from the rendered page (metrics, dataframes, charts, decision text), not merely that
a notice appeared.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from dashboard_harness import badge_labels, chart_count, run_page

import balance_pricing_engine.dashboard.services as services_module
from balance_pricing_engine.dashboard.freshness import ResultFreshness, assess_result
from balance_pricing_engine.dashboard.models import (
    SCENARIO_HOME_LOAN,
    SCENARIO_JOINT,
    SCENARIO_TERM_DEPOSIT,
    ScenarioRequest,
)
from balance_pricing_engine.pricing_intelligence.revision import DatabaseStamp
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_scenario_trigger import run_triggered_scenarios

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
STAMP = DatabaseStamp("market.db", 1, 2, 3, "1" * 32)
CONFIG = "config-hash"
REQUEST = ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)


def _stored(request=REQUEST, stamp=STAMP, config=CONFIG):
    return (request, object(), stamp, NOW, config)


def _assess(stored, *, mode=SCENARIO_HOME_LOAN, request=REQUEST, stamp=STAMP, config=CONFIG, ok=True):
    return assess_result(
        stored,
        mode=mode,
        request=request,
        db_stamp=stamp,
        config_fingerprint=config,
        terms_available=ok,
    )


# ------------------------------------------------------------------- the helper itself


def test_a_matching_stored_result_is_current():
    outcome = _assess(_stored())
    assert outcome.status is ResultFreshness.CURRENT and outcome.is_current


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"request": ScenarioRequest(12, "MARKET_LEADER", None, 0.0, None)}, "INPUTS_CHANGED"),
        ({"request": ScenarioRequest(12, "MARKET_MEDIAN", None, 5.0, None)}, "INPUTS_CHANGED"),
        ({"request": ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.5)}, "INPUTS_CHANGED"),
        ({"request": None}, "INPUT_UNAVAILABLE"),
        ({"stamp": ("market.db", 2)}, "MARKET_DATA_CHANGED"),
        ({"config": "other"}, "CONFIG_CHANGED"),
        ({"ok": False}, "TERM_UNAVAILABLE"),
        ({"mode": SCENARIO_TERM_DEPOSIT}, "WRONG_MODE"),
    ],
)
def test_every_changed_condition_has_its_own_reason(kwargs, expected):
    outcome = _assess(_stored(), **kwargs)
    assert outcome.status.name == expected
    assert not outcome.is_current and outcome.message


@pytest.mark.parametrize(
    "stored",
    [
        (REQUEST, object(), STAMP, NOW),  # pre-provenance session state: no config fingerprint
        (REQUEST, object(), STAMP, NOW, None),
        (REQUEST,),
        "garbage",
        (),
    ],
)
def test_legacy_or_malformed_session_state_is_stale(stored):
    outcome = _assess(stored)
    assert outcome.status is ResultFreshness.LEGACY_RESULT and not outcome.is_current


def test_a_result_stored_for_another_mode_is_never_current():
    stored = _stored(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None, mode=SCENARIO_JOINT))
    assert _assess(stored, mode=SCENARIO_HOME_LOAN).status is ResultFreshness.WRONG_MODE


def test_a_missing_result_has_no_freshness_outcome():
    assert _assess(None) is None


def test_the_generic_message_tells_the_user_how_to_regenerate():
    message = _assess(_stored(), request=None).message
    assert "Previous modelled results are hidden" in message
    assert "click run scenario" in message.lower()


# ------------------------------------------------------------------------ AppTests


def _modelled_output_visible(app) -> bool:
    return bool(app.metric) and bool(app.dataframe) and chart_count(app) > 0


def _assert_modelled_output_absent(app):
    """No stale metric, table, chart, decision text or review/provenance badge is rendered."""
    assert not app.metric
    assert not app.dataframe
    assert chart_count(app) == 0
    subheaders = " ".join(s.value for s in app.subheader)
    assert "Results" not in subheaders and "Decision-support summary" not in subheaders
    text = " ".join(m.value for m in app.markdown)
    assert "Model-best" not in text and "above the market leader" not in text
    badges = " ".join(badge_labels(app))
    assert "MODELLED" not in badges and "Human review required" not in badges


def _run(app, key="scenario_run"):
    app.button(key=key).click().run()
    assert not app.exception
    return app


def _hidden_notice(app) -> str:
    notices = [e.value for e in (*app.warning, *app.info) if "hidden" in e.value]
    assert notices, "a hidden result must say why"
    return " ".join(notices)


def test_t01_a_valid_run_displays_the_result(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    assert _modelled_output_visible(app)
    assert not any("hidden" in e.value for e in (*app.warning, *app.info))


def test_t02_changing_the_strategy_hides_the_previous_result(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
    _assert_modelled_output_absent(app)
    assert "Click Run scenario" in _hidden_notice(app)


def test_t03_changing_the_benchmark_offset_hides_the_previous_result(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    app.number_input(key="scenario_offset").set_value(10.0).run()
    _assert_modelled_output_absent(app)


def test_t04_changing_the_manual_rate_hides_the_previous_result(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.selectbox(key="scenario_strategy").select("MANUAL").run()
    _run(app)
    assert _modelled_output_visible(app)
    app.number_input(key="scenario_manual_rate").set_value(5.5).run()
    _assert_modelled_output_absent(app)


def test_t05_changing_the_term_hides_the_previous_result(dual_market_factory):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_TERM_DEPOSIT).run()
    _run(app, "scenario_run_term_deposit")
    assert _modelled_output_visible(app)
    app.selectbox(key="scenario_term_term_deposit").select("6M").run()
    _assert_modelled_output_absent(app)


def test_t06_changing_the_current_advertised_rate_hides_dependent_output(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.number_input(key="scenario_current_rate").set_value(4.89).run()
    _run(app)
    assert any("rank #" in c.value for c in app.caption)
    app.number_input(key="scenario_current_rate").set_value(4.50).run()
    _assert_modelled_output_absent(app)
    assert not any("rank #" in c.value for c in app.caption)


def test_t07_a_changed_database_stamp_hides_the_previous_result(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    with ObservationStore(market.database) as writer:
        writer._connection.execute("UPDATE observations SET observation_count=observation_count+1")
        writer._connection.commit()
    app.run()
    _assert_modelled_output_absent(app)
    assert "Market data has changed" in _hidden_notice(app)


def test_t08_a_changed_engine_config_hides_the_previous_result(market_factory, tmp_path):
    import yaml

    from balance_pricing_engine.dashboard.settings import load_settings

    market = market_factory()
    original = load_settings({}).engine_config_text
    path = tmp_path / "engine.yaml"
    path.write_text(original)
    app = run_page(
        "scenarios", market.database, market.now, env={"PRICING_WATCH_CONFIG": str(path)}
    )
    _run(app)
    data = yaml.safe_load(original)
    data["response"]["loan_rate_elasticity"] *= 2
    path.write_text(yaml.safe_dump(data))
    app.run()
    _assert_modelled_output_absent(app)
    assert "Engine configuration has changed" in _hidden_notice(app)


def test_t09_an_unavailable_market_term_hides_the_previous_result(market_factory):
    from balance_pricing_engine.dashboard.views import scenarios

    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    with patch.object(scenarios, "_still_available", return_value=False):
        app.run()
    _assert_modelled_output_absent(app)
    assert "no longer available" in _hidden_notice(app)


def test_t10_switching_mode_never_shows_another_modes_output(dual_market_factory):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    _run(app)
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_TERM_DEPOSIT).run()
    _assert_modelled_output_absent(app)  # no deposit result yet
    _run(app, "scenario_run_term_deposit")
    labels = {m.label for m in app.metric}
    assert "Expected deposit volume" in labels and "Expected loan volume" not in labels
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_JOINT).run()
    _assert_modelled_output_absent(app)
    _run(app, "scenario_run_joint")
    labels = {m.label for m in app.metric}
    assert "⌂ Expected loan volume" in labels and "◎ Expected deposit volume" in labels
    app.segmented_control(key="scenario_mode").set_value("home_loan").run()
    labels = {m.label for m in app.metric}
    assert "Expected loan volume" in labels and "◎ Expected deposit volume" not in labels


def test_t10b_a_result_planted_under_another_modes_key_is_not_rendered(dual_market_factory):
    market = dual_market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    app.session_state["scenario_result_term_deposit"] = app.session_state["scenario_result"]
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_TERM_DEPOSIT).run()
    assert not app.exception
    _assert_modelled_output_absent(app)


def test_t11_an_unavailable_input_never_exposes_a_stale_result(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    assert _modelled_output_visible(app)
    with patch(
        "balance_pricing_engine.dashboard.views.scenarios._define", return_value=None
    ):
        app.run()
    assert not app.exception
    _assert_modelled_output_absent(app)
    assert "Previous modelled results are hidden" in _hidden_notice(app)


def test_t12_a_plain_rerender_does_not_rerun_the_optimiser(market_factory):
    market = market_factory()
    with patch.object(
        services_module, "run_triggered_scenarios", wraps=run_triggered_scenarios
    ) as optimiser:
        app = _run(run_page("scenarios", market.database, market.now))
        for _ in range(3):
            app.run()
        assert optimiser.call_count == 1
        assert _modelled_output_visible(app)
        app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
        app.run()
        assert optimiser.call_count == 1  # changing an input never recomputes either


def test_t13_an_explicit_rerun_replaces_the_result_and_timestamp(market_factory):
    market = market_factory()
    times = iter(
        [datetime(2026, 10, 11, 1, tzinfo=timezone.utc), datetime(2026, 10, 11, 2, tzinfo=timezone.utc)]
    )
    with patch("balance_pricing_engine.dashboard.views.scenarios.datetime") as clock:
        clock.now.side_effect = lambda tz=None: next(times)
        app = _run(run_page("scenarios", market.database, market.now))
        first = app.session_state["scenario_result"]
        app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
        _assert_modelled_output_absent(app)
        _run(app)
    second = app.session_state["scenario_result"]
    assert first[0].strategy == "MARKET_MEDIAN" and second[0].strategy == "MARKET_LEADER"
    assert first[3] < second[3]
    assert _modelled_output_visible(app)
    assert any("Computed 11 Oct 2026 15:00" in c.value for c in app.caption)


def test_hidden_results_are_kept_and_return_when_the_inputs_return(market_factory):
    market = market_factory()
    with patch.object(
        services_module, "run_triggered_scenarios", wraps=run_triggered_scenarios
    ) as optimiser:
        app = _run(run_page("scenarios", market.database, market.now))
        app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
        _assert_modelled_output_absent(app)
        app.selectbox(key="scenario_strategy").select("MARKET_MEDIAN").run()
        assert _modelled_output_visible(app) and optimiser.call_count == 1


def test_a_hidden_result_keeps_its_timestamp_only_as_labelled_history(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
    captions = [c.value for c in app.caption]
    assert not any(c.startswith("Computed ") for c in captions)
    assert any("hidden result" in c.lower() and "computed" in c.lower() for c in captions)


def test_t14_legacy_session_state_is_hidden_until_recomputed(market_factory):
    market = market_factory()
    app = _run(run_page("scenarios", market.database, market.now))
    app.session_state["scenario_result"] = app.session_state["scenario_result"][:4]
    app.run()
    _assert_modelled_output_absent(app)
    assert "configuration" in _hidden_notice(app)
    _run(app)
    assert _modelled_output_visible(app)


def test_computed_at_is_read_only_from_a_well_formed_entry():
    from balance_pricing_engine.dashboard.freshness import stored_computed_at

    assert stored_computed_at(_stored()) == NOW
    assert stored_computed_at((REQUEST, object(), STAMP, "not a time", CONFIG)) is None
    assert stored_computed_at((REQUEST,)) is None and stored_computed_at(None) is None


@pytest.mark.parametrize("stamp", [None, ("market.db", 1), ("market.db", 0)])
def test_unknown_or_legacy_database_provenance_is_never_current(stamp):
    assert _assess(_stored(stamp=stamp), stamp=stamp).status is ResultFreshness.MARKET_DATA_CHANGED


@pytest.mark.parametrize(
    "mode,run_key",
    [
        (SCENARIO_HOME_LOAN, "scenario_run"),
        (SCENARIO_TERM_DEPOSIT, "scenario_run_term_deposit"),
        (SCENARIO_JOINT, "scenario_run_joint"),
    ],
)
def test_wal_commit_hides_each_scenario_mode_with_writer_open(dual_market_factory, mode, run_key):
    market = dual_market_factory()
    with ObservationStore(market.database) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint=0")
        app = run_page("scenarios", market.database, market.now)
        app.segmented_control(key="scenario_mode").set_value(mode).run()
        _run(app, run_key)
        assert app.metric, [(e.type, e.value) for e in (*app.error, *app.warning, *app.info)]
        before = market.database.stat().st_mtime_ns
        writer._connection.execute("UPDATE observations SET observation_count=observation_count+1")
        writer._connection.commit()
        assert market.database.stat().st_mtime_ns == before
        app.run()
        assert not app.exception
        _assert_modelled_output_absent(app)
        assert "Market data has changed" in _hidden_notice(app)


def test_commit_during_scenario_computation_never_labels_result_current(market_factory):
    market = market_factory()
    with ObservationStore(market.database):
        original = services_module.DashboardServices.scenarios

        def concurrent_commit(services, request):
            result = original(services, request)
            with ObservationStore(market.database) as other:
                other._connection.execute(
                    "UPDATE observations SET observation_count=observation_count+1"
                )
                other._connection.commit()
            return result

        with patch.object(services_module.DashboardServices, "scenarios", concurrent_commit):
            app = _run(run_page("scenarios", market.database, market.now))
        _assert_modelled_output_absent(app)
        assert "Market data has changed" in _hidden_notice(app)
