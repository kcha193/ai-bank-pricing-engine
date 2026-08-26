"""Pricing-engine integration: market-derived and manual competitor assumptions.

The manual path is legacy behaviour that every existing scenario depends on, so
it is asserted as strictly as the new market-derived paths.
"""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    MarketStrategyError,
    apply_market_strategy_to_config,
    resolve_competitor_rate,
)

MARKET = [("ANZ", 4.79), ("BNZ", 4.85), ("Westpac", 4.85), ("ASB", 4.89), ("Kiwibank", 4.89)]


@pytest.fixture
def snapshot(make_record):
    return build_market_snapshot(
        [make_record(bank=bank, rate_pct=rate) for bank, rate in MARKET]
    )


@pytest.fixture
def config():
    return load_config("configs/base.yaml")


# --------------------------------------------------------------- resolution


def test_manual_strategy_uses_the_supplied_rate(snapshot):
    resolved = resolve_competitor_rate(
        CompetitorRateStrategy.MANUAL, snapshot=snapshot, manual_rate=0.0509
    )

    assert resolved.competitor_loan_rate == 0.0509
    assert resolved.source_banks == ()
    assert "market intelligence not consulted" in resolved.rationale


def test_manual_strategy_needs_a_rate():
    with pytest.raises(MarketStrategyError, match="requires manual_rate"):
        resolve_competitor_rate(CompetitorRateStrategy.MANUAL)


def test_market_median_resolves_to_the_median(snapshot):
    resolved = resolve_competitor_rate(CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot)

    assert resolved.rate_pct == 4.85
    assert resolved.competitor_loan_rate == 0.0485
    assert set(resolved.source_banks) == {bank for bank, _ in MARKET}


def test_market_leader_resolves_to_the_best_rate(snapshot):
    resolved = resolve_competitor_rate(CompetitorRateStrategy.MARKET_LEADER, snapshot=snapshot)

    assert resolved.rate_pct == 4.79
    assert resolved.competitor_loan_rate == 0.0479
    assert "ANZ" in resolved.rationale


def test_resolved_assumption_as_of_reflects_the_snapshot_not_the_leading_bank(make_record):
    """entries[0] is whichever bank leads on rate, not the snapshot's own as_of."""
    day_one = datetime(2026, 8, 20, tzinfo=timezone.utc)
    day_two = day_one + timedelta(days=1)
    lopsided_snapshot = build_market_snapshot(
        [
            make_record(bank="ANZ", rate_pct=4.79, observed_at=day_one),  # leads, but older
            make_record(bank="BNZ", rate_pct=4.85, observed_at=day_two),  # confirmed later
        ]
    )

    resolved = resolve_competitor_rate(
        CompetitorRateStrategy.MARKET_LEADER, snapshot=lopsided_snapshot
    )

    assert lopsided_snapshot.as_of == day_two
    assert resolved.as_of == day_two


def test_market_second_lowest_excludes_the_leader(snapshot):
    resolved = resolve_competitor_rate(
        CompetitorRateStrategy.MARKET_SECOND_LOWEST, snapshot=snapshot
    )

    assert resolved.rate_pct == 4.85


def test_market_mean_resolves_to_the_mean(snapshot):
    resolved = resolve_competitor_rate(CompetitorRateStrategy.MARKET_MEAN, snapshot=snapshot)

    assert resolved.rate_pct == pytest.approx(4.854)


def test_offset_expresses_leader_minus_five_bps(snapshot):
    resolved = resolve_competitor_rate(
        CompetitorRateStrategy.MARKET_LEADER, snapshot=snapshot, offset_bps=-5
    )

    assert resolved.rate_pct == pytest.approx(4.74)
    assert resolved.competitor_loan_rate == 0.0474
    assert "offset -5 bps" in resolved.rationale


def test_a_market_strategy_requires_a_snapshot():
    with pytest.raises(MarketStrategyError, match="requires a market snapshot"):
        resolve_competitor_rate(CompetitorRateStrategy.MARKET_MEDIAN)


