from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    ProductType,
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
