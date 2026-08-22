"""CLI behaviour, exercised against a temporary store and saved fixtures."""

from datetime import datetime, timezone

import pytest

from balance_pricing_engine.pricing_intelligence import cli
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)


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
