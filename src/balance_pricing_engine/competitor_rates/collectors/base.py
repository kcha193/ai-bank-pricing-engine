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
import inspect
import logging
from pathlib import Path
import re
import time
from typing import Callable, ClassVar, Optional, Sequence
from urllib.request import Request, urlopen

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.competitor_rates.extractors import parse_html_rates
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.competitor_rates.normalise import (
    detect_new_business_only,
    extract_cashback,
    extract_package_requirement,
    normalise_lvr_values,
)

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


def _accepts_skipped_row_keys(parser: Callable[..., list[CompetitorRateRecord]]) -> bool:
    """Whether ``parser`` (a bound ``parse_mortgage_html``/``parse_term_deposit_html``)
    will accept the ``skipped_row_keys`` keyword.

    A bank collector outside this codebase may still override the documented
    two-argument extension point (``html``, ``captured_at_utc``) from before
    this parameter existed. Introspecting the bound method, rather than
    always passing the keyword, keeps such an override working -- it simply
    does not participate in skipped-row tracking -- instead of failing the
    whole bank collection with a ``TypeError``.
    """
    try:
        parameters = inspect.signature(parser).parameters
    except (TypeError, ValueError):
        return False
    return "skipped_row_keys" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )


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
    #: category_agnostic_key() of every row this run that parsed a term and
    #: rate but then failed CompetitorRateRecord construction outright -- see
    #: parse_html_rates(). Surfaced here (rather than only logged) so
    #: PricingIntelligenceService.collect() can exclude each row's stored
    #: predecessor from withdrawal detection instead of reporting it REMOVED.
    skipped_row_keys: tuple[str, ...] = ()

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

    @property
    def skipped_row_keys(self) -> set[str]:
        """Union of every attempt's hard-skipped row keys.

        See :attr:`BankCollectionResult.skipped_row_keys`.
        """
        return {key for result in self.results for key in result.skipped_row_keys}

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


def _fetch_with_retries(config: CollectionConfig, url: str, attempt: Callable[[], bytes]) -> bytes:
    """Retry a single fetch attempt with the configured backoff.

    Shared by the default HTTP fetcher and the browser-rendering fetcher, so
    a transient browser launch/navigation/timeout failure gets the same
    ``max_retries``/``retry_backoff_seconds`` resilience as a transient HTTP
    failure -- live collection currently requires the browser path, so
    without this operators had no way to get the configured retry behaviour
    where it actually runs.
    """
    last_error: Optional[Exception] = None
    for attempt_index in range(config.max_retries + 1):
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001 - re-raised as FetchError below
            last_error = exc
            if attempt_index < config.max_retries:
                backoff = config.retry_backoff_seconds * (2**attempt_index)
                logger.warning(
                    "fetch failed (attempt %s/%s) for %s: %s; retrying in %.1fs",
                    attempt_index + 1,
                    config.max_retries + 1,
                    url,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
    raise FetchError(f"could not fetch {url}: {last_error}") from last_error


#: Browser readiness: some table cell shows a percentage. ``parse_html_rates`` reads rates only
#: from ``td``/``th`` cells, so this is the earliest point a parse can succeed.
RATE_TABLE_READY_JS = (
    "() => Array.from(document.querySelectorAll('td, th'))"
    ".some((cell) => /\\d+(?:\\.\\d+)?\\s*%/.test(cell.textContent))"
)


def build_browser_fetcher(config: CollectionConfig) -> Fetcher:
    """Return a fetcher that renders the page in a headless browser first.

    Every tracked bank now builds its rate table client-side, so the static HTML
    contains the page furniture but not the rates. This path is opt-in and needs
    the optional ``playwright`` dependency; without it the caller gets a clear
    :class:`FetchError` rather than a silent empty parse. A missing dependency
    is not retried (retrying will not install it); a transient launch,
    navigation, or timeout failure is, via :func:`_fetch_with_retries`.
    """

    def fetch(url: str) -> bytes:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise FetchError(
                "JavaScript rendering was requested but playwright is not installed. "
                "Install the optional extra: pip install -e '.[browser]' "
                "&& python3 -m playwright install chromium"
            ) from exc

        def attempt() -> bytes:  # pragma: no cover - exercised only against live pages
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=config.user_agent)
                # Not "networkidle": these marketing pages carry persistent
                # background chatter (chat widgets, analytics beacons) that
                # can keep the network from ever going idle, so "networkidle"
                # either times out (FetchError) or -- worse -- returns
                # inconsistently, sometimes before the rate table has
                # rendered (ParserError).
                page.goto(
                    url,
                    wait_until="load",
                    timeout=int(config.request_timeout_seconds * 1000),
                )
                # "load" alone is not sufficient for every bank: BNZ's rate
                # table is populated by a client-side XHR
                # (api.bnz.co.nz/v1/ratesfeed/home/xml) fired after the
                # "load" event, not present in the initial document -- a
                # 2026-09-17 change dropped "networkidle" without accounting
                # for this and broke BNZ's live collection (confirmed via the
                # collector canary and a live re-check 2026-09-18). Waiting
                # for an actual advertised-rate percentage to appear in the
                # rendered text is a universal readiness signal across every
                # bank's markup, unlike a bank-specific CSS selector: it
                # resolves near-instantly for a bank whose table is already
                # in the initial document (ASB confirmed 2026-09-17; Westpac
                # too, though it can still fail downstream for unrelated
                # reasons -- see ParserError), and gives an async-rendering
                # bank like BNZ the time its XHR actually needs. The
                # percentage must be in a table cell (RATE_TABLE_READY_JS),
                # the only place the parser reads rates from: a promotional
                # banner ("Earn 4.85% p.a.") in the page text would otherwise
                # satisfy the wait before the rate table renders. A timeout
                # here is not re-raised: the page still loaded, so this falls
                # through to the ordinary "no rates confidently extracted"
                # ParserError downstream rather than masking that outcome as
                # a FetchError.
                try:
                    page.wait_for_function(
                        RATE_TABLE_READY_JS,
                        timeout=int(config.request_timeout_seconds * 1000),
                    )
                except PlaywrightTimeoutError:
                    pass
                html = page.content()
                browser.close()
            return html.encode("utf-8")

        return _fetch_with_retries(config, url, attempt)

    return fetch


