"""CLI behaviour, exercised against a temporary store and saved fixtures."""

import argparse
import json
from pathlib import Path
import re
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from balance_pricing_engine.competitor_rates.collectors import (
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence import cli
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

# Collection timestamps are dated relative to "now", not pinned to a calendar
# date. Market snapshots exclude observations older than
# max_observation_age_hours (168h by default), so a fixed date here passes when
# written and starts failing silently once real time crosses that window --
# which is exactly what happened on 2026-08-27.
DAY_ONE = datetime.now(timezone.utc) - timedelta(hours=2)
DAY_TWO = datetime.now(timezone.utc) - timedelta(hours=1)


def cut_anz_one_year(bank, html):
    if bank == "ANZ":
        return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
    return html


@pytest.fixture
def populated_database(tmp_path, fixture_fetcher):
    """A database with one collection already recorded."""
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(path),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
    return str(path)


@pytest.fixture
def repriced_database(tmp_path, fixture_fetcher):
    """A database with a baseline collection plus a real reprice on top."""
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(path),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
    return str(path)


@pytest.fixture
def mixed_product_database(tmp_path, make_record):
    """A current mortgage and deposit market with one change in each."""
    path = tmp_path / "observations.db"
    home_loan = make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)
    deposit_a = make_record(
        bank="ANZ",
        product_type=ProductType.TERM_DEPOSIT,
        product_name="Term Deposit",
        rate_category="term_deposit",
        rate_pct=3.50,
        observed_at=DAY_TWO,
    )
    deposit_b = make_record(
        bank="BNZ",
        product_type=ProductType.TERM_DEPOSIT,
        product_name="Term Deposit",
        rate_category="term_deposit",
        rate_pct=4.00,
        observed_at=DAY_TWO,
    )

    def change_for(record):
        return RateChange(
            observation_key=record.observation_key(),
            bank=record.bank,
            product_type=ProductType(record.product_type),
            product_name=record.product_name,
            rate_category=record.rate_category,
            term_months=record.term_months_normalised,
            change_type=ChangeType.RATE_INCREASE,
            detected_at=DAY_TWO,
            previous_rate_pct=record.advertised_rate_pct - 0.10,
            current_rate_pct=record.advertised_rate_pct,
            change_bps=10.0,
            run_id="test-run",
        )

    with ObservationStore(path) as store:
        # One record_observations() call is one product's collection run --
        # collection_runs.product_type (used to scope "the latest run" per
        # market) cannot represent more than one product for a single run_id.
        store.record_observations([home_loan], observed_at=DAY_TWO)
        store.record_observations([deposit_a, deposit_b], observed_at=DAY_TWO)
        store.record_changes([change_for(home_loan), change_for(deposit_a)])
    return str(path)


class _StubCollector:
    """Returns pre-built records instead of parsing HTML.

    Mirrors the helper of the same name in test_scheduled_collection_workflow.py
    -- lets an off-default-category collection be built directly from records
    rather than fragile HTML-fixture surgery.
    """

    def __init__(self, bank, records):
        self.bank = bank
        self._records = records

    def collect_term_deposit_rates(self, captured_at_utc=None, persist_snapshot=True):
        return BankCollectionResult(
            bank=self.bank,
            product_type=ProductType.TERM_DEPOSIT,
            status=CollectionStatus.SUCCESS,
            source_url=f"https://www.example-bank.co.nz/{self.bank.lower()}/deposits",
            records=tuple(self._records),
        )


def run(argv):
    return cli.main(argv)


def _minimal_engine_config(**pricing_intelligence_overrides):
    return {
        "random_seed": 1,
        "simulation": {"n_loan_customers": 100, "n_deposit_customers": 100},
        "market": {
            "cash_rate": 0.04,
            "wholesale_funding_spread": 0.01,
            "competitor_loan_rate": 0.06,
            "competitor_deposit_rate": 0.04,
            "home_loan_reference_rate": 0.0525,
            "term_deposit_reference_rate": 0.0400,
        },
        "pricing_strategy": {
            "home_loan_spread_bps": {"min": -50, "max": 50, "step": 5},
            "term_deposit_spread_bps": {"min": -40, "max": 40, "step": 5},
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": 0.65,
            "funding_shortfall_penalty_rate": 0.02,
        },
        "response": {
            "loan_rate_elasticity": -85.0,
            "loan_competitor_gap_elasticity": -65.0,
            "deposit_rate_elasticity": 95.0,
            "deposit_competitor_gap_elasticity": 75.0,
        },
        "pricing_intelligence": pricing_intelligence_overrides,
    }


