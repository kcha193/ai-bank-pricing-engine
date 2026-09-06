"""Independent audit regressions for unsafe benchmark and numeric inputs."""

from datetime import datetime, timezone

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.positioning import compute_competitive_position
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import (
    MarketStrategyError,
    apply_market_strategy_to_config,
    resolve_competitor_rate,
)


@pytest.mark.parametrize(
    "strategy, expected",
    [
        ("MARKET_MEDIAN", 0.0485),
        ("MARKET_LEADER", 0.048),
        ("MARKET_SECOND_LOWEST", 0.049),
        ("MARKET_MEAN", 0.0485),
    ],
)
def test_own_bank_is_excluded_from_service_and_overlay_benchmarks(make_record, strategy, expected):
    now = datetime.now(timezone.utc)
    rows = [
        make_record(bank=b, rate_pct=r, observed_at=now)
        for b, r in [("ASB", 4.0), ("ANZ", 4.8), ("BNZ", 4.9)]
    ]
    intelligence = PricingIntelligenceConfig(our_bank="ASB")
    config = load_config("configs/base.yaml").model_copy(
        update={"pricing_intelligence": intelligence}
    )
    with PricingIntelligenceService(config=intelligence, store=ObservationStore(":memory:")) as s:
        s.store.record_observations(rows, observed_at=now)
        snapshot = s.get_market_snapshot(now=now)
        # Public market still includes us; competitor assumptions must not.
        assert set(snapshot.banks) == {"ASB", "ANZ", "BNZ"}
        assumption = s.resolve_competitor_assumption(strategy, now=now)
        assert assumption.competitor_loan_rate == expected
        assert set(assumption.source_banks) == {"ANZ", "BNZ"}
        overlay = apply_market_strategy_to_config(config, strategy, snapshot=snapshot)
        assert overlay.market.competitor_loan_rate == expected


def test_own_bank_alone_cannot_supply_competitor_benchmark(make_record):
    now = datetime.now(timezone.utc)
    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(our_bank="ASB"), store=ObservationStore(":memory:")
    ) as s:
        s.store.record_observations([make_record(bank="ASB", observed_at=now)], observed_at=now)
        with pytest.raises(MarketStrategyError):
            s.resolve_competitor_assumption(now=now)


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -0.01])
def test_invalid_manual_assumption_is_rejected(rate):
    with pytest.raises(MarketStrategyError):
        resolve_competitor_rate("MANUAL", manual_rate=rate)


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -1.0])
def test_invalid_position_cannot_claim_market_leadership(make_record, rate):
    market = build_market_snapshot([make_record()])[12]
    with pytest.raises(ValueError):
        compute_competitive_position(market, rate)


@pytest.mark.parametrize("offset", [float("nan"), float("inf")])
def test_invalid_offset_cannot_produce_a_benchmark(make_record, offset):
    market = build_market_snapshot([make_record()])
    with pytest.raises(MarketStrategyError):
        resolve_competitor_rate("MARKET_LEADER", snapshot=market, offset_bps=offset)
