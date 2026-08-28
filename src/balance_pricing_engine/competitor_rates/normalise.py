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
    if "floating" in text:
        return NormalisedTerm(raw_label=raw_label, months=None, days=None, special_label="floating")
    if "variable" in text:
        return NormalisedTerm(raw_label=raw_label, months=None, days=None, special_label="variable")

    days_match = re.search(r"\b(\d+)\s*days?\b", text)
    if days_match:
        days = int(days_match.group(1))
        return NormalisedTerm(raw_label=raw_label, months=max(round(days / 30), 1), days=days)

    months_match = re.search(r"\b(\d+)\s*months?\b", text)
    if months_match:
        return NormalisedTerm(raw_label=raw_label, months=int(months_match.group(1)), days=None)

    years_match = re.search(r"\b(\d+)\s*years?\b", text)
    if years_match:
        return NormalisedTerm(raw_label=raw_label, months=int(years_match.group(1)) * 12, days=None)

    for word, number in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s+months?\b", text):
            return NormalisedTerm(raw_label=raw_label, months=number, days=None)
        if re.search(rf"\b{word}\s+years?\b", text):
            return NormalisedTerm(raw_label=raw_label, months=number * 12, days=None)

    return NormalisedTerm(raw_label=raw_label, months=None, days=None)


def normalise_frequency(label: str | None) -> str:
    """Map an advertised interest payment frequency to the canonical vocabulary.

    A row offering the depositor a choice between multiple payment options
    (e.g. "Monthly or at maturity") is deliberately resolved to "unknown"
    rather than confidently picking one of the mentioned keywords:
    interest_payment_frequency is identity-bearing, so an unordered
    if-chain that always prefers e.g. "maturity" over "month" would collapse
    two rows offering genuinely different option sets (e.g. "Monthly or at
    maturity" vs "Quarterly or at maturity") onto the same identity, and a
    rate difference between them would then read as a conflicting
    observation and reject both instead of keeping two valid comparable
    rates. "unknown" is the same fallback already used for a row that
    publishes no frequency at all.
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
    if "annual" in text or "yearly" in text:
        matches.add("annually")
    if len(matches) == 1:
        return matches.pop()
    return "unknown"


def normalise_lvr_condition(label: str | None) -> str:
    """Map common mortgage LVR/equity disclosures to canonical labels."""
    if not label:
        return "standard"
    text = label.lower()
    if "20%" in text and "equity" in text:
        return "min_20_percent_equity"
    if "lvr" in text and ("under 80%" in text or "< 80%" in text or "<80%" in text):
        return "min_20_percent_equity"
    if "low equity" in text:
        return "low_equity_margin_applies"
    return "standard"


def normalise_balance(label: str | None) -> tuple[float | None, float | None]:
    """Extract min/max currency amounts from a displayed balance tier.

    Two dollar figures in one string are not necessarily a range. A term-deposit
    row often carries its own minimum *and* the product-wide minimum inherited
    from the table heading, e.g.::

        "30 days Minimum balance $5,000 | 1.90% p.a. | Term Deposit Minimum balance $1,000"

    Reading those in document order gives min=5,000/max=1,000 -- an invalid
    record that the model rejects, which previously aborted the whole bank's
    parse. Amounts are therefore only treated as a range when they ascend;
    otherwise the first (most specific, row-level) figure is the minimum and the
    maximum is left unknown rather than invented.
    """
    if not label:
        return None, None
    amounts = [
        float(value.replace(",", ""))
        for value in re.findall(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", label)
    ]
    if not amounts:
        return None, None
    if len(amounts) == 1:
        return amounts[0], None
    if amounts[1] > amounts[0]:
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

    lvr_match = _LVR_PATTERN.search(text) or _LVR_PATTERN_PREFIXED.search(text)
    if lvr_match:
        lvr = float(lvr_match.group(1)) / 100
        if 0 < lvr <= 1:
            return round(lvr, 4), round(1 - lvr, 4)

    equity_match = _EQUITY_PATTERN.search(text)
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
