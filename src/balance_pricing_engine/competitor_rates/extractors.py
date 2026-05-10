"""Official-page fetching and conservative HTML extraction."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

from balance_pricing_engine.competitor_rates.models import (
    PARSER_VERSION,
    CompetitorRateRecord,
    ProductType,
    SourceAttempt,
)
from balance_pricing_engine.competitor_rates.normalise import (
    normalise_balance,
    normalise_frequency,
    normalise_lvr_condition,
    normalise_term,
)
from balance_pricing_engine.competitor_rates.sources import RateSource


class _TableTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []
        if tag.lower() == "img" and self._in_cell:
            attrs_by_name = {key.lower(): value for key, value in attrs if value}
            label = attrs_by_name.get("alt") or attrs_by_name.get("title")
            if label:
                self._cell_parts.append(label)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            self.title = _clean_text(" ".join(self._title_parts))
        if tag in {"td", "th"} and self._in_cell:
            cell = _clean_text(" ".join(self._cell_parts))
            self._current_row.append(cell)
            self._in_cell = False
        if tag == "tr":
            if any(self._current_row):
                self.rows.append(self._current_row)
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)


def fetch_source(
    source: RateSource,
    raw_root: str | Path = "data/raw/competitor_rates",
    timeout_seconds: int = 30,
) -> tuple[str | None, SourceAttempt]:
    """Fetch one official source page and persist its raw HTML snapshot."""
    request = Request(source.url, headers={"User-Agent": "ai-bank-pricing-engine/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
        html = body.decode("utf-8", errors="replace")
        raw_hash = hashlib.sha256(body).hexdigest()
        date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshot_dir = Path(raw_root) / date_dir
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{source.bank.lower()}_{source.product_type.value}.html"
        snapshot_path.write_text(html, encoding="utf-8")
        return html, SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=True,
            snapshot_path=str(snapshot_path),
            raw_html_sha256=raw_hash,
        )
    except Exception as exc:  # pragma: no cover - exercised by live/network paths
        return None, SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=False,
            error=str(exc),
        )


def extract_source_rates(
    source: RateSource,
    raw_root: str | Path = "data/raw/competitor_rates",
    captured_at_utc: datetime | None = None,
    use_browser_fallback: bool = False,
) -> tuple[list[CompetitorRateRecord], SourceAttempt]:
    """Fetch and parse one official source page."""
    captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
    html, attempt = fetch_source(source, raw_root=raw_root)
    if html is None:
        if not use_browser_fallback:
            return [], attempt
        rendered_html, rendered_attempt = fetch_source_with_playwright(source, raw_root=raw_root)
        if rendered_html is None:
            if rendered_attempt.error:
                attempt.error = f"{attempt.error}; {rendered_attempt.error}"
            return [], attempt
        html = rendered_html
        attempt = rendered_attempt
    records = parse_html_rates(
        bank=source.bank,
        product_type=source.product_type,
        source_url=source.url,
        html=html,
        captured_at_utc=captured_at_utc,
    )
    if not records and use_browser_fallback:
        rendered_html, rendered_attempt = fetch_source_with_playwright(source, raw_root=raw_root)
        if rendered_html is not None:
            records = parse_html_rates(
                bank=source.bank,
                product_type=source.product_type,
                source_url=source.url,
                html=rendered_html,
                captured_at_utc=captured_at_utc,
            )
            attempt.snapshot_path = rendered_attempt.snapshot_path
            attempt.raw_html_sha256 = rendered_attempt.raw_html_sha256
            attempt.success = rendered_attempt.success
        elif rendered_attempt.error:
            attempt.error = rendered_attempt.error
    attempt.records_extracted = len(records)
    if not records:
        attempt.success = False
        attempt.error = "page fetched but no rates were confidently extracted"
    return records, attempt


def fetch_source_with_playwright(
    source: RateSource,
    raw_root: str | Path = "data/raw/competitor_rates",
    timeout_seconds: int = 45,
) -> tuple[str | None, SourceAttempt]:
    """Render one source page with Playwright when the optional dependency is available."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=False,
            error="Playwright fallback requested but playwright is not installed",
        )

    try:  # pragma: no cover - opt-in live browser path
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(source.url, wait_until="networkidle", timeout=timeout_seconds * 1000)
            html = page.content()
            browser.close()
        raw_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshot_dir = Path(raw_root) / date_dir
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = (
            snapshot_dir / f"{source.bank.lower()}_{source.product_type.value}_rendered.html"
        )
        snapshot_path.write_text(html, encoding="utf-8")
        return html, SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=True,
            snapshot_path=str(snapshot_path),
            raw_html_sha256=raw_hash,
        )
    except Exception as exc:  # pragma: no cover - opt-in live browser path
        return None, SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=False,
            error=f"Playwright fallback failed: {exc}",
        )


