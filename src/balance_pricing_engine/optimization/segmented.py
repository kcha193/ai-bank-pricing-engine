"""Segmented portfolio-level product-tier pricing optimiser.

Every tier rate is chosen against the complete portfolio objective (OPT-002)::

    loan interest income - expected credit loss - capital charge
    - deposit interest expense - wholesale funding cost
    - funding shortfall penalty + excess deposit income

The funding terms couple every loan tier to every deposit tier, so no tier can be
priced on its own. The search is split into four separable steps:

A. ``precompute_*_tier_candidates`` scores each tier's customers once per candidate rate.
B. Portfolio aggregation sums the chosen tier values in tier order.
C. ``evaluate_segmented_portfolio`` applies the canonical ``pricing.profit`` and
   ``pricing.balance_sheet`` functions to the aggregate (the reported economics).
D. ``solve_segmented_portfolio`` searches tier-rate combinations: exhaustively when the
   combination count is small (a proven optimum on the grid), otherwise by deterministic
   multi-start coordinate descent (a best-known solution, never claimed optimal).

Selection follows the joint grid search: candidates meeting a configured
``min_return_on_equity`` are preferred; then higher objective, lower funding shortfall,
smaller distance to the market reference rates, and earlier enumeration order.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import prod
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from balance_pricing_engine.config import (
    DepositTierConfig,
    LoanTierConfig,
    ResponseCoefficientsConfig,
    SegmentedPricingConfig,
)
from balance_pricing_engine.optimization.grid_search import _spread_to_market_bps
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_capital_charge,
    expected_credit_loss,
    expected_deposit_cost,
    expected_deposit_volume,
    expected_excess_deposit_income,
    expected_loan_interest_income,
    expected_loan_volume,
    expected_wholesale_funding_cost,
    portfolio_return_on_equity,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)

SOLVER_EXACT = "EXACT"
SOLVER_COORDINATE_DESCENT = "COORDINATE_DESCENT"

# Benchmarked 2026-11-21 (docs/audits/2026-11-21-phase-3a-evidence, Apple arm64,
# Python 3.12): about two million combinations search exhaustively in ~0.03 s with a
# ~80 MB peak in 250,000-candidate blocks. Coordinate descent on the same fixtures
# finished 0.2-0.3% below the exact optimum, so exhaustive search is preferred wherever
# it is affordable.
DEFAULT_EXACT_SEARCH_MAX_COMBINATIONS = 2_000_000
DEFAULT_EVALUATION_BLOCK = 250_000
# Hard ceilings so neither configuration nor a caller can request unbounded work.
MAX_EXACT_SEARCH_COMBINATIONS = 10_000_000
MAX_SOLVER_ITERATIONS = 1_000
MAX_SOLVER_STARTS = 32
MAX_EVALUATION_BLOCK = 4_000_000
# A coordinate move must improve the objective by more than
# max(objective_tolerance, RELATIVE_OBJECTIVE_TOLERANCE * |objective|) dollars.
RELATIVE_OBJECTIVE_TOLERANCE = 1e-12

_METHODS = ("auto", "exact", "coordinate_descent")

Choice = tuple[tuple[int, ...], tuple[int, ...]]


@dataclass(frozen=True)
class TierResult:
    tier_name: str
    rate: float
    volume: float
    interest_income: float
    credit_loss: float
    interest_cost: float
    capital_charge: float = 0.0


@dataclass(frozen=True)
class SolverSettings:
    """Bounded search controls for the segmented optimiser.

    ``method="auto"`` searches exhaustively when the tier-combination count is at most
    ``exact_search_max_combinations`` (always for one loan and one deposit tier, which
    is the joint grid search), and uses coordinate descent otherwise.
    """

    method: str = "auto"
    exact_search_max_combinations: int = DEFAULT_EXACT_SEARCH_MAX_COMBINATIONS
    max_iterations: int = 100
    max_starts: int = 8
    objective_tolerance: float = 1e-6
    evaluation_block_size: int = DEFAULT_EVALUATION_BLOCK

    def __post_init__(self) -> None:
        if self.method not in _METHODS:
            raise ValueError(f"Solver method must be one of {_METHODS}.")
        if not 1 <= self.exact_search_max_combinations <= MAX_EXACT_SEARCH_COMBINATIONS:
            raise ValueError(
                "exact_search_max_combinations must be between 1 and "
                f"{MAX_EXACT_SEARCH_COMBINATIONS:,}."
            )
        if not 1 <= self.max_iterations <= MAX_SOLVER_ITERATIONS:
            raise ValueError(f"max_iterations must be between 1 and {MAX_SOLVER_ITERATIONS}.")
        if not 1 <= self.max_starts <= MAX_SOLVER_STARTS:
            raise ValueError(f"max_starts must be between 1 and {MAX_SOLVER_STARTS}.")
        if not (np.isfinite(self.objective_tolerance) and self.objective_tolerance >= 0):
            raise ValueError("objective_tolerance must be finite and non-negative.")
        if not 1 <= self.evaluation_block_size <= MAX_EVALUATION_BLOCK:
            raise ValueError(
                f"evaluation_block_size must be between 1 and {MAX_EVALUATION_BLOCK:,}."
            )


@dataclass(frozen=True)
class SolverDiagnostics:
    """How the segmented portfolio was searched. ``optimality_proven`` is True only for
    a completed exhaustive search; coordinate descent reports a best-known solution."""

    solver_method: str
    converged: bool
    iterations: int
    starts: int
    candidate_evaluations: int
    best_known_objective: float
    optimality_proven: bool
    search_space_size: int
    uniform_baseline_feasible: bool
    uniform_baseline_objective: Optional[float]
    iteration_limit_reached: bool = False
    cycle_detected: bool = False
    start_objectives: tuple[tuple[float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Additive ``summary.json`` payload (JSON-safe, bounded size)."""
        return {
            "solver_method": self.solver_method,
            "solver_converged": self.converged,
            "solver_iterations": self.iterations,
            "solver_starts": self.starts,
            "candidate_evaluations": self.candidate_evaluations,
            "best_known_objective": self.best_known_objective,
            "optimality_proven": self.optimality_proven,
            "search_space_size": self.search_space_size,
            "uniform_baseline_feasible": self.uniform_baseline_feasible,
            "uniform_baseline_objective": self.uniform_baseline_objective,
            "iteration_limit_reached": self.iteration_limit_reached,
            "cycle_detected": self.cycle_detected,
        }


