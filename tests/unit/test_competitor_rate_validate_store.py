from datetime import datetime, timezone
from pathlib import Path

from balance_pricing_engine.competitor_rates.models import CompetitorRateRun
from balance_pricing_engine.competitor_rates.store import load_rate_run, write_rate_run
from balance_pricing_engine.competitor_rates.validate import validate_rate_run


FIXTURE = Path("tests/fixtures/competitor_rates/latest.json")


def test_validate_rate_run_accepts_fixture_without_errors():
    run = load_rate_run(FIXTURE)

    summary = validate_rate_run(run, strict=True)

    assert summary.status == "valid"
    assert summary.error_count == 0
    assert summary.total_records == 10


def test_validate_rate_run_flags_duplicate_records():
    run = load_rate_run(FIXTURE)
    duplicate = run.model_copy(update={"records": [*run.records, run.records[0]]})

    summary = validate_rate_run(duplicate, strict=True)

    assert summary.status == "failed"
    assert any("Duplicate" in issue.message for issue in summary.issues)


def test_validate_rate_run_flags_parser_failures_and_missing_expected_sources():
    run = load_rate_run(FIXTURE)
    failed_attempt = run.source_attempts[0].model_copy(
        update={"success": False, "records_extracted": 0, "error": "no rates"}
    )
    missing = run.model_copy(
        update={
            "source_attempts": [failed_attempt, *run.source_attempts[2:]],
            "records": run.records[2:],
        }
    )

    summary = validate_rate_run(missing, strict=True)

    assert summary.status == "failed"
    assert any("failed" in issue.message for issue in summary.issues)
    assert any("Missing expected source" in issue.message for issue in summary.issues)


def test_validate_rate_run_warns_on_large_historical_rate_movements():
    run = load_rate_run(FIXTURE)
    previous = run.model_copy(
        update={
            "run_id": "previous",
            "records": [
                record.model_copy(update={"advertised_rate_pct": record.advertised_rate_pct - 1.0})
                for record in run.records
            ],
        }
    )

    summary = validate_rate_run(run, previous_run=previous, strict=False)

    assert summary.status == "warning"
    assert any("movement" in issue.message for issue in summary.issues)


def test_write_rate_run_persists_latest_csv_history_and_report(tmp_path):
    run = load_rate_run(FIXTURE)

    write_rate_run(run, tmp_path)

    assert (tmp_path / "latest.json").exists()
    assert (tmp_path / "latest.csv").exists()
    assert (tmp_path / "validation_report_latest.json").exists()
    history_files = list((tmp_path / "history").glob("*.json"))
    assert len(history_files) == 1
    reloaded = CompetitorRateRun.model_validate_json((tmp_path / "latest.json").read_text())
    assert reloaded.run_id == "fixture-run"


def test_load_rate_run_preserves_utc_capture_timestamp():
    run = load_rate_run(FIXTURE)

    assert run.captured_at_utc == datetime(2026, 4, 27, tzinfo=timezone.utc)