def test_config_flag_loads_pricing_intelligence_settings_from_engine_config(
    tmp_path, fixture_fetcher, capsys
):
    """configs/base.yaml's pricing_intelligence block must actually take effect."""
    custom_db = tmp_path / "from_config.db"
    pi_config = PricingIntelligenceConfig(
        database_path=str(custom_db),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=pi_config, store=ObservationStore(custom_db), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(_minimal_engine_config(database_path=str(custom_db)))
    )

    exit_code = run(["--config", str(engine_config_path), "snapshot"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ MORTGAGE MARKET" in output


def test_db_path_env_var_is_honored_with_no_database_or_config_flag(
    populated_database, monkeypatch, capsys
):
    """PRICING_INTELLIGENCE_DB_PATH (documented in .env.example) must be read
    when neither --database nor --config points somewhere else -- previously
    the CLI always fell back to PricingIntelligenceConfig()'s hard-coded
    default path regardless of this env var."""
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", populated_database)

    exit_code = run(["snapshot"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ MORTGAGE MARKET" in output


def test_explicit_database_flag_overrides_the_env_var(tmp_path, populated_database, monkeypatch, capsys):
    """An explicit --database must win over PRICING_INTELLIGENCE_DB_PATH --
    an operator's command-line choice must not be silently overridden by
    an ambient environment variable."""
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "unused.db"))

    exit_code = run(["--database", populated_database, "snapshot"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ MORTGAGE MARKET" in output
    assert not (tmp_path / "unused.db").exists(), "the env-var path must not have been touched"


def test_snapshot_renders_the_market_grid(populated_database, capsys):
    exit_code = run(["--database", populated_database, "snapshot"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ MORTGAGE MARKET" in output
    assert "ANZ" in output
    assert "Median" in output


def test_snapshot_details_a_single_term(populated_database, capsys):
    exit_code = run(["--database", populated_database, "snapshot", "--term", "12"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "12 MONTH FIXED" in output
    assert "#1" in output
    assert "Spread:" in output


def test_deposit_snapshot_detail_uses_deposit_category_and_worst_rate(
    mixed_product_database, capsys
):
    exit_code = run(
        [
            "--database",
            mixed_product_database,
            "--product",
            "term_deposit",
            "snapshot",
            "--term",
            "12",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "12 MONTH FIXED (term_deposit)" in output
    assert "Best:   4.00" in output
    assert "Worst:  3.50" in output


def test_deposit_snapshot_honours_an_explicit_special_category(
    tmp_path, make_record, capsys
):
    """--product term_deposit --category special must not be rewritten to
    the term_deposit default: a bank can genuinely advertise a deposit
    special, and --category claims to select the comparison category."""
    path = tmp_path / "observations.db"
    record = make_record(
        bank="ANZ",
        product_type=ProductType.TERM_DEPOSIT,
        product_name="Term Deposit Special",
        rate_category="special",
        rate_pct=4.50,
    )
    config = PricingIntelligenceConfig(database_path=str(path))
    with PricingIntelligenceService(config=config, store=ObservationStore(path)) as service:
        service.store.record_observations([record])

    exit_code = run(
        [
            "--database",
            str(path),
            "--product",
            "term_deposit",
            "snapshot",
            "--category",
            "special",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ TERM DEPOSIT MARKET (special)" in output
    assert "ANZ" in output


def test_snapshot_reports_a_term_with_no_data(populated_database, capsys):
    exit_code = run(["--database", populated_database, "snapshot", "--term", "120"])

    assert exit_code == 1
    assert "No data at 120 months" in capsys.readouterr().out


def test_snapshot_on_an_empty_database_asks_for_a_collection(tmp_path, capsys):
    exit_code = run(["--database", str(tmp_path / "empty.db"), "snapshot"])

    assert exit_code == 1
    assert "Run 'collect' first" in capsys.readouterr().out


def test_changes_reports_a_quiet_market(populated_database, capsys):
    exit_code = run(["--database", populated_database, "changes", "--days", "7"])

    assert exit_code == 0
    assert "No changes detected" in capsys.readouterr().out


def test_changes_command_renders_detected_events(repriced_database, capsys):
    exit_code = run(["--database", repriced_database, "changes", "--days", "7"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "MARKET CHANGES" in output
    assert "ANZ" in output


def test_changes_command_filters_to_the_selected_product(mixed_product_database, capsys):
    exit_code = run(
        ["--database", mixed_product_database, "--product", "term_deposit", "changes"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "1 events" in output
    assert "term_deposit" in output
    assert "special" not in output


def test_changes_alert_identifies_the_selected_deposit_market(mixed_product_database, capsys):
    exit_code = run(
        [
            "--database",
            mixed_product_database,
            "--product",
            "term_deposit",
            "changes",
            "--alerts",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ TERM DEPOSIT RATE CHANGE" in output
    assert "NZ MORTGAGE RATE CHANGE" not in output


@pytest.fixture
def standard_category_database(tmp_path, make_record):
    """A mortgage market with one detected change recorded under a
    non-default rate category ("standard", not "special")."""
    path = tmp_path / "observations.db"
    standard_record = make_record(rate_category="standard", observed_at=DAY_TWO)
    change = RateChange(
        observation_key=standard_record.observation_key(),
        bank=standard_record.bank,
        product_type=ProductType(standard_record.product_type),
        product_name=standard_record.product_name,
        rate_category=standard_record.rate_category,
        term_months=standard_record.term_months_normalised,
        change_type=ChangeType.RATE_INCREASE,
        detected_at=DAY_TWO,
        previous_rate_pct=standard_record.advertised_rate_pct - 0.10,
        current_rate_pct=standard_record.advertised_rate_pct,
        change_bps=10.0,
        run_id="test-run",
    )
    with ObservationStore(path) as store:
        store.record_observations([standard_record], observed_at=DAY_TWO)
        store.record_changes([change])
    return str(path)


def test_changes_command_default_category_misses_a_standard_repricing(
    standard_category_database, capsys
):
    exit_code = run(["--database", standard_category_database, "changes", "--days", "7"])

    assert exit_code == 0
    assert "No changes detected" in capsys.readouterr().out


def test_changes_command_category_all_surfaces_a_standard_repricing(
    standard_category_database, capsys
):
    exit_code = run(
        ["--database", standard_category_database, "changes", "--days", "7", "--category", "all"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "(1 events)" in output


def test_changes_command_explicit_category_reaches_a_standard_repricing(
    standard_category_database, capsys
):
    exit_code = run(
        [
            "--database",
            standard_category_database,
            "changes",
            "--days",
            "7",
            "--category",
            "standard",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "(1 events)" in output


def test_changes_command_category_all_alerts_still_show_market_context(
    standard_category_database, capsys
):
    """--category all resolves to rate_category=None, which get_market_snapshot()
    now treats as the unfiltered view -- but format_alert()'s market-context
    guard requires snapshot.rate_category == change.rate_category, which a
    single None-category snapshot can never satisfy against a change's own
    concrete category ("standard" here). Without a snapshot built for that
    change's own category, every alert would silently lose its market
    median/rank context."""
    exit_code = run(
        [
            "--database",
            standard_category_database,
            "changes",
            "--days",
            "7",
            "--category",
            "all",
            "--alerts",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Market median:" in output


def test_changes_command_can_render_as_alerts(repriced_database, capsys):
    exit_code = run(["--database", repriced_database, "changes", "--days", "7", "--alerts"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ MORTGAGE RATE CHANGE" in output


def test_collect_handler_reports_the_collection_summary(tmp_path, fixture_fetcher, capsys):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        args = argparse.Namespace(no_persist=False, no_snapshot=True, alerts=False)
        exit_code = cli._collect(service, args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "ANZ" in output


def test_collect_handler_reports_changes_and_dispatches_alerts(
    tmp_path, fixture_fetcher, capsys
):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        args = argparse.Namespace(no_persist=False, no_snapshot=True, alerts=True)
        exit_code = cli._collect(service, args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Detected changes:" in output
    assert "NZ MORTGAGE RATE CHANGE" in output


def test_collect_handler_reports_validation_issues(tmp_path, fixture_fetcher, capsys):
    def corrupt_kiwibank_three_year(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>55.35%")
        return html

    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=corrupt_kiwibank_three_year)
        args = argparse.Namespace(no_persist=False, no_snapshot=True, alerts=False)
        exit_code = cli._collect(service, args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Validation issues:" in output
    assert "rate_out_of_range" in output


def test_collect_handler_fails_when_every_bank_fails(tmp_path, fixture_fetcher, capsys):
    banks = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        fetcher=fixture_fetcher(failing_banks=banks),
    ) as service:
        args = argparse.Namespace(no_persist=False, no_snapshot=True, alerts=False)
        exit_code = cli._collect(service, args)

    assert exit_code == 1


def test_collect_handler_fails_when_validation_rejects_every_record(
    tmp_path, fixture_fetcher, capsys
):
    """Pages parsing successfully is not success if nothing survives validation."""

    def corrupt_every_rate(bank, html):
        return re.sub(r"\d\.\d\d%", "99.99%", html)

    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        fetcher=fixture_fetcher(
            failing_banks=["ASB", "BNZ", "Westpac", "Kiwibank"], mutate=corrupt_every_rate
        ),
    ) as service:
        args = argparse.Namespace(no_persist=False, no_snapshot=True, alerts=False)
        exit_code = cli._collect(service, args)

    assert exit_code == 1


def test_collect_handler_fails_when_deposit_rows_are_all_off_default_category(
    tmp_path, make_record, capsys
):
    """The interactive ``--product term_deposit`` path must apply the same
    comparable-category check the scheduled workflow uses (see
    scripts/collect_market_rates_workflow.py's ``_comparable_rate_count``):
    a collection that accepts only ``pie_term_deposit`` rows -- a real,
    non-``non_comparable`` category -- while missing every ordinary
    ``term_deposit`` row leaves the default deposit market snapshot empty,
    even though ``any_success`` is true and ``rate_count`` is positive."""
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    pie_record = make_record(
        bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="pie_term_deposit"
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        collectors=[_StubCollector("ANZ", [pie_record])],
    ) as service:
        args = argparse.Namespace(
            no_persist=False, no_snapshot=True, alerts=False, product=ProductType.TERM_DEPOSIT.value
        )
        exit_code = cli._collect(service, args)

    assert exit_code == 1


def test_history_lists_observed_states(populated_database, capsys):
    exit_code = run(
        ["--database", populated_database, "history", "--bank", "ANZ", "--term", "12"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "RATE HISTORY" in output
    assert "ANZ" in output
    assert "4.79" in output


def test_history_defaults_to_the_selected_deposit_market(mixed_product_database, capsys):
    exit_code = run(
        ["--database", mixed_product_database, "--product", "term_deposit", "history"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "term_deposit" in output
    assert "special" not in output


def test_history_reports_no_match(populated_database, capsys):
    exit_code = run(["--database", populated_database, "history", "--bank", "Rabobank"])

    assert exit_code == 1
    assert "No history" in capsys.readouterr().out


def test_market_state_reports_a_label_and_metrics(populated_database, capsys):
    exit_code = run(["--database", populated_database, "market-state"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "MARKET STATE: LOW" in output
    assert "changes_7d" in output
    assert "dominant_direction" in output


def test_market_state_filters_to_the_selected_deposit_market(mixed_product_database, capsys):
    exit_code = run(
        ["--database", mixed_product_database, "--product", "term_deposit", "market-state"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "changes_7d          1" in output


def test_position_reports_competitive_features(populated_database, capsys):
    exit_code = run(
        ["--database", populated_database, "position", "--rate", "4.95", "--term", "12"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "COMPETITIVE POSITION" in output
    assert "our_rate_minus_median_bps" in output
    assert "market_rank" in output


def test_position_uses_the_selected_deposit_market(mixed_product_database, capsys):
    exit_code = run(
        [
            "--database",
            mixed_product_database,
            "--product",
            "term_deposit",
            "position",
            "--rate",
            "3.75",
            "--term",
            "12",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "market_best                 4.00" in output


def test_strategy_resolves_a_market_median_assumption(populated_database, capsys):
    exit_code = run(
        ["--database", populated_database, "strategy", "--strategy", "MARKET_MEDIAN"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "MARKET_MEDIAN" in output
    assert "competitor_loan_rate" in output


def test_strategy_uses_the_selected_deposit_market(mixed_product_database, capsys):
    exit_code = run(
        [
            "--database",
            mixed_product_database,
            "--product",
            "term_deposit",
            "strategy",
            "--strategy",
            "MARKET_LEADER",
            "--term",
            "12",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "advertised rate       4.000%" in output
    assert "competitor_deposit_rate" in output


def test_strategy_supports_a_leader_offset(populated_database, capsys):
    exit_code = run(
        [
            "--database",
            populated_database,
            "strategy",
            "--strategy",
            "MARKET_LEADER",
            "--offset-bps",
            "-5",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "4.740" in output
    assert "-5" in output


def test_an_error_is_reported_without_a_traceback(tmp_path, capsys):
    exit_code = run(
        ["--database", str(tmp_path / "empty.db"), "position", "--rate", "4.95"]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert output.startswith("ERROR:")


def test_an_unknown_strategy_is_rejected_by_the_parser(populated_database):
    with pytest.raises(SystemExit):
        run(["--database", populated_database, "strategy", "--strategy", "GUESSING"])


def _month_label(base: datetime, months_back: int) -> str:
    """RBNZ's "%b-%Y" period label ``months_back`` months before ``base`` --
    used so ocr-command test fixtures stay fresh relative to the wall clock
    instead of pinning a calendar date that will eventually read as stale
    against resolve_cash_rate()'s currency check (see
    test_no_wallclock_timebombs)."""
    total_months = base.year * 12 + (base.month - 1) - months_back
    year, month = divmod(total_months, 12)
    return datetime(year, month + 1, 1).strftime("%b-%Y")


def test_ocr_reports_the_current_rate_and_trend(tmp_path, capsys, monkeypatch):
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    now = datetime.now(timezone.utc)
    # months_back starts at 1, not 0: an "End of Month" reading can't exist
    # for the current, still-in-progress month -- the newest real
    # observation is always the most recently *closed* month.
    current_period = _month_label(now, 1)
    series = OcrSeries(
        observations=(
            OcrObservation(current_period, 2.50, 2.98),
            OcrObservation(_month_label(now, 2), 2.50, 2.85),
            OcrObservation(_month_label(now, 3), 2.25, 2.68),
            OcrObservation(_month_label(now, 4), 2.25, 2.63),
        ),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=now,
    )
    monkeypatch.setattr(cli, "fetch_ocr_series", lambda **kwargs: series)

    exit_code = run(["--database", str(tmp_path / "empty.db"), "ocr"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "2.50%" in output
    assert current_period in output
    assert "2.98%" in output
    assert "+25 bps" in output  # 3-month change: 2.50 vs 2.25
    assert series.source_url in output


def test_ocr_does_not_require_a_populated_database(tmp_path, capsys, monkeypatch):
    """OCR is a separate data source; it must work even before any bank
    collection has ever run against this database."""
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    now = datetime.now(timezone.utc)
    series = OcrSeries(
        observations=(OcrObservation(_month_label(now, 1), 2.50, 2.98),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=now,
    )
    monkeypatch.setattr(cli, "fetch_ocr_series", lambda **kwargs: series)

    database_path = tmp_path / "never-collected.db"
    exit_code = run(["--database", str(database_path), "ocr"])

    assert exit_code == 0
    # A store-independent command must never create/migrate a database it
    # will never touch, and must not require a read-only --database path to
    # even be writable.
    assert not database_path.exists()


def test_ocr_reports_a_parser_error_without_a_traceback(tmp_path, capsys, monkeypatch):
    from balance_pricing_engine.competitor_rates.collectors.base import ParserError

    def broken(*args, **kwargs):
        raise ParserError("the RBNZ page structure changed")

    monkeypatch.setattr(cli, "fetch_ocr_series", broken)

    exit_code = run(["--database", str(tmp_path / "empty.db"), "ocr"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert output.startswith("ERROR:")
    assert "RBNZ page structure changed" in output


def test_ocr_rejects_a_stale_series_instead_of_reporting_it(tmp_path, capsys, monkeypatch):
    """The ocr command must apply the same currency check
    resolve_cash_rate(RBNZ_OCR) applies before the pricing overlay would
    accept a series -- it must not print a rate as "the current OCR" that
    the pricing seam would reject as stale."""
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    now = datetime.now(timezone.utc)
    # ~220 days stale -- well past MAX_OCR_OBSERVATION_AGE_DAYS -- computed
    # relative to now rather than a fixed calendar date, per
    # test_no_wallclock_timebombs.
    stale_series = OcrSeries(
        observations=(OcrObservation((now - timedelta(days=220)).strftime("%b-%Y"), 5.50, 5.80),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=now,
    )
    monkeypatch.setattr(cli, "fetch_ocr_series", lambda **kwargs: stale_series)

    exit_code = run(["--database", str(tmp_path / "empty.db"), "ocr"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert output.startswith("ERROR:")
    assert "looks stale" in output


def test_ocr_uses_the_collection_settings_loaded_from_config(tmp_path, capsys, monkeypatch):
    """A --config's pricing_intelligence.collection settings (timeout,
    retries, backoff, user agent) must reach the OCR fetch too -- store
    independence is not fetch-configuration independence."""
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    now = datetime.now(timezone.utc)
    series = OcrSeries(
        observations=(OcrObservation(_month_label(now, 1), 2.50, 2.98),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=now,
    )
    seen_configs = []

    def fake_fetch_ocr_series(**kwargs):
        seen_configs.append(kwargs.get("config"))
        return series

    monkeypatch.setattr(cli, "fetch_ocr_series", fake_fetch_ocr_series)

    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(
            _minimal_engine_config(collection={"max_retries": 7, "retry_backoff_seconds": 3.5})
        )
    )

    exit_code = run(["--config", str(engine_config_path), "ocr"])

    assert exit_code == 0
    assert len(seen_configs) == 1
    assert seen_configs[0] is not None
    assert seen_configs[0].max_retries == 7
    assert seen_configs[0].retry_backoff_seconds == 3.5


@pytest.mark.parametrize(
    'arguments',
    [
        ['position', '--rate', 'nan'],
        ['position', '--rate', 'inf'],
        ['position', '--rate', '-1'],
        ['snapshot', '--term', '0'],
        ['changes', '--days', '-7'],
        ['history', '--days', '0'],
        ['strategy', '--manual-rate', 'nan'],
        ['strategy', '--offset-bps', 'inf'],
    ],
)
def test_invalid_numeric_arguments_are_rejected_before_opening_store(tmp_path, arguments):
    database = tmp_path / 'must_not_be_created.db'
    with pytest.raises(SystemExit) as exc:
        run(['--database', str(database), *arguments])
    assert exc.value.code == 2
    assert not database.exists()


def test_missing_config_is_reported_without_escaping_cli_boundary(tmp_path, capsys):
    assert run(['--config', str(tmp_path / 'missing.yaml'), 'snapshot']) == 1
    assert capsys.readouterr().out.startswith('ERROR:')


def test_empty_market_state_is_unknown_instead_of_calm(tmp_path, capsys):
    assert run(['--database', str(tmp_path / 'empty.db'), 'market-state']) == 1
    output = capsys.readouterr().out
    assert 'LOW' not in output
    assert 'No fresh market data' in output


# ---------------------------------------------------------------- run command


def test_run_command_reports_status_and_persists_a_run_record(tmp_path, fixture_fetcher, monkeypatch, capsys):
    monkeypatch.delenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    database = tmp_path / "observations.db"
    monkeypatch.setattr(
        cli,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fixture_fetcher()
        ),
    )

    exit_code = run(
        [
            "--database",
            str(database),
            "run",
            "--lock-file",
            str(tmp_path / "collection.lock"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Status:      SUCCESS" in output
    assert "Run id:" in output

    with ObservationStore(database) as store:
        record = store.get_latest_run_record()
    assert record is not None
    assert record.status == "SUCCESS"
    assert set(record.banks_successful) == {"ANZ", "ASB", "Westpac", "BNZ", "Kiwibank"}


def test_run_command_dry_run_persists_nothing(tmp_path, fixture_fetcher, capsys, monkeypatch):
    database = tmp_path / "observations.db"
    monkeypatch.setattr(
        cli,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fixture_fetcher()
        ),
    )

    exit_code = run(
        [
            "--database",
            str(database),
            "run",
            "--dry-run",
            "--lock-file",
            str(tmp_path / "collection.lock"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Dry run: nothing persisted" in output
    assert "Run id:" not in output

    with ObservationStore(database) as store:
        assert store.get_latest_run_record() is None
        assert store.get_current_observations() == []


def test_run_command_dry_run_does_not_create_the_database_file(
    tmp_path, fixture_fetcher, capsys, monkeypatch
):
    """``run --dry-run`` against a database path that doesn't exist yet must
    not bring one into existence -- opening ``ObservationStore`` used to
    create and schema-initialise the file regardless of ``--dry-run``,
    contradicting the CLI's own 'nothing persisted' message."""
    database = tmp_path / "observations.db"
    monkeypatch.setattr(
        cli,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fixture_fetcher()
        ),
    )

    exit_code = run(
        [
            "--database",
            str(database),
            "run",
            "--dry-run",
            "--lock-file",
            str(tmp_path / "collection.lock"),
        ]
    )
    capsys.readouterr()

    assert exit_code == 0
    assert not database.exists()


def test_brief_daily_reports_no_change(populated_database, capsys):
    exit_code = run(["--database", populated_database, "brief"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ PRICING INTELLIGENCE" in output
    assert "No material mortgage pricing changes detected today." in output


def test_brief_daily_date_uses_new_zealand_time_not_utc(populated_database, capsys, monkeypatch):
    """Under the checked-in 07:30 Pacific/Auckland schedule, UTC is still
    the previous day at that hour -- the brief's displayed calendar date
    must reflect NZ local time, not raw UTC, or every morning brief is
    headed with yesterday's date.

    The frozen instant is derived from the real "now" via .replace()/
    timedelta, not a fixed datetime(YYYY, ...) literal -- this module is one
    of test_no_wallclock_timebombs.py's guarded CLOCK_SENSITIVE_MODULES, and
    a hard-coded calendar date there is exactly the timebomb it screens for.
    """
    from zoneinfo import ZoneInfo

    from balance_pricing_engine.pricing_intelligence import cli as cli_module

    # NZ is always UTC+12 or UTC+13, so 23:30 UTC on any date always lands
    # on the *next* calendar day in NZ time, regardless of whether NZDT is
    # active -- no fixed calendar anchor needed to reproduce the crossover.
    real_now = datetime.now(timezone.utc)
    fixed_utc = real_now.replace(hour=23, minute=30, second=0, microsecond=0)
    expected_nz_date = fixed_utc.astimezone(ZoneInfo("Pacific/Auckland")).strftime("%d %b %Y")
    utc_date_string = fixed_utc.strftime("%d %b %Y")
    assert expected_nz_date != utc_date_string, "the two dates must actually differ for this test"

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz is not None else fixed_utc

    monkeypatch.setattr(cli_module, "datetime", FrozenDatetime)

    exit_code = run(["--database", populated_database, "brief"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert expected_nz_date in output
    assert utc_date_string not in output


def test_brief_weekly_reports_market_activity(populated_database, capsys):
    exit_code = run(["--database", populated_database, "brief", "--weekly"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "NZ MORTGAGE PRICING -- WEEKLY" in output


def test_brief_scopes_changes_to_the_configured_market(tmp_path, fixture_fetcher, capsys):
    """A brief's change query must be scoped to selected_banks/tracked_terms_months
    the same way get_market_state() already is -- otherwise a bank dropped
    from the current configuration can still show up in the brief's TODAY
    listing and repricing counts even though the snapshot and market-state
    assessment it is delivered alongside no longer include it."""
    database = tmp_path / "observations.db"
    broad_config = PricingIntelligenceConfig(
        database_path=str(database),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )

    def mutate(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>1 year</td><td>4.89%", "<td>1 year</td><td>4.69%")
        return html

    with PricingIntelligenceService(
        config=broad_config, store=ObservationStore(database), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=mutate)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(
            _minimal_engine_config(
                database_path=str(database),
                # Kiwibank dropped from the current configuration.
                selected_banks=["ANZ", "BNZ", "ASB", "Westpac"],
            )
        )
    )

    exit_code = run(["--config", str(engine_config_path), "brief"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Kiwibank" not in output


def test_brief_daily_change_window_uses_nz_midnight_not_a_rolling_24h_window(
    tmp_path, fixture_fetcher, capsys, monkeypatch
):
    """Around the Pacific/Auckland DST transition, consecutive 07:30 runs
    are only 23 hours apart -- a rolling 24h UTC lookback would re-include
    yesterday's already-reported change under today's TODAY section; a
    delayed or manual run has the same problem any day of the year. The
    brief's change query must instead be bounded by NZ-local midnight, the
    same boundary its displayed date now uses.

    The frozen instant and the change's timestamp are both derived from the
    real "now" via .replace()/timedelta, not fixed datetime(YYYY, ...)
    literals -- this module is one of test_no_wallclock_timebombs.py's
    guarded CLOCK_SENSITIVE_MODULES."""
    from zoneinfo import ZoneInfo

    from balance_pricing_engine.pricing_intelligence import cli as cli_module
    from balance_pricing_engine.pricing_intelligence import store as store_module

    nz = ZoneInfo("Pacific/Auckland")
    real_now = datetime.now(timezone.utc)
    nz_midnight_today = real_now.astimezone(nz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    frozen_now_utc = (nz_midnight_today + timedelta(minutes=30)).astimezone(timezone.utc)
    # 20 hours before "now" -- inside any rolling 24h UTC window, but ~19.5
    # hours before today's NZ midnight, i.e. yesterday, NZ-local.
    change_detected_at = frozen_now_utc - timedelta(hours=20)

    database = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(database),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(database), fetcher=fixture_fetcher()
    ) as service:
        service.collect(
            captured_at_utc=change_detected_at - timedelta(hours=1), persist_snapshot=False
        )
        service._fetcher = fixture_fetcher(
            mutate=lambda bank, html: (
                html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
                if bank == "ANZ"
                else html
            )
        )
        service.collect(captured_at_utc=change_detected_at, persist_snapshot=False)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now_utc.astimezone(tz) if tz is not None else frozen_now_utc

    monkeypatch.setattr(cli_module, "datetime", FrozenDatetime)
    # store.get_changes_since()'s own now=None default must resolve to the
    # same frozen instant -- otherwise its upper bound (detected_at <= now)
    # is anchored to the real wall clock instead, decoupling it from the
    # since= boundary _brief computed from its own (frozen) now.
    monkeypatch.setattr(store_module, "datetime", FrozenDatetime)

    exit_code = run(["--database", str(database), "brief"])
    output = capsys.readouterr().out

    assert exit_code == 0
    # Scoped to the TODAY section specifically, not a blanket "ANZ"/"-20 bps"
    # not in output check: the 7-DAY MARKET and MOST COMPETITIVE sections
    # legitimately mention both once a quiet TODAY section stopped
    # short-circuiting the rest of the brief.
    today_section = output.split("TODAY\n", 1)[1].split("\n\n", 1)[0]
    assert today_section.strip() == "No material mortgage pricing changes detected today.", (
        "yesterday's (NZ-local) change must not appear under today's TODAY section"
    )


def test_brief_with_no_market_data_fails_cleanly(tmp_path, capsys):
    exit_code = run(["--database", str(tmp_path / "empty.db"), "brief"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "No market data" in output


def test_brief_send_reports_when_no_channel_is_configured(populated_database, capsys, monkeypatch):
    monkeypatch.delenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    exit_code = run(["--database", populated_database, "brief", "--send"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "no webhook or Telegram channel is configured" in output


def test_brief_send_delivers_to_a_configured_webhook(populated_database, capsys, monkeypatch):
    monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    captured = []

    def fake_urlopen(request, timeout=None):
        import json

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        captured.append(json.loads(request.data))
        return FakeResponse()

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    exit_code = run(["--database", populated_database, "brief", "--send"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert len(captured) == 1
    assert "Delivered to 1/1 configured channel(s)." in output


def test_brief_send_fails_when_every_configured_channel_fails_to_deliver(
    populated_database, capsys, monkeypatch
):
    """A scheduled `run && brief --send` line must not read as healthy to
    cron/monitoring (which only sees this command's own exit code) when
    every configured sink failed or timed out and nobody actually received
    the briefing."""
    monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x")

    def failing_urlopen(request, timeout=None):
        raise OSError("simulated webhook outage")

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", failing_urlopen)

    exit_code = run(["--database", populated_database, "brief", "--send"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Delivered to 0/1 configured channel(s)." in output


def test_run_command_reports_already_running(tmp_path, capsys):
    from balance_pricing_engine.pricing_intelligence.locking import SingleInstanceLock

    lock_path = tmp_path / "collection.lock"
    database = tmp_path / "observations.db"

    with SingleInstanceLock(lock_path):
        exit_code = run(
            ["--database", str(database), "run", "--lock-file", str(lock_path)]
        )

    output = capsys.readouterr().out
    assert exit_code == 3  # EXIT_ALREADY_RUNNING
    assert "Skipped" in output


def test_run_command_default_lock_file_is_derived_from_product(tmp_path, capsys, monkeypatch):
    """Without an explicit --lock-file, `run` must derive the same default
    lock path scripts/collect_market_rates_workflow.py derives (see
    orchestration.default_lock_file_path) -- otherwise an interactive
    `pricing-intelligence run` and the deployed scheduled job for the same
    product could acquire different locks and collect concurrently against
    the same database."""
    from balance_pricing_engine.pricing_intelligence.locking import SingleInstanceLock
    from balance_pricing_engine.pricing_intelligence.orchestration import (
        default_lock_file_path,
    )

    monkeypatch.chdir(tmp_path)
    database = Path("observations.db")
    lock_path = Path(default_lock_file_path("home_loan", database))

    with SingleInstanceLock(lock_path):
        exit_code = run(["--database", str(database), "run"])

    output = capsys.readouterr().out
    assert exit_code == 3  # EXIT_ALREADY_RUNNING
    assert "Skipped" in output


# --------------------------------------------------------------- health command


def test_health_reports_ok_on_an_empty_database(tmp_path, capsys):
    exit_code = run(["--database", str(tmp_path / "empty.db"), "health"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Database: OK" in output
    assert "Last run: none recorded yet" in output
    assert "UNKNOWN" in output
    assert "Telegram: not configured" in output
    assert "Latest market snapshot: EMPTY" in output


def test_health_reports_a_run_and_collector_status(tmp_path, fixture_fetcher, capsys):
    from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle

    database = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(database), collection=CollectionConfig(inter_request_delay_seconds=0)
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(service, alert_sinks={})

    exit_code = run(["--database", str(database), "health"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Last run:" in output
    assert "FRESH" in output
    assert "Latest market snapshot: VALID" in output


def test_health_marks_a_bank_stale_once_past_the_freshness_window(
    tmp_path, fixture_fetcher, capsys
):
    """A bank that stops being attempted at all (scheduling disabled, the
    machine off for a while) never fails again, so consecutive_failures
    alone would report it FRESH indefinitely -- even once its last success
    is older than max_observation_age_hours and the market snapshot has
    already discarded its rates as stale. The two must not contradict each
    other.

    Collector-health timestamps come from run_operational_cycle's own
    started_at (real wall-clock), not the simulated captured_at_utc, so a
    stale last_success_at is set up directly via update_collector_health()
    rather than by backdating a full operational cycle."""
    database = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(database),
        collection=CollectionConfig(inter_request_delay_seconds=0),
        max_observation_age_hours=1.0,
    )
    stale_capture = datetime.now(timezone.utc) - timedelta(hours=2)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=stale_capture, persist_snapshot=False)
        for bank in config.selected_banks:
            service.store.update_collector_health(
                bank, "home_loan", success=True, at=stale_capture
            )

    # --database alone only overrides database_path -- max_observation_age_hours
    # must come from a --config YAML, or the CLI falls back to the default
    # 168h band and this 2-hour-old observation never reads as stale.
    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(
            _minimal_engine_config(
                database_path=str(database),
                max_observation_age_hours=1.0,
            )
        )
    )

    exit_code = run(["--config", str(engine_config_path), "health"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "STALE" in output
    assert "Latest market snapshot: EMPTY" in output


def test_health_reports_telegram_configured(populated_database, capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")

    exit_code = run(["--database", populated_database, "health"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Telegram: configured" in output


def test_health_exits_nonzero_when_the_database_is_broken(populated_database, capsys):
    with ObservationStore(populated_database) as store:
        store._connection.execute("UPDATE observations SET rate_pct = 999")
        store._connection.commit()

    exit_code = run(["--database", populated_database, "health"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "ISSUES DETECTED" in output
    assert "plausible rate bounds" in output


def test_health_uses_configured_validation_bounds(populated_database, tmp_path, capsys):
    """A --config-supplied validation band must be what check_integrity()
    applies, not silently-constructed default thresholds -- otherwise the
    health command can report 'Database: OK' for a value outside a stricter
    configured band. populated_database's stored rates are ~4.79-4.89%,
    comfortably inside the default max_mortgage_rate_pct (15.0); a config
    tightening it to 4.0 must make the same data flag as implausible."""
    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(
            _minimal_engine_config(
                database_path=populated_database,
                validation={"max_mortgage_rate_pct": 4.0},
            )
        )
    )

    exit_code = run(["--config", str(engine_config_path), "health"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "ISSUES DETECTED" in output
    assert "plausible rate bounds" in output


# --------------------------------------------------------- telegram-test command


def test_telegram_test_reports_not_configured(capsys, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    exit_code = run(["telegram-test"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "not configured" in output


def test_telegram_test_sends_the_fixed_message(capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    captured = []

    def fake_urlopen(request, timeout=None):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        captured.append(json.loads(request.data))
        return FakeResponse()

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    exit_code = run(["telegram-test"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0]["chat_id"] == "-100999"
    assert "NZ Pricing Intelligence Telegram test successful." in captured[0]["text"]
    assert "Sent:" in output


def test_telegram_test_reports_a_delivery_failure(capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")

    def broken_urlopen(request, timeout=None):
        raise ConnectionError("telegram unreachable")

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", broken_urlopen)

    exit_code = run(["telegram-test"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Telegram test failed" in output


# ------------------------------------------------------------------ backup command


def test_backup_writes_a_timestamped_copy(populated_database, tmp_path, capsys):
    destination = tmp_path / "backups"

    exit_code = run(["--database", populated_database, "backup", "--destination", str(destination)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Backup written to" in output
    assert len(list(destination.glob("*.db"))) == 1


def test_backup_reports_when_there_is_no_database_yet(tmp_path, capsys):
    exit_code = run(
        ["--database", str(tmp_path / "missing.db"), "backup", "--destination", str(tmp_path / "backups")]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "nothing to back up" in output
