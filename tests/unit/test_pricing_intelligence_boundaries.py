"""Architectural boundaries between market intelligence and pricing.

Pricing intelligence answers "what is the market doing?"; the optimiser answers
"what should we price?". These are asserted as tests because the separation is
easy to erode one convenient import at a time.
"""

from pathlib import Path

import pytest

SOURCE_ROOT = Path("src/balance_pricing_engine")
PRICING_PACKAGES = ("optimization", "pricing", "scenarios", "synthetic")


def python_files(package: str) -> list[Path]:
    return sorted((SOURCE_ROOT / package).rglob("*.py"))


@pytest.mark.parametrize("package", PRICING_PACKAGES)
def test_pricing_layers_do_not_import_market_intelligence(package):
    """The optimiser must not depend on where competitor rates came from."""
    offenders = [
        path
        for path in python_files(package)
        if "pricing_intelligence" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], (
        "pricing layers must stay independent of market intelligence; "
        "route market-derived assumptions through pricing_intelligence.strategy"
    )


def test_market_intelligence_does_not_import_the_optimiser():
    """Nothing in the intelligence layer may price."""
    offenders = []
    for path in python_files("pricing_intelligence"):
        text = path.read_text(encoding="utf-8")
        if "optimization" in text or "grid_search" in text or "scenarios.runner" in text:
            offenders.append(path.name)

    assert offenders == [], f"market intelligence must not price: {offenders}"


def test_strategy_is_the_only_module_touching_engine_config():
    """The config overlay lives in exactly one place."""
    touching = [
        path.name
        for path in python_files("pricing_intelligence")
        if "config.market.model_copy" in path.read_text(encoding="utf-8")
    ]

    assert touching == ["strategy.py"]


def test_collectors_are_the_only_place_with_bank_specific_knowledge():
    """A bank changing its site must not require touching market logic."""
    bank_names = ("anz", "asb", "bnz", "westpac", "kiwibank")
    offenders = []
    for path in python_files("pricing_intelligence"):
        lowered = path.read_text(encoding="utf-8").lower()
        # config.py legitimately lists the default tracked banks.
        if path.name == "config.py":
            continue
        if any(f'"{bank}' in lowered or f"'{bank}" in lowered for bank in bank_names):
            offenders.append(path.name)

    assert offenders == [], (
        f"bank-specific knowledge belongs in competitor_rates/collectors/: {offenders}"
    )
