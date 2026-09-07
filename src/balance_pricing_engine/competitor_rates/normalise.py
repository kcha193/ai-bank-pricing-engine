"""Normalisation helpers for advertised competitor-rate fields."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


@dataclass(frozen=True)
class NormalisedTerm:
    raw_label: str
    months: Optional[int]
    days: Optional[int]
    special_label: Optional[str] = None


def normalise_term(label: str) -> NormalisedTerm:
    """Normalise a bank-advertised term while preserving the original label."""
    raw_label = label.strip()
    text = raw_label.lower()
    # A sign is only a sign when it is attached to the digits: "-1 year" is a
    # malformed duration, but "Fixed - 1 year" uses the dash as a separator and
    # states an ordinary 12-month term. The lookbehind keeps a numeric range
    # ("6-12 months") out too. Rejecting a separator would silently strip the
    # row's fixed term and drop the product from the market comparison.
    if re.search(r"(?<![\d.])[-−]\d|\d+\.\d+\s*(?:days?|months?|years?)\b", text):
        return NormalisedTerm(raw_label=raw_label, months=None, days=None)
    if any(word in text for word in ("floating", "revolving", "flexible")):
        return NormalisedTerm(raw_label=raw_label, months=None, days=None, special_label="floating")
    if "variable" in text:
        return NormalisedTerm(raw_label=raw_label, months=None, days=None, special_label="variable")

    days_match = re.search(r"\b(\d+)\s*days?\b", text)
    if days_match:
        days = int(days_match.group(1))
        if days == 0:
            return NormalisedTerm(raw_label=raw_label, months=None, days=None)
        return NormalisedTerm(raw_label=raw_label, months=max(round(days / 30), 1), days=days)

    months_match = re.search(r"\b(\d+)\s*months?\b", text)
    if months_match:
        return NormalisedTerm(raw_label=raw_label, months=int(months_match.group(1)) or None, days=None)

    years_match = re.search(r"\b(\d+)\s*years?\b", text)
    if years_match:
        return NormalisedTerm(raw_label=raw_label, months=int(years_match.group(1)) * 12 or None, days=None)

    for word, number in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s+months?\b", text):
            return NormalisedTerm(raw_label=raw_label, months=number, days=None)
        if re.search(rf"\b{word}\s+years?\b", text):
            return NormalisedTerm(raw_label=raw_label, months=number * 12, days=None)

    return NormalisedTerm(raw_label=raw_label, months=None, days=None)


#: Matches "annual rate"/"annual interest rate"/"annual percentage rate"/
#: "annualised rate"/"yearly rate"/"annual percentage yield"/"annual return"
#: and similar -- a common column-header phrasing that describes the *rate's
#: own annualised unit*, not a payment-frequency option. "Rate" is not the
#: only word a bank uses for this: a managed-fund/PIE row commonly labels its
#: figure "Annual Percentage Yield" (APY) or "Annual Return" instead (see
#: also the "PIE effective rate of return" columns this codebase already
#: excludes from market statistics elsewhere), so "yield"/"return" qualify
#: here too. See normalise_frequency()'s docstring for why none of these must
#: count as "annually" the way a genuine "paid annually"/"monthly or
#: annually" does. Allows up to two words between the annual/yearly
#: adjective and the ending word (covering "annual interest rate", "annual
#: percentage rate", "annual percentage yield", ...) rather than
#: special-casing just "interest"/"percentage" -- \w+ can't cross a
#: table-cell separator like " | " in the already-flattened text, so this
#: can't accidentally bridge an unrelated "annual" in one cell to a "rate"/
#: "yield"/"return" in another.
_ANNUAL_RATE_LABEL_PATTERN = re.compile(
    r"(?:annual(?:ised|ized)?|yearly)(?:\s+\w+){0,2}\s+(?:rate|yield|return)\b"
)


def normalise_frequency(label: str | None) -> str:
    """Map an advertised interest payment frequency to the canonical vocabulary.

    A row offering the depositor a choice between multiple payment options
    (e.g. "Monthly or at maturity") resolves to a stable combination of the
    matched keywords (sorted and joined, e.g. "at_maturity+monthly"), not to
    one of them picked confidently by an unordered if-chain, nor collapsed
    to plain "unknown": interest_payment_frequency is identity-bearing (see
    :meth:`~.models.CompetitorRateRecord.eligibility_group_key`), and a
    deposit row's ``product_name`` is often a fixed generic string ("Term
    deposit") with no other field to distinguish it -- either shortcut would
    collapse two rows offering genuinely different option sets (e.g.
    "Monthly or at maturity" vs "Quarterly or at maturity") onto the same
    identity, and a rate difference between them would then read as a
    conflicting observation and reject both instead of keeping two valid
    comparable rates. Sorting before joining keeps the value stable
    regardless of the order the source page happens to list the options in.
    Plain "unknown" is kept only for a row that publishes no frequency at
    all -- the same fallback as before.

    "annual"/"yearly" is matched after stripping any "annual (interest)
    rate" phrase from the text first: a deposit table commonly labels its
    rate column "Annual Interest Rate" or "Annual Rate" to describe the
    rate's own annualised unit, not a payment option -- counting that as a
    genuine "annually" would let the extractor's aligned column header
    fabricate a payment frequency the row never actually advertised, while
    a real "paid annually"/"monthly or annually" mention elsewhere in the
    same text survives the strip untouched.
    """
    if not label:
        return "unknown"
    text = label.lower()
    matches = set()
    if "maturity" in text:
        matches.add("at_maturity")
    if "month" in text:
        matches.add("monthly")
    if "quarter" in text:
        matches.add("quarterly")
    annual_text = _ANNUAL_RATE_LABEL_PATTERN.sub("", text)
    if "annual" in annual_text or "yearly" in annual_text:
        matches.add("annually")
    if not matches:
        return "unknown"
    return "+".join(sorted(matches))


_LOW_EQUITY_PATTERN = re.compile(
    r"(?:less than|under|below|<)\s*\d+(?:\.\d+)?\s*%\s*equity"
    r"|(?:lvr\s*(?:above|over|more than|>)\s*\d+(?:\.\d+)?\s*%)"
    r"|(?:(?:above|over|more than|>)\s*\d+(?:\.\d+)?\s*%\s*lvr)",
    re.I,
)

#: A negation cue anywhere in the same clause as a _LOW_EQUITY_PATTERN
#: match -- before *or after* it -- reverses its meaning: "not less than
#: 20% equity" states a minimum was met (standard), not that it was missed
#: (low-equity); "no longer available for LVR above 80%" and the
#: postpositive "LVR above 80% is unavailable" both exclude low-equity
#: borrowers rather than describing one. Checked only within the current
#: clause (see _is_low_equity()), not the whole string, so an unrelated
#: negation in an earlier or later sentence of a longer disclosure cannot
#: suppress a genuine low-equity phrase.
_LOW_EQUITY_NEGATION_PATTERN = re.compile(
    r"\bnot\b|\bno\s+longer\b|\bunavailable\b|\bexcludes?\b|\bdoes\s+not\b",
    re.I,
)

#: A clause boundary for _is_low_equity()'s per-clause negation scoping.
#:
#: A full stop or semicolon always starts a new clause -- but only when the
#: "." is a sentence-ending period, not a decimal separator: "Not available
#: at 4.99% for LVR above 80%" must keep "Not available" in the same clause
#: as the LVR match, not split it off at the "." inside "4.99".
#:
#: A comma only starts a new clause when followed by a coordinating
#: conjunction ("Not available to trusts, but LVR above 80% attracts a
#: margin" -- the ", but" introduces a genuine second clause the leading
#: negation must not reach into). An unqualified comma is deliberately
#: excluded: it is just as commonly a list separator within one clause
#: ("not less than 20%, 25% or 30% equity"), where treating it as a boundary
#: would wrongly let the negation's own clause split apart.
#:
#: A *bare* conjunction (no comma) only starts a new clause when it directly
#: restarts an LVR condition ("Maximum 90% LVR and LVR above 80% attracts a
#: margin" -- the repeated "LVR" is the second clause's own subject).
#: Without that anchor, a bare conjunction is deliberately not a boundary:
#: "LVR above 80% or 85% is unavailable" states two rate options for *one*
#: condition, and splitting at "or" would cut the trailing "is unavailable"
#: negation out of the clause it belongs to.
_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"(?<!\d)\.(?!\d)"
    r"|;"
    r"|,\s*(?:but|and|or|however|though|yet)\b"
    r"|\b(?:but|and|or|however|though|yet)\s+(?=lvr\b)",
    re.I,
)


def _low_equity_clause_spans(text: str) -> list[tuple[int, int]]:
    """Spans of every clause containing a genuine (non-negated)
    _LOW_EQUITY_PATTERN match.

    A negation anywhere in the same clause -- before *or after* the matched
    phrase -- reverses its meaning, so a parser checking only one side would
    classify the *opposite* condition as low-equity: "not less than 20%
    equity" (negation first) states a minimum was met, and "LVR above 80%
    are unavailable" (negation after) excludes low-equity borrowers rather
    than describing one. Either wording could otherwise emit a false
    ELIGIBILITY_CHANGE describing the reversed condition.

    Every match is checked, not just the first: a disclosure can combine a
    negated restriction with a genuine one in separate clauses ("not
    available for LVR above 90%. LVR above 80% attracts a margin"), and
    stopping at the first (negated) match would let the second, genuine
    clause go unclassified.

    Returns spans, not just a bool, so :func:`normalise_lvr_values` can mask
    out exactly the genuinely low-equity clause(s) rather than discarding an
    independent hard bound stated in a *different* clause of the same
    disclosure (e.g. "Maximum 90% LVR. LVR above 80% attracts a margin").
    """
    spans = []
    for match in _LOW_EQUITY_PATTERN.finditer(text):
        clause_start = 0
        # Searched over the *full* text, not text[:match.start()]: the
        # bare-conjunction alternative's (?=lvr\b) lookahead must be able to
        # see the "lvr" it anchors on, which sits at/after match.start() --
        # endpos truncation would hide it from the lookahead entirely,
        # since re treats the string as if it ended there.
        for boundary in _CLAUSE_BOUNDARY_PATTERN.finditer(text):
            if boundary.start() >= match.start():
                break
            clause_start = boundary.end()
        clause_end = len(text)
        trailing_boundary = _CLAUSE_BOUNDARY_PATTERN.search(text, match.end())
        if trailing_boundary is not None:
            clause_end = trailing_boundary.start()
        clause = text[clause_start:clause_end]
        if not _LOW_EQUITY_NEGATION_PATTERN.search(clause):
            spans.append((clause_start, clause_end))
    return spans


def _is_low_equity(text: str) -> bool:
    """Whether ``text`` genuinely describes a low-equity/high-LVR condition."""
    return bool(_low_equity_clause_spans(text))


def normalise_lvr_condition(label: str | None) -> str:
    """Map common mortgage LVR/equity disclosures to canonical labels."""
    if not label:
        return "standard"
    text = label.lower()
    if _is_low_equity(text):
        return "low_equity_margin_applies"
    if "20%" in text and "equity" in text:
        return "min_20_percent_equity"
    if "lvr" in text and ("under 80%" in text or "< 80%" in text or "<80%" in text):
        return "min_20_percent_equity"
    if "low equity" in text:
        return "low_equity_margin_applies"
    return "standard"


#: An explicit range separator directly between two dollar amounts (only
#: whitespace either side), e.g. "$10,000 to $49,999" or "$10,000-$49,999".
#: See normalise_balance()'s docstring for why ascending order alone is not
#: enough to infer a range.
_BALANCE_RANGE_SEPARATOR_PATTERN = re.compile(
    r"\$\s*[0-9][0-9,]*(?:\.\d+)?\s*(?:to|-|–|—)\s*\$"
)


def normalise_balance(label: str | None) -> tuple[float | None, float | None]:
    """Extract min/max currency amounts from a displayed balance tier.

    Two dollar figures in one string are not necessarily a range. A term-deposit
    row often carries its own minimum *and* the product-wide minimum inherited
    from the table heading, e.g.::

        "30 days Minimum balance $5,000 | 1.90% p.a. | Term Deposit Minimum balance $1,000"

    Reading those in document order gives min=5,000/max=1,000 -- an invalid
    record that the model rejects, which previously aborted the whole bank's
    parse. Two unrelated minimums can also happen to ascend, though -- e.g. a
    product-wide "minimum $1,000" followed by a row-specific "minimum
    $5,000" -- and ``max_balance_or_deposit`` is identity-bearing
    (``eligibility_group_key()``), so inventing a maximum from numeric order
    alone would let either boilerplate minimum changing turn an unchanged
    advertised rate into false new/removed product events. Amounts are
    therefore only treated as a range when an explicit range separator (a
    "to"/hyphen directly between the two dollar amounts, nothing else in
    between -- see :data:`_BALANCE_RANGE_SEPARATOR_PATTERN`) confirms it.

    Confirming a separator exists *somewhere* in the text is not enough,
    though: an unrelated leading amount before a genuine, unconnected range
    (e.g. "Minimum $1,000 | Balance $5,000 to $9,999") would otherwise still
    pair the first two amounts found -- ``$1,000``/``$5,000`` -- as if the
    range began at the leading minimum, fabricating a maximum the text never
    associated with it. The separator match is therefore anchored to start
    exactly where the first dollar amount does, so it can only confirm a
    range whose own two endpoints *are* the first two amounts found;
    otherwise the first (most specific, row-level) figure is the minimum and
    the maximum is left unknown rather than invented.
    """
    if not label:
        return None, None
    figures = list(re.finditer(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", label))
    if not figures:
        return None, None
    amounts = [float(figure.group(1).replace(",", "")) for figure in figures]
    if len(amounts) == 1:
        return amounts[0], None
    range_begins_at_first_amount = _BALANCE_RANGE_SEPARATOR_PATTERN.match(
        label, figures[0].start()
    )
    if amounts[1] > amounts[0] and range_begins_at_first_amount:
        return amounts[0], amounts[1]
    return amounts[0], None


_LVR_PATTERN = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*%\s*(?:or\s+less\s+)?lvr", re.I)
_LVR_PATTERN_PREFIXED = re.compile(r"lvr\s*(?:of\s*)?(?:<=?|under|below|up to)?\s*(\d{2,3})\s*%", re.I)
_EQUITY_PATTERN = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:or\s+more\s+)?(?:minimum\s+)?equity", re.I)
_CASHBACK_PATTERN = re.compile(
    r"(?:cash\s?back|cash\s+contribution)[^.]{0,120}|\$[\d,]+\s*cash\s?back", re.I
)
_NEW_BUSINESS_PATTERN = re.compile(
    r"\bnew\s+(?:lending|business|customers?|borrowing|home\s+loans?)\b", re.I
)
_PACKAGE_PATTERN = re.compile(
    r"[^.]*\b(?:salary\s+credit(?:ed)?|transactional?\s+account|package|bundle|"
    r"main\s+bank|everyday\s+account)\b[^.]{0,160}",
    re.I,
)


def normalise_lvr_values(text: str | None) -> tuple[float | None, float | None]:
    """Extract ``(max_lvr, min_equity)`` as decimals from an eligibility string.

    Banks express the same constraint either way round ("80% LVR" or "20%
    equity"). Whichever is published, the complement is derived so downstream
    comparisons are consistent. Returns ``(None, None)`` when neither is stated
    rather than assuming a standard-LVR product.
    """
    if not text:
        return None, None

    # These fields express upper LVR/lower equity bounds only. A genuinely
    # low-equity clause's own threshold cannot be stored here without
    # reversing its meaning -- masked out rather than disqualifying the
    # whole string, so an independent hard bound stated in a *different*
    # clause of the same disclosure (e.g. "Maximum 90% LVR. LVR above 80%
    # attracts a margin") still resolves.
    search_text = text
    for clause_start, clause_end in _low_equity_clause_spans(text):
        search_text = (
            search_text[:clause_start]
            + " " * (clause_end - clause_start)
            + search_text[clause_end:]
        )

    lvr_match = _LVR_PATTERN.search(search_text) or _LVR_PATTERN_PREFIXED.search(search_text)
    if lvr_match:
        lvr = float(lvr_match.group(1)) / 100
        if 0 < lvr <= 1:
            return round(lvr, 4), round(1 - lvr, 4)

    equity_match = _EQUITY_PATTERN.search(search_text)
    if equity_match:
        equity = float(equity_match.group(1)) / 100
        if 0 <= equity < 1:
            return round(1 - equity, 4), round(equity, 4)

    return None, None


def extract_cashback(text: str | None) -> str | None:
    """Return the advertised cashback/cash-contribution phrase, if any."""
    if not text:
        return None
    match = _CASHBACK_PATTERN.search(text)
    return match.group(0).strip() if match else None


def detect_new_business_only(text: str | None) -> bool | None:
    """Return True when a rate is advertised as new-lending only.

    Returns ``None`` (unknown) rather than ``False`` when the page says nothing,
    because absence of the phrase is not evidence the rate is open to existing
    lending.
    """
    if not text:
        return None
    return True if _NEW_BUSINESS_PATTERN.search(text) else None


def extract_package_requirement(text: str | None) -> str | None:
    """Return the phrase describing a bundling/salary-credit requirement, if any."""
    if not text:
        return None
    match = _PACKAGE_PATTERN.search(text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(0)).strip()
