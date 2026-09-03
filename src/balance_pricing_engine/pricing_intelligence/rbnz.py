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
        """Movement in bps from ``months_back`` months ago to the current reading.

        None when the series is too short to answer -- callers should treat
        that as "unknown", not as a zero move.
        """
        if len(self.observations) <= months_back:
            return None
        from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps

        previous = self.observations[months_back]
        return rate_change_bps(previous.ocr_pct, self.current.ocr_pct)


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

        try:  # pragma: no cover - exercised only against the live page
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
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised into FetchError
            raise FetchError(f"browser rendering failed for {url}: {exc}") from exc

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
    resolved_fetcher = fetcher or (
        build_dashboard_browser_fetcher(config)
        if render_javascript
        else build_default_fetcher(config)
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

    table_index = html.find("chart__table", heading_index)
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
    for row_match in _ROW_PATTERN.finditer(window):
        cells = _CELL_PATTERN.findall(row_match.group(1))
        if len(cells) != 3:
            continue
        period, ocr_text, bank_bill_text = (cell.strip() for cell in cells)
        if period in ("Date", "") or period in seen_periods:
            continue
        try:
            ocr_pct = float(ocr_text)
        except ValueError:
            continue
        if not (MIN_PLAUSIBLE_OCR_PCT <= ocr_pct <= MAX_PLAUSIBLE_OCR_PCT):
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
