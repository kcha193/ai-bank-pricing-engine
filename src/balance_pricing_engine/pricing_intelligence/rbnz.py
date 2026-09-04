"""RBNZ Official Cash Rate intelligence.

Answers the same question the rest of this package answers for bank pricing --
"what is happening in the market?" -- for the one funding-cost input this
engine has never verified: ``market.cash_rate``. It never sets that value
itself; see :func:`pricing_intelligence.strategy.apply_cash_rate_strategy_to_config`
for the seam into the optimiser.

Shape difference from the bank collectors, on purpose: RBNZ's own published
chart already carries the full monthly OCR history back to 2008 in a single
page, so unlike bank rates (a fresh number that must be accumulated over many
collections to build history) there is nothing to accumulate here. Each fetch
returns the whole series; there is no local observation store, no change
detection, and no scheduled collection for this module -- a single point-in-
time read is already as complete as the source itself.

Parsing anchor, and why it matters: the RBNZ page renders several charts with
an identical accessible-table shape (``Date | <series> | <series>``) as a
sighted-user-equivalent companion to the visual chart -- one for the OCR and
90-day bank bill rate, others for exchange rates. All of them look alike by
column shape alone. Grabbing the first ``Date``-headed table on the page
silently returns FX-rate data as if it were the OCR: NZD/USD sits in roughly
the same 0-1 numeric range confusable with a plausible OCR, and none of it
would be range-rejected by a max_ocr_pct check. The parser therefore anchors
on the heading text ("Official Cash Rate (OCR)") and takes the next table
after it, never the first table matching the column shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Optional

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.competitor_rates.collectors.base import (
    Fetcher,
    FetchError,
    ParserError,
    _fetch_with_retries,
    build_default_fetcher,
)

logger = logging.getLogger(__name__)

OCR_SOURCE_URL = (
    "https://www.rbnz.govt.nz/statistics/key-statistics/"
    "exchange-rates-and-wholesale-interest-rates"
)

#: Text the parser anchors on. The page's exact wording has moved before
#: ("Official Cash Rate (OCR)" vs "Official Cash Rate") and may again; matching
#: the stable substring rather than the whole heading is deliberate.
_OCR_HEADING_ANCHOR = "Official Cash Rate"

# Widest plausible band across NZ's actual OCR history (0.25% GFC/COVID floor,
# 8.00% pre-GFC peak), with margin. A secondary check only -- the heading
# anchor above is what actually prevents ingesting the wrong series, since
# NZD/USD-shaped decoy values already fall inside almost any numeric band a
# rate could plausibly occupy.
MIN_PLAUSIBLE_OCR_PCT = 0.0
MAX_PLAUSIBLE_OCR_PCT = 10.0

_ROW_PATTERN = re.compile(
    r'<tr class="chart__table-row">(.*?)</tr>', re.DOTALL
)
_CELL_PATTERN = re.compile(r"<t[dh][^>]*>([^<]*)</t[dh]>")

#: Format of ``OcrObservation.period`` as RBNZ labels it, e.g. "Aug-2026".
_PERIOD_FORMAT = "%b-%Y"


def _parse_period(period: str) -> tuple[int, int]:
    """``OcrObservation.period`` (e.g. "Aug-2026") as a ``(year, month)`` pair."""
    parsed = datetime.strptime(period, _PERIOD_FORMAT)
    return parsed.year, parsed.month


def _shift_year_month(year: int, month: int, delta_months: int) -> tuple[int, int]:
    """``(year, month)`` shifted by ``delta_months`` (negative moves earlier)."""
    total_months = year * 12 + (month - 1) + delta_months
    return total_months // 12, total_months % 12 + 1


#: Substrings (case-insensitive) the OCR and 90-day-bank-bill header cells
#: are expected to contain, in that column order -- see the header-label
#: check in :func:`parse_ocr_series`.
_OCR_COLUMN_LABEL_HINT = "ocr"
_BANK_BILL_COLUMN_LABEL_HINT = "bank bill"


def _header_matches_expected_column_order(header_cells: list[str]) -> bool:
    """Whether a 3-cell header row has OCR in column 2 and the 90-day bank
    bill rate in column 3, the order every row below is parsed by position
    against. Data rows carry no column label of their own -- if RBNZ ever
    reordered these two columns while keeping the same heading and 3-cell
    shape, both values still pass the plausibility band, so a swap would
    otherwise silently overlay the bank-bill rate into pricing as if it were
    the OCR."""
    if len(header_cells) != 3:
        return False
    ocr_column, bank_bill_column = header_cells[1].lower(), header_cells[2].lower()
    return _OCR_COLUMN_LABEL_HINT in ocr_column and _BANK_BILL_COLUMN_LABEL_HINT in bank_bill_column


@dataclass(frozen=True)
class OcrObservation:
    """One month's published OCR, with the corroborating 90-day bank bill rate."""

    period: str  # e.g. "Aug-2026", exactly as RBNZ labels it
    ocr_pct: float
    bank_bill_90d_pct: Optional[float]