def build_default_fetcher(config: CollectionConfig) -> Fetcher:
    """Return a fetcher applying the configured timeout, retries, and backoff."""

    def fetch(url: str) -> bytes:
        def attempt() -> bytes:
            request = Request(url, headers={"User-Agent": config.user_agent})
            with urlopen(request, timeout=config.request_timeout_seconds) as response:
                return response.read()

        return _fetch_with_retries(config, url, attempt)

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

    #: What an *unmarked* fixed row means for this bank.
    #:
    #: Live pages put "Special" in the table heading, not in each row, so a row
    #: often arrives as bare `6 month | 4.79% p.a.` with no marker to match.
    #: Whether that unmarked row is a special or a carded rate is bank-specific
    #: and cannot be inferred generically: ASB and BNZ publish their headline
    #: (special-tier) rates in an unmarked table, while Westpac marks its
    #: specials explicitly, so an unmarked Westpac row is a carded rate.
    #: ``None`` means "leave the extractor's fallback category alone", which is
    #: right for banks that label both tiers in-row.
    default_fixed_category: ClassVar[Optional[str]] = None

    #: Whether an unmarked row defaulted to ``default_fixed_category`` also
    #: carries this bank's 20% minimum-equity condition.
    #:
    #: True only for banks whose unmarked row *is* the conditional/special
    #: tier (ASB, BNZ) -- the row that would otherwise have matched
    #: ``min_equity_product_markers`` if it had carried the marker word.
    #: False for banks like Westpac, whose unmarked row defaults to the
    #: unconditional standard tier.
    default_fixed_category_implies_min_equity: ClassVar[bool] = False

    #: Products that are advertised mortgage rates but are not comparable
    #: front-book fixed lending: subsidised green/top-up lending, and products
    #: closed to new applications. They are kept for audit under the
    #: ``non_comparable`` category rather than dropped, but must never enter a
    #: market statistic -- a subsidised 1.00% top-up rate would wreck any
    #: median, spread, or market-leader calculation it reached.
    excluded_product_markers: ClassVar[tuple[str, ...]] = (
        "top up",
        "top-up",
        "closed to new application",
    )

    #: Deposit-page rows that are not term deposits at all. Bank investment
    #: pages list managed-fund and KiwiSaver returns in the same tables: BNZ
    #: advertises a "1 year | 12.47% | Growth Fund" row that the generic parser
    #: reads as a 12.47% one-year term deposit. NZ term deposits sit near 4%, so
    #: a fund return entering a market median would destroy it.
    excluded_deposit_product_markers: ClassVar[tuple[str, ...]] = (
        "growth fund",
        "balanced fund",
        "conservative fund",
        "moderate fund",
        "cash fund",
        "income fund",
        "managed fund",
        "kiwisaver",
        # Tax-adjusted PIE "effective rate of return" columns. ANZ and Westpac
        # publish these beside the advertised rate, one per marginal tax band:
        #   "1 year | 4.60 % p.a. | Effective rate 39%"
        # They are not advertised rates. Because deposits rank highest-is-best,
        # including them made both banks look like market leaders by 70 bps when
        # their actual advertised 1-year rate (3.90%) is at parity with ASB.
        "effective rate",
        "income tax rate",
        "after tax",
        "prescribed investor rate",
        # Savings accounts and fee tables on the same page. Kiwibank's deposit page lists
        # Notice Saver / Online Call rates ("2.20 % p.a. for 32 days notice period") and an
        # early-termination charge ("within the first 30 days"), which parsed as 1-month term
        # deposits; a notice period is not a term.
        "notice saver",
        "notice period",
        "online call",
        "call account",
        "termination charge",
    )

    #: Word-boundary-matched counterpart to excluded_deposit_product_markers,
    #: for a marker too short to check as a plain substring without risking a
    #: false positive inside an unrelated word (e.g. bare "pir" also matching
    #: "special rate ex-PIR-es 30 September"). Banks commonly abbreviate
    #: "prescribed investor rate" to "PIR" on the page itself (e.g. "PIR 28%:
    #: 4.01%"), one row per tax band -- without recognising it, an abbreviated
    #: row matches no marker, keeps the generic "term_deposit" category, and
    #: can outrank every genuinely advertised rate for MARKET_LEADER.
    excluded_deposit_product_word_markers: ClassVar[tuple[str, ...]] = ("pir",)

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
        self,
        html: str,
        captured_at_utc: datetime,
        skipped_row_keys: Optional[list[str]] = None,
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
            skipped_row_keys=skipped_row_keys,
        )
        return [self.enrich_mortgage_record(record) for record in records]

    def parse_term_deposit_html(
        self,
        html: str,
        captured_at_utc: datetime,
        skipped_row_keys: Optional[list[str]] = None,
    ) -> list[CompetitorRateRecord]:
        """Parse advertised term-deposit rates from this bank's page HTML."""
        records = parse_html_rates(
            bank=self.bank,
            product_type=ProductType.TERM_DEPOSIT,
            source_url=self.term_deposit_url or "",
            html=html,
            captured_at_utc=captured_at_utc,
            skipped_row_keys=skipped_row_keys,
        )
        return [self.enrich_term_deposit_record(record) for record in records]

    def enrich_term_deposit_record(
        self, record: CompetitorRateRecord
    ) -> CompetitorRateRecord:
        """Fence non-deposit products out of the comparable term-deposit set.

        Kept with full provenance under ``non_comparable`` rather than dropped,
        mirroring the mortgage treatment of subsidised and closed products.
        """
        lowered = record.raw_text_context.lower()
        excluded = any(
            marker in lowered for marker in self.excluded_deposit_product_markers
        ) or any(
            re.search(rf"\b{re.escape(marker)}\b", lowered)
            for marker in self.excluded_deposit_product_word_markers
        )
        if excluded:
            return record.model_copy(update={"rate_category": "non_comparable"})
        return record

    def enrich_mortgage_record(self, record: CompetitorRateRecord) -> CompetitorRateRecord:
        """Derive optional eligibility/promotion fields from the row's own text.

        Enrichment reads only ``raw_text_context`` -- the evidence retained on
        the record itself -- so every derived field can be audited back to the
        exact text it came from.
        """
        context = record.raw_text_context
        lowered = context.lower()

        # Only overridden when the generic extractor's own per-cell category
        # ("special"/"standard", checked against the literal words) fell
        # through to a generic fallback -- e.g. BNZ brands its special rate
        # "Classic", which _rate_category() doesn't recognise on its own.
        # raw_text_context is the whole row (shared by every rate this row
        # split into), so re-deriving unconditionally from it would clobber
        # an already-specific per-cell category with whichever bank-specific
        # marker the row happens to also contain -- typically "special",
        # checked first here, mislabelling a real "standard" rate that sits
        # in the same row.
        category = record.rate_category
        defaulted_to_fixed_category = False
        if any(marker in lowered for marker in self.excluded_product_markers):
            # Retained with full provenance, but fenced out of every market
            # statistic. Checked before any other categorisation so a
            # subsidised product can never be promoted into a comparable tier.
            category = "non_comparable"
        elif category in ("fixed", "home_loan"):
            if any(marker in lowered for marker in self.special_product_markers):
                category = "special"
            elif any(marker in lowered for marker in self.standard_product_markers):
                category = "standard"
            elif self.default_fixed_category is not None:
                # No marker in the row: fall back to what an unmarked fixed row
                # means for this bank. Without this, ASB and BNZ drop out of the
                # comparison entirely and the market median is computed over
                # three banks while reporting five.
                category = self.default_fixed_category
                defaulted_to_fixed_category = True

        max_lvr, min_equity = normalise_lvr_values(context)
        implies_min_equity = any(
            marker in lowered for marker in self.min_equity_product_markers
        ) or (defaulted_to_fixed_category and self.default_fixed_category_implies_min_equity)
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
        parser: Callable[..., list[CompetitorRateRecord]],
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

        skipped_row_keys: list[str] = []
        try:
            if _accepts_skipped_row_keys(parser):
                records = parser(html, captured_at_utc, skipped_row_keys=skipped_row_keys)
            else:
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
                skipped_row_keys=tuple(skipped_row_keys),
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
                skipped_row_keys=tuple(skipped_row_keys),
            )

        return BankCollectionResult(
            bank=self.bank,
            product_type=product_type,
            status=CollectionStatus.SUCCESS,
            source_url=url,
            records=tuple(records),
            snapshot_path=snapshot_path,
            content_sha256=content_hash,
            skipped_row_keys=tuple(skipped_row_keys),
        )

    def _write_snapshot(
        self, html: str, product_type: ProductType, captured_at_utc: datetime
    ) -> str:
        directory = self.raw_root / captured_at_utc.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        # Includes time-of-day, not just the date: a second collection later
        # the same UTC day must not silently overwrite the earlier run's raw
        # evidence, which is what an intraday change would need to audit.
        run_stamp = captured_at_utc.strftime("%H%M%S%f")
        path = directory / f"{self.bank.lower()}_{product_type.value}_{run_stamp}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)


