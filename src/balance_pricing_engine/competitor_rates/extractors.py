"""Official-page fetching and conservative HTML extraction."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

from pydantic import ValidationError

from balance_pricing_engine.competitor_rates.models import (
    PARSER_VERSION,
    CompetitorRateRecord,
    ProductType,
    SourceAttempt,
    category_agnostic_key,
)
from balance_pricing_engine.competitor_rates.normalise import (
    normalise_balance,
    normalise_frequency,
    normalise_lvr_condition,
    normalise_lvr_values,
    normalise_term,
)
from balance_pricing_engine.competitor_rates.sources import RateSource


logger = logging.getLogger(__name__)


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
    skipped_row_keys: list[str] | None = None,
) -> list[CompetitorRateRecord]:
    """Parse percentage-bearing rows from bank HTML into canonical records.

    This intentionally favours precision over recall. It parses table rows and nearby labels with
    recognisable terms, frequencies, balances, and mortgage conditions. Rows that include a
    percentage but no recognisable product term are skipped.

    When ``skipped_row_keys`` is given, a row that parses a term and rate but
    then fails :class:`CompetitorRateRecord` construction outright has its
    :func:`~balance_pricing_engine.competitor_rates.models.category_agnostic_key`
    appended to it, rather than being dropped with no trace: a caller with a
    stored baseline (``pricing_intelligence``) can use this to recognise that
    the row's predecessor is unknown this run, not withdrawn.
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
        pie_table = product == ProductType.TERM_DEPOSIT and _is_pie_return_table(
            current_header_row
        )
        for rate, category, own_header, own_context in rate_categories:
            if pie_table and category == "term_deposit":
                category = "pie_term_deposit"
            # own_context (this record's row text with sibling rate-bearing
            # cells excluded) plus own_header, not the shared `context` --
            # so a bank-specific category alias living only in the header
            # (e.g. BNZ's "Classic") reaches
            # BankPricingCollector.enrich_mortgage_record() later, which reads
            # raw_text_context, while a sibling cell's own eligibility/promo
            # text (e.g. "Special 4.79% with 1% cashback" next to "Standard
            # 5.39%") does not leak into a record that was never under that
            # cell. Every other context-derived field below is likewise
            # derived from this same scoped raw_text_context, not the shared
            # `context` -- otherwise a sibling's eligibility condition (e.g.
            # "Special 4.79% with 20% equity" next to "Standard 5.39%")
            # would still leak into lvr_condition/special_conditions even
            # though only raw_text_context itself was fixed.
            raw_text_context = (
                _clean_text(own_context + " | " + own_header) if own_header else own_context
            )
            min_balance, max_balance = (
                normalise_balance(raw_text_context)
                if product == ProductType.TERM_DEPOSIT
                else (None, None)
            )
            interest_frequency = (
                normalise_frequency(_strip_term_label(raw_text_context, term.raw_label))
                if product == ProductType.TERM_DEPOSIT
                else "unknown"
            )
            try:
                record = CompetitorRateRecord(
                    bank=bank,
                    product_type=product,
                    product_name=(
                        own_header
                        if product == ProductType.HOME_LOAN
                        and term.special_label in {"floating", "variable"}
                        and own_header and len(rate_categories) > 1
                        else _product_name(product, row, category)
                    ),
                    rate_category=category,
                    term_label_raw=term.raw_label,
                    term_months_normalised=term.months,
                    term_days_raw=term.days,
                    advertised_rate_pct=rate,
                    interest_payment_frequency=interest_frequency,
                    min_balance_or_deposit=min_balance,
                    max_balance_or_deposit=max_balance,
                    lvr_condition=(
                        normalise_lvr_condition(raw_text_context)
                        if product == ProductType.HOME_LOAN
                        else None
                    ),
                    special_conditions=_special_conditions(raw_text_context),
                    source_url=source_url,
                    source_bank_page_title=parser.title,
                    captured_at_utc=captured_at_utc,
                    raw_text_context=raw_text_context,
                    raw_html_sha256=raw_hash,
                    parser_version=PARSER_VERSION,
                )
            except ValidationError as exc:
                # One unparseable row must not cost the whole bank. Kiwibank's
                # term-deposit page produced a single invalid record and
                # returned zero rates instead of its other 36 -- the same
                # failure-isolation principle already applied per bank, missing
                # here per row. Logged rather than silently dropped, so a parser
                # regression is still visible.
                logger.warning(
                    "%s %s: skipping unparseable row (%s): %s",
                    bank,
                    product.value,
                    exc.error_count() if hasattr(exc, "error_count") else "invalid",
                    raw_text_context[:160],
                )
                if skipped_row_keys is not None:
                    skipped_row_keys.append(
                        category_agnostic_key(
                            bank,
                            product,
                            term.months,
                            term.raw_label,
                            interest_frequency,
                            min_balance,
                            max_balance,
                        )
                    )
                continue
            records.append(record)
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