def test_a_market_strategy_requires_data_at_the_term(snapshot):
    with pytest.raises(MarketStrategyError, match="requires advertised rates at 36 months"):
        resolve_competitor_rate(
            CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot, term_months=36
        )


def test_second_lowest_fails_clearly_when_every_bank_is_tied(make_record):
    tied = build_market_snapshot(
        [make_record(bank=bank, rate_pct=4.79) for bank in ("ANZ", "BNZ")]
    )

    with pytest.raises(MarketStrategyError, match="tied"):
        resolve_competitor_rate(CompetitorRateStrategy.MARKET_SECOND_LOWEST, snapshot=tied)


def test_an_extreme_offset_is_rejected(snapshot):
    with pytest.raises(MarketStrategyError, match="non-positive"):
        resolve_competitor_rate(
            CompetitorRateStrategy.MARKET_LEADER, snapshot=snapshot, offset_bps=-1000
        )


def test_strategies_accept_plain_strings(snapshot):
    assert (
        resolve_competitor_rate("MARKET_MEDIAN", snapshot=snapshot).rate_pct == 4.85
    )


# ------------------------------------------------------------ config overlay


def test_manual_leaves_the_config_completely_untouched(config):
    result = apply_market_strategy_to_config(config, CompetitorRateStrategy.MANUAL)

    assert result is config, "the legacy path must not even copy the config"
    # Compare against the loaded config rather than a literal: the invariant is
    # that MANUAL changes nothing, not that base.yaml holds any given rate.
    assert result.market.competitor_loan_rate == config.market.competitor_loan_rate


def test_manual_is_the_default_strategy(config):
    assert apply_market_strategy_to_config(config) is config


def test_market_median_overlays_the_competitor_rate(config, snapshot):
    result = apply_market_strategy_to_config(
        config, CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot
    )

    assert result.market.competitor_loan_rate == 0.0485
    assert result is not config
    assert config.market.competitor_loan_rate != 0.0485, "the original config is unchanged"


def test_market_leader_overlays_the_competitor_rate(config, snapshot):
    result = apply_market_strategy_to_config(
        config, CompetitorRateStrategy.MARKET_LEADER, snapshot=snapshot
    )

    assert result.market.competitor_loan_rate == 0.0479


def test_the_spread_anchor_moves_with_the_competitor_rate_by_default(config, snapshot):
    result = apply_market_strategy_to_config(
        config, CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot
    )

    assert result.market.home_loan_reference_rate == 0.0485


def test_the_spread_anchor_can_be_left_alone(config, snapshot):
    result = apply_market_strategy_to_config(
        config,
        CompetitorRateStrategy.MARKET_MEDIAN,
        snapshot=snapshot,
        set_reference_rate=False,
    )

    assert result.market.competitor_loan_rate == 0.0485
    assert result.market.home_loan_reference_rate == config.market.home_loan_reference_rate


def test_the_deposit_side_is_untouched_by_phase_one(config, snapshot):
    result = apply_market_strategy_to_config(
        config, CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot
    )

    assert result.market.competitor_deposit_rate == config.market.competitor_deposit_rate


def test_the_overlaid_config_is_revalidated(config, snapshot):
    """An overlay must not be able to smuggle in an invalid config."""
    result = apply_market_strategy_to_config(
        config, CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot
    )

    assert type(config).model_validate(result.model_dump())


def test_an_overlaid_config_still_drives_the_optimiser(config, snapshot):
    """The overlay must produce a config the existing engine can actually run."""
    from balance_pricing_engine.rate_grids import build_configured_rate_grids

    result = apply_market_strategy_to_config(
        config, CompetitorRateStrategy.MARKET_LEADER, snapshot=snapshot
    )
    loan_grid, deposit_grid = build_configured_rate_grids(result)

    assert len(loan_grid) > 0
    assert len(deposit_grid) > 0
    assert min(loan_grid) > 0
