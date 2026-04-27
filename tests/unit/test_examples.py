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
