"""Market-derived competitor assumptions for the pricing engine.

This is the *only* seam between pricing intelligence and pricing optimisation.
The optimiser keeps asking the same question it always asked -- "what is the
competitor rate?" -- and this module decides how that number is sourced. No
strategy logic leaks into the grid search, the profit model, or the scenario
runner.

``MANUAL`` is the default and leaves configuration untouched, so every existing
scenario, config file, and test keeps its current behaviour.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from balance_pricing_engine.competitor_rates.apply import (
    CompetitorRateResolutionError,
    revalidate_config,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.bps import bps_to_percent
from balance_pricing_engine.pricing_intelligence.rbnz import OcrSeries
from balance_pricing_engine.pricing_intelligence.rbnz import _parse_period as _parse_ocr_period
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot, TermMarket


class MarketStrategyError(CompetitorRateResolutionError):
    """A market-derived competitor assumption could not be resolved.

    Subclasses the existing competitor-rate error so delivery layers that
    already map that failure to a 422 keep working unchanged.
    """


def _require_snapshot_product(
    snapshot: Optional[MarketSnapshot], expected: ProductType, side: str
) -> None:
    """Reject a snapshot overlaid onto the wrong side.

    ``MarketSnapshot.product_type`` also drives ranking direction (deposits
    rank highest-first, loans lowest-first) via :class:`TermMarket`, so a
    mismatched snapshot would not just misattribute the rate -- MARKET_LEADER
    could resolve the opposite product's benchmark entirely.
    """
    if snapshot is None:
        return
    actual = ProductType(snapshot.product_type)
    if actual is not expected:
        raise MarketStrategyError(
            f"{side} snapshot must be a {expected.value} market view, got {actual.value}"
        )


class CompetitorRateStrategy(str, Enum):
    """How the optimiser's competitor rate assumption is sourced."""

    #: Use the rate already in the config. Legacy behaviour, and the default.
    MANUAL = "MANUAL"
    #: The median advertised rate across tracked banks at the chosen term.
    MARKET_MEDIAN = "MARKET_MEDIAN"
    #: The most competitive advertised rate (lowest for loans).
    MARKET_LEADER = "MARKET_LEADER"
    #: The best rate excluding banks tied at rank 1 -- a more robust target than
    #: the leader when one bank is running a short-lived promotion.
    MARKET_SECOND_LOWEST = "MARKET_SECOND_LOWEST"
    #: The arithmetic mean across tracked banks.
    MARKET_MEAN = "MARKET_MEAN"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ResolvedCompetitorAssumption:
    """A competitor rate assumption plus the evidence behind it."""

    strategy: CompetitorRateStrategy
    #: Decimal rate ready for ``MarketConfig`` (0.0479 for 4.79%).
    competitor_loan_rate: float
    #: The same figure in advertised percent, for reporting.
    rate_pct: float
    term_months: Optional[int]
    offset_bps: float
    source_banks: tuple[str, ...]
    as_of: Optional[datetime]
    rationale: str

    def summary(self) -> str:
        return (
            f"{self.strategy.value} @ {self.term_months}m -> {self.rate_pct:.2f}% "
            f"({self.rationale})"
        )


