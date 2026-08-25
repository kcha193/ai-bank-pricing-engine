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
    normalise_lvr_values,
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
    current_header_row: list[str] | None = None
    for row in parser.rows:
        if _is_header_row(row):
            current_header_row = row
            continue
        context = _clean_text(" | ".join(row))
        term_label = _find_term_label(row, product)
        if term_label is None:
            current_header_row = row
            continue
        term = normalise_term(term_label)
        rate_categories = _extract_rates_with_categories(
            row, context, product, term.special_label, header_row=current_header_row
        )
        if not rate_categories:
            continue
        min_balance, max_balance = (
            normalise_balance(context) if product == ProductType.TERM_DEPOSIT else (None, None)
        )
        for rate, category, own_header in rate_categories:
            # Extends only *this* record's own stored context with its own
            # aligned header cell (not folded into `context` itself, used
            # above for lvr_condition/special_conditions/balance/frequency at
            # parse time) -- so a bank-specific category alias living only in
            # the header (e.g. BNZ's "Classic") reaches
            # BankPricingCollector.enrich_mortgage_record() later, which reads
            # raw_text_context. Using each record's own column's header only,
            # not the whole header row, keeps a sibling column's marker (e.g.
            # a "Special" header's min-equity implication) out of a record
            # that was never actually under that column.
            raw_text_context = (
                _clean_text(context + " | " + own_header) if own_header else context
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
                    raw_text_context=raw_text_context,
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


# Bounds the gap a marker-then-percentage match may span: the percentage
# must be the marker's *own* value, not merely the nearest one that happens
# to follow within 20 characters. Excludes crossing ";"/"." (a new clause)
# and, specifically, the word "rate" -- the one noun in this domain that
# reliably introduces a genuine, unrelated value (e.g. "Cashback offer;
# rate 5.00%", where "cashback" has no percentage of its own at all, and the
# only "%" in the text is the real rate in a separate clause). Without this,
# the marker-then-percentage match would strip that genuine rate outright,
# leaving the row with no plausible rate and silently dropping it.
_MARKER_TO_PERCENTAGE_GAP = r"(?:(?!rate)[^%;.]){0,20}"

# A marker word immediately followed by "rate" (allowing an inflection --
# "discounted rate", "cashback rate") names the *type* of rate being
# advertised, not a separate promotional quantity: "5.00% discounted rate"
# and "5.00% bonus rate" both mean the rate itself is 5.00%, not that a
# further discount/bonus of 5.00% applies on top of some other rate.
# Excluded from the percentage-then-marker alternative so that genuine rate
# survives instead of being stripped down to nothing.
_NOT_A_RATE_LABEL = r"(?!\w*\s+rate\b)"

_PROMOTIONAL_PERCENTAGE_MARKERS = ("cashback", "cash back", "discount", "bonus", "rebate")
_PROMOTIONAL_MARKER_ALTERNATION = "|".join(
    marker.replace(" ", r"\s+") for marker in _PROMOTIONAL_PERCENTAGE_MARKERS
)
_PROMOTIONAL_PERCENTAGE_PATTERN = re.compile(
    r"(?<![\d.])\d{1,2}(?:\.\d{1,3})?\s*%\s*(?:" + _PROMOTIONAL_MARKER_ALTERNATION + r")"
    + _NOT_A_RATE_LABEL
    + r"|(?:" + _PROMOTIONAL_MARKER_ALTERNATION + r")" + _MARKER_TO_PERCENTAGE_GAP
    + r"\d{1,2}(?:\.\d{1,3})?\s*%",
    re.I,
)

# Home-loan-only: "deposit"/"equity" describe a borrower's eligibility
# position (e.g. "5% deposit required"), unlike in TERM_DEPOSIT context,
# where "deposit" is the product name itself and appears next to every
# genuine rate -- stripping it there would discard real rates wholesale.
_ELIGIBILITY_PERCENTAGE_MARKERS = ("deposit", "equity")
_ELIGIBILITY_MARKER_ALTERNATION = "|".join(_ELIGIBILITY_PERCENTAGE_MARKERS)
# The qualifiers this domain commonly inserts between the percentage and
# its marker ("0.75% low equity margin", "5% minimum deposit required",
# hyphenated as "0.75% low-equity margin") -- unlike a bare "0.75% equity",
# without tolerating them the percentage-then-marker alternative requires
# the marker immediately after the percentage and misses the eligibility
# percentage entirely, leaving it as a second plausible rate alongside the
# genuine one. "minimum" matches normalise.py's own _EQUITY_PATTERN
# vocabulary. Scoped to these demonstrated qualifiers rather than a broader
# gap allowance: an open-ended gap here risks stripping a genuine rate that
# happens to be followed by an unrelated later mention of "equity" (e.g. a
# separate eligibility clause in the same cell).
_ELIGIBILITY_QUALIFIERS = ("low", "minimum", "min")
_ELIGIBILITY_QUALIFIER_ALTERNATION = "|".join(_ELIGIBILITY_QUALIFIERS)
_ELIGIBILITY_PERCENTAGE_PATTERN = re.compile(
    r"(?<![\d.])\d{1,2}(?:\.\d{1,3})?\s*%\s*"
    r"(?:(?:" + _ELIGIBILITY_QUALIFIER_ALTERNATION + r")[\s-]+)?"
    r"(?:" + _ELIGIBILITY_MARKER_ALTERNATION + r")"
    + _NOT_A_RATE_LABEL
    + r"|(?:" + _ELIGIBILITY_MARKER_ALTERNATION + r")" + _MARKER_TO_PERCENTAGE_GAP
    + r"\d{1,2}(?:\.\d{1,3})?\s*%",
    re.I,
)


def _strip_non_rate_percentages(text: str, product: ProductType) -> str:
    """Remove a non-rate percentage's span (e.g. "1% cashback", reversed
    "cashback of 1%", or -- for home loans only -- "5% deposit required")
    so it isn't mistaken for a genuine rate sharing its cell or row. Only
    the marker-adjacent percentage is removed -- rejecting the whole
    cell/row would also discard a genuine rate advertised alongside it
    (e.g. "Special 5.00% with 1% cashback"). Both grammatical orders are
    stripped: the marker can follow its percentage or precede it.
    """
    stripped = _PROMOTIONAL_PERCENTAGE_PATTERN.sub("", text)
    if product == ProductType.HOME_LOAN:
        stripped = _ELIGIBILITY_PERCENTAGE_PATTERN.sub("", stripped)
    return stripped


def _extract_rate(context: str, product: ProductType) -> float | None:
    rates = [
        float(match.group(1))
        for match in re.finditer(
            r"(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)\s*%", _strip_non_rate_percentages(context, product)
        )
    ]
    if not rates:
        return None
    plausible = [rate for rate in rates if 0 <= rate <= 15]
    return plausible[-1] if plausible else rates[-1]


def _extract_plausible_rate(text: str, product: ProductType) -> float | None:
    """Like :func:`_extract_rate`, but never falls back to an implausible
    reading -- used per-cell below, where a non-rate percentage (e.g. "70%
    LVR" or "80% equity") must not be mistaken for a rate just because it is
    the only percentage in that particular cell.
    """
    rates = [
        float(match.group(1))
        for match in re.finditer(
            r"(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)\s*%", _strip_non_rate_percentages(text, product)
        )
    ]
    plausible = [rate for rate in rates if 0 <= rate <= 15]
    return plausible[-1] if plausible else None


def _extract_rates_with_categories(
    row: list[str],
    context: str,
    product: ProductType,
    special_label: str | None,
    header_row: list[str] | None = None,
) -> list[tuple[float, str, str | None]]:
    """One ``(rate, category, own_header_cell)`` triple per rate-bearing
    cell in the row.

    Most rows advertise a single rate, handled the existing way: the last
    plausible percentage anywhere in the row, categorised from the row's
    full joined text. A row with *several* rate-bearing cells -- e.g.
    adjacent "Special 4.79%" and "Standard 5.39%" cells for one term --
    needs each rate categorised from its own cell instead: categorising from
    the row-wide context finds whichever category word appears anywhere in
    the row (typically "special", checked first) and pairs it with
    whichever rate ``_extract_rate`` happened to pick last, silently
    dropping the other rate and mislabelling the one that is kept.

    Some tables carry the category only in the column header (e.g. a
    "Special" / "Standard" header pair over bare "4.79%" / "5.39%" data
    cells), so a data cell with no category word of its own falls back to
    ``header_row``'s cell at the same column index before giving up to the
    generic category -- otherwise both bare cells collide on the same
    generic category and the same ``observation_key()``, and one silently
    overwrites the other. The same header fallback applies when only one
    cell carries a rate: a single "Special rate" header over a bare data
    cell must still resolve to "special", not the generic default.

    The third element is that same aligned header cell (or ``None`` when
    there is no ``header_row`` or the index is out of range), returned
    *per record* rather than folded into a shared context here: callers use
    it to extend only that record's own stored context, e.g. so a
    bank-specific alias living only in the header (BNZ's "Classic") reaches
    later enrichment. Returning the *whole* header row instead would leak a
    sibling column's marker into every record from the row -- e.g. a
    "Special" header's min-equity marker wrongly applied to the "Standard"
    record sharing that row, since eligibility enrichment has no per-cell
    boundary of its own to filter it back out.
    """
    cell_rates = [
        (idx, cell, _extract_plausible_rate(cell, product)) for idx, cell in enumerate(row)
    ]
    plausible_cells = [(idx, cell, rate) for idx, cell, rate in cell_rates if rate is not None]

    if not plausible_cells:
        rate = _extract_rate(context, product)
        if rate is None:
            return []
        return [(rate, _rate_category(product, context, special_label), None)]

    if len(plausible_cells) == 1:
        idx, cell, rate = plausible_cells[0]
        own_header = header_row[idx] if header_row is not None and idx < len(header_row) else None
        hint = _rate_category_hint(product, cell, special_label)
        if hint is None and own_header is not None:
            hint = _rate_category_hint(product, own_header, special_label)
        if hint is None:
            hint = _rate_category(product, context, special_label)
        return [(rate, hint, own_header)]

    results: list[tuple[float, str, str | None]] = []
    for idx, cell, rate in plausible_cells:
        own_header = header_row[idx] if header_row is not None and idx < len(header_row) else None
        hint = _rate_category_hint(product, cell, special_label)
        if hint is None and own_header is not None:
            hint = _rate_category_hint(product, own_header, special_label)
        if hint is None:
            hint = _rate_category(product, "", special_label)
        results.append((rate, hint, own_header))
    return results


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


def _rate_category_hint(product: ProductType, text: str, special_label: str | None) -> str | None:
    """Category implied by ``text`` alone, or ``None`` if it carries no
    recognisable category word -- distinct from :func:`_rate_category`, which
    always returns *some* category by falling back to a generic default.
    Callers that have another source of category information (e.g. a column
    header) to try before giving up need to tell "no hint" apart from "the
    generic fallback", which a function that never returns ``None`` can't do.
    """
    lowered = text.lower()
    if product == ProductType.TERM_DEPOSIT:
        if "pie" in lowered:
            return "pie_term_deposit"
        if "special" in lowered:
            return "special"
        return None
    if special_label in {"floating", "variable"}:
        return special_label
    if "special" in lowered:
        return "special"
    if "standard" in lowered:
        return "standard"
    if "fixed" in lowered:
        return "fixed"
    return None


def _rate_category(product: ProductType, context: str, special_label: str | None) -> str:
    hint = _rate_category_hint(product, context, special_label)
    if hint is not None:
        return hint
    return "term_deposit" if product == ProductType.TERM_DEPOSIT else "home_loan"


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
    """Drop an exact repeat row, without collapsing two distinct simultaneous
    tiers (e.g. different LVR conditions or product names at the same rate).

    Runs before mortgage eligibility enrichment, so ``max_lvr``/``min_equity``
    are not yet populated *on the record* and can't be read from it directly.
    ``lvr_condition`` alone is too coarse to stand in for them here: it only
    distinguishes three buckets ("standard", "min_20_percent_equity",
    "low_equity_margin_applies"), so two simultaneous tiers like "70% LVR" and
    "80% LVR" both normalise to "standard" and would otherwise collide onto
    the same key. The numeric LVR is instead re-derived straight from
    ``raw_text_context`` via :func:`~.normalise.normalise_lvr_values` -- the
    same extraction :meth:`.collectors.base.BankPricingCollector.enrich_mortgage_record`
    applies later from the same field -- purely as an extra key discriminator;
    the record's own ``max_lvr``/``min_equity`` are left for enrichment to set.
    """
    seen: set[tuple] = set()
    deduped: list[CompetitorRateRecord] = []
    for record in records:
        numeric_lvr, _ = normalise_lvr_values(record.raw_text_context)
        key = (
            record.bank,
            str(record.product_type),
            record.term_label_raw.lower(),
            record.rate_category,
            record.interest_payment_frequency,
            record.advertised_rate_pct,
            record.product_name.strip().lower(),
            record.lvr_condition or "",
            numeric_lvr,
            record.min_balance_or_deposit,
            record.max_balance_or_deposit,
        )
        if key not in seen:
            seen.add(key)
            deduped.append(record)
    return deduped
