"""RBNZ OCR intelligence: parsing anchored on the heading, never network."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.competitor_rates.collectors.base import ParserError
from balance_pricing_engine.pricing_intelligence.rbnz import (
    MAX_PLAUSIBLE_OCR_PCT,
    MIN_PLAUSIBLE_OCR_PCT,
    OCR_SOURCE_URL,
    OcrObservation,
    fetch_ocr_series,
    parse_ocr_series,
)

FIXTURE = Path("tests/fixtures/pricing_intelligence/rbnz_ocr.html").read_text()
CAPTURED_AT = datetime(2026, 9, 3, tzinfo=timezone.utc)


def test_parses_the_real_ocr_table_not_the_decoy_fx_table():
    """The regression that matters: two tables share the same column shape,
    and only the heading text tells them apart."""
    series = parse_ocr_series(FIXTURE, captured_at_utc=CAPTURED_AT)

    assert series.current.ocr_pct == 2.5
    assert series.current.period == "Aug-2026"
    # The decoy table's NZD/USD value for the same period was 0.59075; a
    # parser that grabbed the first "Date" table would report that instead.
    assert series.current.ocr_pct != 0.59075


def test_extraction_stops_before_the_next_chart():
    """Rows from the chart after OCR (Real TWI) must not leak in."""
    series = parse_ocr_series(FIXTURE, captured_at_utc=CAPTURED_AT)

    assert len(series.observations) == 5
    assert all(obs.ocr_pct <= MAX_PLAUSIBLE_OCR_PCT for obs in series.observations)


def test_observations_are_newest_first():
    series = parse_ocr_series(FIXTURE, captured_at_utc=CAPTURED_AT)

    periods = [obs.period for obs in series.observations]
    assert periods == ["Aug-2026", "Jul-2026", "Jun-2026", "May-2026", "Aug-2025"]


def test_bank_bill_rate_is_captured_alongside_ocr():
    series = parse_ocr_series(FIXTURE, captured_at_utc=CAPTURED_AT)

    assert series.current.bank_bill_90d_pct == 2.98


def test_change_bps_is_exact_and_uses_decimal_arithmetic():
    series = parse_ocr_series(FIXTURE, captured_at_utc=CAPTURED_AT)

    # Aug-2026 (2.50) vs Jun-2026 (2.25): +25 bps, not 24.999999999999996.
    assert series.change_bps(months_back=2) == 25.0
    assert repr(series.change_bps(months_back=2)) == "25.0"


def test_change_bps_is_none_when_the_series_is_too_short():
    series = parse_ocr_series(FIXTURE, captured_at_utc=CAPTURED_AT)

    assert series.change_bps(months_back=100) is None


def test_current_raises_on_an_empty_series():
    from balance_pricing_engine.pricing_intelligence.rbnz import OcrSeries

    empty = OcrSeries(observations=(), source_url=OCR_SOURCE_URL, captured_at_utc=CAPTURED_AT)

    with pytest.raises(ValueError, match="no observations"):
        empty.current


def test_missing_heading_raises_a_clear_parser_error():
    html = "<html><body><h1>Something else entirely</h1></body></html>"

    with pytest.raises(ParserError, match="Official Cash Rate"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_heading_with_no_following_table_raises():
    html = "<html><body><h2>Official Cash Rate (OCR)</h2><p>no table here</p></body></html>"

    with pytest.raises(ParserError, match="no accessible data table"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_a_table_with_no_plausible_rows_raises():
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">chart__table</td>
        <td class="chart__table-data-cell">not-a-number</td>
        <td class="chart__table-data-cell">also-not-a-number</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="no plausible rows"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_an_implausible_rate_is_skipped_not_fatal():
    """One bad row (e.g. a parser regression on one cell) does not cost the
    whole series when the rest of the table is still readable."""
    html = f"""
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">{MAX_PLAUSIBLE_OCR_PCT + 5}</td>
        <td class="chart__table-data-cell">3.0</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert len(series.observations) == 1
    assert series.current.period == "Jul-2026"


def test_duplicate_periods_are_deduplicated_keeping_the_first():
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">9.9</td>
        <td class="chart__table-data-cell">9.9</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert len(series.observations) == 1
    assert series.current.ocr_pct == 2.5


def test_a_bank_bill_cell_that_fails_to_parse_leaves_it_none():
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">n/a</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert series.current.ocr_pct == 2.5
    assert series.current.bank_bill_90d_pct is None


def test_plausibility_bounds_are_documented_constants():
    assert MIN_PLAUSIBLE_OCR_PCT == 0.0
    assert MAX_PLAUSIBLE_OCR_PCT == 10.0


def test_fetch_ocr_series_uses_the_injected_fetcher_not_the_network():
    calls = []

    def fake_fetcher(url):
        calls.append(url)
        return FIXTURE.encode("utf-8")

    series = fetch_ocr_series(fetcher=fake_fetcher, captured_at_utc=CAPTURED_AT)

    assert calls == [OCR_SOURCE_URL]
    assert series.current.ocr_pct == 2.5


def test_fetch_ocr_series_accepts_a_string_or_bytes_fetcher_result():
    series_bytes = fetch_ocr_series(
        fetcher=lambda url: FIXTURE.encode("utf-8"), captured_at_utc=CAPTURED_AT
    )
    series_str = fetch_ocr_series(fetcher=lambda url: FIXTURE, captured_at_utc=CAPTURED_AT)

    assert series_bytes.current.ocr_pct == series_str.current.ocr_pct == 2.5


def test_ocr_observation_is_a_plain_immutable_value():
    obs = OcrObservation(period="Aug-2026", ocr_pct=2.5, bank_bill_90d_pct=2.98)

    with pytest.raises(AttributeError):
        obs.ocr_pct = 9.9  # type: ignore[misc]


def test_dashboard_browser_fetcher_routes_through_the_shared_retry_helper(monkeypatch):
    """The dashboard fetcher must honour configured max_retries/backoff, not
    fail on the first transient browser launch/navigation/timeout error --
    the same guarantee ``build_browser_fetcher`` gives the bank collectors."""
    from balance_pricing_engine.pricing_intelligence import rbnz

    seen = []

    def fake_fetch_with_retries(config, url, attempt):
        seen.append((config, url))
        return b"<html>ok</html>"

    monkeypatch.setattr(rbnz, "_fetch_with_retries", fake_fetch_with_retries)

    config = CollectionConfig(max_retries=3, retry_backoff_seconds=1.5)
    fetcher = rbnz.build_dashboard_browser_fetcher(config)
    html = fetcher(rbnz.OCR_SOURCE_URL)

    assert html == b"<html>ok</html>"
    assert seen == [(config, rbnz.OCR_SOURCE_URL)]