@dataclass(frozen=True)
class SegmentedOptimizationResult:
    loan_tiers: list[TierResult]
    deposit_tiers: list[TierResult]
    total_loan_volume: float
    total_deposit_volume: float
    total_loan_interest_income: float
    total_credit_loss: float
    capital_charge: float
    total_deposit_interest_cost: float
    wholesale_funding_cost: float
    excess_deposit_income: float
    funding_shortfall_penalty: float
    funding_shortfall: float
    total_objective: float
    return_on_equity: float = 0.0
    roe_constraint_relaxed: bool = False
    solver: Optional[SolverDiagnostics] = None
    candidate_rates: Optional[dict[str, dict[str, tuple[float, ...]]]] = None


@dataclass(frozen=True)
class PortfolioParameters:
    """Funding and capital inputs shared by every candidate portfolio."""

    required_deposit_funding_ratio: float
    funding_shortfall_penalty_rate: float
    loan_rwa_weight: float
    capital_hurdle_rate: float
    wholesale_funding_rate: float
    cash_rate: float

    @classmethod
    def from_market(
        cls,
        market: MarketState,
        *,
        required_deposit_funding_ratio: float,
        funding_shortfall_penalty_rate: float,
        loan_rwa_weight: float,
        capital_hurdle_rate: float,
    ) -> "PortfolioParameters":
        return cls(
            required_deposit_funding_ratio=required_deposit_funding_ratio,
            funding_shortfall_penalty_rate=funding_shortfall_penalty_rate,
            loan_rwa_weight=loan_rwa_weight,
            capital_hurdle_rate=capital_hurdle_rate,
            wholesale_funding_rate=market.wholesale_funding_rate,
            cash_rate=market.cash_rate,
        )


