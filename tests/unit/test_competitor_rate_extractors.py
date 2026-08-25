from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

from balance_pricing_engine.competitor_rates import extractors
from balance_pricing_engine.competitor_rates.extractors import (
    extract_all_sources,
    extract_source_rates,
    fetch_source,
    fetch_source_with_playwright,
    parse_html_rates,
)
from balance_pricing_engine.competitor_rates.models import SourceAttempt
from balance_pricing_engine.competitor_rates.sources import RateSource
from balance_pricing_engine.competitor_rates.models import ProductType


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


def test_parse_html_rates_skips_rows_without_rates_or_terms():
    html = """
    <html><head><title>Noisy rates</title></head><body><table>
      <tr><th>Type</th><th>Term</th><th>Rate</th></tr>
      <tr><td>Fee disclosure</td><td>No term</td><td>5.00%</td></tr>
      <tr><td>Fixed</td><td>six months</td><td>No rate here</td></tr>
      <tr><td>Fixed package</td><td>six months</td><td>5.80%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="BNZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].term_label_raw == "six months"
    assert records[0].rate_category == "fixed"


def test_parse_html_rates_deduplicates_identical_rows_and_marks_special_deposits():
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Deposit</th><th>Frequency</th><th>Rate</th></tr>
      <tr><td>Special 6 months</td><td>$10,000+</td><td>At maturity</td><td>4.70%</td></tr>
      <tr><td>Special 6 months</td><td>$10,000+</td><td>At maturity</td><td>4.70%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="Westpac",
        product_type="term_deposit",
        source_url="https://example.test/td",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].rate_category == "special"
    assert records[0].special_conditions is not None


def test_parse_html_rates_preserves_simultaneous_tiers_at_the_same_rate():
    """Two distinct LVR tiers happening to share a rate must not collapse to one."""
    html = """
    <html><body><table>
      <tr><th>Type</th><th>Term</th><th>Rate</th></tr>
      <tr><td>Fixed Special</td><td>1 year</td><td>4.79%</td></tr>
      <tr><td>Fixed Special (requires 20% equity)</td><td>1 year</td><td>4.79%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 2
    assert {record.lvr_condition for record in records} == {
        "standard",
        "min_20_percent_equity",
    }


def test_parse_html_rates_preserves_simultaneous_numeric_lvr_tiers():
    """Two numeric LVR tiers ("70% LVR", "80% LVR") both normalise to the same
    coarse lvr_condition bucket ("standard" -- neither matches the "under 80%"
    wording normalise_lvr_condition looks for), so lvr_condition alone can't
    tell them apart at dedup time, before enrichment has populated max_lvr.
    Deduping on lvr_condition alone would collapse a real, distinct tier and
    lose its later changes entirely.
    """
    html = """
    <html><body><table>
      <tr><th>Type</th><th>LVR</th><th>Term</th><th>Rate</th></tr>
      <tr><td>Fixed Special</td><td>70% LVR</td><td>1 year</td><td>4.79%</td></tr>
      <tr><td>Fixed Special</td><td>80% LVR</td><td>1 year</td><td>4.79%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 2
    assert {record.lvr_condition for record in records} == {"standard"}


def test_parse_html_rates_splits_a_row_with_two_rate_bearing_cells():
    """A row that carries "Special 4.79%" and "Standard 5.39%" as two
    separate cells for one term must produce two records, each with its own
    rate paired to its own category -- not one record for the last cell's
    rate mislabelled with whichever category word appears anywhere in the
    row (here "special", checked first, would otherwise be paired with
    5.39% while 4.79% is silently dropped)."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Special</th><th>Standard</th></tr>
      <tr><td>1 year</td><td>Special 4.79%</td><td>Standard 5.39%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 2
    by_category = {record.rate_category: record.advertised_rate_pct for record in records}
    assert by_category == {"special": 4.79, "standard": 5.39}


