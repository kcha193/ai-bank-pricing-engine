"""Deterministic market-state classification.

Classification is rule-based on purpose. The label drives how a pricing
recommendation is framed, so it must be reproducible, explainable to a
governance forum, and identical for identical inputs -- none of which an LLM
would guarantee. The supporting metrics are always returned alongside the label
so a reader can see exactly why it was reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence

from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.config import MarketStateThresholds
from balance_pricing_engine.pricing_intelligence.positioning import (
    MarketActivity,
    compute_market_activity,
)


class MarketState(str, Enum):
    """How competitively active the advertised market currently is."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MarketStateAssessment:
    """A market-state label plus the metrics that produced it."""

    state: MarketState
    activity: MarketActivity
    rationale: str

    @property
    def changes_7d(self) -> int:
        return self.activity.changes

    @property
    def banks_changed_7d(self) -> int:
        return self.activity.banks_changed

    @property
    def mean_change_bps(self) -> float:
        return self.activity.mean_change_bps

    @property
    def dominant_direction(self) -> str:
        return self.activity.dominant_direction

    def as_dict(self) -> dict:
        """Serialisable form, matching the documented metrics payload."""
        return {
            "state": self.state.value,
            "changes_7d": self.activity.changes,
            "banks_changed_7d": self.activity.banks_changed,
            "terms_changed_7d": self.activity.terms_changed,
            "mean_change_bps": self.activity.mean_change_bps,
            "dominant_direction": self.activity.dominant_direction,
            "rationale": self.rationale,
        }


def classify_market_state(
    changes: Sequence[RateChange],
    thresholds: Optional[MarketStateThresholds] = None,
    now: Optional[datetime] = None,
) -> MarketStateAssessment:
    """Classify recent repricing activity into a market state.

    Bands are evaluated most-severe first. HIGH and ELEVATED each require both
    a change count and a breadth-of-banks condition, so one bank repricing its
    whole curve does not read as a market-wide repricing round. A single large
    average movement escalates LOW to ELEVATED, because the first bank of a
    repricing round moving 25 bps matters more than three banks moving 2 bps.
    """
    thresholds = thresholds or MarketStateThresholds()
    activity = compute_market_activity(
        changes,
        lookback_days=thresholds.lookback_days,
        now=now,
        directional_dominance_ratio=thresholds.directional_dominance_ratio,
    )

    large_move = abs(activity.mean_change_bps) >= thresholds.large_mean_move_bps

    if (
        activity.changes >= thresholds.high_changes
        and activity.banks_changed >= thresholds.high_banks_changed
    ):
        state = MarketState.HIGH
        rationale = (
            f"{activity.changes} rate changes across {activity.banks_changed} banks in "
            f"{thresholds.lookback_days} days meets the HIGH band "
            f"(>= {thresholds.high_changes} changes, >= {thresholds.high_banks_changed} banks)"
        )
    elif (
        activity.changes >= thresholds.elevated_changes
        and activity.banks_changed >= thresholds.elevated_banks_changed
    ):
        state = MarketState.ELEVATED
        rationale = (
            f"{activity.changes} rate changes across {activity.banks_changed} banks in "
            f"{thresholds.lookback_days} days meets the ELEVATED band "
            f"(>= {thresholds.elevated_changes} changes, "
            f">= {thresholds.elevated_banks_changed} banks)"
        )
    elif activity.changes >= thresholds.normal_changes and large_move:
        state = MarketState.ELEVATED
        rationale = (
            f"only {activity.changes} change(s), but a mean movement of "
            f"{activity.mean_change_bps:+.1f} bps meets the "
            f"{thresholds.large_mean_move_bps:.0f} bps large-move escalation"
        )
    elif activity.changes >= thresholds.normal_changes:
        state = MarketState.NORMAL
        rationale = (
            f"{activity.changes} rate change(s) across {activity.banks_changed} bank(s) in "
            f"{thresholds.lookback_days} days sits in the NORMAL band"
        )
    else:
        state = MarketState.LOW
        rationale = (
            f"no rate changes detected in the last {thresholds.lookback_days} days"
        )

    return MarketStateAssessment(state=state, activity=activity, rationale=rationale)
