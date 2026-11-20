"""Browser readiness evidence for rendered bank rate pages (DATA-007).

A successful navigation is not proof of a rate table. The old readiness signal -- any table
cell containing a percentage -- was satisfied by fee tables, marketing percentages and the
first rows of a table still being populated. Readiness is now judged from the same evidence
the collection itself depends on:

* **Rate rows.** The bank's own parser, run on the rendered DOM, must find at least the
  bank's minimum number of *comparable* rows for the requested product (fund returns, PIE
  effective-rate columns and fenced products do not count). Fewer than that is ``PARTIAL``.
* **Product evidence.** The page's title, headings or table captions must name the requested
  product. A page that only names the other product is ``WRONG_PRODUCT``, so a home-loan
  collection is never "ready" because term-deposit rates are visible, and vice versa.

The checks run in Python on ``page.content()`` snapshots, so they are deterministic and
unit-testable with saved HTML. Nothing here fetches anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
import re

from balance_pricing_engine.competitor_rates.models import ProductType


class ReadinessState(str, Enum):
    READY = "READY"
    #: Some comparable rows rendered, but fewer than the bank's minimum.
    PARTIAL = "PARTIAL"
    #: No comparable rows yet.
    NOT_READY = "NOT_READY"
    #: Rows rendered, but the page does not name the requested product.
    WRONG_PRODUCT = "WRONG_PRODUCT"


#: Phrases that identify each product in a page's title/headings/captions. Measured on the
#: saved live snapshots (2026-08-25 to 2026-11-16, five banks): every home-loan page title
#: names "home loan" and none names a deposit phrase; every deposit page title names "term
#: deposit" or "investment" and none names "home loan" or "mortgage".
PRODUCT_EVIDENCE_PHRASES: dict[ProductType, tuple[str, ...]] = {
    ProductType.HOME_LOAN: ("home loan", "mortgage"),
    ProductType.TERM_DEPOSIT: ("term deposit", "term investment", "investment", "term pie"),
}

_EVIDENCE_TAGS = frozenset({"title", "h1", "h2", "h3", "h4", "caption"})


class _EvidenceTextParser(HTMLParser):
    """Collects the text of title/heading/caption elements only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _EVIDENCE_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _EVIDENCE_TAGS and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth:
            self.parts.append(data)


def product_evidence(html: str) -> set[ProductType]:
    """Products named in the page's title, headings (h1-h4) or table captions."""
    parser = _EvidenceTextParser()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).lower()
    return {
        product
        for product, phrases in PRODUCT_EVIDENCE_PHRASES.items()
        if any(phrase in text for phrase in phrases)
    }


@dataclass(frozen=True)
class ReadinessAssessment:
    """One look at a rendered page."""

    state: ReadinessState
    product_type: ProductType
    rate_rows: int
    required_rows: int
    products_named: frozenset[ProductType] = frozenset()

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY

    def describe(self) -> str:
        named = ", ".join(sorted(product.value for product in self.products_named)) or "none"
        return (
            f"{self.state.value}: {self.rate_rows} of {self.required_rows} required comparable "
            f"{self.product_type.value} rate rows; products named on page: {named}"
        )


def assess_readiness(
    html: str, product_type: ProductType, *, rate_rows: int, required_rows: int
) -> ReadinessAssessment:
    """Classify a rendered page given how many comparable rows the parser found.

    Product evidence is checked first: rows parsed from a page that names only the other
    product prove nothing about the requested one. A page naming neither product is treated
    the same way -- absence of evidence is not evidence.
    """
    named = frozenset(product_evidence(html))
    if rate_rows > 0 and product_type not in named:
        state = ReadinessState.WRONG_PRODUCT
    elif rate_rows >= required_rows:
        state = ReadinessState.READY
    elif rate_rows > 0:
        state = ReadinessState.PARTIAL
    else:
        state = ReadinessState.NOT_READY
    return ReadinessAssessment(
        state=state,
        product_type=product_type,
        rate_rows=rate_rows,
        required_rows=required_rows,
        products_named=named,
    )


class _RateCellParser(HTMLParser):
    """Finds a percentage in real ``td``/``th`` text. ``HTMLParser`` reads ``<script>``
    bodies as raw data outside any cell, so a rate written inside an inline script (common
    for client-rendered tables) is not mistaken for a rendered one."""

    _PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_cell = 0
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"td", "th"}:
            self._in_cell += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._in_cell:
            self._in_cell -= 1

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._PERCENT.search(data):
            self.found = True


def any_rate_cell(html: str) -> bool:
    """Fallback readiness when the browser fetcher has no collector-supplied check: some
    table cell shows a percentage. Weaker than the collector check, but a timeout on it is
    still a classified failure, never ignored."""
    parser = _RateCellParser()
    parser.feed(html)
    return parser.found
