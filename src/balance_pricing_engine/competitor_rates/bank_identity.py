"""Canonical identity for the tracked NZ banks (DATA-014).

One policy for every layer that names a bank in configuration: ``our_bank``, ``selected_banks``
and the legacy path's ``bank_specific`` setting. Records carry the collector's canonical name
(``BankPricingCollector.bank``), so canonicalising configuration onto that name is what makes
own-bank exclusion and bank filtering work.

The registry is the collector registry itself: each collector declares its canonical ``bank``
and the explicit ``bank_aliases`` operators commonly write ("ASB Bank", "Bank of New Zealand").
Matching is exact after case-folding and whitespace collapsing -- no fuzzy or prefix matching,
so an unknown institution can never be mapped onto a tracked bank. An unknown or ambiguous name
raises :class:`UnknownBankError` (a ``ValueError``, so Pydantic reports it as a validation error).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Optional


class UnknownBankError(ValueError):
    """A bank name that is neither a canonical tracked bank nor one of its declared aliases."""


def _normalise(name: str) -> str:
    return " ".join(name.split()).casefold()


@lru_cache(maxsize=1)
def _alias_map() -> dict[str, str]:
    # Imported lazily: configuration modules import this one, and the collector package
    # imports configuration.
    from balance_pricing_engine.competitor_rates.collectors import COLLECTOR_CLASSES

    owners: dict[str, set[str]] = {}
    for collector in COLLECTOR_CLASSES:
        for name in (collector.bank, *collector.bank_aliases):
            owners.setdefault(_normalise(name), set()).add(collector.bank)
    ambiguous = sorted(alias for alias, banks in owners.items() if len(banks) > 1)
    if ambiguous:
        raise RuntimeError(f"bank aliases claimed by more than one collector: {ambiguous}")
    return {alias: next(iter(banks)) for alias, banks in owners.items()}


def canonical_banks() -> tuple[str, ...]:
    """Every canonical tracked bank, sorted."""
    return tuple(sorted(set(_alias_map().values())))


def canonical_bank(name: str) -> str:
    """The canonical bank for ``name``, or :class:`UnknownBankError`."""
    canonical = _alias_map().get(_normalise(name)) if isinstance(name, str) else None
    if canonical is None:
        raise UnknownBankError(
            f"unknown bank {name!r}; expected one of {list(canonical_banks())} "
            "or a declared alias (e.g. 'ASB Bank')"
        )
    return canonical


def canonical_bank_or_none(name: str) -> Optional[str]:
    try:
        return canonical_bank(name)
    except UnknownBankError:
        return None


def same_bank(a: str, b: str) -> bool:
    """Whether two names identify one institution.

    Both tracked: compared by canonical identity. Otherwise (an untracked name in a test or a
    third-party record) a case- and whitespace-insensitive comparison, never an alias guess.
    """
    canonical_a, canonical_b = canonical_bank_or_none(a), canonical_bank_or_none(b)
    if canonical_a is not None and canonical_b is not None:
        return canonical_a == canonical_b
    return _normalise(a) == _normalise(b)


def canonicalise_bank_list(names: Iterable[str]) -> list[str]:
    """Canonical names in the given order; an unknown name or a duplicate institution raises."""
    result: list[str] = []
    for name in names:
        canonical = canonical_bank(name)
        if canonical in result:
            raise UnknownBankError(f"{name!r} duplicates {canonical!r}: one institution listed twice")
        result.append(canonical)
    return result
