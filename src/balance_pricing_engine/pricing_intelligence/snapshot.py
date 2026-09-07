"""Current-market view: per-term dispersion, ranking, and leadership.

Purely descriptive. Nothing here decides what any bank *should* price -- that is
the optimiser's job. This module answers "where does the market sit right now?".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from statistics import fmean, median
from typing import Iterable, Optional

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps, spread_bps


def _better_is_lower(product_type: ProductType) -> bool:
    """Home-loan borrowers want the lowest rate; depositors want the highest."""
    return ProductType(product_type) is ProductType.HOME_LOAN


def _banks_match(a: str, b: str) -> bool:
    """Case/whitespace-insensitive bank-name comparison.

    ``PricingIntelligenceConfig.our_bank`` accepts an arbitrary string, not
    validated against a collector's own canonical bank name. An exact-string
    comparison against a differently-cased or whitespace-padded ``our_bank``
    would silently fail to exclude the tracked bank's own advertised rate
    from market statistics, letting it influence MARKET_MEDIAN/MARKET_LEADER
    and, through those, the resulting pricing recommendation.
    """
    return a.strip().casefold() == b.strip().casefold()


@dataclass(frozen=True)
class BankTermRate:
    """One bank's advertised rate at one term, with its market rank."""

    bank: str
    rate_pct: float
    rank: int
    product_name: str
    rate_category: str
    source_url: str
    observed_at: datetime
    max_lvr: Optional[float] = None
    cashback_text: Optional[str] = None

    @property
    def is_leader(self) -> bool:
        return self.rank == 1


@dataclass(frozen=True)
class TermMarket:
    """Market statistics for a single fixed term."""

    term_months: int
    product_type: ProductType
    entries: tuple[BankTermRate, ...]

    @property
    def bank_count(self) -> int:
        return len(self.entries)

    @property
    def rates(self) -> tuple[float, ...]:
        return tuple(entry.rate_pct for entry in self.entries)

    @property
    def lowest_rate(self) -> float:
        return min(self.rates)

    @property
    def highest_rate(self) -> float:
        return max(self.rates)

    @property
    def mean_rate(self) -> float:
        return round(fmean(self.rates), 6)

    @property
    def median_rate(self) -> float:
        return round(median(self.rates), 6)

    @property
    def spread_bps(self) -> float:
        """Dispersion between the cheapest and dearest advertised rate, in bps."""
        return spread_bps(self.lowest_rate, self.highest_rate)

    @property
    def best_rate(self) -> float:
        """The most competitive rate, respecting product direction."""
        return self.lowest_rate if _better_is_lower(self.product_type) else self.highest_rate

    @property
    def market_leaders(self) -> tuple[str, ...]:
        """Every bank holding rank 1 -- more than one when the best rate is tied."""
        return tuple(entry.bank for entry in self.entries if entry.rank == 1)

    @property
    def market_leader(self) -> Optional[str]:
        """A single representative leader, or None when the term has no entries.

        Ties are broken alphabetically so the value is deterministic; use
        :attr:`market_leaders` when the tie itself matters.
        """
        leaders = self.market_leaders
        return sorted(leaders)[0] if leaders else None

    @property
    def second_best_rate(self) -> Optional[float]:
        """The best rate excluding every bank tied at rank 1.

        ``None`` when every bank is tied at the leading rate.
        """
        contenders = [entry.rate_pct for entry in self.entries if entry.rank != 1]
        if not contenders:
            return None
        return min(contenders) if _better_is_lower(self.product_type) else max(contenders)

    def rate_for(self, bank: str) -> Optional[float]:
        for entry in self.entries:
            if entry.bank == bank:
                return entry.rate_pct
        return None

    def rank_for(self, bank: str) -> Optional[int]:
        for entry in self.entries:
            if entry.bank == bank:
                return entry.rank
        return None

    def ranking(self) -> tuple[BankTermRate, ...]:
        """Entries ordered most competitive first."""
        return self.entries

    def excluding_bank(self, bank: Optional[str]) -> "TermMarket":
        """Return external competitors with ranks recalculated after exclusion."""
        if bank is None:
            return self
        entries = tuple(entry for entry in self.entries if not _banks_match(entry.bank, bank))
        ranks = _competition_ranks([entry.rate_pct for entry in entries])
        return replace(self, entries=tuple(
            replace(entry, rank=rank) for entry, rank in zip(entries, ranks)
        ))


@dataclass(frozen=True)
class MarketSnapshot:
    """The advertised market across every tracked term at a point in time."""

    as_of: datetime
    product_type: ProductType
    terms: dict[int, TermMarket]
    rate_category: Optional[str] = None

    @property
    def banks(self) -> tuple[str, ...]:
        return tuple(
            sorted({entry.bank for market in self.terms.values() for entry in market.entries})
        )

    @property
    def term_months(self) -> tuple[int, ...]:
        return tuple(sorted(self.terms))

    def term(self, term_months: int) -> Optional[TermMarket]:
        return self.terms.get(term_months)

    def __getitem__(self, term_months: int) -> TermMarket:
        return self.terms[term_months]

    def __contains__(self, term_months: object) -> bool:
        return term_months in self.terms

    def render(self) -> str:
        """Render the bank-by-term grid used by the CLI and dashboard."""
        if not self.terms:
            return "No market data."
        terms = self.term_months
        banks = self.banks
        header = "Bank".ljust(12) + "".join(f"{_term_label(t):>9}" for t in terms)
        lines = [header, "-" * len(header)]
        for bank in banks:
            cells = []
            for term in terms:
                rate = self.terms[term].rate_for(bank)
                cells.append(f"{rate:>9.2f}" if rate is not None else f"{'-':>9}")
            lines.append(bank.ljust(12) + "".join(cells))
        lines.append("-" * len(header))
        lines.append("Median".ljust(12) + "".join(f"{self.terms[t].median_rate:>9.2f}" for t in terms))
        lines.append("Best".ljust(12) + "".join(f"{self.terms[t].best_rate:>9.2f}" for t in terms))
        lines.append("Spread bps".ljust(12) + "".join(f"{self.terms[t].spread_bps:>9.0f}" for t in terms))
        return "\n".join(lines)


