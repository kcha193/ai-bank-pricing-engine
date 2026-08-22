from pathlib import Path


def test_run_baseline_reports_market_relative_pricing_section():
    script = Path("examples/run_baseline.py").read_text(encoding="utf-8")

    assert "Market-Relative Pricing Recommendation" in script
    assert "Market home loan reference rate" in script
    assert "Home loan spread to market" in script
    assert "Market term deposit reference rate" in script
    assert "Term deposit spread to market" in script


def test_examples_do_not_mutate_python_import_path():
    for script_path in Path("examples").glob("*.py"):
        script = script_path.read_text(encoding="utf-8")

        assert "sys.path" not in script


def test_competitor_rate_example_reports_reference_metadata():
    script = Path("examples/run_with_competitor_rates.py").read_text(encoding="utf-8")

    assert "Competitor rate inputs" in script
    assert "Mortgage competitor reference" in script
    assert "Term deposit competitor reference" in script


def test_market_intelligence_example_reuses_the_existing_optimiser():
    script = Path("examples/run_market_intelligence_scenarios.py").read_text(encoding="utf-8")

    assert "run_baseline_diagnostics" in script
    assert "apply_market_strategy_to_config" in script
    assert "MARKET_MEDIAN" in script
    assert "MARKET_LEADER" in script
    assert "MANUAL" in script


def test_market_intelligence_example_states_its_limitations():
    script = Path("examples/run_market_intelligence_scenarios.py").read_text(encoding="utf-8")

    assert "not negotiated customer rates" in script
    assert "does not replace internal pricing governance" in script
