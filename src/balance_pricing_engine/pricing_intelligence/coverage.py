"""Benchmark coverage and input age for market-derived competitor assumptions (DATA-003).

A market median, mean or leader computed from one bank is not a market statistic, and a
snapshot's ``as_of`` (its *newest* input) says nothing about how old the other inputs are. This
module answers two questions about one term of one product's market view:

* **Coverage** -- how many *distinct, eligible* banks contribute? The snapshot already holds one
  representative per bank per term, after the trust rules (staleness, quarantine fallbacks,
  rejected/voided readings, non-comparable products, the Phase 2b special-comparability policy)
  have been applied, so the count here is a count of banks, never of records. The operator's own
  bank is excluded, as it is from every market statistic.
* **Input age** -- when was the *oldest* contributing observation confirmed, and how old is it
  relative to an explicit evaluation time (never the wall clock during a historical
  reconstruction)?

Purely descriptive. Enforcement belongs to :mod:`.strategy`, the one seam where an assumption
reaches the optimiser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot

#: Minimum distinct eligible banks behind a market-derived benchmark. Three of the five tracked
#: NZ banks: one bank is not a market, and two cannot distinguish a median from a mean or expose
#: a single outlier. Three is the smallest set with a real median, and the same floor the peer
#: plausibility check (``PeerPlausibilityThresholds.min_peers``) uses before it trusts a median.
DEFAULT_MIN_BANKS = 3

#: An input older than this is reported as *aged* -- still inside the freshness window (and so
#: still used), but no longer recently confirmed. Two missed daily collections.
DEFAULT_AGED_INPUT_HOURS = 48.0


class CoverageStatus(str, Enum):
    #: Enough distinct eligible banks; every input recently confirmed.
    SUFFICIENT = "SUFFICIENT"
    #: Enough banks, but at least one input is older than the aged-input threshold.
    SUFFICIENT_AGED = "SUFFICIENT_AGED"
    #: Fewer distinct eligible banks than the minimum: not a market benchmark.
    INSUFFICIENT = "INSUFFICIENT"

    def __str__(self) -> str:
        return self.value


def product_phrase(product_type: ProductType | str, term_months: int) -> str:
    """``"12-month term deposits"`` / ``"12-month home loans"``."""
    noun = (
        "term deposits"
        if ProductType(product_type) is ProductType.TERM_DEPOSIT
        else "home loans"
    )
    return f"{term_months}-month {noun}"


def _banks_phrase(count: int) -> str:
    return f"{count} eligible bank" + ("" if count == 1 else "s")


@dataclass(frozen=True)
class BenchmarkCoverage:
    """How many distinct eligible banks a term's benchmark rests on, and how old its inputs are."""

    product_type: ProductType
    term_months: int
    #: Distinct contributing banks, alphabetical.
    eligible_banks: tuple[str, ...]
    min_banks: int
    #: The time ages are measured against (the snapshot's evaluation time, or ``as_of``).
    evaluated_at: datetime
    oldest_observed_at: Optional[datetime]
    newest_observed_at: Optional[datetime]
    oldest_input_age_hours: Optional[float]
    aged_input_hours: float
    status: CoverageStatus
    #: The operator's own bank had a rate here and was removed from the benchmark.
    own_bank_excluded: bool = False

    @property
    def eligible_bank_count(self) -> int:
        return len(self.eligible_banks)

    @property
    def sufficient(self) -> bool:
        return self.status is not CoverageStatus.INSUFFICIENT

    @property
    def aged(self) -> bool:
        return self.status is CoverageStatus.SUFFICIENT_AGED

    def shortfall_message(self) -> str:
        """The operator-facing reason a market-derived assumption was refused."""
        return (
            f"Insufficient competitor coverage for "
            f"{product_phrase(self.product_type, self.term_months)}: "
            f"{_banks_phrase(self.eligible_bank_count)}; minimum required: {self.min_banks}."
        )

    def describe(self) -> str:
        """One factual line: bank count against the minimum, then the oldest input's age."""
        if not self.sufficient:
            return self.shortfall_message()
        text = (
            f"{_banks_phrase(self.eligible_bank_count)} (minimum {self.min_banks}): "
            f"{', '.join(self.eligible_banks)}"
        )
        if self.oldest_input_age_hours is not None:
            text += f"; oldest input {self.oldest_input_age_hours:.0f} h old"
        if self.aged:
            text += f" (older than {self.aged_input_hours:.0f} h)"
        if self.own_bank_excluded:
            text += "; own bank excluded"
        return text


def assess_benchmark_coverage(
    snapshot: MarketSnapshot,
    term_months: int,
    *,
    min_banks: int = DEFAULT_MIN_BANKS,
    our_bank: Optional[str] = None,
    now: Optional[datetime] = None,
    aged_input_hours: float = DEFAULT_AGED_INPUT_HOURS,
) -> BenchmarkCoverage:
    """Coverage and input age for one term of ``snapshot``.

    ``now`` overrides the evaluation time; otherwise the snapshot's own ``evaluated_at`` is used,
    then its ``as_of``. Never the wall clock: a historical reconstruction must age its inputs
    against its own point in time.
    """
    if min_banks < 1:
        raise ValueError("min_banks must be at least 1")
    evaluated_at = now or snapshot.evaluated_at or snapshot.as_of
    market = snapshot.term(term_months)
    entries = market.entries if market is not None else ()
    remaining = market.excluding_bank(our_bank).entries if market is not None else ()
    banks = tuple(sorted({entry.bank for entry in remaining}))
    observed = [entry.observed_at for entry in remaining]
    oldest = min(observed, default=None)
    newest = max(observed, default=None)
    age = (evaluated_at - oldest).total_seconds() / 3600 if oldest is not None else None
    if len(banks) < min_banks:
        status = CoverageStatus.INSUFFICIENT
    elif age is not None and age > aged_input_hours:
        status = CoverageStatus.SUFFICIENT_AGED
    else:
        status = CoverageStatus.SUFFICIENT
    return BenchmarkCoverage(
        product_type=ProductType(snapshot.product_type),
        term_months=term_months,
        eligible_banks=banks,
        min_banks=min_banks,
        evaluated_at=evaluated_at,
        oldest_observed_at=oldest,
        newest_observed_at=newest,
        oldest_input_age_hours=age,
        aged_input_hours=aged_input_hours,
        status=status,
        own_bank_excluded=len(remaining) < len(entries),
    )
