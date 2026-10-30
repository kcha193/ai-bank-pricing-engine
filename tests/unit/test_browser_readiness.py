"""The browser fetcher's readiness wait, run in a real headless Chromium against local markup.

Skipped where the optional ``[browser]`` extra or its Chromium build is not installed (ordinary
CI). No network access: every page is ``set_content``.
"""

import pytest

from balance_pricing_engine.competitor_rates.collectors.base import RATE_TABLE_READY_JS

sync_api = pytest.importorskip("playwright.sync_api")

#: A promotional percentage is in the page text immediately; the rate table arrives later, the
#: way BNZ's client-side XHR fills its table after the load event.
LATE_TABLE = """
<html><body>
  <div class="banner">Earn up to 4.85% p.a. on a term deposit</div>
  <div id="rates"></div>
  <script>
    setTimeout(() => {
      document.getElementById('rates').innerHTML =
        '<table><tr><th>Term</th><th>Rate</th></tr><tr><td>1 year</td><td>4.05%</td></tr></table>';
    }, 400);
  </script>
</body></html>
"""

#: The page loaded but its rate table never renders (a blocked or failed XHR).
NO_TABLE = '<html><body><div class="banner">Earn up to 4.85% p.a.</div></body></html>'


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - Chromium not installed is an environment gap
            pytest.skip(f"Chromium unavailable: {type(exc).__name__}")
        yield browser.new_page()
        browser.close()


def test_readiness_waits_for_the_rate_table_not_a_banner_percentage(page):
    page.set_content(LATE_TABLE)
    page.wait_for_function(RATE_TABLE_READY_JS, timeout=5000)
    # Not page.content(): the inline script's own source text contains "4.05%".
    assert page.evaluate("() => document.querySelector('td:last-child').textContent") == "4.05%"


def test_the_previous_body_text_predicate_resolved_before_the_table(page):
    """The defect being fixed: any percentage anywhere in the page text satisfied readiness."""
    page.set_content(LATE_TABLE)
    page.wait_for_function("() => /\\d+(?:\\.\\d+)?\\s*%/.test(document.body.innerText)")
    assert page.evaluate("() => document.querySelector('table')") is None


def test_banner_text_alone_never_satisfies_readiness(page):
    page.set_content(NO_TABLE)
    with pytest.raises(sync_api.TimeoutError):
        page.wait_for_function(RATE_TABLE_READY_JS, timeout=800)
