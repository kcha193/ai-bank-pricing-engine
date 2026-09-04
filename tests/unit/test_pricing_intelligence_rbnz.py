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
    OcrSeries,
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


def test_the_header_row_is_captured_without_a_marker_workaround():
    """A minimal reproduction of the real page's table shape -- no literal
    "chart__table" text placed ahead of the header the way other synthetic
    fixtures in this file do. The substring's first real occurrence here is
    inside the header row's own class="chart__table-row" attribute, *after*
    that row's opening "<tr class=" -- an anchor search that lands there
    truncates the header's opening tag out of the parse window, so it would
    never match the row regex at all, silently defeating both the "Date"
    skip and the column-order validation. This must parse successfully, not
    raise "no OCR table header"."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>
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
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert series.current.period == "Aug-2026"
    assert series.current.ocr_pct == 2.5


def test_a_row_with_an_extra_class_or_attribute_still_matches():
    """The row regex must match structurally, not on the exact literal
    class="chart__table-row" attribute value. If the newest row gained an
    extra modifier class (e.g. highlighting it as the latest reading) or any
    other attribute while older rows kept the plain markup, a regex that
    required that exact string would fail to match the newest row at all --
    it would never enter the parse loop, so none of the newest-row guards
    get a chance to run, and the row after it would be silently promoted to
    `current` instead."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row chart__table-row--latest" data-latest="true">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert series.current.period == "Aug-2026"
    assert len(series.observations) == 2


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


def test_change_bps_resolves_by_calendar_period_not_list_position():
    """If an intermediate month is missing (e.g. a skipped invalid row),
    ``change_bps(1)`` must not compare against whatever row happens to sit
    next in the list -- that could silently be two calendar months away and
    get mislabelled as a one-month change."""
    series = OcrSeries(
        observations=(
            OcrObservation("Aug-2026", 2.50, 2.98),
            # Jul-2026 is missing entirely -- position 1 is really Jun-2026.
            OcrObservation("Jun-2026", 2.25, 2.68),
        ),
        source_url=OCR_SOURCE_URL,
        captured_at_utc=CAPTURED_AT,
    )

    assert series.change_bps(months_back=1) is None  # no Jul-2026 observation
    assert series.change_bps(months_back=2) == 25.0  # Aug-2026 vs Jun-2026


def test_current_raises_on_an_empty_series():
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


def test_swapped_ocr_and_bank_bill_columns_are_rejected():
    """Data rows carry no column label of their own -- they're parsed by
    position against the header. If RBNZ ever swapped the OCR and 90-day
    bank-bill columns while keeping the same heading and 3-cell shape, both
    values would still pass the plausibility band, so the swap must be
    caught here rather than silently overlaying the bank-bill rate into
    pricing as if it were the OCR."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
        <td class="chart__table-data-cell">OCR</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.98</td>
        <td class="chart__table-data-cell">2.5</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="not in the expected order"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_an_unrecognised_column_header_is_rejected():
    """A header that doesn't look like either OCR or bank-bill in its data
    columns (e.g. a page redesign that renamed both) must fail loud rather
    than silently parse by position against labels it never actually
    confirmed."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">Series A</td>
        <td class="chart__table-data-cell">Series B</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="not in the expected order"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_data_rows_are_rejected_without_a_validated_header():
    """Reaching a data-shaped row before any header row has confirmed the
    column order must be fatal -- a redesigned page that dropped the header
    entirely while also reordering the columns would otherwise still parse
    silently by position (both OCR and bank-bill values are plausible-looking
    percentages)."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="no OCR table header"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_a_table_with_no_plausible_rows_raises():
    """The sole data row is also the newest row, so this now surfaces through
    the newest-row guard rather than the (now practically unreachable except
    when every row is filtered out before parsing is even attempted)
    end-of-loop "no plausible rows" fallback."""
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

    with pytest.raises(ParserError, match="the newest OCR row"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_a_table_with_only_header_and_duplicate_rows_raises_no_plausible_rows():
    """Every row is filtered out before a float parse is even attempted (a
    repeated header, then a duplicate period) -- no row is ever a parse
    candidate, so this is the one path that still reaches the end-of-loop
    fallback rather than the newest-row guard."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="no plausible rows"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_an_implausible_rate_in_an_older_row_is_skipped_not_fatal():
    """One bad *older* row (e.g. a parser regression on one cell) does not
    cost the whole series when the newest row and the rest of the table are
    still readable."""
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
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">{MAX_PLAUSIBLE_OCR_PCT + 5}</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert len(series.observations) == 1
    assert series.current.period == "Aug-2026"


