"""Frozen view models returned by DashboardServices. No behaviour, no Streamlit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Mapping, Optional

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.health import FreshnessState, MarketExclusion

#: Pricing Studio modes. A single-side scenario overlays one competitor benchmark; the joint mode
#: overlays both and reads the optimiser's joint loan/deposit answer.
SCENARIO_HOME_LOAN = "home_loan"
SCENARIO_TERM_DEPOSIT = "term_deposit"
SCENARIO_JOINT = "joint"
SCENARIO_MODES = (SCENARIO_HOME_LOAN, SCENARIO_TERM_DEPOSIT, SCENARIO_JOINT)


@dataclass(frozen=True)
class HeaderInfo:
    last_update: Optional[datetime]  # latest run's completed_at
    market_state: Optional[str]  # backend MarketState value
    fresh_banks: int
    configured_banks: int
    product_type: ProductType = ProductType.HOME_LOAN


@dataclass(frozen=True)
class MatrixCell:
    rate_pct: Optional[float]
    is_leader: bool = False
    stale: bool = False
    rate_category: Optional[str] = None
    #: The latest reading was quarantined; ``rate_pct`` is the last confirmed rate (a fallback).
    quarantined: bool = False


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
    #: The backend's most competitive rate for this product (lowest for mortgages, highest for
    #: deposits). ``None`` only for positional callers that predate product awareness.
    best_pct: Optional[float] = None
    product_type: ProductType = ProductType.HOME_LOAN


@dataclass(frozen=True)
class BankPosition:
    bank: str
    rate_pct: float
    rank: int
    vs_leader_bps: float
    vs_median_bps: float
    last_change_at: Optional[datetime]
    freshness: FreshnessState
    quarantined: bool = False  # the shown rate is the last confirmed one; the latest was rejected


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
    selectable_terms: tuple[int, ...]  # live snapshot terms; matrix also retains stale terms
    default_term: Optional[int]
    exclusions: tuple[MarketExclusion, ...]
    product_type: ProductType = ProductType.HOME_LOAN


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
    rate_category: str = ""  # additive: retain compatibility with positional callers
    product_type: ProductType = ProductType.HOME_LOAN  # the event's real product, from the store


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
    leader_pct: float  # the product's best rate: lowest for mortgages, highest for deposits
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
    product_type: ProductType = ProductType.HOME_LOAN


@dataclass(frozen=True)
class ScenarioRequest:
    """Operator inputs. Rates here are percent (``4.89``); the optimiser works in fractions.

    ``mode`` picks the side the competitor benchmark is overlaid on. For a single-side mode the
    first five fields describe that side (a deposit term, a deposit benchmark ...). ``joint``
    reads them as the loan side and the ``deposit_*`` fields as the deposit side; it has no
    manual or current-rate input.
    """

    term_months: int
    strategy: str  # a CompetitorRateStrategy value
    manual_rate_pct: Optional[float]
    offset_bps: float
    current_rate_pct: Optional[float]  # operator's own advertised rate; positioning only
    mode: str = SCENARIO_HOME_LOAN
    deposit_term_months: Optional[int] = None  # joint only
    deposit_strategy: Optional[str] = None  # joint only
    deposit_offset_bps: float = 0.0  # joint only


@dataclass(frozen=True)
class ScenarioRowView:
    """One comparison row. Rates are fractions (``0.0489``), as the optimiser reports them."""

    label: str
    strategy: str
    competitor_rate: float
    our_rate: float
    objective: float


@dataclass(frozen=True)
class CurvePoint:
    rate: float  # fraction
    objective: float


@dataclass(frozen=True)
class SelectedScenario:
    our_rate: float
    objective: float
    expected_loan_volume: float
    funding_shortfall: float
    competitor_rate: float
    rationale: str
    #: The optimiser's own grid-edge and policy-band flags (additive; default for old callers).
    boundary_warning: bool = False
    policy_band_breached: bool = False
    #: ``our_rate``/``competitor_rate`` are the scenario's own side; these name both sides of
    #: the optimiser's joint answer so a deposit result is never read off a loan field.
    mode: str = SCENARIO_HOME_LOAN
    loan_rate: Optional[float] = None
    deposit_rate: Optional[float] = None
    expected_deposit_volume: Optional[float] = None
    deposit_interest_cost: Optional[float] = None
    loan_competitor_rate: Optional[float] = None
    deposit_competitor_rate: Optional[float] = None


@dataclass(frozen=True)
class PositionView:
    rank: int
    bps_vs_best: float
    bps_vs_median: float
    market_best_pct: float
    market_median_pct: float
    bank_count: int


@dataclass(frozen=True)
class ScenarioView:
    request: ScenarioRequest
    rows: tuple[ScenarioRowView, ...]
    selected: SelectedScenario
    curve: tuple[CurvePoint, ...]
    position: Optional[PositionView]
    model_optimum_rate: Optional[float]
    excluded_banks: tuple[MarketExclusion, ...]
    mode: str = SCENARIO_HOME_LOAN
    #: Best objective at each candidate deposit rate (``curve`` is per loan rate).
    deposit_curve: tuple[CurvePoint, ...] = ()
    #: Joint mode only: stale deposit-side banks (``excluded_banks`` holds the loan side).
    deposit_excluded_banks: tuple[MarketExclusion, ...] = ()


@dataclass(frozen=True)
class RunRow:
    run_id: str
    started_at: datetime
    status: str
    banks_ok: int
    banks_failed: int
    observations: int
    changes: int
    duration_seconds: float


@dataclass(frozen=True)
class RunDetail:
    row: RunRow
    banks_successful: tuple[str, ...]
    banks_failed: tuple[str, ...]
    validation_failures: int
    suspicious: int
    market_state: Optional[str]
    error_summary: Optional[str]


@dataclass(frozen=True)
class SuspiciousRow:
    """A reading flagged as implausible. Quarantined: never part of a trusted benchmark."""

    bank: str
    term_months: Optional[int]
    previous_pct: Optional[float]
    observed_pct: Optional[float]
    change_bps: Optional[float]
    detected_at: datetime