def resolve_competitor_rate(
    strategy: CompetitorRateStrategy | str = CompetitorRateStrategy.MANUAL,
    snapshot: Optional[MarketSnapshot] = None,
    term_months: int = 12,
    manual_rate: Optional[float] = None,
    offset_bps: float = 0.0,
) -> ResolvedCompetitorAssumption:
    """Resolve the competitor rate assumption for one term.

    ``offset_bps`` shifts the resolved market rate, which is what expresses a
    scenario such as "market leader minus 5 bps" without inventing a strategy
    for every possible offset.

    ``manual_rate`` is a decimal rate (0.0509), matching ``MarketConfig``;
    market-derived rates are read in advertised percent and converted here, so
    the percent/decimal boundary lives in exactly one place.
    """
    strategy = CompetitorRateStrategy(strategy)

    if strategy is CompetitorRateStrategy.MANUAL:
        if manual_rate is None:
            raise MarketStrategyError("MANUAL competitor-rate strategy requires manual_rate")
        return ResolvedCompetitorAssumption(
            strategy=strategy,
            competitor_loan_rate=manual_rate,
            rate_pct=manual_rate * 100,
            term_months=term_months,
            offset_bps=0.0,
            source_banks=(),
            as_of=None,
            rationale="manual scenario assumption; market intelligence not consulted",
        )

    if snapshot is None:
        raise MarketStrategyError(f"{strategy.value} requires a market snapshot")
    term_market = snapshot.term(term_months)
    if term_market is None or not term_market.entries:
        raise MarketStrategyError(
            f"{strategy.value} requires advertised rates at {term_months} months; "
            f"snapshot covers {list(snapshot.term_months)}"
        )

    base_pct, rationale = _resolve_market_rate(strategy, term_market)
    rate_pct = base_pct + bps_to_percent(offset_bps)
    if rate_pct <= 0:
        raise MarketStrategyError(
            f"{strategy.value} with an offset of {offset_bps:+.0f} bps produces a "
            f"non-positive competitor rate ({rate_pct:.3f}%)"
        )
    if offset_bps:
        rationale = f"{rationale}, offset {offset_bps:+.0f} bps"

    return ResolvedCompetitorAssumption(
        strategy=strategy,
        # Rounded so a config snapshot reads 0.0485 rather than
        # 0.048499999999999995; 8dp is far finer than any advertised rate.
        competitor_loan_rate=round(rate_pct / 100, 8),
        rate_pct=rate_pct,
        term_months=term_months,
        offset_bps=offset_bps,
        source_banks=tuple(entry.bank for entry in term_market.entries),
        # The snapshot's own as_of, not any one bank's observed_at: entries
        # are ordered by competitiveness, not time, so entries[0] is simply
        # whichever bank leads on rate -- not the timestamp of the market
        # view this assumption was actually resolved from.
        as_of=snapshot.as_of,
        rationale=rationale,
    )


def _resolve_market_rate(
    strategy: CompetitorRateStrategy, term_market: TermMarket
) -> tuple[float, str]:
    banks = term_market.bank_count
    if strategy is CompetitorRateStrategy.MARKET_MEDIAN:
        return term_market.median_rate, f"median advertised rate across {banks} banks"
    if strategy is CompetitorRateStrategy.MARKET_MEAN:
        return term_market.mean_rate, f"mean advertised rate across {banks} banks"
    if strategy is CompetitorRateStrategy.MARKET_LEADER:
        leaders = ", ".join(term_market.market_leaders)
        return term_market.best_rate, f"market-leading advertised rate ({leaders})"
    if strategy is CompetitorRateStrategy.MARKET_SECOND_LOWEST:
        second = term_market.second_best_rate
        if second is None:
            raise MarketStrategyError(
                "MARKET_SECOND_LOWEST requires at least one bank outside the leading "
                f"rate; all {banks} tracked banks are tied at {term_market.best_rate:.2f}%"
            )
        return second, f"best advertised rate excluding the {len(term_market.market_leaders)} leader(s)"
    raise MarketStrategyError(f"unsupported competitor-rate strategy: {strategy}")


