"""Term-deposit extraction against the structures the live bank pages carried on 2026-10-30.

Each fixture under ``tests/fixtures/pricing_intelligence/term_deposits_2026-10-30/`` is the
live rendered page trimmed to its tables (verified to parse to exactly the same records as the
full page). A live diagnostic collection that day found three extraction defects these tests pin:

* BNZ advertises a 7-day term; ``max(round(7 / 30), 1)`` made it a "1-month" rate that collided
  with the 30-day rate, so validation rejected both and BNZ was reported as a failed bank.
* ANZ, BNZ and Westpac publish their PIE fund's "rate of return" table beside the ordinary
  term-deposit table, without the word PIE in any cell; those rows were ordinary term deposits.
  Equal rates hid this (dedupe), but a PIE rate above the advertised rate would win market
  leadership, and a different one would trigger the same conflict as BNZ's 7-day row.
* Kiwibank's page carries a savings-account table (Notice Saver, Online Call) and an early
  termination charge table whose sentences parsed as 1-month term deposits.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.competitor_rates.normalise import normalise_term
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pricing_intelligence"
LIVE = FIXTURES / "term_deposits_2026-10-30"
BANKS = ("ANZ", "ASB", "BNZ", "Kiwibank", "Westpac")
OBSERVED_AT = datetime(2026, 10, 30, tzinfo=timezone.utc)

#: The ordinary advertised rates on the live pages that day (by bank, months). Kiwibank does not
#: offer 18 months, and that absence must stay an absence.
ADVERTISED = {
    "ANZ": {6: 3.60, 12: 4.00, 18: 4.20, 24: 4.30, 36: 4.40},
    "ASB": {6: 3.60, 12: 4.00, 18: 4.30, 24: 4.30, 36: 4.40},
    "BNZ": {6: 3.60, 12: 4.05, 18: 4.30, 24: 4.35, 36: 4.60},
    "Kiwibank": {6: 3.60, 12: 4.05, 24: 4.30, 36: 4.60},
    "Westpac": {6: 3.45, 12: 4.00, 18: 4.30, 24: 4.30, 36: 4.60},
}


def parse(bank, html=None):
    html = html if html is not None else (LIVE / f"{bank.lower()}_term_deposits.html").read_text()
    collector = COLLECTORS_BY_BANK[bank](fetcher=lambda url: b"")
    return collector.parse_term_deposit_html(html, OBSERVED_AT)


def comparable(records):
    return [r for r in records if r.rate_category == "term_deposit"]


# ------------------------------------------------------------- term normalisation


@pytest.mark.parametrize(
    ("label", "months", "days"),
    [
        ("30 days", 1, 30),
        ("90 days", 3, 90),
        ("180 days", 6, 180),
        ("270 days", 9, 270),
        ("365 days", 12, 365),
        # Not a whole number of months: kept by its own label, never rounded into a neighbour.
        ("7 days", None, 7),
        ("100 days", None, 100),
        ("200 days", None, 200),
    ],
)
def test_day_terms_map_to_months_only_when_they_are_a_whole_number_of_months(label, months, days):
    term = normalise_term(label)
    assert (term.months, term.days) == (months, days)


# ---------------------------------------------------------- per-bank live structures


@pytest.mark.parametrize("bank", BANKS)
def test_every_bank_yields_its_advertised_rates_at_the_tracked_terms(bank):
    by_term = defaultdict(set)
    for record in comparable(parse(bank)):
        by_term[record.term_months_normalised].add(record.advertised_rate_pct)
    for term, rate in ADVERTISED[bank].items():
        assert by_term[term] == {rate}, (bank, term, by_term[term])
    assert set(ADVERTISED[bank]) <= set(by_term)


def test_a_term_the_bank_does_not_offer_is_not_invented():
    assert 18 not in {r.term_months_normalised for r in parse("Kiwibank")}


@pytest.mark.parametrize("bank", BANKS)
def test_no_two_comparable_rows_share_an_identity_with_different_rates(bank):
    """What validation rejects as conflicting_observation (failing the whole bank)."""
    rates = defaultdict(set)
    for record in parse(bank):
        if record.rate_category != "non_comparable":
            rates[record.observation_key()].add(record.advertised_rate_pct)
    assert {key: found for key, found in rates.items() if len(found) > 1} == {}


def test_bnz_seven_day_rate_is_kept_without_colliding_with_its_30_day_rate():
    records = comparable(parse("BNZ"))
    seven = [r for r in records if r.term_days_raw == 7]
    thirty = [r for r in records if r.term_days_raw == 30]
    assert [(r.term_months_normalised, r.advertised_rate_pct) for r in seven] == [(None, 1.70)]
    assert [(r.term_months_normalised, r.advertised_rate_pct) for r in thirty] == [(1, 1.85)]


@pytest.mark.parametrize("bank", ["ANZ", "BNZ", "Westpac"])
def test_pie_rate_of_return_tables_are_not_ordinary_term_deposits(bank):
    records = parse(bank)
    assert not [r for r in comparable(records) if "rate of return" in r.raw_text_context.lower()]
    pie = [r for r in records if r.rate_category == "pie_term_deposit"]
    assert {r.term_months_normalised for r in pie} >= {6, 12, 24, 36}
    assert all(r.product_name == "PIE term deposit" for r in pie)


@pytest.mark.parametrize(
    ("bank", "pie_header", "pie_row", "inflated_row"),
    [
        # Raise only the PIE table's 1-year figure; the advertised rate must still represent
        # the bank and the bank must not fail validation.
        (
            "BNZ",
            "Rate of return p.a.",
            "<td>1 year</td><td>4.05%</td>",
            "<td>1 year</td><td>4.95%</td>",
        ),
        ("ANZ", "Rate of return", "<td>4.00 % p.a.</td>", "<td>4.95 % p.a.</td>"),
        ("Westpac", "Return Frequency", "<td>4% p.a.</td>", "<td>4.95% p.a.</td>"),
    ],
)
def test_a_higher_pie_rate_never_becomes_the_banks_advertised_rate(
    bank, pie_header, pie_row, inflated_row
):
    html = (LIVE / f"{bank.lower()}_term_deposits.html").read_text()
    split = html.index(pie_header)
    pie_part = html[split:]
    assert pie_row in pie_part
    html = html[:split] + pie_part.replace(pie_row, inflated_row, 1)
    one_year = [r for r in comparable(parse(bank, html)) if r.term_months_normalised == 12]
    assert {r.advertised_rate_pct for r in one_year} == {ADVERTISED[bank][12]}


def test_an_effective_rate_of_return_column_does_not_make_a_table_pie():
    """ANZ's 2026-08-27 layout: one ordinary table, with the tax-adjusted column beside it."""
    records = parse("ANZ", (FIXTURES / "anz_term_deposits.html").read_text())
    assert {(r.term_months_normalised, r.advertised_rate_pct) for r in comparable(records)} == {
        (6, 3.55),
        (12, 3.90),
        (24, 4.30),
    }
    assert not [r for r in records if r.rate_category == "pie_term_deposit"]