def test_parse_html_rates_uses_column_headers_for_bare_multi_rate_cells():
    """When the category label lives only in the column header -- not the
    data cell itself -- a row with two bare rate cells ("4.79%", "5.39%",
    neither containing "Special"/"Standard") must still split into two
    records, one per header column, instead of both cells falling through to
    the generic "home_loan" category and colliding on the same
    observation_key (silently dropping one rate)."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Special</th><th>Standard</th></tr>
      <tr><td>1 year</td><td>4.79%</td><td>5.39%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 2
    by_category = {record.rate_category: record.advertised_rate_pct for record in records}
    assert by_category == {"special": 4.79, "standard": 5.39}


def test_parse_html_rates_uses_column_header_for_bare_single_rate_cell():
    """The header fallback used for multi-rate rows must also apply when
    only one cell in the row carries a plausible rate: a single "Special
    rate" header over a bare "4.79%" data cell must resolve to "special",
    not the generic "home_loan" default that ignores the header entirely."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Special rate</th></tr>
      <tr><td>1 year</td><td>4.79%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].rate_category == "special"
    assert records[0].advertised_rate_pct == 4.79


def test_parse_html_rates_rejects_promotional_percentage_alongside_a_real_rate():
    """A row carrying a genuine rate cell plus an unrelated promotional
    percentage in another cell (e.g. "1% cashback") must emit only the real
    rate. Treating both cells as plausible rates would emit a second, bogus
    observation that -- after enrichment -- can collide on identity with the
    real rate and silently overwrite it in storage."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Rate</th><th>Cashback</th></tr>
      <tr><td>1 year</td><td>5.00%</td><td>1% cashback</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.00


def test_parse_html_rates_extracts_the_rate_when_cashback_shares_its_cell():
    """A genuine rate and a promotional percentage sharing one cell (e.g.
    "Special 5.00% with 1% cashback") must still yield the real rate.
    Rejecting the whole cell because it contains "cashback" would discard
    the genuine rate too, and the row-wide fallback would then pick the
    cashback percentage itself as "the rate" instead."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Rate</th></tr>
      <tr><td>1 year</td><td>Special 5.00% with 1% cashback</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.00