def apply_market_strategy_to_config(
    config,
    strategy: CompetitorRateStrategy | str = CompetitorRateStrategy.MANUAL,
    snapshot: Optional[MarketSnapshot] = None,
    term_months: int = 12,
    offset_bps: float = 0.0,
    set_reference_rate: bool = True,
    deposit_snapshot: Optional[MarketSnapshot] = None,
    deposit_strategy: Optional[CompetitorRateStrategy | str] = None,
    deposit_term_months: Optional[int] = None,
    deposit_offset_bps: float = 0.0,
):
    """Return ``config`` with its loan-side competitor assumption resolved.

    Under ``MANUAL`` the config is returned unchanged -- byte-for-byte the
    existing behaviour, so manual scenarios are unaffected by this layer.

    Pass ``deposit_snapshot`` to overlay the deposit side too. Each side is
    resolved independently: a market-derived loan rate with a manual deposit
    assumption is a legitimate scenario, and omitting both ``deposit_snapshot``
    and ``deposit_strategy`` leaves ``market.competitor_deposit_rate`` exactly
    as the config holds it. Passing ``deposit_strategy`` as a market strategy
    without a ``deposit_snapshot`` raises, the same as the loan side raises
    for ``strategy`` without ``snapshot`` -- a requested market strategy must
    not silently fall back to the stale configured rate.

    When ``set_reference_rate`` is true the market-relative spread anchor
    (``home_loan_reference_rate``) is moved with the competitor rate, so
    ``pricing_strategy.home_loan_spread_bps`` reads as spread-to-market. This
    mirrors :func:`competitor_rates.apply.apply_competitor_rates_to_config`.
    Set it false to keep spreads anchored to an independent benchmark.
    """
    resolved_strategy = CompetitorRateStrategy(strategy)
    resolved_deposit_strategy = (
        CompetitorRateStrategy(deposit_strategy)
        if deposit_strategy is not None
        else resolved_strategy
    )
    # Only a side that will actually be resolved from its snapshot needs that
    # snapshot validated: MANUAL never consults it (resolve_competitor_rate()
    # short-circuits before touching snapshot), so an irrelevant or even
    # wrong-typed snapshot passed alongside MANUAL must not block the
    # documented "MANUAL returns the config unchanged" behaviour -- or, on the
    # deposit side, an otherwise-valid market overlay on the other side.
    if resolved_strategy is not CompetitorRateStrategy.MANUAL:
        _require_snapshot_product(snapshot, ProductType.HOME_LOAN, "loan-side")
    if resolved_deposit_strategy is not CompetitorRateStrategy.MANUAL:
        _require_snapshot_product(deposit_snapshot, ProductType.TERM_DEPOSIT, "deposit-side")

    resolved = resolve_competitor_rate(
        strategy=strategy,
        snapshot=snapshot,
        term_months=term_months,
        manual_rate=config.market.competitor_loan_rate,
        offset_bps=offset_bps,
    )

    updates: dict = {}
    if resolved.strategy is not CompetitorRateStrategy.MANUAL:
        updates["competitor_loan_rate"] = resolved.competitor_loan_rate
        if set_reference_rate and config.market.home_loan_reference_rate is not None:
            updates["home_loan_reference_rate"] = resolved.competitor_loan_rate

    # Attempted whenever the caller expressed any intent for the deposit
    # side -- a snapshot, an explicit strategy, or both -- not only when a
    # snapshot happens to be present. Otherwise an explicit deposit_strategy
    # (e.g. MARKET_MEDIAN) with no deposit_snapshot would skip resolution
    # silently instead of failing the way the loan side already does; the
    # resolve_competitor_rate() call below is what raises "requires a market
    # snapshot" for that case.
    if deposit_snapshot is not None or deposit_strategy is not None:
        resolved_deposit = resolve_competitor_rate(
            strategy=deposit_strategy if deposit_strategy is not None else strategy,
            snapshot=deposit_snapshot,
            term_months=(
                deposit_term_months if deposit_term_months is not None else term_months
            ),
            manual_rate=config.market.competitor_deposit_rate,
            offset_bps=deposit_offset_bps,
        )
        if resolved_deposit.strategy is not CompetitorRateStrategy.MANUAL:
            updates["competitor_deposit_rate"] = resolved_deposit.competitor_loan_rate
            if (
                set_reference_rate
                and config.market.term_deposit_reference_rate is not None
            ):
                updates["term_deposit_reference_rate"] = (
                    resolved_deposit.competitor_loan_rate
                )

    # No side resolved to a market rate: MANUAL must return the very same
    # object, not a copy, so existing scenarios are untouched.
    if not updates:
        return config

    updated_config = config.model_copy(
        update={"market": config.market.model_copy(update=updates)}
    )
    return revalidate_config(updated_config, context="market-intelligence overlay")


class CashRateStrategy(str, Enum):
    """How the optimiser's wholesale funding-cost anchor is sourced.

    A second, independent seam from :class:`CompetitorRateStrategy`: the OCR
    comes from RBNZ, not the bank collectors, and feeds a different
    ``MarketConfig`` field (``cash_rate``, not a competitor rate). Kept as its
    own strategy/resolver/overlay rather than folded into
    :func:`apply_market_strategy_to_config` -- that function already has two
    independent sides (loan, deposit); a third, unrelated data source belongs
    in its own function, not a longer parameter list on that one.
    """

    #: Use the rate already in the config. Legacy behaviour, and the default.
    MANUAL = "MANUAL"
    #: The most recently published RBNZ Official Cash Rate.
    RBNZ_OCR = "RBNZ_OCR"

    def __str__(self) -> str:
        return self.value


