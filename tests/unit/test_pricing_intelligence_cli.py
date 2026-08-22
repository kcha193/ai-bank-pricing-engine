"""CLI behaviour, exercised against a temporary store and saved fixtures."""

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence import cli
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)


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


def run(argv):
    return cli.main(argv)


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


def test_history_lists_observed_states(populated_database, capsys):
    exit_code = run(
        ["--database", populated_database, "history", "--bank", "ANZ", "--term", "12"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "RATE HISTORY" in output
    assert "ANZ" in output
    assert "4.79" in output


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


def test_position_reports_competitive_features(populated_database, capsys):
    exit_code = run(
        ["--database", populated_database, "position", "--rate", "4.95", "--term", "12"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "COMPETITIVE POSITION" in output
    assert "our_rate_minus_median_bps" in output
    assert "market_rank" in output


def test_strategy_resolves_a_market_median_assumption(populated_database, capsys):
    exit_code = run(
        ["--database", populated_database, "strategy", "--strategy", "MARKET_MEDIAN"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "MARKET_MEDIAN" in output
    assert "competitor_loan_rate" in output


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
