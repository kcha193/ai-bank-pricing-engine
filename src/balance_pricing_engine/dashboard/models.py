"""Frozen view models returned by DashboardServices. No behaviour, no Streamlit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

from balance_pricing_engine.pricing_intelligence.health import FreshnessState, MarketExclusion


@dataclass(frozen=True)
class HeaderInfo:
    last_update: Optional[datetime]  # latest run's completed_at
    market_state: Optional[str]  # backend MarketState value
    fresh_banks: int
    configured_banks: int


@dataclass(frozen=True)
class MatrixCell:
    rate_pct: Optional[float]
    is_leader: bool = False
    stale: bool = False
    rate_category: Optional[str] = None


@dataclass(frozen=True)
class MarketMatrix:
    banks: tuple[str, ...]
    terms: tuple[int, ...]
    cells: Mapping[tuple[str, int], MatrixCell]


@dataclass(frozen=True)
class RankedBank:
    rank: int
    bank: str
    rate_pct: float
    product_name: str


@dataclass(frozen=True)
class TermSummary:
    term_months: int
    leaders: tuple[str, ...]
    lowest_pct: float
    median_pct: float
    highest_pct: float
    spread_bps: float
    banks_observed: int
    fresh_banks: int
    ranking: tuple[RankedBank, ...]


@dataclass(frozen=True)
class BankPosition:
    bank: str
    rate_pct: float
    rank: int
    vs_leader_bps: float
    vs_median_bps: float
    last_change_at: Optional[datetime]
    freshness: FreshnessState


@dataclass(frozen=True)
class Overview:
    header: HeaderInfo
    changes_today: int
    changes_7d: int
    banks_repricing_7d: int
    dominant_direction: str
    most_active_term: Optional[int]
    market_state_rationale: str
    matrix: MarketMatrix
    default_term: Optional[int]
    exclusions: tuple[MarketExclusion, ...]