# RBNZ's Monetary Policy Committee reviews the OCR roughly every six weeks
# (seven times a year), and the dashboard's "End of Month" series is normally
# refreshed within the first couple of weeks of the following month -- so the
# newest observation is ordinarily well under a month old by the time it is
# fetched. A gap much larger than that means the fetch didn't actually pick up
# a current series (a stalled page, a parser that silently stopped updating),
# and pricing off it would carry a rate that could already be one or more OCR
# reviews out of date without any signal that it happened.
MAX_OCR_OBSERVATION_AGE_DAYS = 70


def _ocr_period_end(period: str) -> datetime:
    """The last calendar day of an ``OcrObservation.period`` (e.g. "Aug-2026")."""
    try:
        year, month = _parse_ocr_period(period)
    except ValueError as exc:
        raise MarketStrategyError(f"could not parse OCR observation period {period!r}") from exc
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ResolvedCashRateAssumption:
    """A cash-rate assumption plus the evidence behind it."""

    strategy: CashRateStrategy
    #: Decimal rate ready for ``MarketConfig.cash_rate`` (0.025 for 2.50%).
    cash_rate: float
    rate_pct: float
    #: RBNZ's own period label for the observation this came from (e.g.
    #: "Aug-2026"), or None under MANUAL.
    as_of_period: Optional[str]
    rationale: str

    def summary(self) -> str:
        return f"{self.strategy.value} -> {self.rate_pct:.2f}% ({self.rationale})"


def resolve_cash_rate(
    strategy: CashRateStrategy | str = CashRateStrategy.MANUAL,
    ocr_series: Optional[OcrSeries] = None,
    manual_rate: Optional[float] = None,
) -> ResolvedCashRateAssumption:
    """Resolve the cash-rate assumption the optimiser should run with."""
    strategy = CashRateStrategy(strategy)

    if strategy is CashRateStrategy.MANUAL:
        if manual_rate is None:
            raise MarketStrategyError("MANUAL cash-rate strategy requires manual_rate")
        return ResolvedCashRateAssumption(
            strategy=strategy,
            cash_rate=manual_rate,
            rate_pct=manual_rate * 100,
            as_of_period=None,
            rationale="manual scenario assumption; RBNZ data not consulted",
        )

    if ocr_series is None:
        raise MarketStrategyError(f"{strategy.value} requires an OCR series")
    current = ocr_series.current
    observation_age_days = (ocr_series.captured_at_utc - _ocr_period_end(current.period)).days
    if observation_age_days > MAX_OCR_OBSERVATION_AGE_DAYS:
        raise MarketStrategyError(
            f"{strategy.value}: the newest OCR observation is for {current.period}, "
            f"{observation_age_days} days before this fetch "
            f"({ocr_series.captured_at_utc:%Y-%m-%d}) -- RBNZ's End-of-Month series looks "
            "stale (unrefreshed for longer than an OCR review cycle); verify against RBNZ's "
            "own OCR announcements before pricing off this feed"
        )
    return ResolvedCashRateAssumption(
        strategy=strategy,
        cash_rate=round(current.ocr_pct / 100, 8),
        rate_pct=current.ocr_pct,
        as_of_period=current.period,
        rationale=(
            f"RBNZ Official Cash Rate as at {current.period} (End of Month reading -- may "
            "not yet reflect an OCR review decided after that month closed)"
        ),
    )


def apply_cash_rate_strategy_to_config(
    config,
    strategy: CashRateStrategy | str = CashRateStrategy.MANUAL,
    ocr_series: Optional[OcrSeries] = None,
):
    """Return ``config`` with ``market.cash_rate`` resolved.

    Under ``MANUAL`` the config is returned unchanged -- byte-for-byte the
    existing behaviour, so manual scenarios are unaffected by this layer, the
    same invariant :func:`apply_market_strategy_to_config` holds for
    competitor rates.

    Only ``cash_rate`` moves. ``wholesale_funding_spread`` is a separate,
    still-unverified assumption -- see docs/modelling_assumptions.md -- and
    is deliberately left untouched here rather than guessed at.
    """
    resolved = resolve_cash_rate(
        strategy=strategy, ocr_series=ocr_series, manual_rate=config.market.cash_rate
    )
    if resolved.strategy is CashRateStrategy.MANUAL:
        return config

    updated_config = config.model_copy(
        update={"market": config.market.model_copy(update={"cash_rate": resolved.cash_rate})}
    )
    return revalidate_config(updated_config, context="RBNZ OCR overlay")
