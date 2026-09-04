"""CLI behaviour, exercised against a temporary store and saved fixtures."""

import argparse
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


def test_ocr_reports_the_current_rate_and_trend(tmp_path, capsys, monkeypatch):
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    series = OcrSeries(
        observations=(
            OcrObservation("Aug-2026", 2.50, 2.98),
            OcrObservation("Jul-2026", 2.50, 2.85),
            OcrObservation("Jun-2026", 2.25, 2.68),
            OcrObservation("May-2026", 2.25, 2.63),
        ),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(cli, "fetch_ocr_series", lambda **kwargs: series)

    exit_code = run(["--database", str(tmp_path / "empty.db"), "ocr"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "2.50%" in output
    assert "Aug-2026" in output
    assert "2.98%" in output
    assert "+25 bps" in output  # 3-month change: 2.50 vs 2.25
    assert series.source_url in output


def test_ocr_does_not_require_a_populated_database(tmp_path, capsys, monkeypatch):
    """OCR is a separate data source; it must work even before any bank
    collection has ever run against this database."""
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    series = OcrSeries(
        observations=(OcrObservation("Aug-2026", 2.50, 2.98),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=datetime.now(timezone.utc),
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


def test_ocr_uses_the_collection_settings_loaded_from_config(tmp_path, capsys, monkeypatch):
    """A --config's pricing_intelligence.collection settings (timeout,
    retries, backoff, user agent) must reach the OCR fetch too -- store
    independence is not fetch-configuration independence."""
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries

    series = OcrSeries(
        observations=(OcrObservation("Aug-2026", 2.50, 2.98),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=datetime.now(timezone.utc),
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
