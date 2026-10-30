"""Render Pricing Watch against disposable synthetic fixtures, never the live database.

Run with the dashboard/browser/dev extras installed:
    python tests/visual/dashboard_review.py --output-dir /tmp/pricing-watch-review

Starts the documented Streamlit entry point on loopback, captures all six destinations at
1440/1024/768/390px in light/dark, and exercises product, filter and scenario interactions.
Screenshots and the JSON report are review evidence, not pixel-perfect golden tests.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PAGES = (
    ("command-centre", "", "Command Centre"),
    ("market-explorer", "market-explorer", "Market Explorer"),
    ("rate-movements", "rate-movements", "Rate Movements"),
    ("market-trends", "market-trends", "Market Trends"),
    ("pricing-studio", "pricing-studio", "Pricing Studio"),
    ("operations", "operations", "Operations & Trust"),
)


def load_fixtures(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_databases(directory):
    base = load_fixtures("base_fixtures", ROOT / "tests/conftest.py")
    fixtures = load_fixtures("market_fixtures", ROOT / "tests/unit/conftest.py")
    fixtures.NOW = datetime.now(timezone.utc)
    record = base.make_record.__wrapped__()
    market = fixtures.market_factory.__wrapped__(directory, record)
    dual = fixtures.dual_market_factory.__wrapped__(market, record)
    healthy = dual(name="healthy.db")
    degraded = dual(name="degraded.db", stale_deposit_banks=("Westpac",))
    no_deposits = market(name="no-deposits.db")
    stale = dual(name="stale.db", stale_deposit_banks=fixtures.BANKS)
    corrupt = directory / "corrupt.db"
    corrupt.write_text("deliberately invalid SQLite fixture")
    return {
        "healthy": healthy.database,
        "degraded": degraded.database,
        "no-deposits": no_deposits.database,
        "stale": stale.database,
        "empty": directory / "missing.db",
        "corrupt": corrupt,
    }


@contextmanager
def server(database, directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    # Optional credentials are never needed by this synthetic, offline UI review.
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL"):
        env.pop(key, None)
    env["PRICING_INTELLIGENCE_DB_PATH"] = str(database)
    env["PRICING_WATCH_CONFIG"] = str(ROOT / "configs/base.yaml")
    env["PRICING_WATCH_OPS_TOKEN"] = "synthetic-preview-only-token"
    env["PYTHONPATH"] = str(ROOT / "src")
    with (directory / f"server-{port}.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                "src/balance_pricing_engine/dashboard/app.py",
                "--server.address",
                "127.0.0.1",
                "--server.port",
                str(port),
                "--server.headless",
                "true",
                "--browser.gatherUsageStats",
                "false",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError(f"Preview server exited; inspect {log.name}")
                try:
                    with urlopen(f"{url}/_stcore/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise TimeoutError("Preview server did not become healthy")
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Streamlit may retain worker threads. This is our disposable child only.
                process.kill()
                process.wait(timeout=5)


def ready(page):
    page.wait_for_function("""() =>
      document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')
        === 'notRunning' && document.querySelectorAll('[data-testid="stSkeleton"]').length === 0
    """)
    expect(page.get_by_test_id("stException")).to_have_count(0)


def capture(page, output, name):
    ready(page)
    # Web fonts are bundled by Streamlit; wait for settled glyphs and chart layout.
    page.evaluate("document.fonts.ready")
    page.wait_for_function("""() => document.getAnimations().every(a =>
      a.playState !== 'running' || a.effect.getComputedTiming().iterations === Infinity)
    """)
    page.screenshot(path=str(output / f"{name}.jpeg"), quality=80)
    overflow = page.locator('[data-testid="stMain"]').evaluate(
        "e => ({width: e.clientWidth, content: e.scrollWidth})"
    )
    assert overflow["content"] <= overflow["width"] + 1, (name, overflow)
    targets = page.locator(
        'button[data-testid="stBaseButton-segmented_control"], '
        'button[data-testid="stBaseButton-segmented_controlActive"]'
    ).evaluate_all("els => els.map(e => e.getBoundingClientRect().height).filter(h => h > 0)")
    assert all(height >= 44 for height in targets), (name, targets)
    return {"screenshot": f"{name}.jpeg", "viewport": page.viewport_size, "overflow": overflow}


def matrix_review(browser, url, output):
    results = []
    for scheme in ("light", "dark"):
        for width in (1440, 1024, 768, 390):
            context = browser.new_context(
                viewport={"width": width, "height": 1000}, color_scheme=scheme
            )
            for name, path, title in PAGES:
                # A fresh session verifies deep links and the initial theme on each page.
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{url}/{path}?product=term_deposit")
                expect(page.get_by_role("heading", name=title, exact=False)).to_be_visible()
                results.append(capture(page, output, f"{name}-{width}-{scheme}"))
                assert not errors, (name, errors)
                if name in ("market-explorer", "market-trends", "rate-movements", "operations"):
                    chart = page.get_by_test_id("stVegaLiteChart").first
                    if chart.count():
                        chart.scroll_into_view_if_needed()
                        results.append(capture(page, output, f"{name}-chart-{width}-{scheme}"))
                page.close()
            context.close()
    return results


def interactions(browser, url, output):
    context = browser.new_context(viewport={"width": 1440, "height": 1100}, color_scheme="light")
    page = context.new_page()
    page.goto(url)
    expect(page.get_by_role("heading", name="Command Centre", exact=True)).to_be_visible()
    ready(page)
    page.locator(".st-key-cc_explore_term_deposit button").click()
    expect(page.get_by_role("heading", name="Term Deposit Market Explorer")).to_be_visible()
    ready(page)
    # Lowest loan / highest deposit ranking and term/bank filters.
    expect(page.get_by_test_id("stMetricValue").first).to_have_text("4.25%")
    page.get_by_test_id("stSelectbox").filter(has_text="Term").get_by_role("combobox").click()
    page.get_by_role("option", name="6M", exact=True).click()
    ready(page)
    expect(page.get_by_test_id("stMetricValue").first).to_have_text("4.00%")
    page.get_by_role("link", name="Market Trends", exact=False).first.click()
    expect(page.get_by_role("heading", name="Term Deposit Market Trends")).to_be_visible()
    ready(page)
    expect(page.get_by_test_id("stSelectbox").first).to_contain_text("6M")
    page.get_by_text("Rate history as a table", exact=True).click()
    expect(page.get_by_test_id("stDataFrame").first).to_be_visible()
    capture(page, output, "trends-data-alternative")
    # Return to an already visited page, then change product using the keyboard.
    page.get_by_role("link", name="Market Explorer", exact=False).first.click()
    expect(page.get_by_role("heading", name="Term Deposit Market Explorer")).to_be_visible()
    ready(page)
    expect(page.get_by_test_id("stSelectbox").first).to_contain_text("6M")
    loan = page.locator('.st-key-pw-product-switch-market_watch_product').get_by_role(
        "button", name="HOME LOANS", exact=False
    )
    loan.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    focus = loan.evaluate("e => ({visible: e.matches(':focus-visible'), width: getComputedStyle(e).outlineWidth})")
    assert focus["visible"] and float(focus["width"].replace("px", "")) >= 2, focus
    capture(page, output, "keyboard-product-focus")
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Home Loan Market Explorer")).to_be_visible()
    ready(page)
    expect(page.get_by_test_id("stSelectbox").first).to_contain_text("1Y")
    page.get_by_role("link", name="Market Trends", exact=False).first.click()
    expect(page.get_by_role("heading", name="Home Loan Market Trends")).to_be_visible()
    ready(page)
    page.get_by_test_id("stSelectbox").filter(has_text="Highlight bank").get_by_role("combobox").click()
    page.get_by_role("option", name="ASB", exact=True).click()
    ready(page)
    page.get_by_role("link", name="Market Explorer", exact=False).first.click()
    expect(page.get_by_role("heading", name="Home Loan Market Explorer")).to_be_visible()
    ready(page)
    expect(page.get_by_test_id("stSelectbox").filter(has_text="Highlight bank")).to_contain_text("ASB")
    page.locator('.st-key-pw-product-switch-market_watch_product').get_by_role(
        "button", name="TERM DEPOSITS", exact=False
    ).click()
    ready(page)
    page.get_by_role("link", name="Pricing Studio", exact=False).first.click()
    expect(page.get_by_role("heading", name="Pricing Studio", exact=True)).to_be_visible()
    ready(page)
    page.locator(".st-key-scenario_run_term_deposit button").click()
    expect(page.get_by_text("Human review required", exact=False)).to_be_visible(timeout=30000)
    ready(page)
    expect(
        page.get_by_test_id("stMetricLabel").filter(has_text="Optimal term-deposit rate")
    ).to_be_visible()
    page.get_by_role("heading", name="2 · Results", exact=True).scroll_into_view_if_needed()
    capture(page, output, "pricing-studio-deposit-results")
    page.set_viewport_size({"width": 390, "height": 1000})
    page.get_by_role("heading", name="2 · Results", exact=True).scroll_into_view_if_needed()
    capture(page, output, "pricing-studio-deposit-results-mobile")
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.get_by_role("tab", name="Modelled financial value", exact=True).click()
    page.locator('[data-testid="stVegaLiteChart"]:visible').first.scroll_into_view_if_needed()
    capture(page, output, "pricing-studio-financial-value")
    page.locator(".st-key-pw-product-switch-scenario").get_by_role(
        "button", name="Joint balance sheet", exact=False
    ).click()
    ready(page)
    expect(page.get_by_text("2 · Results", exact=True)).to_have_count(0)
    page.locator(".st-key-scenario_run_joint button").click()
    expect(page.get_by_text("Human review required", exact=False)).to_be_visible(timeout=30000)
    ready(page)
    page.get_by_role("heading", name="2 · Results", exact=True).scroll_into_view_if_needed()
    capture(page, output, "pricing-studio-joint-results")
    page.get_by_role("link", name="Operations & Trust", exact=False).first.click()
    expect(page.get_by_role("heading", name="Operations & Trust", exact=True)).to_be_visible()
    ready(page)
    page.get_by_role("tab", name="Term deposit", exact=True).click()
    page.get_by_text("Term deposit · Fresh banks", exact=True).scroll_into_view_if_needed()
    capture(page, output, "operations-deposit-health")
    page.get_by_role("tab", name="Collection & maintenance", exact=True).click()
    expect(page.get_by_role("heading", name="Operations are locked")).to_be_visible()
    expect(page.get_by_role("button", name="Run Pricing Watch", exact=True)).to_have_count(0)
    capture(page, output, "operations-locked")
    page.set_viewport_size({"width": 390, "height": 1000})
    page.get_by_test_id("stExpandSidebarButton").click()
    expect(page.get_by_test_id("stSidebar").get_by_role("link", name="Market Explorer", exact=False)).to_be_visible()
    page.get_by_test_id("stSidebar").get_by_role("link", name="Market Explorer", exact=False).click()
    expect(page.get_by_role("heading", name="Term Deposit Market Explorer")).to_be_visible()
    ready(page)
    capture(page, output, "mobile-navigation-return")
    page.emulate_media(reduced_motion="reduce")
    transition = page.locator('.st-key-pw-product-switch-market_watch_product').get_by_role(
        "button", name="HOME LOANS", exact=False
    ).evaluate("e => getComputedStyle(e).transitionDuration")
    assert all(float(value.strip().removesuffix("s")) == 0 for value in transition.split(",")), transition
    # The browser never unlocks Operations or clicks a collection/notification control.
    context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="pricing-watch-visual-") as temp,
        sync_playwright() as p,
    ):
        scratch = Path(temp)
        databases = seed_databases(scratch)
        browser = p.chromium.launch()
        try:
            with server(databases["healthy"], scratch) as url:
                results = matrix_review(browser, url, output)
                interactions(browser, url, output)
            for state in ("degraded", "no-deposits", "stale", "empty", "corrupt"):
                with server(databases[state], scratch) as url:
                    page = browser.new_page(viewport={"width": 1024, "height": 1000})
                    page.goto(f"{url}/market-explorer?product=term_deposit")
                    expect(
                        page.get_by_role("heading", name="Term Deposit Market Explorer")
                    ).to_be_visible()
                    results.append(capture(page, output, f"state-{state}"))
                    if state in ("no-deposits", "stale", "empty", "corrupt"):
                        expect(page.get_by_test_id("stMetricValue")).to_have_count(0)
                    page.close()
        finally:
            browser.close()
    (output / "report.json").write_text(
        json.dumps({"renders": results, "interactions": "passed"}, indent=2)
    )
    print(
        f"Passed {len(results)} rendered checks plus product, filter, scenario and safety interactions."
    )


if __name__ == "__main__":
    main()