def test_kiwibank_savings_accounts_and_charges_are_not_term_deposits():
    records = parse("Kiwibank")
    leaked = [
        r
        for r in records
        if r.rate_category != "non_comparable"
        and any(
            marker in r.raw_text_context.lower()
            for marker in ("notice saver", "online call", "termination charge")
        )
    ]
    assert leaked == []
    # Kept for audit, not dropped.
    assert any("notice saver" in r.raw_text_context.lower() for r in records)


def test_kiwibank_pie_column_stays_pie_and_its_ordinary_column_stays_comparable():
    records = parse("Kiwibank")
    one_year = {r.rate_category for r in records if r.term_months_normalised == 12}
    assert {"term_deposit", "pie_term_deposit"} <= one_year


def test_asb_single_table_is_unaffected():
    records = parse("ASB")
    assert {r.rate_category for r in records} == {"term_deposit"}
    assert len(records) == 13


# ------------------------------------------------------- end to end through the service


def test_a_term_deposit_collection_of_every_bank_succeeds_and_stays_separate(fixture_fetcher):
    now = datetime.now(timezone.utc)
    config = PricingIntelligenceConfig(
        database_path=":memory:",
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(":memory:"), fetcher=fixture_fetcher()
    ) as service:
        loans = service.collect(captured_at_utc=now - timedelta(minutes=5), persist_snapshot=False)
        deposits = service.collect(
            captured_at_utc=now, persist_snapshot=False, product_type=ProductType.TERM_DEPOSIT
        )
        assert set(deposits.succeeded_banks) == set(BANKS)
        assert deposits.validation.errors == []
        assert all(
            ProductType(r.product_type) is ProductType.TERM_DEPOSIT
            for r in deposits.validation.accepted
        )

        snapshot = service.get_market_snapshot(product_type=ProductType.TERM_DEPOSIT, now=now)
        assert snapshot.rate_category == "term_deposit"
        for term in (6, 12, 24, 36):
            market = snapshot.term(term)
            assert {entry.bank for entry in market.entries} == set(BANKS)
            assert {e.bank: e.rate_pct for e in market.entries} == {
                bank: ADVERTISED[bank][term] for bank in BANKS
            }
            # Deposits rank highest-is-best.
            assert market.entries[0].rate_pct == max(e.rate_pct for e in market.entries)
        assert {e.bank for e in snapshot.term(18).entries} == set(BANKS) - {"Kiwibank"}

        # The home-loan market is untouched by the deposit collection.
        loan_snapshot = service.get_market_snapshot(now=now)
        assert all(
            ProductType(r.product_type) is ProductType.HOME_LOAN for r in loans.validation.accepted
        )
        assert loan_snapshot.product_type is ProductType.HOME_LOAN
        assert loan_snapshot.term(12).entries
