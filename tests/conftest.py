from datetime import datetime, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import COLLECTOR_CLASSES
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord

PRICING_INTELLIGENCE_FIXTURES = Path(__file__).parent / "fixtures" / "pricing_intelligence"

#: Maps each collector's official URL to its saved fixture page, so collection
#: tests exercise the real fetch/parse path without touching a live bank site.
FIXTURE_PAGES = {
    collector.home_loan_url: f"{collector.bank.lower()}_home_loans.html"
    for collector in COLLECTOR_CLASSES
}
#: Term-deposit pages trimmed (tables and their nearest headings only) from the
#: live pages rendered on 2026-10-30, so the PIE tables, day-based terms and
#: savings-account tables the live pages carry are all exercised offline.
TERM_DEPOSIT_FIXTURE_DIR = "term_deposits_2026-10-30"
FIXTURE_PAGES.update(
    {
        collector.term_deposit_url: (
            f"{TERM_DEPOSIT_FIXTURE_DIR}/{collector.bank.lower()}_term_deposits.html"
        )
        for collector in COLLECTOR_CLASSES
    }
)


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regenerate golden test fixtures.",
    )


@pytest.fixture
def fixture_fetcher():
    """Build a fetcher serving saved bank HTML, with optional faults injected.

    ``failing_banks`` raises for those banks (collection failure isolation), and
    ``mutate(bank, html) -> html`` rewrites a page (rate moves, withdrawals,
    parser-breaking edits).
    """

    def build(failing_banks=(), mutate=None, empty_banks=()):
        failing = set(failing_banks)
        empty = set(empty_banks)
        url_to_bank = {
            url: collector.bank
            for collector in COLLECTOR_CLASSES
            for url in (collector.home_loan_url, collector.term_deposit_url)
        }

        def fetch(url: str) -> bytes:
            bank = url_to_bank[url]
            if bank in failing:
                raise ConnectionError(f"simulated network failure for {bank}")
            if bank in empty:
                return b"<html><body><p>Our rates are being updated.</p></body></html>"
            html = (PRICING_INTELLIGENCE_FIXTURES / FIXTURE_PAGES[url]).read_text(
                encoding="utf-8"
            )
            if mutate is not None:
                html = mutate(bank, html)
            return html.encode("utf-8")

        return fetch

    return build


@pytest.fixture
def make_record():
    """Build a canonical mortgage observation with sensible defaults."""

    def build(
        bank="ANZ",
        term_months=12,
        rate_pct=4.79,
        rate_category="special",
        observed_at=None,
        **overrides,
    ) -> CompetitorRateRecord:
        payload = {
            "bank": bank,
            "product_type": "home_loan",
            "product_name": "Fixed Special",
            "rate_category": rate_category,
            "term_label_raw": f"{term_months} months",
            "term_months_normalised": term_months,
            "advertised_rate_pct": rate_pct,
            "source_url": f"https://www.example-bank.co.nz/{bank.lower()}/rates",
            "captured_at_utc": observed_at or datetime(2026, 8, 20, tzinfo=timezone.utc),
            "raw_text_context": f"{bank} {term_months} months {rate_pct}%",
            "raw_html_sha256": "a" * 64,
        }
        payload.update(overrides)
        return CompetitorRateRecord(**payload)

    return build