def test_an_implausible_rate_in_the_newest_row_is_fatal():
    """The newest row failing to parse must not silently promote an older
    row to ``current`` -- that would report last month's OCR as if it were
    the live one, with nothing to signal the substitution happened."""
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

    with pytest.raises(ParserError, match="the newest OCR row"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_an_unparsable_rate_in_the_newest_row_is_fatal():
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
        <td class="chart__table-data-cell">not-a-number</td>
        <td class="chart__table-data-cell">3.0</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="the newest OCR row"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_a_missing_cell_on_the_newest_row_is_fatal():
    """A malformed newest row must fail loud even when the defect is a wrong
    cell count (a missing/extra cell), not just a bad rate value -- the
    cell-count filter must not silently `continue` past it before the
    newest-row guard gets a chance to run."""
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
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="the newest OCR row"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_an_empty_period_on_the_newest_row_is_fatal():
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell"></td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="the newest OCR row"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_a_missing_cell_on_an_older_row_is_skipped_not_fatal():
    """The same wrong-cell-count defect on an *older* row must remain
    non-fatal, matching the treatment already given to a bad rate value on
    an older row."""
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
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert len(series.observations) == 1
    assert series.current.period == "Aug-2026"


def test_a_malformed_period_label_on_an_older_row_is_skipped_not_fatal():
    """A period that doesn't parse as month-year (e.g. a space instead of a
    dash) must not be retained: change_bps() calls _parse_period() on every
    retained observation, so one bad older row would otherwise raise a raw
    ValueError and fail the whole ocr command even when the current row and
    the requested comparison month are both fine."""
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
        <td class="chart__table-data-cell">Jul 2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    series = parse_ocr_series(html, captured_at_utc=CAPTURED_AT)

    assert len(series.observations) == 1
    assert series.current.period == "Aug-2026"
    # The malformed row must not have slipped through and later broken
    # change_bps() on some other observation's lookup.
    assert series.change_bps(months_back=1) is None


def test_a_malformed_period_label_on_the_newest_row_is_fatal():
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug 2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.85</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="the newest OCR row"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_out_of_order_rows_are_rejected_not_silently_trusted():
    """The table is documented as newest-first, and OcrSeries.current relies
    on that: whatever row is accepted first becomes "the current OCR". If a
    later row turns out to be *newer* than an already-accepted row -- the
    table isn't actually sorted the way this parser assumes -- the earlier
    "current" choice can no longer be trusted, even though it may still be
    well within the 70-day currency window on its own."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.25</td>
        <td class="chart__table-data-cell">2.68</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">2.5</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="not strictly newest-first"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


def test_out_of_order_row_is_caught_even_when_its_own_rate_is_unparsable():
    """The ordering check must run as soon as a later row's *period* is known
    to be newer than an already-accepted row, before any rate-related
    skip/continue -- a partially rendered newer row (valid period, bad rate)
    must not be silently skipped in a way that leaves an already-proven-wrong
    earlier row as `current`."""
    html = """
    <h2>Official Cash Rate (OCR)</h2>
    <table>chart__table
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Date</td>
        <td class="chart__table-data-cell">OCR</td>
        <td class="chart__table-data-cell">90 day bank bill rate</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Jul-2026</td>
        <td class="chart__table-data-cell">2.25</td>
        <td class="chart__table-data-cell">2.68</td>
      </tr>
      <tr class="chart__table-row">
        <td class="chart__table-data-cell">Aug-2026</td>
        <td class="chart__table-data-cell">not-a-number</td>
        <td class="chart__table-data-cell">2.98</td>
      </tr>
    </table>
    """

    with pytest.raises(ParserError, match="not strictly newest-first"):
        parse_ocr_series(html, captured_at_utc=CAPTURED_AT)


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


def test_fetch_ocr_series_honours_a_falsey_injected_fetcher():
    """A caller-supplied fetcher that happens to be falsey (e.g. a callable
    test double implementing __len__ and currently empty) must still be
    used -- `fetcher or (...)` would otherwise discard it and silently build
    the live browser fetcher instead, which could contact RBNZ or fail on
    the optional playwright dependency despite an explicit fetcher having
    been supplied."""

    class FalseyFetcher:
        def __init__(self):
            self.calls = []

        def __len__(self):
            return 0  # bool(self) is False

        def __call__(self, url):
            self.calls.append(url)
            return FIXTURE.encode("utf-8")

    falsey_fetcher = FalseyFetcher()
    assert not falsey_fetcher

    series = fetch_ocr_series(fetcher=falsey_fetcher, captured_at_utc=CAPTURED_AT)

    assert falsey_fetcher.calls == [OCR_SOURCE_URL]
    assert series.current.ocr_pct == 2.5


def test_ocr_observation_is_a_plain_immutable_value():
    obs = OcrObservation(period="Aug-2026", ocr_pct=2.5, bank_bill_90d_pct=2.98)

    with pytest.raises(AttributeError):
        obs.ocr_pct = 9.9  # type: ignore[misc]


def test_dashboard_browser_fetcher_routes_through_the_shared_retry_helper(monkeypatch):
    """The dashboard fetcher must honour configured max_retries/backoff, not
    fail on the first transient browser launch/navigation/timeout error --
    the same guarantee ``build_browser_fetcher`` gives the bank collectors."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed; nothing to route through retries")

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
