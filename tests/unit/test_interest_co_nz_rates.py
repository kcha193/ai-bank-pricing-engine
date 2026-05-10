from datetime import datetime, timezone

from balance_pricing_engine.competitor_rates import interest_co_nz
from balance_pricing_engine.competitor_rates.models import ProductType


CAPTURED_AT = datetime(2026, 5, 10, tzinfo=timezone.utc)


def test_parse_interest_co_nz_mortgage_rows_extracts_major_bank_one_year_rates():
    html = """
    <html><body><table>
      <tr>
        <th>Institution</th><th>Product</th><th>Variable floating</th>
        <th>6 months</th><th>1 year</th><th>2 years</th>
      </tr>
      <tr><td><img alt="ANZ"></td><td>Standard</td><td>5.79</td><td>5.09</td><td>5.29</td><td>5.89</td></tr>
      <tr><td></td><td>Special LVR under 80%</td><td></td><td>4.49</td><td>4.69</td><td>5.29</td></tr>
      <tr><td><img alt="ASB"></td><td>Standard</td><td>5.79</td><td>4.49</td><td>4.65</td><td>5.25</td></tr>
      <tr><td><img alt="Westpac"></td><td>Special LVR < 80%</td><td></td><td>4.69</td><td>4.69</td><td>5.19</td></tr>
    </table></body></html>
    """

    records = interest_co_nz.parse_interest_co_nz_mortgage_rates(
        html,
        captured_at_utc=CAPTURED_AT,
    )

    assert [(record.bank, record.rate_category, record.advertised_rate_pct) for record in records] == [
        ("ANZ", "standard", 5.29),
        ("ANZ", "special", 4.69),
        ("ASB", "standard", 4.65),
        ("Westpac", "special", 4.69),
    ]
    assert all(record.product_type == ProductType.HOME_LOAN for record in records)
    assert records[1].lvr_condition == "min_20_percent_equity"
    assert records[0].source_url == interest_co_nz.INTEREST_CO_NZ_MORTGAGE_URL


def test_parse_interest_co_nz_term_deposit_rows_extracts_twelve_month_rates():
    html = """
    <html><body><table>
      <tr>
        <th>Institution</th><th>Credit Rating</th><th>Minimum Deposit</th>
        <th>Credit</th><th>Paid</th><th>12 mths</th><th>18 mths</th>
      </tr>
      <tr><td><img alt="ANZ"></td><td>AA-</td><td>$10,000</td><td>Q</td><td>Y</td><td>3.70</td><td>4.00</td></tr>
      <tr><td><img alt="ASB"></td><td>AA-</td><td>$5,000</td><td>Q</td><td>Y</td><td>3.75</td><td>4.00</td></tr>
      <tr><td></td><td>AA-</td><td>$10,000</td><td>Q</td><td>Y</td><td>3.75</td><td>4.00</td></tr>
      <tr><td><img alt="Kiwibank"></td><td>A1</td><td>$1,000</td><td>M</td><td>M</td><td>3.75</td><td>4.25</td></tr>
    </table></body></html>
    """

    records = interest_co_nz.parse_interest_co_nz_term_deposit_rates(
        html,
        captured_at_utc=CAPTURED_AT,
    )

    assert [(record.bank, record.advertised_rate_pct) for record in records] == [
        ("ANZ", 3.70),
        ("ASB", 3.75),
        ("Kiwibank", 3.75),
    ]
    assert all(record.product_type == ProductType.TERM_DEPOSIT for record in records)
    assert records[0].term_months_normalised == 12
    assert records[0].min_balance_or_deposit == 10000


def test_parse_interest_co_nz_term_deposit_rows_deduplicates_validation_equivalent_rates():
    html = """
    <html><body><table>
      <tr>
        <th>Institution</th><th>Credit Rating</th><th>Minimum Deposit</th>
        <th>Credit</th><th>Paid</th><th>12 mths</th><th>18 mths</th>
      </tr>
      <tr><td><img alt="ASB"></td><td>AA-</td><td>$5,000</td><td>Q</td><td>Y</td><td>3.75</td><td>4.00</td></tr>
      <tr><td></td><td>AA-</td><td>$10,000</td><td>Q</td><td>Y</td><td>3.75</td><td>4.00</td></tr>
    </table></body></html>
    """

    records = interest_co_nz.parse_interest_co_nz_term_deposit_rates(
        html,
        captured_at_utc=CAPTURED_AT,
    )

    assert len(records) == 1
    assert records[0].bank == "ASB"
    assert records[0].min_balance_or_deposit == 5000


def test_fetch_interest_co_nz_rates_returns_records_and_attempts(monkeypatch, tmp_path):
    mortgage_html = """
    <table>
      <tr><th>Institution</th><th>Product</th><th>Variable floating</th><th>6 months</th><th>1 year</th></tr>
      <tr><td><img alt="ANZ"></td><td>Standard</td><td>5.79</td><td>5.09</td><td>5.29</td></tr>
    </table>
    """
    deposit_html = """
    <table>
      <tr><th>Institution</th><th>Credit Rating</th><th>Minimum Deposit</th><th>Credit</th><th>Paid</th><th>12 mths</th></tr>
      <tr><td><img alt="ANZ"></td><td>AA-</td><td>$10,000</td><td>Q</td><td>Y</td><td>3.70</td></tr>
    </table>
    """

    def fake_fetch(url, raw_root, captured_at_utc):
        if url == interest_co_nz.INTEREST_CO_NZ_MORTGAGE_URL:
            return mortgage_html, "mortgage.html", "a" * 64, None
        return deposit_html, "deposit.html", "b" * 64, None

    monkeypatch.setattr(interest_co_nz, "_fetch_interest_co_nz_page", fake_fetch)

    records, attempts = interest_co_nz.fetch_interest_co_nz_rates(
        raw_root=tmp_path,
        captured_at_utc=CAPTURED_AT,
    )

    assert len(records) == 2
    assert {(record.product_type, record.bank) for record in records} == {
        (ProductType.HOME_LOAN, "ANZ"),
        (ProductType.TERM_DEPOSIT, "ANZ"),
    }
    assert len(attempts) == 10
    assert sum(attempt.success for attempt in attempts) == 2