def collect_all_rates(
    collectors: Sequence[BankPricingCollector],
    product_type: ProductType | str = ProductType.HOME_LOAN,
    captured_at_utc: Optional[datetime] = None,
    persist_snapshot: bool = True,
    inter_request_delay_seconds: Optional[float] = None,
) -> CollectionReport:
    """Collect mortgage rates from every collector, isolating per-bank failures.

    A collector raising or returning nothing produces a FAILED entry in the
    report; the remaining banks still run.
    """
    # Coerced, not compared as given: the dispatch below is an identity test
    # against ProductType.HOME_LOAN, which a plain "home_loan" string never
    # satisfies -- it would fall through to term deposits and hand the caller
    # the wrong product with no error raised anywhere.
    product_type = ProductType(product_type)
    captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
    results: list[BankCollectionResult] = []
    for index, collector in enumerate(collectors):
        if index and inter_request_delay_seconds:
            time.sleep(inter_request_delay_seconds)
        collect = (
            collector.collect_mortgage_rates
            if product_type is ProductType.HOME_LOAN
            else collector.collect_term_deposit_rates
        )
        try:
            results.append(
                collect(
                    captured_at_utc=captured_at_utc, persist_snapshot=persist_snapshot
                )
            )
        except Exception as exc:  # noqa: BLE001 - a broken collector must not stop the run
            logger.exception("collector %s raised unexpectedly", collector.bank)
            results.append(
                BankCollectionResult(
                    bank=collector.bank,
                    product_type=product_type,
                    status=CollectionStatus.FAILED,
                    source_url=getattr(
                        collector,
                        "home_loan_url"
                        if product_type is ProductType.HOME_LOAN
                        else "term_deposit_url",
                        "",
                    )
                    or "",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            )
    return CollectionReport(results=tuple(results), captured_at_utc=captured_at_utc)


def collect_all_mortgage_rates(
    collectors: Sequence[BankPricingCollector],
    captured_at_utc: Optional[datetime] = None,
    persist_snapshot: bool = True,
    inter_request_delay_seconds: Optional[float] = None,
) -> CollectionReport:
    """Collect mortgage rates from every collector, isolating per-bank failures.

    Thin wrapper over :func:`collect_all_rates`, kept because existing callers
    and tests use this name.
    """
    return collect_all_rates(
        collectors,
        product_type=ProductType.HOME_LOAN,
        captured_at_utc=captured_at_utc,
        persist_snapshot=persist_snapshot,
        inter_request_delay_seconds=inter_request_delay_seconds,
    )
