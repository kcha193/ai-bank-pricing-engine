import yaml

from balance_pricing_engine.competitor_rates import cli


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
