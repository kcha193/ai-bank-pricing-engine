"""NZ public bank pricing intelligence.

Answers "what is happening in the NZ retail banking pricing market?" and feeds
that answer to the pricing optimiser, which separately answers "given the market
and our economics, what should we do?".

The two concerns stay apart on purpose: nothing in this package computes prices,
and the optimiser depends on it only through
:mod:`balance_pricing_engine.pricing_intelligence.strategy`.
"""

from balance_pricing_engine.pricing_intelligence.bps import (
    percent_to_bps,
    rate_change_bps,
    spread_bps,
)
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

__all__ = [
    "PricingIntelligenceConfig",
    "percent_to_bps",
    "rate_change_bps",
    "spread_bps",
]
