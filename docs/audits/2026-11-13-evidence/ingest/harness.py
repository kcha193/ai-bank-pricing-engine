"""Shared offline harness: fixture-backed fetcher, temp SQLite store, no network."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile

from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK, build_collectors
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

REPO = __import__('pathlib').Path(__file__).resolve().parents[4]
FX = REPO / "tests/fixtures/pricing_intelligence"
T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
BANKS = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]

HL_FIXTURE = {b: FX / f"{b.lower()}_home_loans.html" for b in BANKS}
TD_FIXTURE = {b: FX / "term_deposits_2026-10-30" / f"{b.lower()}_term_deposits.html" for b in BANKS}


def url_map(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """URL -> html. overrides: {"ANZ:term_deposit": html or None(=raise)}"""
    m = {}
    for b in BANKS:
        c = COLLECTORS_BY_BANK[b]
        m[c.home_loan_url] = HL_FIXTURE[b].read_text()
        m[c.term_deposit_url] = TD_FIXTURE[b].read_text()
    for key, html in (overrides or {}).items():
        b, prod = key.split(":")
        c = COLLECTORS_BY_BANK[b]
        url = c.home_loan_url if prod == "home_loan" else c.term_deposit_url
        m[url] = html
    return m


def make_fetcher(mapping):
    calls = []
    def fetch(url):
        calls.append(url)
        html = mapping.get(url)
        if html is None:
            raise ConnectionError(f"simulated outage for {url}")
        return html.encode("utf-8")
    fetch.calls = calls
    return fetch


def make_service(db_path: str | None = None, mapping=None, raw_root=None, **cfg):
    db_path = db_path or str(Path(tempfile.mkdtemp(prefix="audit-ingest-")) / "obs.db")
    raw_root = raw_root or str(Path(tempfile.mkdtemp(prefix="audit-raw-")))
    config = PricingIntelligenceConfig(database_path=db_path, **cfg)
    config = config.model_copy(update={"collection": config.collection.model_copy(
        update={"inter_request_delay_seconds": 0.0, "max_retries": 0})})
    fetcher = make_fetcher(mapping if mapping is not None else url_map())
    collectors = build_collectors(banks=config.selected_banks, config=config.collection,
                                  fetcher=fetcher, raw_root=raw_root)
    svc = PricingIntelligenceService(config=config, store=ObservationStore(db_path),
                                     collectors=collectors, raw_root=raw_root)
    return svc, db_path
