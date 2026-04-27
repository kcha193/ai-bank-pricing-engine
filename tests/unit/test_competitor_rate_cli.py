import yaml

from balance_pricing_engine.competitor_rates import cli
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRun,
    SourceAttempt,
    ValidationIssue,
)
from balance_pricing_engine.competitor_rates.store import load_rate_run


FIXTURE = "tests/fixtures/competitor_rates/latest.json"


def test_validate_command_reports_fixture_status(capsys):
    exit_code = cli.main(["validate", "--source-file", FIXTURE])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Validation status: valid" in captured.out


def test_report_command_prints_counts(capsys):
    exit_code = cli.main(["report", "--source-file", FIXTURE])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ANZ|home_loan: 1" in captured.out


def test_apply_command_prints_assumptions_without_mutating_config(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_config()), encoding="utf-8")

    exit_code = cli.main(["apply", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Mortgage competitor reference" in captured.out
    assert "Term deposit competitor reference" in captured.out
    assert "competitor_loan_rate: 0.0675" in config_path.read_text(encoding="utf-8")


def test_apply_command_writes_generated_config_when_requested(tmp_path):
    config_path = tmp_path / "config.yaml"
    output_dir = tmp_path / "generated"
    config_path.write_text(yaml.safe_dump(_config()), encoding="utf-8")

    exit_code = cli.main(["apply", "--config", str(config_path), "--write", "--output-dir", str(output_dir)])

    assert exit_code == 0
    generated = list(output_dir.glob("with_competitor_rates_*.yaml"))
    assert len(generated) == 1
    raw = yaml.safe_load(generated[0].read_text(encoding="utf-8"))
    assert raw["market"]["competitor_loan_rate"] == 0.565 / 10


def test_apply_command_reports_disabled_config_without_source_override(tmp_path, capsys):
    raw_config = _config()
    raw_config["competitor_rates"]["enabled"] = False
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")

    exit_code = cli.main(["apply", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "disabled" in captured.out


def test_main_reports_command_errors(monkeypatch, capsys):
    def raise_error(args):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_validate", raise_error)

    exit_code = cli.main(["validate", "--source-file", FIXTURE])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR: boom" in captured.out


def test_fetch_command_writes_warning_artifacts_for_parser_failures(tmp_path, monkeypatch, capsys):
    attempt = SourceAttempt(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        success=False,
        records_extracted=0,
        error="no rates",
    )

    def fake_extract_all_sources(sources, raw_root, captured_at_utc, use_browser_fallback):
        assert raw_root == str(tmp_path / "raw")
        assert use_browser_fallback is True
        return [], [attempt]

    monkeypatch.setattr(cli, "extract_all_sources", fake_extract_all_sources)

    exit_code = cli.main(
        [
            "fetch",
            "--strict",
            "false",
            "--browser-fallback",
            "--raw-dir",
            str(tmp_path / "raw"),
            "--output-dir",
            str(tmp_path / "processed"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Validation status: warning" in captured.out
    assert "Official source failed" in captured.out
    assert (tmp_path / "processed" / "latest.json").exists()


def test_fetch_command_returns_nonzero_for_strict_failures(tmp_path, monkeypatch):
    attempt = SourceAttempt(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        success=False,
        records_extracted=0,
        error="no rates",
    )
    monkeypatch.setattr(
        cli,
        "extract_all_sources",
        lambda sources, raw_root, captured_at_utc, use_browser_fallback: ([], [attempt]),
    )

    exit_code = cli.main(["fetch", "--output-dir", str(tmp_path)])

    assert exit_code == 1


def test_validate_command_returns_nonzero_for_failed_file(tmp_path):
    run = load_rate_run(FIXTURE)
    failed = run.model_copy(
        update={"records": [*run.records, run.records[0]]}
    )
    source_file = tmp_path / "failed.json"
    source_file.write_text(failed.model_dump_json(), encoding="utf-8")

    exit_code = cli.main(["validate", "--source-file", str(source_file)])

    assert exit_code == 1


def test_report_command_prints_validation_issues(tmp_path, capsys):
    run = load_rate_run(FIXTURE)
    with_issue = run.model_copy(
        update={
            "validation_summary": run.validation_summary.model_copy(
                update={
                    "status": "warning",
                    "warning_count": 1,
                    "issues": [ValidationIssue(severity="warning", message="check me")],
                }
            )
        }
    )
    source_file = tmp_path / "warning.json"
    source_file.write_text(with_issue.model_dump_json(), encoding="utf-8")

    exit_code = cli.main(["report", "--source-file", str(source_file)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "WARNING: check me" in captured.out


def test_configured_competitor_rates_enables_explicit_source_file():
    configured = cli._configured_competitor_rates(None, FIXTURE)

    assert configured.enabled is True
    assert configured.source_file == FIXTURE


def test_build_run_counts_records_by_bank_product():
    run = load_rate_run(FIXTURE)

    built = cli._build_run(run.records[:2], run.source_attempts[:2], run.captured_at_utc)

    assert isinstance(built, CompetitorRateRun)
    assert built.counts_by_bank_product_type == {
        "ANZ|home_loan": 1,
        "ANZ|term_deposit": 1,
    }


def _config() -> dict:
    return {
        "random_seed": 42,
        "simulation": {"n_loan_customers": 20, "n_deposit_customers": 20},
        "market": {
            "cash_rate": 0.0425,
            "wholesale_funding_spread": 0.012,
            "competitor_loan_rate": 0.0675,
            "competitor_deposit_rate": 0.045,
            "home_loan_reference_rate": 0.0525,
            "term_deposit_reference_rate": 0.0400,
        },
        "pricing_strategy": {
            "home_loan_spread_bps": {"min": -10, "max": 10, "step": 10},
            "term_deposit_spread_bps": {"min": -10, "max": 10, "step": 10},
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": 0.65,
            "funding_shortfall_penalty_rate": 0.018,
        },
        "response": {
            "loan_rate_elasticity": -85.0,
            "loan_competitor_gap_elasticity": -65.0,
            "deposit_rate_elasticity": 95.0,
            "deposit_competitor_gap_elasticity": 75.0,
        },
        "competitor_rates": {
            "enabled": True,
            "source_file": FIXTURE,
            "selected_banks": ["ANZ", "ASB", "Westpac", "BNZ", "Kiwibank"],
            "max_staleness_hours": 10000,
            "strict": True,
            "mortgage": {
                "strategy": "market_average",
                "term_months": 12,
                "include_specials": True,
                "require_min_20_percent_equity": True,
            },
            "term_deposit": {
                "strategy": "market_average",
                "term_months": 12,
                "product_type": "term_deposit",
            },
        },
    }
