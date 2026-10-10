"""Frozen view models returned by DashboardServices. No behaviour, no Streamlit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Mapping, Optional

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


@dataclass(frozen=True)
class ChangeWindow:
    """Look-back window for the Rate Changes page (frozen so it can key a cache)."""

    kind: Literal["today", "7d", "30d", "custom"]
    start: Optional[date] = None
    end: Optional[date] = None


@dataclass(frozen=True)
class ChangeFilters:
    bank: Optional[str] = None
    term_months: Optional[int] = None
    direction: Optional[str] = None  # "UP" | "DOWN"
    change_type: Optional[str] = None


@dataclass(frozen=True)
class ChangeRow:
    key: str
    detected_at: datetime
    bank: str
    term_months: Optional[int]
    previous_pct: Optional[float]
    current_pct: Optional[float]
    change_bps: Optional[float]
    direction: str
    change_type: str
    previous_rank: Optional[int]
    new_rank: Optional[int]


@dataclass(frozen=True)
class EventDetail:
    row: ChangeRow
    product_name: str
    rate_category: str
    source_url: Optional[str]
    observed_at: Optional[datetime]
    effective_date_text: Optional[str]
    parser_version: Optional[str]
    market_median_pct: Optional[float]
    market_state: Optional[str]


@dataclass(frozen=True)
class HistoryPoint:
    day: date
    bank: str
    rate_pct: float


@dataclass(frozen=True)
class MarketPoint:
    day: date
    median_pct: float
    leader_pct: float
    spread_bps: float
    leaders: tuple[str, ...]


@dataclass(frozen=True)
class TimelineEvent:
    detected_at: datetime
    bank: str
    change_bps: float
    offset_days: float  # days after the first recorded move in the window


@dataclass(frozen=True)
class HistoryData:
    points: tuple[HistoryPoint, ...]
    market: tuple[MarketPoint, ...]
    timeline: tuple[TimelineEvent, ...]
