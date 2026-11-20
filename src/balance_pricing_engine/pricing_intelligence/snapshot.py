"""Current-market view: per-term dispersion, ranking, and leadership.

Purely descriptive. Nothing here decides what any bank *should* price -- that is
the optimiser's job. This module answers "where does the market sit right now?".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from statistics import fmean, median
from typing import Iterable, Mapping, Optional

from balance_pricing_engine.competitor_rates.bank_identity import same_bank
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    LvrEligibilityBasis,
    ProductType,
)
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps, spread_bps
from balance_pricing_engine.pricing_intelligence.deposit_reference import (
    DepositReferencePolicy,
    ReferenceAssessment,
    ReferenceExclusion,
    assess_reference_eligibility,
    reference_exclusion,
)


def _better_is_lower(product_type: ProductType) -> bool:
    """Home-loan borrowers want the lowest rate; depositors want the highest."""
    return ProductType(product_type) is ProductType.HOME_LOAN


def _banks_match(a: str, b: str) -> bool:
    """Whether two bank names identify one institution (DATA-014).

    Delegates to :func:`~balance_pricing_engine.competitor_rates.bank_identity.same_bank`, the
    one identity policy: tracked banks compare by canonical identity (an alias is its bank), so a
    direct caller passing an alias still excludes the bank's own advertised rate from
    MARKET_MEDIAN/MARKET_LEADER. ``PricingIntelligenceConfig.our_bank`` is canonicalised at load.
    """
    return same_bank(a, b)


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
    quarantined: bool = False
    # --- Eligibility evidence (Phase 2f, additive) ----------------------------------------
    #: Home loans: how ``max_lvr`` was established (``published``/``inferred``/``unknown``).
    lvr_basis: Optional[LvrEligibilityBasis] = None
    eligibility_inferred: bool = False
    #: Term deposits: the representative's own published terms.
    interest_payment_frequency: Optional[str] = None
    min_balance: Optional[float] = None
    max_balance: Optional[float] = None
    #: Term deposits under a reference policy: how the reference depositor's balance and
    #: payment frequency were matched. ``None`` in an all-tier view (nothing was assessed).
    balance_basis: Optional[str] = None
    frequency_basis: Optional[str] = None
    min_equity: Optional[float] = None

    @property
    def is_leader(self) -> bool:
        return self.rank == 1

    @property
    def reference_eligibility_established(self) -> bool:
        """True only when both the balance tier and the payment frequency were published."""
        return self.balance_basis == "published" and self.frequency_basis == "published"

    @property
    def eligibility_note(self) -> str:
        """A short, factual eligibility label for evidence tables and rationales."""
        if self.balance_basis is not None or self.frequency_basis is not None:
            if self.reference_eligibility_established:
                return "Reference balance tier and payment frequency published"
            gaps = []
            if self.balance_basis == "not_stated":
                gaps.append("balance tier not published")
            if self.frequency_basis == "not_stated":
                gaps.append("payment frequency not published")
            if self.frequency_basis == "fallback":
                gaps.append(f"fallback payment frequency ({self.interest_payment_frequency})")
            return "Eligibility not established: " + "; ".join(gaps)
        if self.lvr_basis is None or (self.max_lvr is None and self.min_equity is None):
            return "Eligibility not established"
        if self.max_lvr is not None:
            bound = f"{self.max_lvr:.4%}".rstrip("%").rstrip("0").rstrip(".") + "% max LVR"
        else:
            bound = f"{self.min_equity:.4%}".rstrip("%").rstrip("0").rstrip(".") + "% min equity"
        source = {
            "inferred": "inferred from special-rate category",
            "published": "published eligibility",
            "unknown": "eligibility provenance unavailable",
        }[self.lvr_basis]
        return f"{bound} — {source}"


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
            if _banks_match(entry.bank, bank):
                return entry.rate_pct
        return None

    def rank_for(self, bank: str) -> Optional[int]:
        for entry in self.entries:
            if _banks_match(entry.bank, bank):
                return entry.rank
        return None

    @property
    def inferred_eligibility_banks(self) -> tuple[str, ...]:
        """Home-loan banks whose representative's LVR condition was inferred, not published."""
        return tuple(entry.bank for entry in self.entries if entry.eligibility_inferred)

    @property
    def unknown_lvr_eligibility_banks(self) -> tuple[str, ...]:
        """Home-loan banks whose representative has unknown LVR eligibility evidence."""
        return tuple(entry.bank for entry in self.entries if entry.lvr_basis == "unknown")

    @property
    def unestablished_reference_banks(self) -> tuple[str, ...]:
        """Term-deposit banks admitted without published reference-depositor eligibility."""
        return tuple(
            entry.bank
            for entry in self.entries
            if entry.balance_basis is not None and not entry.reference_eligibility_established
        )

    @property
    def eligibility_caveat_banks(self) -> tuple[str, ...]:
        """Banks whose representative rests on inferred or unestablished eligibility."""
        return tuple(sorted({
            *self.inferred_eligibility_banks,
            *self.unknown_lvr_eligibility_banks,
            *self.unestablished_reference_banks,
        }))

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
    #: The time this view describes the market *at* -- ``now`` for a live view, the run start for
    #: a reconstruction. ``as_of`` is the newest input, which says nothing about the oldest; input
    #: ages are measured against this (see :mod:`.coverage`). ``None`` for legacy callers.
    evaluated_at: Optional[datetime] = None
    #: The reference depositor this view describes (term deposits). ``None`` for an all-tier
    #: view, which is descriptive only and never a default pricing benchmark (DATA-005).
    deposit_reference: Optional[DepositReferencePolicy] = None
    #: Records the reference depositor cannot get, with the reason. Kept for audit.
    reference_exclusions: tuple[ReferenceExclusion, ...] = ()

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
    quarantined_keys: Optional[set[str]] = None,
    admitted_keys: Optional[set[str]] = None,
    evaluated_at: Optional[datetime] = None,
    deposit_reference: Optional[DepositReferencePolicy] = None,
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

    ``admitted_keys`` admits specific records outside ``rate_category`` by
    ``observation_key()`` -- the term-deposit specials that
    :mod:`.deposit_specials` found comparable. An admitted record keeps its own
    ``rate_category`` on its :class:`BankTermRate`, so a promotional
    representative stays visible. On an exact rate tie the record in
    ``rate_category`` itself is preferred, whatever the input order.

    ``evaluated_at`` records when the view was evaluated (see :attr:`MarketSnapshot.evaluated_at`);
    it never filters records.

    ``deposit_reference`` (term deposits only) restricts each bank's candidates to records the
    reference depositor can get and orders them by confirmed comparability before rate (see
    :mod:`.deposit_reference`). Records it excludes are listed in
    :attr:`MarketSnapshot.reference_exclusions`. Without it the view is all-tier: each bank's
    best rate across every tier and frequency -- descriptive, never a default benchmark.

    The representative is chosen by a total order over the record's own content, so the
    result never depends on input (HTML row) order.
    """
    product = ProductType(product_type)
    lower_is_better = _better_is_lower(product)
    allowed_banks = set(banks) if banks is not None else None
    allowed_terms = set(terms_months) if terms_months is not None else None
    excluded_markers = tuple(name.lower() for name in excluded_product_names or ())

    reference = deposit_reference if product is ProductType.TERM_DEPOSIT else None
    best_by_term_bank: dict[int, dict[str, CompetitorRateRecord]] = {}
    best_key: dict[tuple[int, str], tuple] = {}
    assessments: dict[int, ReferenceAssessment] = {}
    exclusions: list[ReferenceExclusion] = []
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
        if (
            rate_category is not None
            and record.rate_category != rate_category
            and record.observation_key() not in (admitted_keys or ())
        ):
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

        assessment: Optional[ReferenceAssessment] = None
        if reference is not None:
            assessment = assess_reference_eligibility(record, reference)
            if not assessment.eligible:
                exclusions.append(reference_exclusion(record, assessment))
                continue
            assessments[id(record)] = assessment

        bucket = best_by_term_bank.setdefault(term, {})
        key = _selection_key(record, assessment, rate_category, lower_is_better)
        if record.bank not in bucket or key < best_key[(term, record.bank)]:
            bucket[record.bank] = record
            best_key[(term, record.bank)] = key
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
                quarantined=record.observation_key() in (quarantined_keys or set()),
                **_eligibility_fields(record, product, assessments.get(id(record))),
            )
            for record, rank in zip(
                ordered, _competition_ranks([r.advertised_rate_pct for r in ordered])
            )
        )
        terms[term] = TermMarket(term_months=term, product_type=product, entries=entries)

    return MarketSnapshot(
        as_of=as_of or latest_observed or datetime.now(timezone.utc),
        product_type=product,
        terms=terms,
        rate_category=rate_category,
        evaluated_at=evaluated_at,
        deposit_reference=reference,
        reference_exclusions=tuple(
            sorted(exclusions, key=lambda e: (e.term_months, e.bank, e.observation_key))
        ),
    )


def _selection_key(
    record: CompetitorRateRecord,
    assessment: Optional[ReferenceAssessment],
    rate_category: Optional[str],
    lower_is_better: bool,
) -> tuple:
    """Total order for one bank's candidates at one term; the smallest key represents the bank.

    The reference payment-frequency match first (when assessed: preferred, then each approved
    fallback, then unstated -- different frequencies are different products), then the most
    competitive rate (exact to 1e-6 %, so float noise cannot split a tie), then the view's own
    category, then record content -- never input order. Among balance-eligible candidates the
    rate decides; an unstated balance tier is admitted and disclosed, not ranked down, so a
    Phase 2b comparable special keeps its place.
    """
    rate = round(record.advertised_rate_pct, 6)
    return (
        assessment.frequency_rank if assessment is not None else 0,
        rate if lower_is_better else -rate,
        0 if record.rate_category == rate_category else 1,
        record.product_name.casefold(),
        record.observation_key(),
    )


def _eligibility_fields(
    record: CompetitorRateRecord,
    product: ProductType,
    assessment: Optional[ReferenceAssessment],
) -> dict:
    if product is ProductType.HOME_LOAN:
        basis = record.lvr_eligibility_basis()
        return {
            "lvr_basis": basis,
            "eligibility_inferred": basis == "inferred",
            "min_equity": record.min_equity,
        }
    return {
        "interest_payment_frequency": record.interest_payment_frequency,
        "min_balance": record.min_balance_or_deposit,
        "max_balance": record.max_balance_or_deposit,
        "balance_basis": assessment.balance_basis if assessment else None,
        "frequency_basis": assessment.frequency_basis if assessment else None,
    }


def term_market_from_rates(
    term_months: int,
    rates: Mapping[str, float],
    product_type: ProductType | str = ProductType.HOME_LOAN,
) -> TermMarket:
    """Build a ``TermMarket`` from already-resolved ``{bank: rate_pct}``.

    For history charts: per-date market statistics come from the same ranking and
    dispersion code the live snapshot uses, never from UI-side arithmetic.
    """
    product = ProductType(product_type)
    lower_is_better = _better_is_lower(product)
    ordered = sorted(rates.items(), key=lambda kv: (kv[1] if lower_is_better else -kv[1], kv[0]))
    ranks = _competition_ranks([rate for _, rate in ordered])
    entries = tuple(
        BankTermRate(
            bank=bank,
            rate_pct=rate,
            rank=rank,
            product_name="",
            rate_category="",
            source_url="",
            observed_at=datetime.min.replace(tzinfo=timezone.utc),
        )
        for (bank, rate), rank in zip(ordered, ranks)
    )
    return TermMarket(term_months=term_months, product_type=product, entries=entries)


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
