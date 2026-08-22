"""Common abstraction for per-bank public rate collection.

Each NZ bank gets its own collector module. Everything generic -- fetching,
retry/backoff, snapshotting, term normalisation, eligibility parsing, record
construction -- lives here, so a bank changing its website requires editing only
that bank's collector, never the shared market logic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import logging
from pathlib import Path
import time
from typing import Callable, ClassVar, Optional, Sequence
from urllib.request import Request, urlopen

from balance_pricing_engine.competitor_rates.extractors import parse_html_rates
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.competitor_rates.normalise import (
    detect_new_business_only,
    extract_cashback,
    extract_package_requirement,
    normalise_lvr_values,
)
from balance_pricing_engine.pricing_intelligence.config import CollectionConfig

logger = logging.getLogger(__name__)

# A fetcher takes a URL and returns the raw response body. Injecting one keeps
# every test off the live bank websites.
Fetcher = Callable[[str], bytes]


class CollectorError(RuntimeError):
    """A collector could not produce records from its source page."""


class ParserError(CollectorError):
    """The page was fetched but its structure was not recognised."""


class FetchError(CollectorError):
    """The page could not be retrieved."""


class CollectionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BankCollectionResult:
    """Structured outcome for one bank/product collection attempt."""

    bank: str
    product_type: ProductType
    status: CollectionStatus
    source_url: str
    records: tuple[CompetitorRateRecord, ...] = ()
    error: Optional[str] = None
    error_type: Optional[str] = None
    snapshot_path: Optional[str] = None
    content_sha256: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status is CollectionStatus.SUCCESS

    @property
    def rate_count(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class CollectionReport:
    """Aggregate result of a collection run across banks.

    Returned as data rather than printed, so callers (CLI, dashboard, scheduler)
    decide how to present or persist it.
    """

    results: tuple[BankCollectionResult, ...]
    captured_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def records(self) -> list[CompetitorRateRecord]:
        """Every record collected across all successful banks."""
        return [record for result in self.results for record in result.records]

    @property
    def succeeded(self) -> tuple[BankCollectionResult, ...]:
        return tuple(result for result in self.results if result.succeeded)

    @property
    def failed(self) -> tuple[BankCollectionResult, ...]:
        return tuple(result for result in self.results if not result.succeeded)

    @property
    def any_success(self) -> bool:
        return bool(self.succeeded)

    def render(self) -> str:
        """Render the fixed-width status table used by the CLI."""
        if not self.results:
            return "No collection attempts."
        width = max(len(result.bank) for result in self.results) + 2
        lines = []
        for result in self.results:
            detail = (
                f"{result.rate_count} rates"
                if result.succeeded
                else (result.error_type or "Error")
            )
            lines.append(f"{result.bank:<{width}}{result.status.value:<9}{detail}")
        return "\n".join(lines)


def build_browser_fetcher(config: CollectionConfig) -> Fetcher:
    """Return a fetcher that renders the page in a headless browser first.

    Every tracked bank now builds its rate table client-side, so the static HTML
    contains the page furniture but not the rates. This path is opt-in and needs
    the optional ``playwright`` dependency; without it the caller gets a clear
    :class:`FetchError` rather than a silent empty parse.
    """

    def fetch(url: str) -> bytes:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise FetchError(
                "JavaScript rendering was requested but playwright is not installed. "
                "Install the optional extra: pip install -e '.[browser]' "
                "&& python3 -m playwright install chromium"
            ) from exc

        try:  # pragma: no cover - exercised only against live pages
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=config.user_agent)
                page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=int(config.request_timeout_seconds * 1000),
                )
                html = page.content()
                browser.close()
            return html.encode("utf-8")
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised into FetchError
            raise FetchError(f"browser rendering failed for {url}: {exc}") from exc

    return fetch


def build_default_fetcher(config: CollectionConfig) -> Fetcher:
    """Return a fetcher applying the configured timeout, retries, and backoff."""

    def fetch(url: str) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(config.max_retries + 1):
            try:
                request = Request(url, headers={"User-Agent": config.user_agent})
                with urlopen(request, timeout=config.request_timeout_seconds) as response:
                    return response.read()
            except Exception as exc:  # noqa: BLE001 - re-raised as FetchError below
                last_error = exc
                if attempt < config.max_retries:
                    backoff = config.retry_backoff_seconds * (2**attempt)
                    logger.warning(
                        "fetch failed (attempt %s/%s) for %s: %s; retrying in %.1fs",
                        attempt + 1,
                        config.max_retries + 1,
                        url,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
        raise FetchError(f"could not fetch {url}: {last_error}") from last_error

    return fetch


class BankPricingCollector(abc.ABC):
    """Base class for a single bank's public rate collector.

    Subclasses declare their official source URLs and override only the parsing
    behaviour that differs from the shared table-driven default.
    """

    bank: ClassVar[str]
    home_loan_url: ClassVar[str]
    term_deposit_url: ClassVar[Optional[str]] = None
    #: Set False until a collector's output has been checked against the live page.
    live_verified: ClassVar[bool] = False

    #: Lowercase substrings in a row's text that mark a discounted/special line.
    #: Banks brand these differently (ANZ "Special", BNZ "Classic"), which is
    #: exactly the kind of knowledge that belongs in the bank's own collector.
    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    #: Substrings marking a carded/standard line.
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard",)
    #: Product markers that imply the bank's 20% minimum-equity condition even
    #: when the row itself does not restate it.
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        config: Optional[CollectionConfig] = None,
        fetcher: Optional[Fetcher] = None,
        raw_root: str | Path = "data/raw/competitor_rates",
        render_javascript: bool = False,
    ) -> None:
        self.config = config or CollectionConfig()
        self.render_javascript = render_javascript
        if fetcher is not None:
            self._fetcher = fetcher
        elif render_javascript:
            self._fetcher = build_browser_fetcher(self.config)
        else:
            self._fetcher = build_default_fetcher(self.config)
        self.raw_root = Path(raw_root)

    # ------------------------------------------------------------------ public

    def collect_mortgage_rates(
        self,
        captured_at_utc: Optional[datetime] = None,
        persist_snapshot: bool = True,
    ) -> BankCollectionResult:
        """Fetch and parse this bank's advertised home-loan rates.

        Never raises: a fetch or parse failure is returned as a FAILED result so
        one bank cannot abort a multi-bank run.
        """
        return self._collect(
            product_type=ProductType.HOME_LOAN,
            url=self.home_loan_url,
            parser=self.parse_mortgage_html,
            captured_at_utc=captured_at_utc,
            persist_snapshot=persist_snapshot,
        )

    def collect_term_deposit_rates(
        self,
        captured_at_utc: Optional[datetime] = None,
        persist_snapshot: bool = True,
    ) -> BankCollectionResult:
        """Fetch and parse this bank's advertised term-deposit rates.

        Phase 2 of the roadmap: the domain model and plumbing are in place, and
        the shared parser handles the common table shape, but these collectors
        are not yet tuned per bank the way the mortgage path is.
        """
        if self.term_deposit_url is None:
            return BankCollectionResult(
                bank=self.bank,
                product_type=ProductType.TERM_DEPOSIT,
                status=CollectionStatus.FAILED,
                source_url="",
                error="no term deposit source configured for this bank",
                error_type="NotConfigured",
            )
        return self._collect(
            product_type=ProductType.TERM_DEPOSIT,
            url=self.term_deposit_url,
            parser=self.parse_term_deposit_html,
            captured_at_utc=captured_at_utc,
            persist_snapshot=persist_snapshot,
        )

    # ------------------------------------------------------------- overridable

    def parse_mortgage_html(
        self, html: str, captured_at_utc: datetime
    ) -> list[CompetitorRateRecord]:
        """Parse advertised mortgage rates from this bank's page HTML.

        The default is the shared deterministic table parser plus eligibility
        enrichment. Override in a bank collector only when that bank's markup
        needs different handling.
        """
        records = parse_html_rates(
            bank=self.bank,
            product_type=ProductType.HOME_LOAN,
            source_url=self.home_loan_url,
            html=html,
            captured_at_utc=captured_at_utc,
        )
        return [self.enrich_mortgage_record(record) for record in records]

    def parse_term_deposit_html(
        self, html: str, captured_at_utc: datetime
    ) -> list[CompetitorRateRecord]:
        """Parse advertised term-deposit rates from this bank's page HTML."""
        return parse_html_rates(
            bank=self.bank,
            product_type=ProductType.TERM_DEPOSIT,
            source_url=self.term_deposit_url or "",
            html=html,
            captured_at_utc=captured_at_utc,
        )

    def enrich_mortgage_record(self, record: CompetitorRateRecord) -> CompetitorRateRecord:
        """Derive optional eligibility/promotion fields from the row's own text.

        Enrichment reads only ``raw_text_context`` -- the evidence retained on
        the record itself -- so every derived field can be audited back to the
        exact text it came from.
        """
        context = record.raw_text_context
        lowered = context.lower()

        category = record.rate_category
        if any(marker in lowered for marker in self.special_product_markers):
            category = "special"
        elif any(marker in lowered for marker in self.standard_product_markers):
            category = "standard"

        max_lvr, min_equity = normalise_lvr_values(context)
        implies_min_equity = any(
            marker in lowered for marker in self.min_equity_product_markers
        )
        if max_lvr is None and (
            record.lvr_condition == "min_20_percent_equity" or implies_min_equity
        ):
            max_lvr, min_equity = 0.80, 0.20

        lvr_condition = record.lvr_condition
        if implies_min_equity and lvr_condition in (None, "standard"):
            lvr_condition = "min_20_percent_equity"

        return record.model_copy(
            update={
                "rate_category": category,
                "lvr_condition": lvr_condition,
                "max_lvr": max_lvr,
                "min_equity": min_equity,
                "new_business_only": detect_new_business_only(context),
                "cashback_text": extract_cashback(context),
                "package_requirement": extract_package_requirement(context),
            }
        )

    # ----------------------------------------------------------------- private

    def _collect(
        self,
        product_type: ProductType,
        url: str,
        parser: Callable[[str, datetime], list[CompetitorRateRecord]],
        captured_at_utc: Optional[datetime],
        persist_snapshot: bool,
    ) -> BankCollectionResult:
        captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
        try:
            body = self._fetcher(url)
        except Exception as exc:  # noqa: BLE001 - isolated into a FAILED result
            logger.warning("%s %s fetch failed: %s", self.bank, product_type.value, exc)
            return BankCollectionResult(
                bank=self.bank,
                product_type=product_type,
                status=CollectionStatus.FAILED,
                source_url=url,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        html = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        snapshot_path = (
            self._write_snapshot(html, product_type, captured_at_utc)
            if persist_snapshot
            else None
        )

        try:
            records = parser(html, captured_at_utc)
        except Exception as exc:  # noqa: BLE001 - isolated into a FAILED result
            logger.warning("%s %s parse failed: %s", self.bank, product_type.value, exc)
            return BankCollectionResult(
                bank=self.bank,
                product_type=product_type,
                status=CollectionStatus.FAILED,
                source_url=url,
                error=str(exc),
                error_type=type(exc).__name__,
                snapshot_path=snapshot_path,
                content_sha256=content_hash,
            )

        if not records:
            return BankCollectionResult(
                bank=self.bank,
                product_type=product_type,
                status=CollectionStatus.FAILED,
                source_url=url,
                error="page fetched but no rates were confidently extracted",
                error_type="ParserError",
                snapshot_path=snapshot_path,
                content_sha256=content_hash,
            )

        return BankCollectionResult(
            bank=self.bank,
            product_type=product_type,
            status=CollectionStatus.SUCCESS,
            source_url=url,
            records=tuple(records),
            snapshot_path=snapshot_path,
            content_sha256=content_hash,
        )

    def _write_snapshot(
        self, html: str, product_type: ProductType, captured_at_utc: datetime
    ) -> str:
        directory = self.raw_root / captured_at_utc.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.bank.lower()}_{product_type.value}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)


def collect_all_mortgage_rates(
    collectors: Sequence[BankPricingCollector],
    captured_at_utc: Optional[datetime] = None,
    persist_snapshot: bool = True,
    inter_request_delay_seconds: Optional[float] = None,
) -> CollectionReport:
    """Collect mortgage rates from every collector, isolating per-bank failures.

    A collector raising or returning nothing produces a FAILED entry in the
    report; the remaining banks still run.
    """
    captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
    results: list[BankCollectionResult] = []
    for index, collector in enumerate(collectors):
        if index and inter_request_delay_seconds:
            time.sleep(inter_request_delay_seconds)
        try:
            results.append(
                collector.collect_mortgage_rates(
                    captured_at_utc=captured_at_utc, persist_snapshot=persist_snapshot
                )
            )
        except Exception as exc:  # noqa: BLE001 - a broken collector must not stop the run
            logger.exception("collector %s raised unexpectedly", collector.bank)
            results.append(
                BankCollectionResult(
                    bank=collector.bank,
                    product_type=ProductType.HOME_LOAN,
                    status=CollectionStatus.FAILED,
                    source_url=getattr(collector, "home_loan_url", ""),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            )
    return CollectionReport(results=tuple(results), captured_at_utc=captured_at_utc)
