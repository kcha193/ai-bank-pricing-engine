"""Reusable competitive-position and market-activity features.

These are the numeric summaries that make market intelligence usable by
something other than a human: pricing strategies consume them today, and they
are shaped to drop into a predictive model later without rework.

Sign convention, stated once and applied everywhere: ``*_bps`` deltas are
``our_rate - reference``. For a home loan a **negative** delta means we are
cheaper than the reference and therefore more competitive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
from typing import Optional, Sequence

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps, spread_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.snapshot import TermMarket


@dataclass(frozen=True)
class CompetitivePosition:
    """Where one rate sits against the advertised market at a single term."""

    term_months: int
    our_rate: float
    market_best: float
    market_median: float
    market_mean: float
    market_worst: float
    our_rate_minus_best_bps: float
    our_rate_minus_median_bps: float
    market_rank: int
    #: Competitors whose advertised rate is numerically lower than ours. For a
    #: home loan those are the competitors undercutting us.
    competitors_below_us: int
    #: Competitors whose advertised rate is numerically higher than ours.
    competitors_above_us: int
    competitors_matching_us: int
    market_spread_bps: float
    bank_count: int

    @property
    def is_market_leader(self) -> bool:
        return self.market_rank == 1

    def as_features(self) -> dict:
        """Flat feature mapping suitable for a model or a report row."""
        return asdict(self)


def compute_competitive_position(
    term_market: TermMarket,
    our_rate: float,
    our_bank: Optional[str] = None,
) -> CompetitivePosition:
    """Position ``our_rate`` against a term's advertised market.

    ``our_bank`` is excluded from the competitor counts when supplied, so a
    tracked bank is not treated as its own competitor. When it is ``None`` (the
    default for this engine, which prices a synthetic bank) every entry in the
    snapshot counts as a competitor.
    """
    lower_is_better = ProductType(term_market.product_type) is ProductType.HOME_LOAN
    competitors = [entry for entry in term_market.entries if entry.bank != our_bank]
    if not competitors:
        raise ValueError(
            f"no competitor rates available at {term_market.term_months} months"
        )

    competitor_rates = [entry.rate_pct for entry in competitors]
    best = min(competitor_rates) if lower_is_better else max(competitor_rates)
    worst = max(competitor_rates) if lower_is_better else min(competitor_rates)
    # Computed from the competitor set, not term_market's own properties:
    # those describe every entry including our own, which would let the rate
    # being positioned shift the very benchmarks it's compared against.
    competitor_median = round(median(competitor_rates), 6)
    competitor_mean = round(fmean(competitor_rates), 6)
    competitor_spread_bps = spread_bps(min(competitor_rates), max(competitor_rates))

    below = sum(1 for rate in competitor_rates if rate_change_bps(our_rate, rate) < 0)
    above = sum(1 for rate in competitor_rates if rate_change_bps(our_rate, rate) > 0)
    matching = sum(1 for rate in competitor_rates if rate_change_bps(our_rate, rate) == 0)

    # Rank among us plus the competitors, using the same competition ranking as
    # the snapshot: everyone strictly more competitive than us sits above.
    more_competitive = below if lower_is_better else above

    return CompetitivePosition(
        term_months=term_market.term_months,
        our_rate=our_rate,
        market_best=best,
        market_median=competitor_median,
        market_mean=competitor_mean,
        market_worst=worst,
        our_rate_minus_best_bps=rate_change_bps(best, our_rate),
        our_rate_minus_median_bps=rate_change_bps(competitor_median, our_rate),
        market_rank=more_competitive + 1,
        competitors_below_us=below,
        competitors_above_us=above,
        competitors_matching_us=matching,
        market_spread_bps=competitor_spread_bps,
        bank_count=len(competitors) + 1,
    )


@dataclass(frozen=True)
class MarketActivity:
    """How much repricing has happened recently, and in which direction."""

    lookback_days: int
    changes: int
    banks_changed: int
    terms_changed: int
    increases: int
    decreases: int
    mean_change_bps: float
    #: "UP", "DOWN", "MIXED" (both directions, neither dominant), or "FLAT".
    dominant_direction: str

    def as_features(self) -> dict:
        return asdict(self)


def compute_market_activity(
    changes: Sequence[RateChange],
    lookback_days: int = 7,
    now: Optional[datetime] = None,
    directional_dominance_ratio: float = 0.7,
    include_suspicious: bool = False,
) -> MarketActivity:
    """Summarise rate movements inside a lookback window.

    Only rate movements count towards activity: a new or withdrawn product is a
    material market event but is not a repricing, and mixing them would distort
    the mean movement.

    Changes flagged suspicious are excluded by default. A suspicious movement is
    more often a parser fault than a reprice, and a single bogus 300 bps jump
    would otherwise dominate the mean and push the market state to ELEVATED --
    a broken parser must not be able to fake a repricing round. Pass
    ``include_suspicious=True`` to see raw activity including unverified moves.

    ``non_comparable`` changes (subsidised/closed products, see
    ``BankPricingCollector.excluded_product_markers``) are always excluded,
    unconditionally: a green top-up repricing is not a market repricing event
    and must not be able to push the market-state classification.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    moves = [
        change
        for change in changes
        if change.is_rate_move
        and change.change_bps is not None
        and cutoff <= change.detected_at <= now
        and (include_suspicious or not change.suspicious)
        and change.rate_category != "non_comparable"
    ]

    if not moves:
        return MarketActivity(
            lookback_days=lookback_days,
            changes=0,
            banks_changed=0,
            terms_changed=0,
            increases=0,
            decreases=0,
            mean_change_bps=0.0,
            dominant_direction="FLAT",
        )

    increases = sum(1 for change in moves if change.change_bps > 0)
    decreases = sum(1 for change in moves if change.change_bps < 0)
    mean_bps = round(fmean(change.change_bps for change in moves), 4)

    if increases and increases / len(moves) >= directional_dominance_ratio:
        direction = "UP"
    elif decreases and decreases / len(moves) >= directional_dominance_ratio:
        direction = "DOWN"
    else:
        direction = "MIXED"

    return MarketActivity(
        lookback_days=lookback_days,
        changes=len(moves),
        banks_changed=len({change.bank for change in moves}),
        terms_changed=len({change.term_months for change in moves}),
        increases=increases,
        decreases=decreases,
        mean_change_bps=mean_bps,
        dominant_direction=direction,
    )