@dataclass(frozen=True)
class OcrSeries:
    """The published OCR history as of one fetch, newest observation first."""

    observations: tuple[OcrObservation, ...]
    source_url: str
    captured_at_utc: datetime

    @property
    def current(self) -> OcrObservation:
        if not self.observations:
            raise ValueError("OcrSeries has no observations")
        return self.observations[0]

    def change_bps(self, months_back: int = 1) -> Optional[float]:
        """Movement in bps from ``months_back`` calendar months ago to the
        current reading.

        Looks up the observation whose *period* is exactly ``months_back``
        calendar months before ``current.period``, not the one ``months_back``
        list positions back -- if any intermediate month was skipped during
        parsing (an invalid or implausible row), a list-position offset would
        silently compare the wrong months and mislabel the result (e.g. an
        actual 2-month gap reported as "change over 1 month"). Returns None
        when no observation matches that calendar month -- callers should
        treat that as "unknown", not as a zero move.
        """
        if not self.observations:
            return None
        from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps

        target_year, target_month = _shift_year_month(*_parse_period(self.current.period), -months_back)
        for observation in self.observations[1:]:
            if _parse_period(observation.period) == (target_year, target_month):
                return rate_change_bps(observation.ocr_pct, self.current.ocr_pct)
        return None


def build_dashboard_browser_fetcher(
    config: Optional[CollectionConfig] = None, settle_milliseconds: int = 5000
) -> Fetcher:
    """Browser fetcher for pages that never reach network-idle.

    ``collectors.base.build_browser_fetcher`` waits for ``networkidle``, which
    is right for the bank rate pages it was built for but never fires on this
    one: the RBNZ statistics page keeps background network activity going
    (chart telemetry, related-content widgets) indefinitely. This waits only
    for the DOM, then gives the chart widget a fixed settle window to render --
    confirmed against the live page to be enough for the accessible data table
    this parser reads to appear.

    Routes attempts through the shared :func:`_fetch_with_retries` helper, the
    same as ``build_browser_fetcher`` and ``build_default_fetcher``, so a
    transient browser launch/navigation/timeout failure here gets the
    configured ``max_retries``/``retry_backoff_seconds`` resilience too,
    instead of failing on the first attempt.
    """
    config = config or CollectionConfig()

    def fetch(url: str) -> bytes:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise FetchError(
                "JavaScript rendering was requested but playwright is not installed. "
                "Install the optional extra: pip install -e '.[browser]' "
                "&& python3 -m playwright install chromium"
            ) from exc

        def attempt() -> bytes:  # pragma: no cover - exercised only against the live page
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=config.user_agent)
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(config.request_timeout_seconds * 1000),
                )
                page.wait_for_timeout(settle_milliseconds)
                html = page.content()
                browser.close()
            return html.encode("utf-8")

        return _fetch_with_retries(config, url, attempt)

    return fetch


