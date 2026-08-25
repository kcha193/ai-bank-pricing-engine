"""Categorisation of rows as the live bank pages actually emit them.

Every fixture here reproduces a page shape observed during live verification on
2026-08-25. The original fixtures all carried a tier word ("Fixed Special") in
the row text; the real pages put "Special" in the *table heading* and emit bare
rows like `6 month | 4.79% p.a.`. That gap let two banks silently drop out of
the market comparison, so these tests pin the real shape.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    ANZCollector,
    ASBCollector,
    BNZCollector,
    WestpacCollector,
)
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot

FIXTURES = Path("tests/fixtures/pricing_intelligence")
OBSERVED_AT = datetime(2026, 8, 25, tzinfo=timezone.utc)


def collect(collector_class, fixture_name):
    html = (FIXTURES / fixture_name).read_bytes()
    collector = collector_class(fetcher=lambda url: html)
    return collector.collect_mortgage_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    ).records


def test_unmarked_asb_rows_are_categorised_as_specials():
    """ASB's live table has no tier word in the row -- the rates are specials."""
    records = collect(ASBCollector, "asb_home_loans_unmarked.html")
    specials = {r.term_months_normalised: r.advertised_rate_pct
                for r in records if r.rate_category == "special"}

    assert specials[6] == 4.79
    assert specials[12] == 4.99
    assert specials[18] == 5.35


def test_unmarked_bnz_rows_are_categorised_as_specials():
    """BNZ's 'Classic' heading never reaches the row text."""
    records = collect(BNZCollector, "bnz_home_loans.html")

    assert any(r.rate_category == "special" for r in records)


def test_unmarked_westpac_rows_are_carded_not_special():
    """Westpac marks specials in-row, so unmarked means standard.

    This is the opposite of ASB/BNZ, which is exactly why the default cannot be
    inferred generically and belongs on each collector.
    """
    records = collect(WestpacCollector, "westpac_home_loans_marked.html")
    by_category = {}
    for record in records:
        by_category.setdefault(record.rate_category, {})[
            record.term_months_normalised
        ] = record.advertised_rate_pct

    assert by_category["special"][6] == 4.75
    assert by_category["special"][12] == 4.99
    assert by_category["standard"][6] == 5.35
    assert by_category["standard"][12] == 5.59


@pytest.mark.parametrize(
    ("marker_text", "collector_class", "fixture"),
    [("Better Homes Top Up", ASBCollector, "asb_home_loans_unmarked.html")],
)
def test_subsidised_products_never_reach_a_comparable_tier(
    marker_text, collector_class, fixture
):
    """A subsidised 1.00% top-up would wreck any median it reached."""
    records = collect(collector_class, fixture)
    comparable = [
        r for r in records if r.rate_category in ("special", "standard")
    ]

    assert all(r.advertised_rate_pct >= 4.0 for r in comparable), (
        f"{marker_text} leaked into a comparable tier"
    )
    assert any(r.rate_category == "non_comparable" for r in records)


def test_closed_products_are_retained_but_fenced_out():
    """Kept for audit, excluded from statistics."""
    records = collect(ASBCollector, "asb_home_loans_unmarked.html")
    closed = [r for r in records if "closed to new applications" in r.product_name.lower()]

    assert closed, "the closed product must still be recorded"
    assert all(r.rate_category == "non_comparable" for r in closed)


def test_non_comparable_products_are_excluded_from_the_market_snapshot():
    records = collect(ASBCollector, "asb_home_loans_unmarked.html")
    snapshot = build_market_snapshot(records, rate_category="special")

    for term_market in snapshot.terms.values():
        assert all(entry.rate_pct >= 4.0 for entry in term_market.entries)


def test_every_bank_reaches_the_comparison_from_unmarked_pages():
    """The regression that mattered: 2 of 5 banks silently vanished."""
    records = []
    records += collect(ASBCollector, "asb_home_loans_unmarked.html")
    records += collect(BNZCollector, "bnz_home_loans.html")
    records += collect(ANZCollector, "anz_home_loans.html")
    snapshot = build_market_snapshot(records, rate_category="special")

    assert {"ASB", "BNZ", "ANZ"} <= set(snapshot.banks), (
        "a bank whose page omits the tier word must not drop out of the market"
    )


def test_bank_defaults_encode_opposite_meanings():
    """Documents why this is per-bank knowledge, not a global rule."""
    assert ASBCollector.default_fixed_category == "special"
    assert BNZCollector.default_fixed_category == "special"
    assert WestpacCollector.default_fixed_category == "standard"
    assert ANZCollector.default_fixed_category is None
