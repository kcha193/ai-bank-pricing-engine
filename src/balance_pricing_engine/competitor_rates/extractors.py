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
    skipped_row_key,
)
from balance_pricing_engine.competitor_rates.normalise import (
    NormalisedTerm,
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
        unreadable_cells = (
            _unreadable_rate_cells(row, current_header_row, product)
            if skipped_row_keys is not None and product == ProductType.TERM_DEPOSIT
            else []
        )
        # A recognised term-deposit rate cell that could not be read (e.g. "4.5O%", or "N/A"
        # under a special-rate heading) is reported like a row that failed record
        # construction: its stored predecessor is unknown this run, not withdrawn, and the
        # bank's collection is partial (DATA-001). Each cell is keyed from its own heading,
        # whether or not a sibling rate was readable: joining every heading would give it
        # whichever balance tier comes first (a dashed "Standard $1,000+" beside an
        # unreadable "Special rate $5,000+" would hide the standard's withdrawal and
        # withdraw the special instead).
        pie_table = product == ProductType.TERM_DEPOSIT and _is_pie_return_table(
            current_header_row
        )
        for idx in unreadable_cells:
            header = current_header_row[idx] if idx < len(current_header_row or []) else ""
            cell_context = _clean_text(
                " | ".join(
                    cell
                    for other, cell in enumerate(row)
                    if other == idx
                    or (
                        other not in unreadable_cells
                        and _extract_plausible_rate(cell, product) is None
                    )
                )
                + (" | " + header if header else "")
            )
            category = _skipped_cell_category(
                product, row, idx, current_header_row, context, term.special_label, pie_table
            )
            _skip_unreadable_rate(skipped_row_keys, bank, product, term, cell_context, category)
        if not rate_categories:
            if (
                not unreadable_cells
                and skipped_row_keys is not None
                and product == ProductType.TERM_DEPOSIT
                and _looks_like_unusable_rate_row(context)
                and not _rate_not_offered(row, current_header_row)
            ):
                # No single cell can be named (e.g. "12 months special | N/A" with no special
                # heading): the row as a whole is unknown.
                row_context = (
                    _clean_text(context + " | " + " | ".join(current_header_row))
                    if current_header_row
                    else context
                )
                _skip_unreadable_rate(skipped_row_keys, bank, product, term, row_context)
            continue
        for rate, category, own_header, own_context in rate_categories:
            # Table-level PIE evidence outranks the special marker, as the per-cell "pie"
            # hint already does: a "special" in a PIE fund's return table is a fund return,
            # never a term-deposit special that could enter the ordinary benchmark.
            if pie_table and category in {"term_deposit", "special"}:
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


def _looks_like_unusable_rate_row(context: str) -> bool:
    """A term row that advertises a rate (a percent sign, or a special) we could not read."""
    lowered = context.lower()
    return "%" in lowered or "special" in lowered


#: Cell values that say a rate is not offered at this term, rather than unreadable.
_DASH_CELLS = frozenset({"-", "–", "—"})
_NOT_OFFERED_CELLS = _DASH_CELLS | {""}
#: A placeholder under a special-rate heading is short ("N/A", "TBC").
_PLACEHOLDER_MAX_WORDS = 2
#: A heading that names a special but describes it rather than carrying its rate ("Special
#: conditions", "Special offer details"): its cells are evidence, never a rate, however short.
#: A heading that also names a rate ("Special rate (available online)") stays a rate column:
#: dropping a placeholder there would turn an unknown rate into a withdrawal.
_NON_RATE_HEADING = re.compile(
    r"\b(?:conditions?|eligib\w*|availab\w*|restrictions?|criteria|notes?|details?"
    r"|descriptions?|requirements?)\b"
)
_RATE_HEADING = re.compile(r"\brates?\b|%|\bp\.?a\b|\binterest\b")
#: A whole percentage, not a fragment of a misread one ("0%" inside "4.O0%").
_WELL_FORMED_PERCENTAGE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*%")


def _descriptive_heading(header_row: list[str] | None, idx: int) -> bool:
    """Whether column ``idx`` describes the offer rather than carrying a rate (see
    :data:`_NON_RATE_HEADING`). Shared by every check that asks which cells are rate slots."""
    header = header_row[idx].lower() if header_row is not None and idx < len(header_row) else ""
    return bool(_NON_RATE_HEADING.search(header)) and not _RATE_HEADING.search(header)


def _rate_not_offered(row: list[str], header_row: list[str] | None = None) -> bool:
    """Whether a row with no readable rate says the rate is not offered.

    True when any rate slot is a dash, wherever the rate column sits ("12 months special |
    $5,000 | -", "Special | 12 months | -"), or when the first rate slot is blank. A rate slot
    is a non-term cell outside a descriptive column: a dash under "Notes" says nothing about
    the rate. "N/A" stays unknown: it could be a broken value as easily as an absence.
    """
    slots = [
        cell.strip()
        for idx, cell in enumerate(row)
        if not _TERM_PHRASE_PATTERN.search(cell) and not _descriptive_heading(header_row, idx)
    ]
    return any(cell in _DASH_CELLS for cell in slots) or (not slots or not slots[0])


def _unreadable_rate_cells(
    row: list[str], header_row: list[str] | None, product: ProductType
) -> list[int]:
    """Indices of cells advertising a term-deposit rate that could not be read.

    A cell qualifies when it holds a digit and a percentage that is not a well-formed number
    ("4.4O%"), or a short placeholder under a heading naming a special ("N/A" below
    "Special" or "Special rate", matching how :func:`_rate_category_hint` reads headings).
    A blank or dash cell means "not offered", and neither a well-formed percentage that is
    not a deposit rate ("Up to 100% of funds") nor a bare "%" label qualifies. Nothing under
    a descriptive heading ("Special conditions", "Availability") qualifies either: "Online
    only" there is a restriction to assess, not a rate that failed to parse.
    """
    found = []
    for idx, cell in enumerate(row):
        text = cell.strip()
        if text in _NOT_OFFERED_CELLS or _extract_plausible_rate(cell, product) is not None:
            continue
        header = (header_row[idx] if header_row is not None and idx < len(header_row) else "")
        header = header.lower()
        if _descriptive_heading(header_row, idx):
            continue
        malformed = (
            "%" in text
            and re.search(r"\d", text) is not None
            and not _WELL_FORMED_PERCENTAGE.search(text)
        )
        # A term or an amount ("12 Months" under "Special term", "$5,000" under "Special
        # minimum deposit") is never a rate placeholder.
        placeholder = (
            "special" in header
            and len(text.split()) <= _PLACEHOLDER_MAX_WORDS
            and not _TERM_PHRASE_PATTERN.search(text)
            and "$" not in text
        )
        if malformed or placeholder:
            found.append(idx)
    return found


def _skip_unreadable_rate(
    skipped_row_keys: list[str],
    bank: str,
    product: ProductType,
    term: NormalisedTerm,
    context: str,
    rate_category: str | None = None,
) -> None:
    """Record an unreadable term-deposit rate as unknown this run (never as a withdrawal).

    ``rate_category``, when the cell names one, keeps an unreadable standard rate from
    shielding a withdrawn special in the same balance tier, and vice versa.
    """
    logger.warning("%s %s: term row has no usable rate: %s", bank, product.value, context[:160])
    min_balance, max_balance = normalise_balance(context)
    group_key = category_agnostic_key(
        bank,
        product,
        term.months,
        term.raw_label,
        normalise_frequency(_strip_term_label(context, term.raw_label)),
        min_balance,
        max_balance,
    )
    skipped_row_keys.append(skipped_row_key(group_key, rate_category))


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
    return header_row is not None and is_pie_return_text(" ".join(header_row))


def is_pie_return_text(text: str) -> bool:
    """Whether ``text`` carries a PIE fund return-table signature.

    See :func:`_is_pie_return_table`. Shared with the deposit-special assessment so both
    layers read the same signature.
    """
    return bool(_PIE_RETURN_HEADER_PATTERN.search(text.lower()))


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
        hint = _cell_category_hint(product, cell, own_header, special_label)
        if hint is None:
            hint = _rate_category(product, context, special_label)
        return [(rate, hint, own_header, context)]

    plausible_indices = {idx for idx, _, _ in plausible_cells}
    results: list[tuple[float, str, str | None, str]] = []
    for idx, cell, rate in plausible_cells:
        own_header = header_row[idx] if header_row is not None and idx < len(header_row) else None
        hint = _cell_category_hint(product, cell, own_header, special_label)
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


def _cell_category_hint(
    product: ProductType, cell: str, own_header: str | None, special_label: str | None
) -> str | None:
    """Category a rate cell names itself, else its own column heading names, else ``None``."""
    hint = _rate_category_hint(product, cell, special_label)
    if hint is None and own_header is not None:
        hint = _rate_category_hint(product, own_header, special_label)
    return hint


def _skipped_cell_category(
    product: ProductType,
    row: list[str],
    idx: int,
    header_row: list[str] | None,
    context: str,
    special_label: str | None,
    pie_table: bool,
) -> str | None:
    """The category an unreadable rate cell would carry if it parsed, or ``None`` if unsure.

    Same rules as :func:`_extract_rates_with_categories`: the cell's own or its heading's
    category word first. Without one, a lone rate takes the row's category and one of several
    the generic default; which applies depends on how many siblings parse, so the answer is
    only kept when both agree (fixture D's "Standard rate" column is ``term_deposit`` either
    way; an unlabelled rate on a "12 Months Special" row is not).
    """
    own_header = header_row[idx] if header_row is not None and idx < len(header_row) else None
    category = _cell_category_hint(product, row[idx], own_header, special_label)
    if category is None:
        alone = _rate_category(product, context, special_label)
        if alone != _rate_category(product, "", special_label):
            return None
        category = alone
    if pie_table and category in {"term_deposit", "special"}:
        return "pie_term_deposit"
    return category


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
