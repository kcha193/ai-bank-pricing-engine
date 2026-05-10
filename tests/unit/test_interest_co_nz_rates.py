from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

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


def test_fetch_interest_co_nz_rates_reports_page_fetch_failures(monkeypatch, tmp_path):
    def fake_fetch(url, raw_root, captured_at_utc):
        return None, None, None, "blocked"

    monkeypatch.setattr(interest_co_nz, "_fetch_interest_co_nz_page", fake_fetch)

    records, attempts = interest_co_nz.fetch_interest_co_nz_rates(
        raw_root=tmp_path,
        captured_at_utc=CAPTURED_AT,
    )

    assert records == []
    assert len(attempts) == 10
    assert all(not attempt.success for attempt in attempts)
    assert {attempt.error for attempt in attempts} == {"blocked"}


def test_fetch_interest_co_nz_page_writes_snapshot(monkeypatch, tmp_path):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def read(self):
            return b"<html><body>rates</body></html>"

    monkeypatch.setattr(
        interest_co_nz,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    html, snapshot_path, raw_hash, error = interest_co_nz._fetch_interest_co_nz_page(
        interest_co_nz.INTEREST_CO_NZ_MORTGAGE_URL,
        raw_root=tmp_path,
        captured_at_utc=CAPTURED_AT,
    )

    assert html == "<html><body>rates</body></html>"
    assert raw_hash
    assert error is None
    assert Path(snapshot_path).exists()


def test_fetch_interest_co_nz_page_reports_fetch_errors(monkeypatch, tmp_path):
    def fail(request, timeout):
        raise URLError("network blocked")

    monkeypatch.setattr(interest_co_nz, "urlopen", fail)

    html, snapshot_path, raw_hash, error = interest_co_nz._fetch_interest_co_nz_page(
        interest_co_nz.INTEREST_CO_NZ_MORTGAGE_URL,
        raw_root=tmp_path,
        captured_at_utc=CAPTURED_AT,
    )

    assert html is None
    assert snapshot_path is None
    assert raw_hash is None
    assert "network blocked" in error


def test_interest_co_nz_helpers_handle_unmatched_and_edge_values():
    html = """
    <table>
      <tr><th>Institution</th><th>Product</th><th>Variable floating</th><th>6 months</th><th>1 year</th></tr>
      <tr><td><img alt="ANZ"></td><td>TotalMoney</td><td>5.94</td><td></td><td></td></tr>
      <tr><td>Non-bank lender</td><td>Standard</td><td>9.99</td><td>8.99</td><td>8.49</td></tr>
      <tr><td><img alt="BNZ"></td><td>Variable floating</td><td>5.75</td><td></td><td>16.00</td></tr>
    </table>
    """

    records = interest_co_nz.parse_interest_co_nz_mortgage_rates(
        html,
        captured_at_utc=CAPTURED_AT,
    )

    assert records == []
    assert interest_co_nz._bank_from_cell("unknown", interest_co_nz.SELECTED_BANKS) is None
    assert interest_co_nz._rate_at_header(["ANZ", "Standard"], ("Institution", "Product", "1 year"), "1 year") is None
    assert interest_co_nz._parse_rate("not a rate") is None
    assert interest_co_nz._parse_rate("16.00") is None
    assert interest_co_nz._looks_like_continuation_row("AA-") is True
    assert interest_co_nz._looks_like_continuation_row("unrelated row") is False
    assert interest_co_nz._mortgage_category("Variable floating") == "floating"
    assert interest_co_nz._interest_co_nz_frequency(["ANZ"]) == "unknown"
