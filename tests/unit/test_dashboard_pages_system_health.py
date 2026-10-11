import pytest

from datetime import datetime, timedelta, timezone

from dashboard_harness import run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, RunRecord

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def frame_with(app, column):
    """The first dataframe on the page that has ``column``."""
    for element in app.dataframe:
        if column in element.value.columns:
            return element.value
    raise AssertionError(f"no dataframe with a {column!r} column")


def test_components_table_shows_database_snapshot_run_and_channels(market_factory, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.delenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", raising=False)
    market = market_factory()
    app = run_page("system_health", market.database, market.now)
    assert not app.exception
    components = frame_with(app, "Component").set_index("Component")["Status"]
    assert components["Database"] == "HEALTHY"
    assert components["Market snapshot"] == "CURRENT"
    assert components["Last run"] == "SUCCESS"
    assert components["Telegram"] == "CONFIGURED" and components["Webhook"] == "NOT CONFIGURED"
    assert any("does not observe the external scheduler" in c.value for c in app.caption)


def test_collector_table_shows_backend_freshness_health_and_failures(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("system_health", market.database, market.now)
    banks = frame_with(app, "Failures").set_index("Bank")
    assert banks.loc["ANZ", "Health"] == "HEALTHY" and banks.loc["ANZ", "Freshness"] == "FRESH"
    westpac = banks.loc["Westpac"]
    assert westpac["Freshness"] == "⚠ STALE" and westpac["Health"] == "⚠ DEGRADED"
    assert westpac["Failures"] == 2 and westpac["Observation age"] == "240 h"
    assert westpac["Current records"] == 1 and westpac["Last error"] == "ConnectionError"


def test_a_failed_bank_is_explained_and_its_exclusion_is_stated(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("system_health", market.database, market.now)
    warnings = " ".join(w.value for w in app.warning)
    assert "Westpac collection failed" in warnings and "marked STALE" in warnings
    assert "Westpac excluded from market statistics" in warnings and "168" in warnings


def test_a_healthy_market_raises_no_warnings(market_factory):
    market = market_factory()
    app = run_page("system_health", market.database, market.now)
    assert len(app.warning) == 0


def test_run_history_and_drill_down(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("system_health", market.database, market.now)
    history = frame_with(app, "Banks OK")
    assert list(history["Status"]) == ["PARTIAL_SUCCESS", "PARTIAL_SUCCESS", "SUCCESS"]
    assert history.iloc[0]["Duration"] == "120 s" and history.iloc[0]["Banks failed"] == 1
    app.selectbox(key="run_detail_home_loan").select_index(0).run()
    assert not app.exception
    detail = frame_with(app, "Field").set_index("Field")["Value"]
    assert detail["Banks failed"] == "Westpac" and detail["Market state"] == "NORMAL"


def test_suspicious_observations_sit_in_their_own_section_apart_from_trusted_data(market_factory):
    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    app = run_page("system_health", market.database, market.now)
    assert any("Excluded from trusted benchmark" in s.value for s in app.subheader)
    suspicious = frame_with(app, "Observed")
    assert suspicious.iloc[0]["Bank"] == "ANZ" and suspicious.iloc[0]["Movement"] == "+360 bps"
    assert suspicious.iloc[0]["Previous"] == "4.89%" and suspicious.iloc[0]["Observed"] == "8.49%"
    assert "SUSPICIOUS" in suspicious.iloc[0]["Status"]
    # it is not the collector table, and no trusted-rate table contains the 8.49 reading
    assert "Failures" not in suspicious.columns


def test_no_suspicious_section_when_there_are_none(market_factory):
    market = market_factory()
    app = run_page("system_health", market.database, market.now)
    assert not any("Excluded from trusted benchmark" in s.value for s in app.subheader)


def test_term_deposit_collector_activity_has_its_own_tab(market_factory):
    market = market_factory()
    store = ObservationStore(market.database)
    when = NOW - timedelta(hours=3)
    store.update_collector_health(
        "ASB", ProductType.TERM_DEPOSIT, success=True, at=when, run_id="td"
    )
    store.update_collector_health(
        "ANZ", ProductType.TERM_DEPOSIT, success=False, error="Timeout", at=when, run_id="td"
    )
    store.write_run_record(
        RunRecord(
            run_id="op-td",
            started_at=when,
            completed_at=when + timedelta(minutes=1),
            status="PARTIAL_SUCCESS",
            product_type=ProductType.TERM_DEPOSIT,
            banks_attempted=("ASB", "ANZ"),
            banks_successful=("ASB",),
            banks_failed=("ANZ",),
            rates_observed=3,
            rates_changed=0,
            validation_failures=0,
            suspicious_observations=0,
            market_state=None,
            error_summary=None,
        )
    )
    store.close()
    app = run_page("system_health", market.database, market.now)
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Mortgage", "Term deposit"]
    runs = [e.value for e in app.dataframe if "Banks OK" in e.value.columns]
    assert [list(r["Status"]) for r in runs] == [
        ["SUCCESS", "SUCCESS", "SUCCESS"],
        ["PARTIAL_SUCCESS"],
    ]


def test_scraped_or_error_text_never_reaches_a_markdown_surface(market_factory):
    market = market_factory()
    store = ObservationStore(market.database)
    hostile = "![x](http://evil.example/beacon.png) :red[boom]"
    store.update_collector_health(
        "ANZ", ProductType.HOME_LOAN, success=False, error=hostile, at=NOW, run_id="run-9"
    )
    store.close()
    app = run_page("system_health", market.database, market.now)
    assert not app.exception
    surfaces = [e.value for e in (*app.markdown, *app.warning, *app.error, *app.info, *app.caption)]
    assert not any("evil.example" in text for text in surfaces)
    assert any(
        hostile in str(v) for v in frame_with(app, "Failures").get("Last error", [])
    )  # shown as plain data in the table


def test_no_data_and_corrupt_database_degrade_gracefully(tmp_path):
    app = run_page("system_health", tmp_path / "none.db", NOW)
    assert not app.exception and any("No market data" in i.value for i in app.info)
    bad = tmp_path / "bad.db"
    bad.write_text("not a SQLite database")
    app = run_page("system_health", bad, NOW)
    assert not app.exception and any("unavailable" in e.value.lower() for e in app.error)


@pytest.mark.parametrize(
    "mortgage,deposit", [(True, False), (False, True), (True, True), (False, False)]
)
def test_quarantines_display_inside_the_corresponding_product_tab(
    quarantine_market_factory, mortgage, deposit
):
    market = quarantine_market_factory(mortgage, deposit)
    app = run_page("system_health", market.database, market.now)
    assert not app.exception
    for tab, expected, bank, rate in zip(
        app.tabs, (mortgage, deposit), ("ANZ", "ASB"), ("8.49%", "8.00%")
    ):
        frames = [e.value for e in tab.dataframe if "Observed" in e.value.columns]
        assert len(frames) == int(expected)
        if expected:
            assert list(frames[0]["Bank"]) == [bank]
            assert list(frames[0]["Observed"]) == [rate]


@pytest.mark.parametrize(
    "age_hours,expected",
    [
        (1, "Its last validated observation is still current."),
        (169, "Its last validated observation remains available but is marked STALE."),
        (None, "No confirmed validated observation is available; freshness is UNKNOWN."),
    ],
)
def test_failed_collection_messages_preserve_observation_freshness(
    tmp_path, make_record, age_hours, expected
):
    database = tmp_path / "failed-bank.db"
    store = ObservationStore(database)
    if age_hours is not None:
        captured = NOW - timedelta(hours=age_hours)
        store.record_observations(
            [make_record(bank="ASB", observed_at=captured)],
            run_id="last-success",
            observed_at=captured,
        )
    # With no observations this is the bank's first-ever failed attempt.
    store.update_collector_health(
        "ASB", ProductType.HOME_LOAN, success=False, error="Timeout", at=NOW, run_id="first-failure"
    )
    store.close()
    app = run_page("system_health", database, NOW)
    assert not app.exception
    messages = [w.value for w in app.warning if w.value.startswith("ASB collection failed.")]
    assert len(messages) == 1 and expected in messages[0]
    if age_hours is None:
        assert "still current" not in messages[0] and "marked STALE" not in messages[0]
        bank = frame_with(app, "Failures").set_index("Bank").loc["ASB"]
        assert bank["Freshness"] == "UNKNOWN" and bank["Current records"] == 0
