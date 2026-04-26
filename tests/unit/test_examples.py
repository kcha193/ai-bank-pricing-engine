from pathlib import Path


def test_run_baseline_reports_market_relative_pricing_section():
    script = Path("examples/run_baseline.py").read_text(encoding="utf-8")

    assert "Market-Relative Pricing Recommendation" in script
    assert "Market home loan reference rate" in script
    assert "Home loan spread to market" in script
    assert "Market term deposit reference rate" in script
    assert "Term deposit spread to market" in script
