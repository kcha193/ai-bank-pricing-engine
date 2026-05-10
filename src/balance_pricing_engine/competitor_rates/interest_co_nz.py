"""Fallback competitor-rate ingestion from interest.co.nz aggregator tables."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from urllib.request import Request, urlopen

from balance_pricing_engine.competitor_rates.extractors import _TableTextParser
from balance_pricing_engine.competitor_rates.models import (
    PARSER_VERSION,
    CompetitorRateRecord,
    ProductType,
    SourceAttempt,
)
from balance_pricing_engine.competitor_rates.normalise import (
    normalise_balance,
    normalise_lvr_condition,
)


INTEREST_CO_NZ_MORTGAGE_URL = "https://www.interest.co.nz/borrowing"
INTEREST_CO_NZ_TERM_DEPOSIT_URL = "https://www.interest.co.nz/saving/term-deposits-1-to-5-years"
INTEREST_CO_NZ_PARSER_VERSION = f"{PARSER_VERSION}-interest-co-nz"
SELECTED_BANKS = ("ANZ", "ASB", "Westpac", "BNZ", "Kiwibank")

_MORTGAGE_HEADERS = (
    "Institution",
    "Product",
    "Variable floating",
    "6 months",
    "1 year",
    "2 years",
    "3 years",
    "4 years",
    "5 years",
)
_TERM_DEPOSIT_HEADERS = (
    "Institution",
    "Credit Rating",
    "Minimum Deposit",
    "Credit",
    "Paid",
    "12 mths",
    "18 mths",
    "2 years",
    "3 years",
    "4 years",
    "5 years",
)


def fetch_interest_co_nz_rates(
    raw_root: str | Path = "data/raw/competitor_rates",
    captured_at_utc: datetime | None = None,
) -> tuple[list[CompetitorRateRecord], list[SourceAttempt]]:
    """Fetch interest.co.nz mortgage and term-deposit tables into canonical records."""
    captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
    records: list[CompetitorRateRecord] = []
    attempts: list[SourceAttempt] = []

    mortgage_html, mortgage_snapshot, mortgage_hash, mortgage_error = _fetch_interest_co_nz_page(
        INTEREST_CO_NZ_MORTGAGE_URL,
        raw_root=raw_root,
        captured_at_utc=captured_at_utc,
    )
    mortgage_records = (
        parse_interest_co_nz_mortgage_rates(mortgage_html, captured_at_utc=captured_at_utc)
        if mortgage_html
        else []
    )
    records.extend(mortgage_records)
    attempts.extend(
        _build_bank_attempts(
            product_type=ProductType.HOME_LOAN,
            url=INTEREST_CO_NZ_MORTGAGE_URL,
            snapshot_path=mortgage_snapshot,
            raw_html_sha256=mortgage_hash,
            records=mortgage_records,
            error=mortgage_error,
        )
    )

    deposit_html, deposit_snapshot, deposit_hash, deposit_error = _fetch_interest_co_nz_page(
        INTEREST_CO_NZ_TERM_DEPOSIT_URL,
        raw_root=raw_root,
        captured_at_utc=captured_at_utc,
    )
    deposit_records = (
        parse_interest_co_nz_term_deposit_rates(deposit_html, captured_at_utc=captured_at_utc)
        if deposit_html
        else []
    )
    records.extend(deposit_records)
    attempts.extend(
        _build_bank_attempts(
            product_type=ProductType.TERM_DEPOSIT,
            url=INTEREST_CO_NZ_TERM_DEPOSIT_URL,
            snapshot_path=deposit_snapshot,
            raw_html_sha256=deposit_hash,
            records=deposit_records,
            error=deposit_error,
        )
    )

    return records, attempts


def parse_interest_co_nz_mortgage_rates(
    html: str,
    captured_at_utc: datetime,
) -> list[CompetitorRateRecord]:
    """Parse one-year mortgage rates from the interest.co.nz borrowing table."""
    raw_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    rows = _rows_with_current_bank(html, SELECTED_BANKS)
    records: list[CompetitorRateRecord] = []
    for row in rows:
        bank = row[0]
        if bank not in SELECTED_BANKS or _is_header(row):
            continue
        product_name = row[1] if len(row) > 1 else ""
        if _skip_mortgage_product(product_name):
            continue
        one_year_rate = _rate_at_header(row, _MORTGAGE_HEADERS, "1 year")
        if one_year_rate is None:
            continue
        context = " | ".join(cell for cell in row if cell)
        category = _mortgage_category(product_name)
        records.append(
            CompetitorRateRecord(
                bank=bank,
                product_type=ProductType.HOME_LOAN,
                product_name=product_name,
                rate_category=category,
                term_label_raw="1 year",
                term_months_normalised=12,
                advertised_rate_pct=one_year_rate,
                interest_payment_frequency="unknown",
                lvr_condition=normalise_lvr_condition(context),
                special_conditions=(
                    "third-party aggregator: interest.co.nz"
                    if category == "special"
                    else None
                ),
                source_url=INTEREST_CO_NZ_MORTGAGE_URL,
                source_bank_page_title="Mortgages | interest.co.nz",
                captured_at_utc=captured_at_utc,
                raw_text_context=context,
                raw_html_sha256=raw_hash,
                parser_version=INTEREST_CO_NZ_PARSER_VERSION,
            )
        )
    return records


def parse_interest_co_nz_term_deposit_rates(
    html: str,
    captured_at_utc: datetime,
) -> list[CompetitorRateRecord]:
    """Parse 12-month bank term-deposit rates from the interest.co.nz table."""
    raw_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    rows = _rows_with_current_bank(html, SELECTED_BANKS)
    records: list[CompetitorRateRecord] = []
    for row in rows:
        bank = row[0]
        if bank not in SELECTED_BANKS or _is_header(row):
            continue
        twelve_month_rate = _rate_at_header(row, _TERM_DEPOSIT_HEADERS, "12 mths")
        if twelve_month_rate is None:
            continue
        context = " | ".join(cell for cell in row if cell)
        min_balance, max_balance = normalise_balance(context)
        records.append(
            CompetitorRateRecord(
                bank=bank,
                product_type=ProductType.TERM_DEPOSIT,
                product_name="Term deposit",
                rate_category="term_deposit",
                term_label_raw="12 mths",
                term_months_normalised=12,
                advertised_rate_pct=twelve_month_rate,
                interest_payment_frequency=_interest_co_nz_frequency(row),
                min_balance_or_deposit=min_balance,
                max_balance_or_deposit=max_balance,
                special_conditions="third-party aggregator: interest.co.nz",
                source_url=INTEREST_CO_NZ_TERM_DEPOSIT_URL,
                source_bank_page_title="Term deposits 1 to 5 years | interest.co.nz",
                captured_at_utc=captured_at_utc,
                raw_text_context=context,
                raw_html_sha256=raw_hash,
                parser_version=INTEREST_CO_NZ_PARSER_VERSION,
            )
        )
    return _dedupe_term_deposit_records(records)


def _fetch_interest_co_nz_page(
    url: str,
    raw_root: str | Path,
    captured_at_utc: datetime,
    timeout_seconds: int = 30,
) -> tuple[str | None, str | None, str | None, str | None]:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 ai-bank-pricing-engine/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except Exception as exc:  # pragma: no cover - live network path
        return None, None, None, str(exc)

    html = body.decode("utf-8", errors="replace")
    raw_hash = hashlib.sha256(body).hexdigest()
    date_dir = captured_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%d")
    snapshot_dir = Path(raw_root) / date_dir
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    slug = "interest_co_nz_" + url.rstrip("/").split("/")[-1].replace("-", "_")
    snapshot_path = snapshot_dir / f"{slug}.html"
    snapshot_path.write_text(html, encoding="utf-8")
    return html, str(snapshot_path), raw_hash, None


def _build_bank_attempts(
    product_type: ProductType,
    url: str,
    snapshot_path: str | None,
    raw_html_sha256: str | None,
    records: list[CompetitorRateRecord],
    error: str | None,
) -> list[SourceAttempt]:
    attempts: list[SourceAttempt] = []
    for bank in SELECTED_BANKS:
        bank_records = [record for record in records if record.bank == bank]
        records_extracted = len(bank_records)
        attempts.append(
            SourceAttempt(
                bank=bank,
                product_type=product_type,
                source_url=url,
                success=error is None and records_extracted > 0,
                records_extracted=records_extracted,
                snapshot_path=snapshot_path,
                raw_html_sha256=raw_html_sha256,
                error=error if error else None if records_extracted else "no rates extracted",
            )
        )
    return attempts


def _rows_with_current_bank(html: str, banks: tuple[str, ...]) -> list[list[str]]:
    parser = _TableTextParser()
    parser.feed(html)
    rows: list[list[str]] = []
    current_bank: str | None = None
    for row in parser.rows:
        if not row:
            continue
        first_bank = _bank_from_cell(row[0], banks)
        if first_bank:
            current_bank = first_bank
            rows.append([current_bank, *row[1:]])
            continue
        if row[0] == "Institution":
            current_bank = None
            continue
        if current_bank and row[0] == "":
            rows.append([current_bank, *row[1:]])
            continue
        if current_bank and _looks_like_continuation_row(row[0]):
            rows.append([current_bank, *row])
            continue
        current_bank = None
    return rows


def _bank_from_cell(value: str, banks: tuple[str, ...]) -> str | None:
    text = value.lower()
    for bank in banks:
        if bank.lower() in text:
            return bank
    return None


def _rate_at_header(row: list[str], headers: tuple[str, ...], header: str) -> float | None:
    index = headers.index(header)
    if len(row) <= index:
        return None
    return _parse_rate(row[index])


def _parse_rate(value: str) -> float | None:
    match = re.search(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)(?:\s*%)?", value)
    if not match:
        return None
    rate = float(match.group(1))
    if 0 <= rate <= 15:
        return rate
    return None


def _is_header(row: list[str]) -> bool:
    return bool(row) and row[0] == "Institution"


def _looks_like_continuation_row(first_cell: str) -> bool:
    text = first_cell.lower()
    if re.fullmatch(r"[a-z]{1,3}[+-]?", first_cell):
        return True
    return any(
        marker in text
        for marker in (
            "standard",
            "special",
            "owner occupied",
            "residential",
            "offset",
            "totalmoney",
            "better",
            "choices",
        )
    )


def _skip_mortgage_product(product_name: str) -> bool:
    text = product_name.lower()
    return any(
        marker in text
        for marker in (
            "top up",
            "good energy",
            "reno loan",
            "better future",
            "offset",
            "totalmoney",
            "everyday",
            "greater choices",
        )
    )


def _mortgage_category(product_name: str) -> str:
    text = product_name.lower()
    if "special" in text:
        return "special"
    if "floating" in text or "variable" in text:
        return "floating"
    return "standard"


def _interest_co_nz_frequency(row: list[str]) -> str:
    paid_code = row[4].strip().upper() if len(row) > 4 else ""
    return {
        "M": "monthly",
        "Q": "quarterly",
        "Y": "annually",
        "A": "annually",
        "S": "annually",
        "Z": "at_maturity",
    }.get(paid_code, "unknown")


def _dedupe_term_deposit_records(
    records: list[CompetitorRateRecord],
) -> list[CompetitorRateRecord]:
    selected: dict[tuple[str, str, int | None, str], CompetitorRateRecord] = {}
    for record in records:
        key = (
            record.bank,
            record.rate_category,
            record.term_months_normalised,
            record.interest_payment_frequency,
        )
        existing = selected.get(key)
        if existing is None:
            selected[key] = record
            continue
        existing_min = existing.min_balance_or_deposit or float("inf")
        record_min = record.min_balance_or_deposit or float("inf")
        if record_min < existing_min:
            selected[key] = record
    return list(selected.values())