def test_parse_html_rates_extracts_the_rate_when_promo_marker_precedes_its_percentage():
    """The promotional-percentage strip must handle both grammatical orders:
    "1% cashback" (percentage then marker) and, reversed, "cashback of 1%"
    (marker then percentage). The prior fix only stripped the first order,
    so "Special 5.00% with cashback of 1%" still retained "1%" and picked
    it as the rate instead of the genuine 5.00%."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Rate</th></tr>
      <tr><td>1 year</td><td>Special 5.00% with cashback of 1%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.00


def test_parse_html_rates_does_not_consume_an_unrelated_rate_after_a_promo_marker():
    """A promotional marker with no percentage of its own (e.g. "Cashback
    offer") must not swallow a genuine, unrelated rate that merely happens
    to appear within a few characters afterwards (e.g. "; rate 5.00%").
    The marker-then-percentage strip must require the percentage to
    actually belong to the promotion, not just be nearby -- otherwise the
    row loses its only rate and is silently dropped instead of parsed."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Rate</th></tr>
      <tr><td>1 year</td><td>Cashback offer; rate 5.00%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.00


def test_parse_html_rates_preserves_a_rate_described_as_discounted_or_bonus():
    """A marker word immediately followed by "rate" (e.g. "5.00% discounted
    rate", "5.00% bonus rate") names the *type* of rate being advertised,
    not a separate promotional quantity on top of it. The
    percentage-then-marker strip must not treat "discount"/"bonus" here as
    a promotion marker for the rate's own percentage -- doing so drops the
    rate to no observation at all, which can later read as a withdrawal."""
    html = """
    <html><body><table>
      <tr><th>Type</th><th>Term</th><th>Rate</th></tr>
      <tr><td>Special fixed</td><td>1 year</td><td>5.00% discounted rate</td></tr>
      <tr><td>Standard</td><td>2 years</td><td>5.99% bonus rate</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert [record.advertised_rate_pct for record in records] == [5.00, 5.99]


def test_parse_html_rates_excludes_a_low_equity_margin_percentage():
    """A low-equity margin percentage sharing a cell with the genuine rate
    (e.g. "Special 5.49% plus a 0.75% low equity margin") must not be
    treated as a second rate. Unlike a bare "0.75% equity", the "low"
    qualifier sits between the percentage and its marker -- without
    tolerating it, the percentage-then-marker strip requires the marker
    immediately after the percentage and leaves the margin percentage
    untouched, colliding with the genuine rate as a second plausible
    reading."""
    html = """
    <html><body><table>
      <tr><th>Type</th><th>Term</th><th>Rate</th></tr>
      <tr><td>Special</td><td>1 year</td>
          <td>Special 5.49% plus a 0.75% low equity margin</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.49


def test_parse_html_rates_ignores_deposit_eligibility_percentage_for_home_loans():
    """A low deposit-requirement percentage sharing a row with a genuine
    home-loan rate (e.g. "5.49%" and, in another cell, "5% deposit
    required") must not be treated as a second rate: unlike an LVR/equity
    percentage, a deposit requirement can fall within the plausible 0-15%
    range, so the numeric-range filter alone doesn't exclude it."""
    html = """
    <html><body><table>
      <tr><th>Type</th><th>Term</th><th>Rate</th><th>Eligibility</th></tr>
      <tr><td>Special fixed</td><td>1 year</td><td>5.49%</td><td>5% deposit required</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="home_loan",
        source_url="https://example.test/home",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.49


def test_parse_html_rates_keeps_a_low_term_deposit_rate_next_to_the_word_deposit():
    """The home-loan-only deposit/equity exclusion must not reach term
    deposits, where "deposit" is the product name itself and appears next
    to every genuine rate -- excluding it there would discard real rates
    wholesale, not just eligibility percentages."""
    html = """
    <html><body><table>
      <tr><th>Term</th><th>Deposit</th><th>Frequency</th><th>Rate</th></tr>
      <tr><td>6 months</td><td>$10,000+</td><td>At maturity</td><td>4.50%</td></tr>
    </table></body></html>
    """

    records = parse_html_rates(
        bank="ANZ",
        product_type="term_deposit",
        source_url="https://example.test/td",
        html=html,
        captured_at_utc=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 4.50


def test_fetch_source_writes_snapshot_for_successful_http_response(tmp_path, monkeypatch):
    source = RateSource("ANZ", ProductType.HOME_LOAN, "https://example.test/home")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def read(self):
            return b"<html><title>Rates</title></html>"

    monkeypatch.setattr(extractors, "urlopen", lambda request, timeout: FakeResponse())

    html, attempt = fetch_source(source, raw_root=tmp_path)

    assert html == "<html><title>Rates</title></html>"
    assert attempt.success is True
    assert attempt.raw_html_sha256
    assert Path(attempt.snapshot_path).exists()


def test_fetch_source_reports_http_errors(monkeypatch):
    source = RateSource("ASB", ProductType.TERM_DEPOSIT, "https://example.test/td")

    def fail(request, timeout):
        raise URLError("blocked")

    monkeypatch.setattr(extractors, "urlopen", fail)

    html, attempt = fetch_source(source)

    assert html is None
    assert attempt.success is False
    assert "blocked" in attempt.error


def test_extract_source_rates_returns_fetch_failure(monkeypatch):
    source = RateSource("ASB", ProductType.HOME_LOAN, "https://example.test/home")
    failed = SourceAttempt(
        bank="ASB",
        product_type="home_loan",
        source_url=source.url,
        success=False,
        error="timeout",
    )
    monkeypatch.setattr(extractors, "fetch_source", lambda source, raw_root: (None, failed))

    records, attempt = extract_source_rates(source)

    assert records == []
    assert attempt.error == "timeout"


def test_extract_source_rates_uses_browser_fallback_when_static_fetch_fails(monkeypatch):
    source = RateSource("BNZ", ProductType.HOME_LOAN, "https://example.test/home")
    failed_attempt = SourceAttempt(
        bank="BNZ",
        product_type="home_loan",
        source_url=source.url,
        success=False,
        error="HTTP Error 403: Forbidden",
    )
    rendered_attempt = SourceAttempt(
        bank="BNZ",
        product_type="home_loan",
        source_url=source.url,
        success=True,
        snapshot_path="rendered.html",
        raw_html_sha256="b" * 64,
    )
    html = """
    <html><body><table>
      <tr><td>Special fixed</td><td>1 year</td><td>5.55%</td></tr>
    </table></body></html>
    """
    monkeypatch.setattr(extractors, "fetch_source", lambda source, raw_root: (None, failed_attempt))
    monkeypatch.setattr(
        extractors,
        "fetch_source_with_playwright",
        lambda source, raw_root: (html, rendered_attempt),
    )

    records, attempt = extract_source_rates(source, use_browser_fallback=True)

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.55
    assert attempt.success is True
    assert attempt.snapshot_path == "rendered.html"


def test_extract_source_rates_uses_browser_fallback_when_static_parse_is_empty(monkeypatch):
    source = RateSource("ANZ", ProductType.HOME_LOAN, "https://example.test/home")
    static_attempt = SourceAttempt(
        bank="ANZ",
        product_type="home_loan",
        source_url=source.url,
        success=True,
        raw_html_sha256="a" * 64,
    )
    rendered_attempt = SourceAttempt(
        bank="ANZ",
        product_type="home_loan",
        source_url=source.url,
        success=True,
        snapshot_path="rendered.html",
        raw_html_sha256="b" * 64,
    )
    html = """
    <html><body><table>
      <tr><td>Special fixed</td><td>1 year</td><td>5.55%</td></tr>
    </table></body></html>
    """
    monkeypatch.setattr(extractors, "fetch_source", lambda source, raw_root: ("<html></html>", static_attempt))
    monkeypatch.setattr(
        extractors,
        "fetch_source_with_playwright",
        lambda source, raw_root: (html, rendered_attempt),
    )

    records, attempt = extract_source_rates(source, use_browser_fallback=True)

    assert len(records) == 1
    assert records[0].advertised_rate_pct == 5.55
    assert attempt.snapshot_path == "rendered.html"


def test_extract_source_rates_reports_empty_rendered_page(monkeypatch):
    source = RateSource("ANZ", ProductType.HOME_LOAN, "https://example.test/home")
    static_attempt = SourceAttempt(
        bank="ANZ",
        product_type="home_loan",
        source_url=source.url,
        success=True,
        raw_html_sha256="a" * 64,
    )
    fallback_attempt = SourceAttempt(
        bank="ANZ",
        product_type="home_loan",
        source_url=source.url,
        success=False,
        error="Playwright fallback requested but playwright is not installed",
    )
    monkeypatch.setattr(extractors, "fetch_source", lambda source, raw_root: ("<html></html>", static_attempt))
    monkeypatch.setattr(
        extractors,
        "fetch_source_with_playwright",
        lambda source, raw_root: (None, fallback_attempt),
    )

    records, attempt = extract_source_rates(source, use_browser_fallback=True)

    assert records == []
    assert attempt.success is False
    assert attempt.error == "page fetched but no rates were confidently extracted"


def test_fetch_source_with_playwright_reports_missing_optional_dependency():
    source = RateSource("ANZ", ProductType.HOME_LOAN, "https://example.test/home")

    html, attempt = fetch_source_with_playwright(source)

    assert html is None
    assert attempt.success is False
    assert "playwright is not installed" in attempt.error


def test_extract_all_sources_collects_records_and_attempts(monkeypatch):
    source = RateSource("ANZ", ProductType.HOME_LOAN, "https://example.test/home")

    def fake_extract_source_rates(source, raw_root, captured_at_utc, use_browser_fallback):
        attempt = SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=True,
            records_extracted=0,
        )
        return [], attempt

    monkeypatch.setattr(extractors, "extract_source_rates", fake_extract_source_rates)

    records, attempts = extract_all_sources([source], use_browser_fallback=True)

    assert records == []
    assert len(attempts) == 1
