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
    """Map an advertised interest payment frequency to the canonical vocabulary."""
    if not label:
        return "unknown"
    text = label.lower()
    if "maturity" in text:
        return "at_maturity"
    if "month" in text:
        return "monthly"
    if "quarter" in text:
        return "quarterly"
    if "annual" in text or "yearly" in text:
        return "annually"
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
    """Extract min/max currency amounts from a displayed balance tier."""
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
    return amounts[0], amounts[1]