def build_pricing_features(
    term_market: TermMarket,
    our_rate: float,
    changes_7d: Sequence[RateChange],
    changes_30d: Sequence[RateChange],
    our_bank: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Combine position and recent-activity features into one flat mapping.

    This is the shape intended for downstream pricing models: one row per term,
    every value numeric or a small categorical.
    """
    position = compute_competitive_position(term_market, our_rate, our_bank=our_bank)
    activity_7d = compute_market_activity(changes_7d, lookback_days=7, now=now)
    activity_30d = compute_market_activity(changes_30d, lookback_days=30, now=now)

    features = position.as_features()
    features.update(
        {
            "changes_7d": activity_7d.changes,
            "banks_changed_7d": activity_7d.banks_changed,
            "mean_change_bps_7d": activity_7d.mean_change_bps,
            "market_direction_7d": activity_7d.dominant_direction,
            "changes_30d": activity_30d.changes,
            "banks_changed_30d": activity_30d.banks_changed,
            "mean_change_bps_30d": activity_30d.mean_change_bps,
            "market_direction_30d": activity_30d.dominant_direction,
        }
    )
    return features


def filter_change_types(
    changes: Sequence[RateChange], change_types: Sequence[ChangeType]
) -> list[RateChange]:
    """Return only the changes matching the given types."""
    allowed = set(change_types)
    return [change for change in changes if change.change_type in allowed]
