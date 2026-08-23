"""Per-bank public rate collectors.

Adding a sixth bank means adding one module here and registering its class in
``COLLECTOR_CLASSES`` -- no change to validation, storage, change detection, or
market logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.competitor_rates.collectors.anz import ANZCollector
from balance_pricing_engine.competitor_rates.collectors.asb import ASBCollector
from balance_pricing_engine.competitor_rates.collectors.base import (
    BankCollectionResult,
    BankPricingCollector,
    CollectionReport,
    CollectionStatus,
    CollectorError,
    Fetcher,
    FetchError,
    ParserError,
    build_browser_fetcher,
    build_default_fetcher,
    collect_all_mortgage_rates,
)
from balance_pricing_engine.competitor_rates.collectors.bnz import BNZCollector
from balance_pricing_engine.competitor_rates.collectors.kiwibank import KiwibankCollector
from balance_pricing_engine.competitor_rates.collectors.westpac import WestpacCollector

COLLECTOR_CLASSES: tuple[type[BankPricingCollector], ...] = (
    ANZCollector,
    ASBCollector,
    BNZCollector,
    WestpacCollector,
    KiwibankCollector,
)

COLLECTORS_BY_BANK: dict[str, type[BankPricingCollector]] = {
    collector.bank: collector for collector in COLLECTOR_CLASSES
}


def build_collectors(
    banks: Optional[Sequence[str]] = None,
    config: Optional[CollectionConfig] = None,
    fetcher: Optional[Fetcher] = None,
    raw_root: str | Path = "data/raw/competitor_rates",
    render_javascript: bool = False,
) -> list[BankPricingCollector]:
    """Instantiate collectors for the requested banks (all five by default).

    ``render_javascript`` routes fetching through a headless browser, which is
    currently required for live collection: every tracked bank renders its rate
    table client-side.
    """
    config = config or CollectionConfig()
    selected = COLLECTOR_CLASSES
    if banks is not None:
        missing = [bank for bank in banks if bank not in COLLECTORS_BY_BANK]
        if missing:
            raise KeyError(f"no collector registered for: {', '.join(missing)}")
        selected = tuple(COLLECTORS_BY_BANK[bank] for bank in banks)
    return [
        collector_class(
            config=config,
            fetcher=fetcher,
            raw_root=raw_root,
            render_javascript=render_javascript,
        )
        for collector_class in selected
    ]


__all__ = [
    "ANZCollector",
    "ASBCollector",
    "BNZCollector",
    "BankCollectionResult",
    "BankPricingCollector",
    "COLLECTORS_BY_BANK",
    "COLLECTOR_CLASSES",
    "CollectionReport",
    "CollectionStatus",
    "CollectorError",
    "FetchError",
    "Fetcher",
    "KiwibankCollector",
    "ParserError",
    "WestpacCollector",
    "build_browser_fetcher",
    "build_collectors",
    "build_default_fetcher",
    "collect_all_mortgage_rates",
]