def fetch_ocr_series(
    fetcher: Optional[Fetcher] = None,
    config: Optional[CollectionConfig] = None,
    render_javascript: bool = True,
    captured_at_utc: Optional[datetime] = None,
) -> OcrSeries:
    """Fetch and parse the published OCR series from the RBNZ website.

    ``render_javascript`` defaults to True: the accessible data table this
    parser reads is populated by the page's chart widget at runtime, so the
    static HTML never contains it -- the same JavaScript-rendering requirement
    as every bank collector in this package, though this page needs its own
    fetch strategy (see :func:`build_dashboard_browser_fetcher`). Pass an
    explicit ``fetcher`` in tests to avoid both the network and a browser.
    """
    config = config or CollectionConfig()
    # `fetcher or (...)` would discard a caller-supplied fetcher that happens
    # to be falsey (e.g. a callable test double implementing `__len__` and
    # currently empty) and silently build the live browser fetcher instead --
    # `is None` is the only correct test for "no fetcher was supplied".
    resolved_fetcher = (
        fetcher
        if fetcher is not None
        else (
            build_dashboard_browser_fetcher(config)
            if render_javascript
            else build_default_fetcher(config)
        )
    )
    captured_at_utc = captured_at_utc or datetime.now(timezone.utc)

    html = resolved_fetcher(OCR_SOURCE_URL)
    html_text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    return parse_ocr_series(html_text, captured_at_utc=captured_at_utc)