def _strip_term_label(text: str, term_label: str) -> str:
    """Remove the row's own term phrase before frequency normalisation.

    ``raw_text_context`` includes every cell in the row, including the term
    label itself (e.g. "6 months"). :func:`~.normalise.normalise_frequency`
    checks for the word "month" anywhere in its input, so an unstripped term
    label alone would misclassify a row that never actually published a
    payment frequency as "monthly" -- while a "1 year"/"2 years" row (matching
    none of its keywords by coincidence) correctly falls back to "unknown",
    the same absence of information reported inconsistently depending on the
    term. ``interest_payment_frequency`` also participates in every
    observation identity, so this inconsistency isn't just cosmetic: a page
    later adding a genuine "at maturity" label would change the key and read
    as a withdrawal-plus-new-product rather than an ordinary update.

    Only the matched term phrase within ``term_label`` is removed, via
    :data:`_TERM_PHRASE_PATTERN` -- not the whole string. ``term_label`` can
    be a combined cell like "6 months, interest paid monthly" when a bank
    publishes term and frequency together (:func:`_find_term_label` returns
    the entire cell, not just the term substring); stripping the whole thing
    would destroy that genuine frequency information along with the term.
    """
    match = _TERM_PHRASE_PATTERN.search(term_label)
    phrase = match.group(0) if match else term_label
    return re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)


#: A bare rate percentage (e.g. "1.00%"), shared by _is_header_row() (a row
#: mentioning "rate" with no percentage anywhere is a header, not data) and
#: _product_name() (a candidate product-name cell must not carry one, since
#: that is exactly the mutable value identity must not embed).
_RATE_PERCENTAGE_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%")


#: "Effective rate of return" is excluded: it is the tax-adjusted comparison column banks also
#: print beside ordinary term-deposit rates (ANZ's single-table layout, 2026-08-27), and those
#: cells are fenced off as non-comparable on their own.
_PIE_RETURN_HEADER_PATTERN = re.compile(
    r"(?<!effective )\brates? of return\b|\breturn frequency\b"
)


def _is_pie_return_table(header_row: list[str] | None) -> bool:
    """Whether a deposit table's header row describes a PIE fund's returns.

    A PIE fund pays a "rate of return", not interest, and ANZ, BNZ and Westpac publish that table
    beside their ordinary term-deposit table with no "PIE" in any cell (only in a heading the
    table parser does not see): "Rate of return", "Rate of return p.a.", "Return Frequency"
    (live pages, 2026-10-30).
    Read as ordinary term deposits, a PIE rate above the advertised rate wins market leadership
    and a different one collides with the advertised rate at the same identity. Kiwibank's
    header names its PIE column outright, which the per-cell "pie" hint already handles.
    """
    return header_row is not None and bool(
        _PIE_RETURN_HEADER_PATTERN.search(" ".join(header_row).lower())
    )


def _is_header_row(row: list[str]) -> bool:
    text = " ".join(row).lower()
    return (
        "rate" in text or bool(re.fullmatch(r"term\s*\d*", row[0].lower()))
    ) and not _RATE_PERCENTAGE_PATTERN.search(text)


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