def _as_vector(values: Iterable[float] | np.ndarray, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or array.size == 0:
        raise ValueError(f"{name} must be a non-empty one-dimensional array.")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite.")
    return array


@dataclass(frozen=True)
class LoanTierCandidates:
    """One loan tier's expected outcomes at each candidate rate."""

    tier_name: str
    rates: np.ndarray
    volume: np.ndarray
    interest_income: np.ndarray
    credit_loss: np.ndarray

    def __post_init__(self) -> None:
        for name in ("rates", "volume", "interest_income", "credit_loss"):
            object.__setattr__(self, name, _as_vector(getattr(self, name), name))
        if not self.rates.size == self.volume.size == self.interest_income.size == (
            self.credit_loss.size
        ):
            raise ValueError(f"Loan tier {self.tier_name!r} candidate arrays differ in length.")

    def choice(self, index: int) -> TierResult:
        return TierResult(
            tier_name=self.tier_name,
            rate=float(self.rates[index]),
            volume=float(self.volume[index]),
            interest_income=float(self.interest_income[index]),
            credit_loss=float(self.credit_loss[index]),
            interest_cost=0.0,
        )


@dataclass(frozen=True)
class DepositTierCandidates:
    """One deposit tier's expected outcomes at each candidate rate."""

    tier_name: str
    rates: np.ndarray
    volume: np.ndarray
    interest_cost: np.ndarray

    def __post_init__(self) -> None:
        for name in ("rates", "volume", "interest_cost"):
            object.__setattr__(self, name, _as_vector(getattr(self, name), name))
        if not self.rates.size == self.volume.size == self.interest_cost.size:
            raise ValueError(
                f"Deposit tier {self.tier_name!r} candidate arrays differ in length."
            )

    def choice(self, index: int) -> TierResult:
        return TierResult(
            tier_name=self.tier_name,
            rate=float(self.rates[index]),
            volume=float(self.volume[index]),
            interest_income=0.0,
            credit_loss=0.0,
            interest_cost=float(self.interest_cost[index]),
        )


@dataclass(frozen=True)
class CoordinateDescentRun:
    """One coordinate-descent run from one start. A fixed point is local, not global."""

    loan_indices: tuple[int, ...]
    deposit_indices: tuple[int, ...]
    initial_objective: float
    objective: float
    meets_roe_floor: bool
    iterations: int
    converged: bool
    iteration_limit_reached: bool
    cycle_detected: bool
    candidate_evaluations: int
    accepted_objectives: tuple[float, ...] = field(default=())


@dataclass(frozen=True)
class SegmentedSearchResult:
    loan_indices: tuple[int, ...]
    deposit_indices: tuple[int, ...]
    result: SegmentedOptimizationResult
    diagnostics: SolverDiagnostics
    roe_constraint_relaxed: bool


# ----------------------------------------------------------------- segmentation


def segment_loan_customers(
    customers: pd.DataFrame,
    tier_configs: list[LoanTierConfig],
) -> dict[str, pd.DataFrame]:
    """Split loan customers into named LVR tiers."""
    return _segment_customers(
        customers=customers,
        tier_configs=tier_configs,
        value_column="lvr",
        upper_bound_attribute="lvr_max",
    )


def segment_deposit_customers(
    customers: pd.DataFrame,
    tier_configs: list[DepositTierConfig],
) -> dict[str, pd.DataFrame]:
    """Split deposit customers into named balance tiers."""
    return _segment_customers(
        customers=customers,
        tier_configs=tier_configs,
        value_column="deposit_amount",
        upper_bound_attribute="balance_max",
    )


def _segment_customers(
    customers: pd.DataFrame,
    tier_configs: Iterable[LoanTierConfig | DepositTierConfig],
    value_column: str,
    upper_bound_attribute: str,
) -> dict[str, pd.DataFrame]:
    cohorts: dict[str, pd.DataFrame] = {}
    assigned_positions = np.zeros(len(customers), dtype=bool)
    previous_upper_bound = 0.0

    for tier in tier_configs:
        upper_bound = getattr(tier, upper_bound_attribute)
        values = customers[value_column].to_numpy()
        if upper_bound is None:
            mask = ~assigned_positions
        else:
            mask = (values >= previous_upper_bound) & (values < upper_bound) & ~assigned_positions
            previous_upper_bound = float(upper_bound)

        cohort = customers.iloc[mask].copy()
        if cohort.empty:
            raise ValueError(f"Tier {tier.name!r} has no customers.")

        cohorts[tier.name] = cohort
        assigned_positions |= mask

    if not assigned_positions.all():
        raise ValueError(f"Some customers are not covered by {value_column} tiers.")

    return cohorts


# ------------------------------------------------------ A. per-tier precomputation


def precompute_loan_tier_candidates(
    customers: pd.DataFrame,
    *,
    tier_name: str,
    rates: np.ndarray,
    market: MarketState,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    response_coefficients: ResponseCoefficientsConfig | None = None,
) -> LoanTierCandidates:
    """Score a loan tier once per candidate rate with the synthetic response model."""
    coefficients = (response_coefficients or ResponseCoefficientsConfig()).loan
    rates = _as_vector(rates, "rates")
    volume, income, loss = [], [], []
    for rate in rates:
        probabilities = loan_acceptance_probability(
            customers,
            float(rate),
            market.competitor_loan_rate,
            loan_rate_elasticity,
            loan_competitor_gap_elasticity,
            coefficients=coefficients,
        )
        volume.append(expected_loan_volume(customers, probabilities))
        income.append(expected_loan_interest_income(customers, probabilities, float(rate)))
        loss.append(expected_credit_loss(customers, probabilities))
    return LoanTierCandidates(tier_name, rates, volume, income, loss)


def precompute_deposit_tier_candidates(
    customers: pd.DataFrame,
    *,
    tier_name: str,
    rates: np.ndarray,
    market: MarketState,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    response_coefficients: ResponseCoefficientsConfig | None = None,
) -> DepositTierCandidates:
    """Score a deposit tier once per candidate rate with the synthetic response model."""
    coefficients = (response_coefficients or ResponseCoefficientsConfig()).deposit
    rates = _as_vector(rates, "rates")
    volume, cost = [], []
    for rate in rates:
        probabilities = deposit_uptake_probability(
            customers,
            float(rate),
            market.competitor_deposit_rate,
            deposit_rate_elasticity,
            deposit_competitor_gap_elasticity,
            coefficients=coefficients,
        )
        volume.append(expected_deposit_volume(customers, probabilities))
        cost.append(expected_deposit_cost(customers, probabilities, float(rate)))
    return DepositTierCandidates(tier_name, rates, volume, cost)


# ---------------------------------------- B/C. aggregation and canonical evaluation


def evaluate_segmented_portfolio(
    loan_tier_choices: Sequence[TierResult],
    deposit_tier_choices: Sequence[TierResult],
    parameters: PortfolioParameters,
) -> SegmentedOptimizationResult:
    """Return the full portfolio decomposition for one tier-rate combination."""
    if not loan_tier_choices or not deposit_tier_choices:
        raise ValueError("At least one loan tier and one deposit tier are required.")
    total_loan_volume = sum(tier.volume for tier in loan_tier_choices)
    total_deposit_volume = sum(tier.volume for tier in deposit_tier_choices)
    total_loan_interest_income = sum(tier.interest_income for tier in loan_tier_choices)
    total_credit_loss = sum(tier.credit_loss for tier in loan_tier_choices)
    total_deposit_interest_cost = sum(tier.interest_cost for tier in deposit_tier_choices)
    terms = _portfolio_terms(
        total_loan_volume,
        total_loan_interest_income,
        total_credit_loss,
        total_deposit_volume,
        total_deposit_interest_cost,
        parameters,
    )
    loan_tiers = [
        replace(
            tier,
            capital_charge=expected_capital_charge(
                tier.volume, parameters.loan_rwa_weight, parameters.capital_hurdle_rate
            ),
        )
        for tier in loan_tier_choices
    ]
    return SegmentedOptimizationResult(
        loan_tiers=loan_tiers,
        deposit_tiers=list(deposit_tier_choices),
        total_loan_volume=total_loan_volume,
        total_deposit_volume=total_deposit_volume,
        total_loan_interest_income=total_loan_interest_income,
        total_credit_loss=total_credit_loss,
        capital_charge=terms["capital_charge"],
        total_deposit_interest_cost=total_deposit_interest_cost,
        wholesale_funding_cost=terms["wholesale_funding_cost"],
        excess_deposit_income=terms["excess_deposit_income"],
        funding_shortfall_penalty=terms["funding_shortfall_penalty"],
        funding_shortfall=terms["funding_shortfall"],
        total_objective=terms["total_objective"],
        return_on_equity=terms["return_on_equity"],
    )


def _portfolio_terms(
    loan_volume,
    loan_interest_income,
    credit_loss,
    deposit_volume,
    deposit_interest_cost,
    parameters: PortfolioParameters,
) -> dict[str, Any]:
    """Canonical objective terms for scalars or broadcastable arrays of portfolios.

    The arithmetic order matches ``grid_search.evaluate_joint_rate_grid`` exactly, so a
    single loan and deposit tier reproduces the joint optimiser bit for bit.
    """
    capital_charge = expected_capital_charge(
        loan_volume, parameters.loan_rwa_weight, parameters.capital_hurdle_rate
    )
    wholesale_funding_cost = expected_wholesale_funding_cost(
        loan_volume, deposit_volume, parameters.wholesale_funding_rate
    )
    excess_deposit_income = expected_excess_deposit_income(
        loan_volume, deposit_volume, parameters.cash_rate
    )
    funding = calculate_funding_result(
        loan_volume,
        deposit_volume,
        parameters.required_deposit_funding_ratio,
        parameters.funding_shortfall_penalty_rate,
    )
    total_objective = (
        loan_interest_income
        - credit_loss
        - capital_charge
        - deposit_interest_cost
        - wholesale_funding_cost
        - funding.funding_penalty
        + excess_deposit_income
    )
    return_on_equity = portfolio_return_on_equity(
        loan_interest_income,
        credit_loss,
        deposit_interest_cost,
        wholesale_funding_cost,
        funding.funding_penalty,
        excess_deposit_income,
        loan_volume,
        parameters.loan_rwa_weight,
    )
    return {
        "capital_charge": capital_charge,
        "wholesale_funding_cost": wholesale_funding_cost,
        "excess_deposit_income": excess_deposit_income,
        "funding_shortfall": funding.funding_shortfall,
        "funding_shortfall_penalty": funding.funding_penalty,
        "total_objective": total_objective,
        "return_on_equity": return_on_equity,
    }


# --------------------------------------------------------------- D. search


class _Problem:
    """Precomputed tier arrays plus selection rules shared by every search method."""

    def __init__(
        self,
        loan_tiers: Sequence[LoanTierCandidates],
        deposit_tiers: Sequence[DepositTierCandidates],
        parameters: PortfolioParameters,
        min_return_on_equity: float | None,
        home_loan_reference_rate: float | None,
        term_deposit_reference_rate: float | None,
    ) -> None:
        if not loan_tiers or not deposit_tiers:
            raise ValueError("At least one loan tier and one deposit tier are required.")
        self.loan_tiers = list(loan_tiers)
        self.deposit_tiers = list(deposit_tiers)
        self.parameters = parameters
        self.min_return_on_equity = min_return_on_equity
        self.loan_distance = [
            _abs_spreads(t.rates, home_loan_reference_rate) for t in self.loan_tiers
        ]
        self.deposit_distance = [
            _abs_spreads(t.rates, term_deposit_reference_rate) for t in self.deposit_tiers
        ]
        self.loan_sizes = tuple(t.rates.size for t in self.loan_tiers)
        self.deposit_sizes = tuple(t.rates.size for t in self.deposit_tiers)

    @property
    def search_space_size(self) -> int:
        return prod(self.loan_sizes) * prod(self.deposit_sizes)

    def evaluate(self, loan_totals, deposit_totals) -> tuple[np.ndarray, np.ndarray, Any]:
        """Objective, shortfall and ROE-floor flags for arrays of portfolio totals."""
        volume, income, loss = loan_totals
        deposit_volume, cost = deposit_totals
        terms = _portfolio_terms(volume, income, loss, deposit_volume, cost, self.parameters)
        objective = np.asarray(terms["total_objective"], dtype=float)
        shortfall = np.broadcast_to(np.asarray(terms["funding_shortfall"]), objective.shape)
        qualifies = None
        if self.min_return_on_equity is not None:
            roe = np.broadcast_to(np.asarray(terms["return_on_equity"]), objective.shape)
            qualifies = roe >= self.min_return_on_equity
        return objective, shortfall, qualifies

    def distance(self, loan_indices: Sequence[int], deposit_indices: Sequence[int]) -> float:
        total = 0.0
        for distances, index in zip(self.loan_distance, loan_indices):
            total = total + distances[index]
        for distances, index in zip(self.deposit_distance, deposit_indices):
            total = total + distances[index]
        return float(np.inf) if np.isnan(total) else float(total)

    def loan_totals(self, indices: Sequence[int], replace_tier: int | None = None):
        """Sequential tier-order sums; ``replace_tier`` substitutes that tier's full array."""
        return tuple(
            _ordered_sum(
                getattr(tier, name) if position == replace_tier else getattr(tier, name)[index]
                for position, (tier, index) in enumerate(zip(self.loan_tiers, indices))
            )
            for name in ("volume", "interest_income", "credit_loss")
        )

    def deposit_totals(self, indices: Sequence[int], replace_tier: int | None = None):
        return tuple(
            _ordered_sum(
                getattr(tier, name) if position == replace_tier else getattr(tier, name)[index]
                for position, (tier, index) in enumerate(zip(self.deposit_tiers, indices))
            )
            for name in ("volume", "interest_cost")
        )

    def score(self, choice: Choice) -> tuple[float, float, bool]:
        objective, shortfall, qualifies = self.evaluate(
            self.loan_totals(choice[0]), self.deposit_totals(choice[1])
        )
        meets = True if qualifies is None else bool(qualifies)
        return float(objective), float(shortfall), meets

    def key(self, choice: Choice, order: int) -> tuple:
        objective, shortfall, meets = self.score(choice)
        return (meets, objective, -shortfall, -self.distance(*choice), -order)

    def result(self, choice: Choice) -> SegmentedOptimizationResult:
        return evaluate_segmented_portfolio(
            [tier.choice(i) for tier, i in zip(self.loan_tiers, choice[0])],
            [tier.choice(i) for tier, i in zip(self.deposit_tiers, choice[1])],
            self.parameters,
        )


def _abs_spreads(rates: np.ndarray, reference_rate: float | None) -> np.ndarray:
    return np.abs(np.array([_spread_to_market_bps(float(r), reference_rate) for r in rates]))


def _ordered_sum(values: Iterable[Any]) -> Any:
    """Left-to-right sum in tier order, matching ``sum()`` in the canonical evaluation."""
    total: Any = 0
    for value in values:
        total = total + value
    return total


def _combine(tiers: Sequence[Any], names: Sequence[str]) -> list[np.ndarray]:
    """Totals over every tier combination, in ``itertools.product`` order."""
    combined = []
    for name in names:
        total = getattr(tiers[0], name)
        for tier in tiers[1:]:
            total = (total[:, None] + getattr(tier, name)[None, :]).ravel()
        combined.append(total)
    return combined


def _best_in_block(
    objective: np.ndarray,
    shortfall: np.ndarray,
    mask: np.ndarray | None,
    distance_of,
) -> int | None:
    """Flat position of the best candidate: objective, shortfall, distance, position."""
    if mask is not None:
        if not mask.any():
            return None
        objective = np.where(mask, objective, -np.inf)
    candidates = np.flatnonzero(objective == objective.max())
    if candidates.size > 1:
        tied_shortfall = shortfall[candidates]
        candidates = candidates[tied_shortfall == tied_shortfall.min()]
    if candidates.size > 1:
        distances = np.array([distance_of(int(c)) for c in candidates])
        candidates = candidates[distances == distances.min()]
    return int(candidates[0])


def _exhaustive_search(
    problem: _Problem, settings: SolverSettings
) -> tuple[Choice, int]:
    """Evaluate every tier combination (``itertools.product`` order) in bounded blocks."""
    loan_totals = _combine(problem.loan_tiers, ("volume", "interest_income", "credit_loss"))
    deposit_totals = _combine(problem.deposit_tiers, ("volume", "interest_cost"))

    def choice_of(loan_row: int, deposit_row: int) -> Choice:
        return (
            tuple(int(i) for i in np.unravel_index(loan_row, problem.loan_sizes)),
            tuple(int(i) for i in np.unravel_index(deposit_row, problem.deposit_sizes)),
        )

    return _cross_search(problem, loan_totals, deposit_totals, choice_of, settings)


def _cross_search(
    problem: _Problem,
    loan_totals: Sequence[np.ndarray],
    deposit_totals: Sequence[np.ndarray],
    choice_of,
    settings: SolverSettings,
) -> tuple[Choice, int]:
    """Best pairing of loan-side and deposit-side portfolio totals.

    Rows are paired loan-major, deposit-minor; ``choice_of(loan_row, deposit_row)`` maps a
    pair back to tier indices. Candidates meeting the ROE floor win when any exists.
    """
    loan_volume, loan_income, loan_loss = loan_totals
    deposit_volume, deposit_cost = deposit_totals
    n_deposit = deposit_volume.size
    rows_per_block = max(1, settings.evaluation_block_size // n_deposit)

    def flat_choice(flat: int) -> Choice:
        return choice_of(*divmod(flat, n_deposit))

    tracks: dict[bool, tuple[tuple, int]] = {}
    evaluations = 0
    for start in range(0, loan_volume.size, rows_per_block):
        rows = slice(start, start + rows_per_block)
        objective, shortfall, qualifies = problem.evaluate(
            (loan_volume[rows, None], loan_income[rows, None], loan_loss[rows, None]),
            (deposit_volume[None, :], deposit_cost[None, :]),
        )
        objective, shortfall = objective.ravel(), shortfall.ravel()
        evaluations += objective.size
        offset = start * n_deposit
        masks = {False: None} if qualifies is None else {True: qualifies.ravel(), False: None}
        for qualifying, mask in masks.items():
            local = _best_in_block(
                objective,
                shortfall,
                mask,
                lambda c, offset=offset: problem.distance(*flat_choice(offset + c)),
            )
            if local is None:
                continue
            flat = offset + local
            key = (
                float(objective[local]),
                -float(shortfall[local]),
                -problem.distance(*flat_choice(flat)),
                -flat,
            )
            if qualifying not in tracks or key > tracks[qualifying][0]:
                tracks[qualifying] = (key, flat)
    best_flat = tracks[True][1] if True in tracks else tracks[False][1]
    return flat_choice(best_flat), evaluations


def run_coordinate_descent(
    loan_tiers: Sequence[LoanTierCandidates],
    deposit_tiers: Sequence[DepositTierCandidates],
    parameters: PortfolioParameters,
    *,
    start: Choice,
    settings: SolverSettings | None = None,
    min_return_on_equity: float | None = None,
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
) -> CoordinateDescentRun:
    """Improve one tier at a time against the complete portfolio objective.

    Each move holds every other tier fixed, so a loan-tier candidate is judged against
    the current total deposit volume and vice versa, with the shortfall penalty and
    excess-deposit income recomputed for every candidate.
    """
    problem = _Problem(
        loan_tiers,
        deposit_tiers,
        parameters,
        min_return_on_equity,
        home_loan_reference_rate,
        term_deposit_reference_rate,
    )
    return _coordinate_descent(
        problem, _validated_choice(problem, start), settings or SolverSettings()
    )


def _coordinate_descent(
    problem: _Problem, start: Choice, settings: SolverSettings
) -> CoordinateDescentRun:
    loan_state, deposit_state = list(start[0]), list(start[1])
    objective, _, meets = problem.score(start)
    initial_objective = objective
    accepted = [objective]
    evaluations = 1
    seen = {start}
    converged = cycle_detected = False
    iterations = 0
    coordinates = [("loan", t) for t in range(len(loan_state))] + [
        ("deposit", t) for t in range(len(deposit_state))
    ]
    while iterations < settings.max_iterations:
        iterations += 1
        moved = False
        for side, tier in coordinates:
            if side == "loan":
                loan_totals = problem.loan_totals(loan_state, replace_tier=tier)
                deposit_totals = problem.deposit_totals(deposit_state)
            else:
                loan_totals = problem.loan_totals(loan_state)
                deposit_totals = problem.deposit_totals(deposit_state, replace_tier=tier)
            values, shortfall, qualifies = problem.evaluate(loan_totals, deposit_totals)
            values, shortfall = np.atleast_1d(values), np.atleast_1d(shortfall)
            evaluations += values.size
            state = loan_state if side == "loan" else deposit_state

            def distance_of(index: int, state=state, tier=tier, side=side) -> float:
                trial = list(state)
                trial[tier] = index
                if side == "loan":
                    return problem.distance(trial, deposit_state)
                return problem.distance(loan_state, trial)

            mask = None if qualifies is None else np.atleast_1d(qualifies)
            best = _best_in_block(values, shortfall, mask, distance_of)
            if best is None:
                best = _best_in_block(values, shortfall, None, distance_of)
            candidate_meets = True if mask is None else bool(mask[best])
            candidate_objective = float(values[best])
            tolerance = max(
                settings.objective_tolerance, RELATIVE_OBJECTIVE_TOLERANCE * abs(objective)
            )
            gains_feasibility = candidate_meets and not meets
            improves = candidate_meets == meets and candidate_objective > objective + tolerance
            if best != state[tier] and (gains_feasibility or improves):
                state[tier] = best
                objective, meets = candidate_objective, candidate_meets
                accepted.append(objective)
                moved = True
        if not moved:
            converged = True
            break
        snapshot = (tuple(loan_state), tuple(deposit_state))
        if snapshot in seen:  # impossible while accepted moves strictly improve the key
            cycle_detected = True
            break
        seen.add(snapshot)
    return CoordinateDescentRun(
        loan_indices=tuple(loan_state),
        deposit_indices=tuple(deposit_state),
        initial_objective=initial_objective,
        objective=objective,
        meets_roe_floor=meets,
        iterations=iterations,
        converged=converged,
        iteration_limit_reached=not converged and not cycle_detected,
        cycle_detected=cycle_detected,
        candidate_evaluations=evaluations,
        accepted_objectives=tuple(accepted),
    )


def _validated_choice(problem: _Problem, choice: Choice) -> Choice:
    loan, deposit = tuple(int(i) for i in choice[0]), tuple(int(i) for i in choice[1])
    if len(loan) != len(problem.loan_sizes) or len(deposit) != len(problem.deposit_sizes):
        raise ValueError("A start must choose one rate for every tier.")
    for index, size in zip(loan + deposit, problem.loan_sizes + problem.deposit_sizes):
        if not 0 <= index < size:
            raise ValueError("A start rate index lies outside its tier grid.")
    return loan, deposit


def _uniform_baseline(
    problem: _Problem, settings: SolverSettings
) -> tuple[Optional[Choice], int]:
    """Best portfolio charging one loan rate and one deposit rate in every tier.

    Feasible only when a rate appears in every tier grid on that side; otherwise no
    uniform comparator exists and none is claimed.
    """
    loan_maps = [_rate_positions(t.rates) for t in problem.loan_tiers]
    deposit_maps = [_rate_positions(t.rates) for t in problem.deposit_tiers]
    common_loan = sorted(set.intersection(*(set(m) for m in loan_maps)))
    common_deposit = sorted(set.intersection(*(set(m) for m in deposit_maps)))
    if not common_loan or not common_deposit:
        return None, 0
    loan_rows = np.array([[m[rate] for m in loan_maps] for rate in common_loan])
    deposit_rows = np.array([[m[rate] for m in deposit_maps] for rate in common_deposit])
    loan_totals = [
        _ordered_sum(getattr(t, name)[loan_rows[:, i]] for i, t in enumerate(problem.loan_tiers))
        for name in ("volume", "interest_income", "credit_loss")
    ]
    deposit_totals = [
        _ordered_sum(
            getattr(t, name)[deposit_rows[:, i]] for i, t in enumerate(problem.deposit_tiers)
        )
        for name in ("volume", "interest_cost")
    ]

    def choice_of(loan_row: int, deposit_row: int) -> Choice:
        return (
            tuple(int(i) for i in loan_rows[loan_row]),
            tuple(int(i) for i in deposit_rows[deposit_row]),
        )

    return _cross_search(problem, loan_totals, deposit_totals, choice_of, settings)


def _rate_positions(rates: np.ndarray) -> dict[float, int]:
    positions: dict[float, int] = {}
    for index, rate in enumerate(rates):
        positions.setdefault(round(float(rate), 9), index)
    return positions


def _default_starts(
    problem: _Problem,
    market_loan_rate: float | None,
    market_deposit_rate: float | None,
) -> list[Choice]:
    def nearest(tiers, target):
        return tuple(int(np.argmin(np.abs(t.rates - target))) for t in tiers)

    params = problem.parameters
    tier_local_loans = tuple(
        int(
            np.argmax(
                t.interest_income
                - t.credit_loss
                - expected_capital_charge(
                    t.volume, params.loan_rwa_weight, params.capital_hurdle_rate
                )
                - t.volume * params.wholesale_funding_rate
            )
        )
        for t in problem.loan_tiers
    )
    tier_local_deposits = tuple(
        int(np.argmax(t.volume * params.wholesale_funding_rate - t.interest_cost))
        for t in problem.deposit_tiers
    )
    starts = [
        (tier_local_loans, tier_local_deposits),
        (tuple(0 for _ in problem.loan_sizes), tuple(0 for _ in problem.deposit_sizes)),
        (
            tuple(n - 1 for n in problem.loan_sizes),
            tuple(n - 1 for n in problem.deposit_sizes),
        ),
        (
            tuple(n // 2 for n in problem.loan_sizes),
            tuple(n // 2 for n in problem.deposit_sizes),
        ),
    ]
    if market_loan_rate is not None and market_deposit_rate is not None:
        starts.append(
            (
                nearest(problem.loan_tiers, market_loan_rate),
                nearest(problem.deposit_tiers, market_deposit_rate),
            )
        )
    return starts


def solve_segmented_portfolio(
    loan_tiers: Sequence[LoanTierCandidates],
    deposit_tiers: Sequence[DepositTierCandidates],
    parameters: PortfolioParameters,
    *,
    settings: SolverSettings | None = None,
    min_return_on_equity: float | None = None,
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
    starts: Sequence[Choice] = (),
) -> SegmentedSearchResult:
    """Choose one rate per tier to maximise the complete portfolio objective."""
    settings = settings or SolverSettings()
    problem = _Problem(
        loan_tiers,
        deposit_tiers,
        parameters,
        min_return_on_equity,
        home_loan_reference_rate,
        term_deposit_reference_rate,
    )
    space = problem.search_space_size
    single_tier = len(problem.loan_tiers) == 1 and len(problem.deposit_tiers) == 1
    if settings.method == "exact":
        use_exact = True
        if space > MAX_EXACT_SEARCH_COMBINATIONS and not single_tier:
            raise ValueError(
                f"Forced exact search over {space:,} combinations exceeds the "
                f"{MAX_EXACT_SEARCH_COMBINATIONS:,} limit."
            )
    elif settings.method == "coordinate_descent":
        use_exact = False
    else:
        # One tier per side is the joint grid search, which is always exhaustive.
        use_exact = single_tier or space <= settings.exact_search_max_combinations

    uniform_choice, uniform_evaluations = _uniform_baseline(problem, settings)
    evaluations = uniform_evaluations
    start_objectives: tuple[tuple[float, float], ...] = ()
    if use_exact:
        choice, exact_evaluations = _exhaustive_search(problem, settings)
        evaluations += exact_evaluations
        method, converged, iterations, n_starts = SOLVER_EXACT, True, 0, 0
        limit_reached = cycle = False
    else:
        candidates = [_validated_choice(problem, s) for s in starts]
        if uniform_choice is not None:
            candidates.append(uniform_choice)
        candidates.extend(
            _default_starts(
                problem,
                home_loan_reference_rate,
                term_deposit_reference_rate,
            )
        )
        unique: list[Choice] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        runs = [_coordinate_descent(problem, s, settings) for s in unique[: settings.max_starts]]
        best_run = max(
            range(len(runs)),
            key=lambda i: problem.key((runs[i].loan_indices, runs[i].deposit_indices), i),
        )
        choice = (runs[best_run].loan_indices, runs[best_run].deposit_indices)
        evaluations += sum(run.candidate_evaluations for run in runs)
        method = SOLVER_COORDINATE_DESCENT
        converged = all(run.converged for run in runs)
        iterations = sum(run.iterations for run in runs)
        n_starts = len(runs)
        limit_reached = any(run.iteration_limit_reached for run in runs)
        cycle = any(run.cycle_detected for run in runs)
        start_objectives = tuple((run.initial_objective, run.objective) for run in runs)

    result = problem.result(choice)
    relaxed = min_return_on_equity is not None and not (
        np.isfinite(result.return_on_equity) and result.return_on_equity >= min_return_on_equity
    )
    uniform_objective = (
        None if uniform_choice is None else float(problem.result(uniform_choice).total_objective)
    )
    diagnostics = SolverDiagnostics(
        solver_method=method,
        converged=converged,
        iterations=iterations,
        starts=n_starts,
        candidate_evaluations=evaluations,
        best_known_objective=float(result.total_objective),
        optimality_proven=method == SOLVER_EXACT,
        search_space_size=space,
        uniform_baseline_feasible=uniform_choice is not None,
        uniform_baseline_objective=uniform_objective,
        iteration_limit_reached=limit_reached,
        cycle_detected=cycle,
        start_objectives=start_objectives,
    )
    result = replace(result, roe_constraint_relaxed=relaxed, solver=diagnostics)
    return SegmentedSearchResult(
        loan_indices=choice[0],
        deposit_indices=choice[1],
        result=result,
        diagnostics=diagnostics,
        roe_constraint_relaxed=relaxed,
    )


# ------------------------------------------------------------ public entry point


def optimize_segmented_rates(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    config: SegmentedPricingConfig,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
    loan_rwa_weight: float = 0.35,
    capital_hurdle_rate: float = 0.12,
    response_coefficients: ResponseCoefficientsConfig | None = None,
    *,
    min_return_on_equity: float | None = None,
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
    solver_settings: SolverSettings | None = None,
    incumbent_tier_rates: dict[str, dict[str, float]] | None = None,
) -> SegmentedOptimizationResult:
    """Choose tier rates jointly against the full portfolio objective.

    ``incumbent_tier_rates`` (``{"loan_tiers": {name: rate}, "deposit_tiers": {...}}``)
    carries a previous solution into a re-optimisation, for example after boundary
    expansion: its rates join their tiers' candidate sets and it seeds coordinate
    descent, so an enlarged search can never return a worse best-known objective.
    """
    response_coefficients = response_coefficients or ResponseCoefficientsConfig()
    loan_segments = segment_loan_customers(loan_customers, config.loan_tiers)
    deposit_segments = segment_deposit_customers(deposit_customers, config.deposit_tiers)
    incumbent = incumbent_tier_rates or {}
    loan_incumbent = incumbent.get("loan_tiers", {})
    deposit_incumbent = incumbent.get("deposit_tiers", {})

    loan_tiers = [
        precompute_loan_tier_candidates(
            loan_segments[tier.name],
            tier_name=tier.name,
            rates=_tier_rates(tier, loan_incumbent.get(tier.name)),
            market=market,
            loan_rate_elasticity=loan_rate_elasticity,
            loan_competitor_gap_elasticity=loan_competitor_gap_elasticity,
            response_coefficients=response_coefficients,
        )
        for tier in config.loan_tiers
    ]
    deposit_tiers = [
        precompute_deposit_tier_candidates(
            deposit_segments[tier.name],
            tier_name=tier.name,
            rates=_tier_rates(tier, deposit_incumbent.get(tier.name)),
            market=market,
            deposit_rate_elasticity=deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=deposit_competitor_gap_elasticity,
            response_coefficients=response_coefficients,
        )
        for tier in config.deposit_tiers
    ]
    parameters = PortfolioParameters.from_market(
        market,
        required_deposit_funding_ratio=required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=funding_shortfall_penalty_rate,
        loan_rwa_weight=loan_rwa_weight,
        capital_hurdle_rate=capital_hurdle_rate,
    )
    starts: list[Choice] = []
    if all(t.tier_name in loan_incumbent for t in loan_tiers) and all(
        t.tier_name in deposit_incumbent for t in deposit_tiers
    ):
        starts.append(
            (
                tuple(_rate_index(t.rates, loan_incumbent[t.tier_name]) for t in loan_tiers),
                tuple(
                    _rate_index(t.rates, deposit_incumbent[t.tier_name]) for t in deposit_tiers
                ),
            )
        )
    search = solve_segmented_portfolio(
        loan_tiers,
        deposit_tiers,
        parameters,
        settings=solver_settings,
        min_return_on_equity=min_return_on_equity,
        home_loan_reference_rate=home_loan_reference_rate,
        term_deposit_reference_rate=term_deposit_reference_rate,
        starts=starts,
    )
    candidate_rates = {
        "loan_tiers": {t.tier_name: tuple(float(r) for r in t.rates) for t in loan_tiers},
        "deposit_tiers": {t.tier_name: tuple(float(r) for r in t.rates) for t in deposit_tiers},
    }
    return replace(search.result, candidate_rates=candidate_rates)


def _tier_rates(
    tier: LoanTierConfig | DepositTierConfig, incumbent_rate: float | None
) -> np.ndarray:
    rates = _build_tier_rate_grid(tier.rate_grid.start, tier.rate_grid.stop, tier.rate_grid.step)
    if incumbent_rate is None:
        return rates
    return np.union1d(rates, [round(float(incumbent_rate), 6)])


def _rate_index(rates: np.ndarray, rate: float) -> int:
    return int(np.argmin(np.abs(rates - rate)))


def _build_tier_rate_grid(start: float, stop: float, step: float) -> np.ndarray:
    """Build a bounded tier grid, appending stop when it is not reached by whole steps."""
    step_count = int(np.floor((stop - start) / step))
    rates = list(np.round(start + np.arange(step_count + 1) * step, 6))
    if not np.isclose(rates[-1], stop, rtol=1e-9, atol=1e-12):
        rates.append(round(stop, 6))
    return np.array(rates, dtype=float)