def parse_ocr_series(html: str, captured_at_utc: datetime) -> OcrSeries:
    """Parse the OCR accessible-data-table out of the page's rendered HTML."""
    heading_index = html.find(_OCR_HEADING_ANCHOR)
    if heading_index == -1:
        raise ParserError(
            f"could not find the {_OCR_HEADING_ANCHOR!r} heading on the RBNZ page; "
            "the page structure may have changed"
        )

    # Anchored on the "<table" tag itself, not the "chart__table" class name:
    # on the live page that class first appears inside the header row's own
    # class="chart__table-row" attribute, which is *after* that row's opening
    # "<tr class=" -- anchoring there truncated the header row's opening tag
    # out of the parse window, so it could never match _ROW_PATTERN at all
    # (the "Date" skip below was consequently dead code against a real page;
    # it only ever fired in hand-written test fixtures that additionally
    # placed literal "chart__table" text before the header to route around
    # this). "<table" always precedes every "<tr>" inside it structurally, so
    # anchoring there keeps the header row intact for the column-order check
    # below to actually run against.
    table_index = html.find("<table", heading_index)
    if table_index == -1:
        raise ParserError(
            "found the OCR heading but no accessible data table followed it"
        )

    # The next chart's table (if any) bounds this search so a later, unrelated
    # heading's table is never accidentally included.
    next_heading_index = html.find("<h2", heading_index + len(_OCR_HEADING_ANCHOR))
    window_end = next_heading_index if next_heading_index != -1 else len(html)
    window = html[table_index:window_end]

    observations: list[OcrObservation] = []
    seen_periods: set[str] = set()
    header_validated = False
    for row_match in _ROW_PATTERN.finditer(window):
        cells = [cell.strip() for cell in _CELL_PATTERN.findall(row_match.group(1))]
        period = cells[0] if cells else ""

        # The header row is the one row exempt from "this must be the current
        # OCR" scrutiny below -- it is identified purely by its literal "Date"
        # label, unconditionally, regardless of position or cell count, so a
        # malformed/duplicated header can never itself be mistaken for a data
        # row (and therefore can never trip the newest-row guard below). But
        # every row after it is parsed by column *position*, not by label, so
        # a header this parser is willing to accept must actually confirm
        # that position -- a header that doesn't (wrong cell count, wrong
        # column order) is fatal, not silently skippable, and so is reaching
        # a data row before any header has validated at all (a redesigned
        # page that dropped the header while also reordering the columns):
        # either way, both OCR and bank-bill values are plausible-looking
        # percentages, so a swap would otherwise be invisible to the
        # plausibility check below.
        if period == "Date":
            if not _header_matches_expected_column_order(cells):
                raise ParserError(
                    "the OCR table's columns are not in the expected order "
                    "(OCR, then 90-day bank bill rate); found header cells "
                    f"{cells!r} -- refusing to parse data rows by position "
                    "against a header that doesn't match"
                )
            header_validated = True
            continue

        if not header_validated:
            raise ParserError(
                "no OCR table header confirming the expected column order "
                "(OCR, then 90-day bank bill rate) was found before the "
                "first data row; refusing to parse data rows by position "
                "without a confirmed header"
            )

        # The series is documented (and relied on by resolve_cash_rate) as
        # newest-first: the first non-header row we reach *is* "the current
        # OCR". An older row failing in any way -- wrong cell count, an empty
        # period, an unparsable/implausible rate -- is a one-off defect the
        # rest of the table survives; the newest row failing means we cannot
        # state the current OCR at all, and must not silently promote an
        # older row into that role instead. This check must run before *any*
        # of the skip/continue branches below, not just the rate-parsing
        # ones -- a malformed newest row (missing/extra cell, blank period)
        # is exactly the case a silent `continue` would otherwise paper over.
        is_newest_row = not observations

        if len(cells) != 3:
            if is_newest_row:
                raise ParserError(
                    "the newest OCR row does not have the expected 3 cells "
                    f"(found {len(cells)}); refusing to fall back to an "
                    "older row as the current OCR"
                )
            continue

        _, ocr_text, bank_bill_text = cells

        if period == "" or period in seen_periods:
            if is_newest_row:
                raise ParserError(
                    "the newest OCR row has an empty or unexpectedly "
                    f"repeated period ({period!r}); refusing to fall back "
                    "to an older row as the current OCR"
                )
            continue

        # Every retained period must parse -- change_bps() calls
        # _parse_period() on every observation, including older ones, so a
        # malformed but nonempty label here (e.g. "Jul 2026" instead of
        # "Jul-2026") would otherwise slip past this filter, get appended,
        # and later blow up change_bps() with a raw ValueError even when the
        # current row and the requested comparison month are both fine.
        try:
            period_year_month = _parse_period(period)
        except ValueError:
            if is_newest_row:
                raise ParserError(
                    "the newest OCR row has a period that doesn't match the "
                    f"expected month-year format ({period!r}); refusing to "
                    "fall back to an older row as the current OCR"
                ) from None
            continue

        # The table is documented -- and OcrSeries.current relies on it -- as
        # newest-first: whatever accepted row lands first becomes "the
        # current OCR". Checked as soon as the period itself is known to be
        # well-formed, before any rate-related skip/continue below -- a later
        # row can carry a perfectly valid, newer period alongside an
        # unparsable or implausible rate (e.g. a partially rendered row), and
        # skipping on the rate alone would silently bypass this check,
        # leaving an earlier (and now provably wrong) "current" in place
        # simply because the row that contradicted it also happened to fail
        # for an unrelated reason.
        if observations and period_year_month >= _parse_period(observations[-1].period):
            raise ParserError(
                f"the OCR table is not strictly newest-first as documented: "
                f"{period} is not older than the preceding accepted row "
                f"({observations[-1].period}) -- refusing to trust "
                f"{observations[0].period} as the current OCR when the "
                "table's own ordering contradicts it"
            )

        try:
            ocr_pct = float(ocr_text)
        except ValueError as exc:
            if is_newest_row:
                raise ParserError(
                    f"the newest OCR row ({period}) has an unparsable rate "
                    f"{ocr_text!r}; refusing to fall back to an older row as "
                    "the current OCR"
                ) from exc
            continue
        if not (MIN_PLAUSIBLE_OCR_PCT <= ocr_pct <= MAX_PLAUSIBLE_OCR_PCT):
            if is_newest_row:
                raise ParserError(
                    f"the newest OCR row ({period}) is {ocr_pct:.3f}%, outside "
                    f"the plausible {MIN_PLAUSIBLE_OCR_PCT:.1f}%-"
                    f"{MAX_PLAUSIBLE_OCR_PCT:.1f}% band; refusing to fall back "
                    "to an older row as the current OCR"
                )
            logger.warning(
                "skipping OCR row for %s: %.3f%% is outside the plausible "
                "%.1f%%-%.1f%% band",
                period,
                ocr_pct,
                MIN_PLAUSIBLE_OCR_PCT,
                MAX_PLAUSIBLE_OCR_PCT,
            )
            continue

        try:
            bank_bill_pct: Optional[float] = float(bank_bill_text)
        except ValueError:
            bank_bill_pct = None
        seen_periods.add(period)
        observations.append(OcrObservation(period, ocr_pct, bank_bill_pct))

    if not observations:
        raise ParserError(
            "the OCR data table was found but no plausible rows were extracted"
        )

    return OcrSeries(
        observations=tuple(observations),
        source_url=OCR_SOURCE_URL,
        captured_at_utc=captured_at_utc,
    )
