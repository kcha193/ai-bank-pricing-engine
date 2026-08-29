from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    ProductType,
    category_agnostic_key,
)
from balance_pricing_engine.competitor_rates.sources import OFFICIAL_SOURCES


def test_competitor_rate_record_accepts_auditable_fields():
    record = CompetitorRateRecord(
        bank="ANZ",
        product_type="home_loan",
        product_name="Fixed home loan special",
        rate_category="special",
        term_label_raw="1 year",
        term_months_normalised=12,
        advertised_rate_pct=5.59,
        interest_payment_frequency="unknown",
        lvr_condition="min_20_percent_equity",
        source_url="https://www.anz.co.nz/rates-fees-agreements/home-loans/",
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
        raw_text_context="1 year special 5.59% p.a.",
        raw_html_sha256="a" * 64,
        parser_version="test",
    )

    assert record.product_type == ProductType.HOME_LOAN
    assert record.validation_status == "unvalidated"
    assert record.validation_warnings == []


def test_competitor_rate_record_requires_source_and_identity_fields():
    with pytest.raises(ValidationError):
        CompetitorRateRecord(
            bank="",
            product_type="term_deposit",
            product_name="",
            rate_category="term_deposit",
            term_label_raw="",
            advertised_rate_pct=4.5,
            interest_payment_frequency="at_maturity",
            source_url="",
            captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
            raw_text_context="",
            raw_html_sha256="b" * 64,
            parser_version="test",
        )


def _make_deposit_record(**overrides):
    payload = {
        "bank": "ANZ",
        "product_type": "term_deposit",
        "product_name": "Term deposit",
        "rate_category": "term_deposit",
        "term_label_raw": "1 year",
        "term_months_normalised": 12,
        "advertised_rate_pct": 4.01,
        "interest_payment_frequency": "unknown",
        "source_url": "https://www.anz.co.nz/rates",
        "captured_at_utc": datetime(2026, 4, 27, tzinfo=timezone.utc),
        "raw_text_context": "1 year 4.01%",
        "raw_html_sha256": "a" * 64,
        "parser_version": "test",
    }
    payload.update(overrides)
    return CompetitorRateRecord(**payload)


def test_interest_payment_frequency_accepts_a_combined_canonical_value():
    """normalise_frequency() legitimately produces a '+'-joined combination
    (e.g. "at_maturity+monthly") for a row offering multiple payment
    options -- widening the field to str must not reject that."""
    record = _make_deposit_record(interest_payment_frequency="at_maturity+monthly")

    assert record.interest_payment_frequency == "at_maturity+monthly"


def test_interest_payment_frequency_canonicalizes_token_order():
    """normalise_frequency() always joins matched tokens in sorted order, but
    interest_payment_frequency participates in observation_key() -- a strict
    source or third-party collector supplying the same option set in a
    different order ("monthly+at_maturity") must still collapse onto the
    same identity as the canonical "at_maturity+monthly", not read as a
    withdrawal and a new product appearing."""
    canonical = _make_deposit_record(interest_payment_frequency="at_maturity+monthly")
    reordered = _make_deposit_record(interest_payment_frequency="monthly+at_maturity")

    assert reordered.interest_payment_frequency == "at_maturity+monthly"
    assert canonical.observation_key() == reordered.observation_key()


def test_interest_payment_frequency_rejects_a_typo():
    """Widening interest_payment_frequency from the closed
    InterestPaymentFrequency Literal to str (to support combined values)
    must not let a source-data typo like "montly" bypass validation --
    _select_frequency() would then fail to match any configured preference
    and could select the malformed row through its fallback."""
    with pytest.raises(ValidationError):
        _make_deposit_record(interest_payment_frequency="montly")


def test_interest_payment_frequency_rejects_a_duplicated_token():
    with pytest.raises(ValidationError):
        _make_deposit_record(interest_payment_frequency="monthly+monthly")


def test_category_agnostic_key_matches_the_method_it_backs():
    """The standalone function must agree with the instance method it backs --
    extractors.parse_html_rates() calls the function directly (on a row that
    never became a validated instance), while everything else calls the
    method, and the two must compute identically for the same identity."""
    record = CompetitorRateRecord(
        bank="ANZ",
        product_type="home_loan",
        product_name="Fixed home loan special",
        rate_category="special",
        term_label_raw="1 year",
        term_months_normalised=12,
        advertised_rate_pct=5.59,
        interest_payment_frequency="unknown",
        source_url="https://www.anz.co.nz/rates-fees-agreements/home-loans/",
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
        raw_text_context="1 year special 5.59% p.a.",
        raw_html_sha256="a" * 64,
        parser_version="test",
    )

    assert category_agnostic_key(
        "ANZ", ProductType.HOME_LOAN, 12, "1 year", "unknown", None, None
    ) == record.category_agnostic_group_key()


def test_official_source_registry_contains_all_required_bank_product_pages():
    assert len(OFFICIAL_SOURCES) == 10
    assert {(source.bank, source.product_type.value) for source in OFFICIAL_SOURCES} == {
        ("ANZ", "home_loan"),
        ("ANZ", "term_deposit"),
        ("ASB", "home_loan"),
        ("ASB", "term_deposit"),
        ("Westpac", "home_loan"),
        ("Westpac", "term_deposit"),
        ("BNZ", "home_loan"),
        ("BNZ", "term_deposit"),
        ("Kiwibank", "home_loan"),
        ("Kiwibank", "term_deposit"),
    }