def parse_html_rates(
    bank: str,
    product_type: str | ProductType,
    source_url: str,
    html: str,
    captured_at_utc: datetime,
) -> list[CompetitorRateRecord]:
    """Parse percentage-bearing rows from bank HTML into canonical records.

    This intentionally favours precision over recall. It parses table rows and nearby labels with
    recognisable terms, frequencies, balances, and mortgage conditions. Rows that include a
    percentage but no recognisable product term are skipped.
    """
    product = ProductType(product_type)
    parser = _TableTextParser()
    parser.feed(html)
    raw_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    records: list[CompetitorRateRecord] = []
    for row in parser.rows:
        if _is_header_row(row):
            continue
        context = _clean_text(" | ".join(row))
        rate = _extract_rate(context)
        if rate is None:
            continue
        term_label = _find_term_label(row, product)
        if term_label is None:
            continue
        term = normalise_term(term_label)
        category = _rate_category(product, context, term.special_label)
        min_balance, max_balance = (
            normalise_balance(context) if product == ProductType.TERM_DEPOSIT else (None, None)
        )
        records.append(
            CompetitorRateRecord(
                bank=bank,
                product_type=product,
                product_name=_product_name(product, row, category),
                rate_category=category,
                term_label_raw=term.raw_label,
                term_months_normalised=term.months,
                term_days_raw=term.days,
                advertised_rate_pct=rate,
                interest_payment_frequency=(
                    normalise_frequency(context)
                    if product == ProductType.TERM_DEPOSIT
                    else "unknown"
                ),
                min_balance_or_deposit=min_balance,
                max_balance_or_deposit=max_balance,
                lvr_condition=(
                    normalise_lvr_condition(context)
                    if product == ProductType.HOME_LOAN
                    else None
                ),
                special_conditions=_special_conditions(context),
                source_url=source_url,
                source_bank_page_title=parser.title,
                captured_at_utc=captured_at_utc,
                raw_text_context=context,
                raw_html_sha256=raw_hash,
                parser_version=PARSER_VERSION,
            )
        )
    return _dedupe_records(records)


def extract_all_sources(
    sources: Iterable[RateSource],
    raw_root: str | Path = "data/raw/competitor_rates",
    captured_at_utc: datetime | None = None,
    use_browser_fallback: bool = False,
) -> tuple[list[CompetitorRateRecord], list[SourceAttempt]]:
    """Fetch and parse all configured official sources."""
    captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
    all_records: list[CompetitorRateRecord] = []
    attempts: list[SourceAttempt] = []
    for source in sources:
        records, attempt = extract_source_rates(
            source,
            raw_root=raw_root,
            captured_at_utc=captured_at_utc,
            use_browser_fallback=use_browser_fallback,
        )
        all_records.extend(records)
        attempts.append(attempt)
    return all_records, attempts


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _is_header_row(row: list[str]) -> bool:
    text = " ".join(row).lower()
    return "rate" in text and not re.search(r"\d+(?:\.\d+)?\s*%", text)


def _extract_rate(context: str) -> float | None:
    rates = [
        float(match.group(1))
        for match in re.finditer(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)\s*%", context)
    ]
    if not rates:
        return None
    plausible = [rate for rate in rates if 0 <= rate <= 15]
    return plausible[-1] if plausible else rates[-1]


def _find_term_label(row: list[str], product: ProductType) -> str | None:
    for cell in row:
        text = cell.lower()
        if product == ProductType.HOME_LOAN and ("floating" in text or "variable" in text):
            return cell
        if re.search(r"\b\d+\s*(?:day|days|month|months|year|years)\b", text):
            return _strip_product_prefix(cell)
        if re.search(
            r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
            r"(?:month|months|year|years)\b",
            text,
        ):
            return _strip_product_prefix(cell)
    return None


def _strip_product_prefix(cell: str) -> str:
    return re.sub(r"^(?:pie|term deposit|fixed|special|standard)\s+", "", cell.strip(), flags=re.I)


def _rate_category(product: ProductType, context: str, special_label: str | None) -> str:
    text = context.lower()
    if product == ProductType.TERM_DEPOSIT:
        if "pie" in text:
            return "pie_term_deposit"
        if "special" in text:
            return "special"
        return "term_deposit"
    if special_label in {"floating", "variable"}:
        return special_label
    if "special" in text:
        return "special"
    if "standard" in text:
        return "standard"
    if "fixed" in text:
        return "fixed"
    return "home_loan"


def _product_name(product: ProductType, row: list[str], category: str) -> str:
    if product == ProductType.HOME_LOAN:
        return next((cell for cell in row if category.lower() in cell.lower()), row[0])
    if category == "pie_term_deposit":
        return "PIE term deposit"
    return "Term deposit"


def _special_conditions(context: str) -> str | None:
    text = context.lower()
    condition_terms = ["equity", "lvr", "owner", "investor", "package", "low equity", "special"]
    if any(term in text for term in condition_terms):
        return context
    return None


def _dedupe_records(records: list[CompetitorRateRecord]) -> list[CompetitorRateRecord]:
    seen: set[tuple[str, str, str, str, str, float]] = set()
    deduped: list[CompetitorRateRecord] = []
    for record in records:
        key = (
            record.bank,
            str(record.product_type),
            record.term_label_raw.lower(),
            record.rate_category,
            record.interest_payment_frequency,
            record.advertised_rate_pct,
        )
        if key not in seen:
            seen.add(key)
            deduped.append(record)
    return deduped
