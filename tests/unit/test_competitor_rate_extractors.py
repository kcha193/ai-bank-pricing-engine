from datetime import datetime, timezone
from pathlib import Path

from balance_pricing_engine.competitor_rates.extractors import parse_html_rates


FIXTURE_DIR = Path("tests/fixtures/competitor_rates")


def test_parse_home_loan_fixture_extracts_special_standard_and_floating_rates():
    html = (FIXTURE_DIR / "sample_bank_home_loan.html").read_text(encoding="utf-8")

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert [record.term_label_raw for record in records] == ["1 year", "2 years", "Floating"]
    assert [record.advertised_rate_pct for record in records] == [5.59, 6.19, 7.99]
    assert records[0].rate_category == "special"
    assert records[0].term_months_normalised == 12
    assert records[0].lvr_condition == "min_20_percent_equity"
    assert records[2].rate_category == "floating"
    assert records[2].raw_html_sha256
    assert "Floating" in records[2].raw_text_context


def test_parse_term_deposit_fixture_extracts_terms_tiers_and_frequency_variants():
    html = (FIXTURE_DIR / "sample_bank_term_deposit.html").read_text(encoding="utf-8")

    records = parse_html_rates(
        bank="ASB",
        product_type="term_deposit",
        source_url="https://example.test/td",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 4
    assert records[0].term_label_raw == "30 days"
    assert records[0].term_days_raw == 30
    assert records[0].term_months_normalised == 1
    assert records[0].min_balance_or_deposit == 5000
    assert records[0].interest_payment_frequency == "at_maturity"
    assert records[2].interest_payment_frequency == "monthly"
    assert records[3].rate_category == "pie_term_deposit"
