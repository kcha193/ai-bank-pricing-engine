"""Browser readiness for live rate collection (DATA-007).

A successful navigation is not proof that a rate table rendered. These tests drive
``build_browser_fetcher`` through an injected fake Playwright (deterministic, fake clock)
and, where a local Chromium is available, through a real browser against local pages.
No test here contacts a public bank URL.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import functools
import http.server
import threading

import pytest

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.competitor_rates.collectors import (
    ANZCollector,
    ASBCollector,
    BNZCollector,
    KiwibankCollector,
    WestpacCollector,
)
from balance_pricing_engine.competitor_rates.collectors.base import (
    CollectionStatus,
    NavigationError,
    PartialRenderError,
    ReadinessTimeoutError,
    build_browser_fetcher,
    collect_all_rates,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.competitor_rates.readiness import ReadinessState

CAPTURED = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
TERMS = ["6 months", "1 year", "18 months", "2 years", "3 years", "4 years", "5 years", "9 months"]


def rate_page(title: str, row_count: int, *, extra: str = "", table_rows: str = "") -> str:
    rows = "".join(
        f"<tr><td>{TERMS[index % len(TERMS)]}</td><td>{4.5 + index / 100:.2f}% p.a.</td></tr>"
        for index in range(row_count)
    )
    return (
        f"<html><head><title>{title}</title></head><body>{extra}"
        f"<table><tr><th>Term</th><th>Interest rate</th></tr>{rows}{table_rows}</table>"
        "</body></html>"
    )


HOME_LOAN_TITLE = "Home loan interest rates | Example"
DEPOSIT_TITLE = "Term deposit rates | Example"
SHELL = "<html><head><title>Home loan interest rates | Example</title></head><body>Loading</body></html>"


def _config(**overrides) -> CollectionConfig:
    values = dict(
        request_timeout_seconds=5,
        retry_backoff_seconds=0,
        max_retries=0,
        browser_readiness_timeout_seconds=10,
        browser_readiness_poll_seconds=0.5,
        readiness_min_rate_rows={"ANZ": {"home_loan": 6, "term_deposit": 6}},
    )
    values.update(overrides)
    return CollectionConfig(**values)


# --------------------------------------------------------------------- fakes


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakePage:
    """A page whose rendered DOM changes over (fake) time: ``timeline`` is
    ``[(seconds_after_load, html), ...]``, the last entry at or before now wins."""

    def __init__(self, clock, timeline, goto_error=None):
        self.clock = clock
        self.timeline = timeline
        self.goto_error = goto_error
        self.loaded_at = None

    def goto(self, url, wait_until, timeout):
        assert wait_until == "load"
        if self.goto_error is not None:
            raise self.goto_error
        self.loaded_at = self.clock()

    def content(self) -> str:
        elapsed = self.clock() - self.loaded_at
        html = self.timeline[0][1]
        for at, candidate in self.timeline:
            if at <= elapsed:
                html = candidate
        return html

    def wait_for_timeout(self, milliseconds):
        self.clock.now += milliseconds / 1000


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self, user_agent):
        return self.page

    def close(self):
        self.closed = True


class FakePlaywright:
    """Hands out one scripted page per launch, so retries see successive attempts."""

    def __init__(self, clock, attempts):
        self.clock = clock
        self.attempts = list(attempts)
        self.browsers: list[FakeBrowser] = []
        self.chromium = self

    def launch(self, headless):
        assert headless is True
        timeline, goto_error = self.attempts.pop(0)
        browser = FakeBrowser(FakePage(self.clock, timeline, goto_error))
        self.browsers.append(browser)
        return browser


def fake_fetcher(config, attempts):
    clock = FakeClock()
    playwright = FakePlaywright(clock, attempts)
    fetcher = build_browser_fetcher(
        config,
        playwright_factory=lambda: contextlib.nullcontext(playwright),
        clock=clock,
    )
    return fetcher, playwright, clock


def collect_home_loans(config, attempts):
    fetcher, playwright, clock = fake_fetcher(config, attempts)
    collector = ANZCollector(config=config, fetcher=fetcher)
    result = collector.collect_mortgage_rates(captured_at_utc=CAPTURED, persist_snapshot=False)
    return result, playwright, clock


# ------------------------------------------------------------ page assessment


def test_t01_an_advertised_rate_table_is_ready():
    collector = ANZCollector(config=_config(), fetcher=lambda url: b"")
    assessment = collector.assess_page_readiness(
        rate_page(HOME_LOAN_TITLE, 8), ProductType.HOME_LOAN
    )
    assert assessment.state is ReadinessState.READY
    assert assessment.rate_rows == 8
    assert assessment.required_rows == 6


def test_t02_an_unrelated_percentage_table_is_not_ready():
    fee_table = (
        "<p>Earn 4.85% p.a. on a special</p>"
        "<table><tr><td>Establishment fee</td><td>0.5%</td></tr>"
        "<tr><td>Low equity margin</td><td>1.25%</td></tr></table>"
    )
    html = f"<html><head><title>{HOME_LOAN_TITLE}</title></head><body>{fee_table}</body></html>"
    collector = ANZCollector(config=_config(), fetcher=lambda url: b"")
    assessment = collector.assess_page_readiness(html, ProductType.HOME_LOAN)
    assert assessment.state is ReadinessState.NOT_READY
    assert assessment.rate_rows == 0


def test_t03_an_incomplete_table_is_partial_not_ready():
    collector = ANZCollector(config=_config(), fetcher=lambda url: b"")
    assessment = collector.assess_page_readiness(
        rate_page(HOME_LOAN_TITLE, 2), ProductType.HOME_LOAN
    )
    assert assessment.state is ReadinessState.PARTIAL
    assert assessment.rate_rows == 2


def test_home_loan_readiness_rejects_a_term_deposit_page():
    collector = ANZCollector(config=_config(), fetcher=lambda url: b"")
    assessment = collector.assess_page_readiness(
        rate_page(DEPOSIT_TITLE, 8), ProductType.HOME_LOAN
    )
    assert assessment.state is ReadinessState.WRONG_PRODUCT


def test_term_deposit_readiness_rejects_a_home_loan_page():
    collector = ANZCollector(config=_config(), fetcher=lambda url: b"")
    assessment = collector.assess_page_readiness(
        rate_page(HOME_LOAN_TITLE, 8), ProductType.TERM_DEPOSIT
    )
    assert assessment.state is ReadinessState.WRONG_PRODUCT


def test_fund_and_pie_return_rows_do_not_count_towards_deposit_readiness():
    fund_rows = "".join(
        f"<tr><td>{term}</td><td>12.47%</td><td>Growth Fund</td></tr>" for term in TERMS
    )
    html = rate_page(DEPOSIT_TITLE, 0, table_rows=fund_rows)
    collector = ANZCollector(config=_config(), fetcher=lambda url: b"")
    assessment = collector.assess_page_readiness(html, ProductType.TERM_DEPOSIT)
    assert assessment.rate_rows == 0
    assert assessment.state is ReadinessState.NOT_READY


def test_readiness_thresholds_are_per_bank_and_product():
    """Defaults are evidence-based per bank (see each collector), not one shared layout."""
    defaults = CollectionConfig()
    thresholds = {
        cls.bank: (
            cls(config=defaults, fetcher=lambda url: b"").required_rate_rows(ProductType.HOME_LOAN),
            cls(config=defaults, fetcher=lambda url: b"").required_rate_rows(
                ProductType.TERM_DEPOSIT
            ),
        )
        for cls in (ANZCollector, ASBCollector, BNZCollector, KiwibankCollector, WestpacCollector)
    }
    assert all(rows >= 3 for pair in thresholds.values() for rows in pair)
    assert len(set(thresholds.values())) > 1


def test_configured_threshold_overrides_the_collector_default():
    config = CollectionConfig(readiness_min_rate_rows={"ASB": {"term_deposit": 11}})
    collector = ASBCollector(config=config, fetcher=lambda url: b"")
    assert collector.required_rate_rows(ProductType.TERM_DEPOSIT) == 11
    assert collector.required_rate_rows(ProductType.HOME_LOAN) == ASBCollector(
        config=CollectionConfig(), fetcher=lambda url: b""
    ).required_rate_rows(ProductType.HOME_LOAN)


def test_threshold_config_rejects_unknown_products_and_non_positive_rows():
    with pytest.raises(ValueError):
        CollectionConfig(readiness_min_rate_rows={"ASB": {"savings": 3}})
    with pytest.raises(ValueError):
        CollectionConfig(readiness_min_rate_rows={"ASB": {"home_loan": 0}})


# --------------------------------------------------------- browser fetch path


def test_immediately_rendered_table_succeeds_without_waiting():
    result, playwright, clock = collect_home_loans(
        _config(), [([(0, rate_page(HOME_LOAN_TITLE, 8))], None)]
    )
    assert result.status is CollectionStatus.SUCCESS
    assert result.rate_count == 8
    assert clock.now == 0
    assert playwright.browsers[0].closed


def test_t04_delayed_javascript_table_is_accepted_within_the_deadline():
    result, _, clock = collect_home_loans(
        _config(), [([(0, SHELL), (3.0, rate_page(HOME_LOAN_TITLE, 8))], None)]
    )
    assert result.status is CollectionStatus.SUCCESS
    assert 3.0 <= clock.now < 10


def test_xhr_style_table_population_waits_for_the_full_table():
    """Rows arriving in batches: a partial table must not be accepted early."""
    timeline = [
        (0, SHELL),
        (1.0, rate_page(HOME_LOAN_TITLE, 2)),
        (2.0, rate_page(HOME_LOAN_TITLE, 4)),
        (4.0, rate_page(HOME_LOAN_TITLE, 8)),
    ]
    result, _, clock = collect_home_loans(_config(), [(timeline, None)])
    assert result.status is CollectionStatus.SUCCESS
    assert result.rate_count == 8
    assert clock.now >= 4.0


def test_an_unrelated_percentage_appearing_first_does_not_end_the_wait():
    marketing = rate_page(HOME_LOAN_TITLE, 0, extra="<p>Earn 4.85% p.a.</p>").replace(
        "</table>", "<tr><td>Establishment fee</td><td>0.5%</td></tr></table>"
    )
    timeline = [(0, marketing), (5.0, rate_page(HOME_LOAN_TITLE, 8))]
    result, _, clock = collect_home_loans(_config(), [(timeline, None)])
    assert result.status is CollectionStatus.SUCCESS
    assert clock.now >= 5.0


def test_t05_a_table_that_never_renders_is_a_classified_readiness_timeout():
    result, playwright, clock = collect_home_loans(_config(), [([(0, SHELL)], None)])
    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "ReadinessTimeoutError"
    assert result.records == ()
    assert 10 <= clock.now < 11
    assert playwright.browsers[0].closed


def test_an_incomplete_table_at_the_deadline_is_a_partial_render_failure():
    result, _, _ = collect_home_loans(
        _config(), [([(0, SHELL), (1.0, rate_page(HOME_LOAN_TITLE, 3))], None)]
    )
    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "PartialRenderError"
    assert "3" in result.error and "6" in result.error


def test_a_term_deposit_table_never_satisfies_home_loan_readiness():
    result, _, _ = collect_home_loans(_config(), [([(0, rate_page(DEPOSIT_TITLE, 8))], None)])
    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "ReadinessTimeoutError"
    assert "product" in result.error


def test_a_navigation_timeout_is_classified_separately():
    result, playwright, _ = collect_home_loans(
        _config(), [([(0, SHELL)], TimeoutError("Timeout 5000ms exceeded navigating"))]
    )
    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "NavigationError"
    assert playwright.browsers[0].closed


def test_t06_a_transient_readiness_timeout_retries_successfully():
    attempts = [([(0, SHELL)], None), ([(0, rate_page(HOME_LOAN_TITLE, 8))], None)]
    result, playwright, _ = collect_home_loans(_config(max_retries=1), attempts)
    assert result.status is CollectionStatus.SUCCESS
    assert len(playwright.browsers) == 2
    assert all(browser.closed for browser in playwright.browsers)


def test_t07_persistent_readiness_failure_stays_classified_after_retries():
    attempts = [([(0, SHELL)], None)] * 3
    result, playwright, _ = collect_home_loans(_config(max_retries=2), attempts)
    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "ReadinessTimeoutError"
    assert "3 attempt" in result.error
    assert len(playwright.browsers) == 3


def test_retry_exhaustion_preserves_the_navigation_classification():
    attempts = [([(0, SHELL)], ConnectionError("net::ERR_NAME_NOT_RESOLVED"))] * 2
    result, _, _ = collect_home_loans(_config(max_retries=1), attempts)
    assert result.error_type == "NavigationError"


def test_t08_one_banks_readiness_failure_does_not_block_the_others():
    config = _config(
        readiness_min_rate_rows={
            "ANZ": {"home_loan": 6},
            "ASB": {"home_loan": 6},
            "BNZ": {"home_loan": 6},
        }
    )
    stuck, _, _ = fake_fetcher(config, [([(0, SHELL)], None)])
    good_a, _, _ = fake_fetcher(config, [([(0, rate_page(HOME_LOAN_TITLE, 8))], None)])
    good_b, _, _ = fake_fetcher(config, [([(0, rate_page(HOME_LOAN_TITLE, 8))], None)])
    collectors = [
        ANZCollector(config=config, fetcher=good_a),
        ASBCollector(config=config, fetcher=stuck),
        BNZCollector(config=config, fetcher=good_b),
    ]
    report = collect_all_rates(
        collectors, product_type=ProductType.HOME_LOAN, captured_at_utc=CAPTURED,
        persist_snapshot=False,
    )
    status = {result.bank: (result.status, result.error_type) for result in report.results}
    assert status["ANZ"][0] is CollectionStatus.SUCCESS
    assert status["BNZ"][0] is CollectionStatus.SUCCESS
    assert status["ASB"] == (CollectionStatus.FAILED, "ReadinessTimeoutError")


def test_an_injected_plain_fetcher_still_works_without_readiness():
    """Custom ``Fetcher`` callables (every fixture test) keep their one-argument contract."""
    collector = ANZCollector(
        config=_config(), fetcher=lambda url: rate_page(HOME_LOAN_TITLE, 2).encode()
    )
    result = collector.collect_mortgage_rates(captured_at_utc=CAPTURED, persist_snapshot=False)
    assert result.status is CollectionStatus.SUCCESS
    assert result.rate_count == 2


def test_readiness_errors_are_fetch_errors():
    assert issubclass(ReadinessTimeoutError, Exception)
    from balance_pricing_engine.competitor_rates.collectors.base import FetchError

    for error in (NavigationError, ReadinessTimeoutError, PartialRenderError):
        assert issubclass(error, FetchError)


def test_readiness_timeout_defaults_to_the_request_timeout():
    assert CollectionConfig(request_timeout_seconds=12).effective_readiness_timeout_seconds == 12
    assert (
        CollectionConfig(
            request_timeout_seconds=12, browser_readiness_timeout_seconds=40
        ).effective_readiness_timeout_seconds
        == 40
    )


# --------------------------------------------- real Chromium against local pages


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
    except Exception:  # noqa: BLE001 - any launch failure means "unverifiable here"
        return False
    return True


requires_chromium = pytest.mark.skipif(
    not _chromium_available(),
    reason="playwright/Chromium not installed: browser integration UNVERIFIED in this run",
)

DELAYED_PAGE = """<html><head><title>Home loan interest rates | Local</title></head><body>
<p>Earn 4.85%% p.a.</p><table id="rates"><tr><th>Term</th><th>Rate</th></tr></table>
<script>
setTimeout(function () {
  var terms = %s;
  var table = document.getElementById('rates');
  terms.forEach(function (term, i) {
    var row = table.insertRow(-1);
    row.insertCell(0).textContent = term;
    row.insertCell(1).textContent = (4.5 + i / 100).toFixed(2) + '%% p.a.';
  });
}, %d);
</script></body></html>"""


@pytest.fixture
def local_site(tmp_path):
    import json

    (tmp_path / "delayed.html").write_text(DELAYED_PAGE % (json.dumps(TERMS), 400))
    (tmp_path / "never.html").write_text(DELAYED_PAGE % (json.dumps([]), 0))
    handler = functools.partial(
        type(
            "QuietHandler",
            (http.server.SimpleHTTPRequestHandler,),
            {"log_message": lambda self, *args: None},
        ),
        directory=str(tmp_path),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@requires_chromium
def test_real_chromium_waits_for_a_delayed_local_rate_table(local_site):
    config = _config(browser_readiness_timeout_seconds=15, request_timeout_seconds=15)

    class LocalANZ(ANZCollector):
        home_loan_url = f"{local_site}/delayed.html"

    collector = LocalANZ(config=config, render_javascript=True)
    result = collector.collect_mortgage_rates(captured_at_utc=CAPTURED, persist_snapshot=False)
    assert result.status is CollectionStatus.SUCCESS, result.error
    assert result.rate_count == len(TERMS)


@requires_chromium
def test_real_chromium_classifies_a_table_that_never_renders(local_site):
    config = _config(browser_readiness_timeout_seconds=2, request_timeout_seconds=10)

    class LocalANZ(ANZCollector):
        home_loan_url = f"{local_site}/never.html"

    collector = LocalANZ(config=config, render_javascript=True)
    result = collector.collect_mortgage_rates(captured_at_utc=CAPTURED, persist_snapshot=False)
    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "ReadinessTimeoutError"
