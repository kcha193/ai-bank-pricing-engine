"""The browser fetcher's fallback readiness wait, in a real headless Chromium.

Used when the fetcher is called without a collector's product-specific check (the collector
path is covered by ``test_collection_browser_readiness.py``). Skipped where the optional
``[browser]`` extra or its Chromium build is not installed (ordinary CI). No network access:
every page is a ``data:`` URL.
"""

import re
from urllib.parse import quote

import pytest

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.competitor_rates.collectors.base import (
    ReadinessTimeoutError,
    build_browser_fetcher,
)
from balance_pricing_engine.competitor_rates.readiness import any_rate_cell

sync_api = pytest.importorskip("playwright.sync_api")

#: A promotional percentage is in the page text immediately; the rate table arrives later, the
#: way BNZ's client-side XHR fills its table after the load event. The script's own source
#: text contains "4.05%" inside a <td> literal.
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


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


@pytest.fixture(scope="module")
def chromium():
    with sync_api.sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except Exception as exc:  # noqa: BLE001 - Chromium not installed is an environment gap
            pytest.skip(f"Chromium unavailable: {type(exc).__name__}")


def _fetcher(timeout: float):
    return build_browser_fetcher(
        CollectionConfig(
            request_timeout_seconds=10,
            browser_readiness_timeout_seconds=timeout,
            max_retries=0,
            retry_backoff_seconds=0,
        )
    )


def test_readiness_waits_for_the_rate_table_not_a_banner_percentage(chromium):
    html = _fetcher(5)(_data_url(LATE_TABLE)).decode()
    rendered = re.sub(r"<script>.*?</script>", "", html, flags=re.DOTALL)
    assert "<td>4.05%</td>" in rendered


def test_banner_text_alone_never_satisfies_readiness(chromium):
    with pytest.raises(ReadinessTimeoutError):
        _fetcher(1)(_data_url(NO_TABLE))


def test_a_rate_inside_inline_script_source_is_not_a_rendered_cell():
    assert not any_rate_cell(LATE_TABLE)
    assert not any_rate_cell(NO_TABLE)
    assert any_rate_cell("<table><tr><td>1 year</td><td>4.05%</td></tr></table>")