_PROMOTIONAL_PERCENTAGE_MARKERS = (
    "cashback",
    "cash back",
    "cash contribution",
    "discount",
    "bonus",
    "rebate",
)
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
# An eligibility margin is sometimes itself labelled with the "p.a." rate
# unit before its qualifier/marker ("0.75% p.a. low equity margin"), unlike
# "0.75% low equity margin" -- tolerated as an optional prefix so it doesn't
# break the qualifier-then-marker match that follows.
_ELIGIBILITY_PERCENTAGE_PATTERN = re.compile(
    r"(?<![\d.])\d{1,2}(?:\.\d{1,3})?\s*%\s*"
    r"(?:p\.?a\.?\s+)?"
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
) -> list[tuple[float, str, str | None, str]]:
    """One ``(rate, category, own_header_cell, own_context)`` quadruple per
    rate-bearing cell in the row.

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

    The fourth element, ``own_context``, is the same idea applied to the row
    itself: when several cells each carry their own rate (e.g. "Special
    4.79% with 1% cashback" and "Standard 5.39%" as two cells), a record's
    context must exclude *other* rate-bearing cells' text, not just the
    header -- otherwise the "Standard" record's stored context still
    contains "cashback" and "Special" from its sibling cell, and later
    enrichment reads eligibility/promotion fields straight off that leaked
    text. Cells that are not themselves one of the row's other plausible
    rates (e.g. a shared "Term" column) are kept for every record, since
    they genuinely apply to the whole row.
    """
    cell_rates = [
        (idx, cell, _extract_plausible_rate(cell, product)) for idx, cell in enumerate(row)
    ]
    plausible_cells = [(idx, cell, rate) for idx, cell, rate in cell_rates if rate is not None]

    if not plausible_cells:
        rate = _extract_rate(context, product)
        if rate is None:
            return []
        return [(rate, _rate_category(product, context, special_label), None, context)]

    if len(plausible_cells) == 1:
        idx, cell, rate = plausible_cells[0]
        own_header = header_row[idx] if header_row is not None and idx < len(header_row) else None
        hint = _rate_category_hint(product, cell, special_label)
        if hint is None and own_header is not None:
            hint = _rate_category_hint(product, own_header, special_label)
        if hint is None:
            hint = _rate_category(product, context, special_label)
        return [(rate, hint, own_header, context)]

    plausible_indices = {idx for idx, _, _ in plausible_cells}
    results: list[tuple[float, str, str | None, str]] = []
    for idx, cell, rate in plausible_cells:
        own_header = header_row[idx] if header_row is not None and idx < len(header_row) else None
        hint = _rate_category_hint(product, cell, special_label)
        if hint is None and own_header is not None:
            hint = _rate_category_hint(product, own_header, special_label)
        if hint is None:
            hint = _rate_category(product, "", special_label)
        own_context = _clean_text(
            " | ".join(
                other_cell
                for other_idx, other_cell in enumerate(row)
                if other_idx == idx or other_idx not in plausible_indices
            )
        )
        results.append((rate, hint, own_header, own_context))
    return results


#: Matches just the term phrase itself (e.g. "6 months", "one year"), not a
#: whole cell -- shared by _find_term_label() (which cell has a term at all)
#: and _strip_term_label() (which narrower substring to remove before
#: frequency normalisation). A bank publishing term and frequency in one
#: combined cell, e.g. "6 months, interest paid monthly", means the cell
#: _find_term_label() returns is not just the term: stripping the whole cell
#: there would destroy the genuine frequency information along with it.
_TERM_PHRASE_PATTERN = re.compile(
    r"\b\d+\s*(?:day|days|month|months|year|years)\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:month|months|year|years)\b",
    re.IGNORECASE,
)


def _find_term_label(row: list[str], product: ProductType) -> str | None:
    for cell in row:
        text = cell.lower()
        if product == ProductType.HOME_LOAN and any(
            word in text for word in ("floating", "variable", "revolving", "flexible")
        ):
            return cell
        if _TERM_PHRASE_PATTERN.search(text):
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
        # Rate-first layouts (Westpac, row[0]="5.59% p.a.") must not embed a
        # mutable percentage in identity: every reprice would become a
        # new/removed product pair. A candidate cell is only used when it
        # does not itself carry a rate percentage -- a product-first row
        # (ASB's "Better Homes Top Up | 1.00% p.a. Fixed for 36 months") can
        # have the category word land inside its rate cell, which is
        # exactly the mutable value this lookup exists to avoid.
        matched = next(
            (
                cell
                for cell in row
                if category.lower() in cell.lower() and not _RATE_PERCENTAGE_PATTERN.search(cell)
            ),
            None,
        )
        if matched is not None:
            return matched
        # No clean category-label cell. _find_term_label() picks the stable
        # term cell (e.g. Westpac's "1 year") for a genuinely rate-first row
        # -- but it is just as able to return a rate-bearing cell itself
        # (the ASB row above: its term/rate share one cell), so it is only
        # safe to use when row[0] confirms the row really is rate-first. A
        # product-first row's own row[0] is the stable choice instead.
        if _RATE_PERCENTAGE_PATTERN.search(row[0]):
            return _find_term_label(row, product) or row[0]
        return row[0]
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