def _term_label(term_months: int) -> str:
    if term_months % 12 == 0 and term_months >= 12:
        years = term_months // 12
        return f"{years}Y"
    return f"{term_months}M"


def build_market_snapshot(
    records: Iterable[CompetitorRateRecord],
    product_type: ProductType | str = ProductType.HOME_LOAN,
    rate_category: Optional[str] = "special",
    terms_months: Optional[Iterable[int]] = None,
    as_of: Optional[datetime] = None,
    banks: Optional[Iterable[str]] = None,
    excluded_product_names: Optional[Iterable[str]] = None,
) -> MarketSnapshot:
    """Build a market snapshot from a set of current observations.

    When a bank advertises several rates at the same term (for example a special
    and a standard line, or two LVR bands), the most competitive one is used, so
    each bank appears once per term. Filter with ``rate_category`` to compare
    like with like -- the default compares advertised specials, which is what
    front-book competition is actually fought on.

    ``excluded_product_names`` drops a record whose ``product_name`` contains
    any of the given markers (case-insensitive) before that selection, so a
    niche, structurally non-comparable product -- e.g. ASB's construction-only
    "Back My Build", deliberately captured under its own product name for
    exactly this purpose (see ``ASBCollector``) -- cannot win MARKET_LEADER on
    rate alone.
    """
    product = ProductType(product_type)
    lower_is_better = _better_is_lower(product)
    allowed_banks = set(banks) if banks is not None else None
    allowed_terms = set(terms_months) if terms_months is not None else None
    excluded_markers = tuple(name.lower() for name in excluded_product_names or ())

    best_by_term_bank: dict[int, dict[str, CompetitorRateRecord]] = {}
    latest_observed: Optional[datetime] = None

    for record in records:
        if ProductType(record.product_type) is not product:
            continue
        if record.rate_category == "non_comparable":
            # Retained for audit on the record itself, but never a market
            # statistic -- excluded unconditionally so an unfiltered
            # (``rate_category=None``) snapshot can't let a subsidised or
            # closed product become the bank representative, market leader,
            # or median input. See ``BankPricingCollector.excluded_product_markers``.
            continue
        if record.term_months_normalised is None:
            continue
        if rate_category is not None and record.rate_category != rate_category:
            continue
        if allowed_banks is not None and record.bank not in allowed_banks:
            continue
        if excluded_markers and any(
            marker in record.product_name.lower() for marker in excluded_markers
        ):
            continue
        term = record.term_months_normalised
        if allowed_terms is not None and term not in allowed_terms:
            continue

        bucket = best_by_term_bank.setdefault(term, {})
        incumbent = bucket.get(record.bank)
        if incumbent is None or _is_more_competitive(
            record.advertised_rate_pct, incumbent.advertised_rate_pct, lower_is_better
        ):
            bucket[record.bank] = record
        latest_observed = (
            record.captured_at_utc
            if latest_observed is None or record.captured_at_utc > latest_observed
            else latest_observed
        )

    terms: dict[int, TermMarket] = {}
    for term, by_bank in best_by_term_bank.items():
        ordered = sorted(
            by_bank.values(),
            key=lambda record: (
                record.advertised_rate_pct if lower_is_better else -record.advertised_rate_pct,
                record.bank,
            ),
        )
        entries = tuple(
            BankTermRate(
                bank=record.bank,
                rate_pct=record.advertised_rate_pct,
                rank=rank,
                product_name=record.product_name,
                rate_category=record.rate_category,
                source_url=record.source_url,
                observed_at=record.captured_at_utc,
                max_lvr=record.max_lvr,
                cashback_text=record.cashback_text,
            )
            for record, rank in zip(ordered, _competition_ranks([r.advertised_rate_pct for r in ordered]))
        )
        terms[term] = TermMarket(term_months=term, product_type=product, entries=entries)

    return MarketSnapshot(
        as_of=as_of or latest_observed or datetime.now(timezone.utc),
        product_type=product,
        terms=terms,
        rate_category=rate_category,
    )


def _is_more_competitive(candidate: float, incumbent: float, lower_is_better: bool) -> bool:
    return candidate < incumbent if lower_is_better else candidate > incumbent


def _competition_ranks(sorted_rates: list[float]) -> list[int]:
    """Standard competition ranking ("1224"): ties share a rank and skip the next.

    Five banks at 4.79 / 4.85 / 4.85 / 4.89 / 4.89 rank 1, 2, 2, 4, 4.
    """
    ranks: list[int] = []
    for index, rate in enumerate(sorted_rates):
        if index and rate_change_bps(sorted_rates[index - 1], rate) == 0:
            ranks.append(ranks[-1])
        else:
            ranks.append(index + 1)
    return ranks
