"""Shared Hypothesis strategies and reference calculations for the economic-invariant suite.

TEST-002 (Phase 3c). Every strategy draws directly from a documented valid economic domain
(``docs/economic_invariant_testing.md`` -> "Valid input domains"), so properties do not depend
on ``assume()`` filters that reject most examples.

The ``reference_*`` functions are independent re-statements of the documented equations in
``docs/modelling_assumptions.md``. They deliberately do not call the production pricing,
funding or optimiser code, so a test comparing the two is not a tautology.

Run modes (see ``docs/economic_invariant_testing.md``):

- default (CI): derandomised, no example database, moderate example budgets;
- ``ECONOMIC_INVARIANT_SCALE=<k>``: multiply every example budget by ``k`` (stress profile);
- ``ECONOMIC_INVARIANT_EXPLORE=1``: random generation, reproducible with ``--hypothesis-seed``.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from itertools import product
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd
from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st

# ------------------------------------------------------------------ run-mode settings

_SCALE = float(os.environ.get("ECONOMIC_INVARIANT_SCALE", "1"))
_EXPLORE = os.environ.get("ECONOMIC_INVARIANT_EXPLORE", "") not in ("", "0")
_MUTATING = bool(os.environ.get("ECONOMIC_MUTANT"))

# Absolute and relative tolerances for money amounts (NZD). Amounts reach ~1e10, so a
# relative tolerance of 1e-9 allows floating-point accumulation over thousands of rows.
# At NZD 1 billion the tolerance is NZD 1.
MONEY_REL_TOL = 1e-9
MONEY_ABS_TOL = 1e-6


def budget(max_examples: int) -> settings:
    """Hypothesis settings for one property: scaled example count, CI-deterministic."""
    return settings(
        max_examples=max(1, int(round(max_examples * _SCALE))),
        deadline=None,
        derandomize=not _EXPLORE,
        database=None if not _EXPLORE else settings.default.database,
        # Mutation runs need a counterexample, and failed properties are expected.
        phases=(Phase.explicit, Phase.generate) if _MUTATING else settings.default.phases,
        print_blob=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )


def money_close(actual: float, expected: float, *, rel: float = MONEY_REL_TOL) -> bool:
    return math.isclose(actual, expected, rel_tol=rel, abs_tol=MONEY_ABS_TOL)


# ------------------------------------------------------------------- scalar domains


def _floats(lo: float, hi: float) -> st.SearchStrategy[float]:
    return st.floats(lo, hi, allow_nan=False, allow_infinity=False, allow_subnormal=False)


#: Offered and competitor rates: 0% to 15% p.a.
rates = _floats(0.0, 0.15)
#: Loan elasticities are non-positive (config: ``le=0``); include exact zero and extremes.
loan_elasticities = st.one_of(st.just(0.0), st.just(-400.0), _floats(-400.0, 0.0))
#: Deposit elasticities are non-negative (config: ``ge=0``).
deposit_elasticities = st.one_of(st.just(0.0), st.just(400.0), _floats(0.0, 400.0))
# Bounded float strategies already emit their endpoints (e.g. exactly 0.0) as boundary cases,
# so zero is covered without giving it half the probability mass. A 50% weight on zero
# volumes or penalties left too few examples with a live funding trade-off: the OPT-002
# shortfall-penalty mutant then escaped the multi-tier brute-force test (Phase 3c evidence).
funding_ratios = _floats(0.0, 1.0)
penalty_rates = _floats(0.0, 0.30)
#: Runoff endpoints 0 (no runoff) and 1 (full runoff) are the M02/M03 cases: weight them.
runoff_rates = st.one_of(st.sampled_from([0.0, 1.0]), _floats(0.0, 1.0), _floats(0.0, 1.0))
discount_rates = _floats(0.0, 0.20)
#: Dollar balances. Sub-dollar amounts (Hypothesis likes values like 1e-300) become 0.0: a
#: positive loan volume near 1e-290 overflows ROE to inf, an unrealistic domain documented
#: in docs/economic_invariant_testing.md -> Known limitations.
volumes = _floats(0.0, 5e9).map(lambda value: 0.0 if value < 1.0 else value)
positive_scale = _floats(0.01, 100.0)


# ------------------------------------------------------------------ customer frames


@st.composite
def loan_customers(draw, min_size: int = 1, max_size: int = 25) -> pd.DataFrame:
    n = draw(st.integers(min_size, max_size))

    def column(lo: float, hi: float) -> np.ndarray:
        return np.array(draw(st.lists(_floats(lo, hi), min_size=n, max_size=n)))

    return pd.DataFrame(
        {
            "risk_score": column(0.0, 1.0),
            "credit_score": column(500.0, 850.0),
            "lvr": column(0.45, 0.98),
            "loan_amount": column(0.0, 2_000_000.0),
            "expected_loss_rate": column(0.0, 0.05),
        }
    )


@st.composite
def deposit_customers(draw, min_size: int = 1, max_size: int = 25) -> pd.DataFrame:
    n = draw(st.integers(min_size, max_size))

    def column(lo: float, hi: float) -> np.ndarray:
        return np.array(draw(st.lists(_floats(lo, hi), min_size=n, max_size=n)))

    return pd.DataFrame(
        {
            "rate_sensitivity": column(0.05, 0.95),
            "relationship_years": column(0.0, 35.0),
            "digital_engagement": column(0.0, 1.0),
            "deposit_amount": column(0.0, 1_000_000.0),
        }
    )


@st.composite
def loan_coefficients(draw):
    from balance_pricing_engine.config import LoanResponseCoefficientsConfig

    return LoanResponseCoefficientsConfig(
        intercept=draw(_floats(-4.0, 4.0)),
        rate_anchor=draw(_floats(0.02, 0.08)),
        risk_score_coef=draw(_floats(-3.0, 0.0)),
        credit_score_coef=draw(_floats(0.0, 1.0)),
        lvr_coef=draw(_floats(-1.0, 0.0)),
    )


@st.composite
def deposit_coefficients(draw):
    from balance_pricing_engine.config import DepositResponseCoefficientsConfig

    return DepositResponseCoefficientsConfig(
        intercept=draw(_floats(-4.0, 4.0)),
        rate_anchor=draw(_floats(0.02, 0.06)),
        rate_sensitivity_coef=draw(_floats(0.0, 2.0)),
        relationship_coef=draw(_floats(0.0, 0.2)),
        digital_coef=draw(_floats(-1.0, 1.0)),
    )


@st.composite
def increasing_rate_pair(draw, lo: float = 0.0, hi: float = 0.15, min_gap: float = 1e-4):
    """Two rates ``r_low < r_high`` at least ``min_gap`` apart, generated directly."""
    low = draw(_floats(lo, hi - min_gap))
    high = draw(_floats(low + min_gap, hi))
    return low, high


# -------------------------------------------------------------------- rate grids


@st.composite
def rate_grids(draw, min_points: int = 1, max_points: int = 4, lo: float = 0.01):
    """Draw ``(start, stop, step)`` in integer basis points before converting to rates."""
    start_bp = draw(st.integers(int(lo * 10_000), 900))
    step_bp = draw(st.integers(5, 60))
    points = draw(st.integers(min_points, max_points))
    start = start_bp / 10_000
    step = step_bp / 10_000
    stop = (start_bp + step_bp * (points - 1)) / 10_000
    return start, stop, step


@st.composite
def market_rate_grids(draw, reference: float):
    """Either a generic grid anywhere, or a fine grid around the market rate (1-9 points,
    5-25 bp steps) where funding and capital terms can move the optimum by a grid step."""
    if draw(st.booleans()):
        return draw(rate_grids(min_points=1, max_points=5, lo=0.005))
    step_bp = draw(st.integers(5, 25))
    points = draw(st.integers(1, 9))
    start_bp = max(5, round(reference * 10_000) - draw(st.integers(0, 150)))
    return start_bp / 10_000, (start_bp + step_bp * (points - 1)) / 10_000, step_bp / 10_000



# ------------------------------------------------------------- funding scenarios


@dataclass(frozen=True)
class FundingScenario:
    required_deposit_funding_ratio: float
    funding_shortfall_penalty_rate: float
    wholesale_funding_rate: float
    cash_rate: float
    loan_rwa_weight: float
    capital_hurdle_rate: float


@st.composite
def funding_scenarios(draw) -> FundingScenario:
    cash = draw(_floats(0.0, 0.08))
    return FundingScenario(
        required_deposit_funding_ratio=draw(funding_ratios),
        funding_shortfall_penalty_rate=draw(penalty_rates),
        wholesale_funding_rate=cash + draw(_floats(0.0, 0.03)),
        cash_rate=cash,
        # Zero (no capital) or a realistic weight: a weight like 1e-300 overflows ROE to inf.
        loan_rwa_weight=draw(st.one_of(st.just(0.0), _floats(0.01, 1.0))),
        capital_hurdle_rate=draw(_floats(0.0, 0.25)),
    )


# ------------------------------------------------------------ independent references
#
# These restate docs/modelling_assumptions.md in plain Python. They never call the
# production modules, so agreement is evidence, not a tautology.


@dataclass(frozen=True)
class ReferenceTerms:
    loan_interest_income: float
    expected_credit_loss: float
    capital_charge: float
    deposit_interest_cost: float
    wholesale_funding_cost: float
    excess_deposit_income: float
    funding_shortfall: float
    funding_shortfall_penalty: float
    total_objective: float
    return_on_equity: float


#: Tier-1 capital ratio documented in docs/modelling_assumptions.md (capital charge).
REFERENCE_TIER1_RATIO = 0.10


def reference_portfolio_terms(
    *,
    loan_volume: float,
    deposit_volume: float,
    loan_interest_income: float,
    credit_loss: float,
    deposit_interest_cost: float,
    scenario: FundingScenario,
    capital_charge: Optional[float] = None,
) -> ReferenceTerms:
    """Canonical single-period objective from aggregate volumes, by hand."""
    if capital_charge is None:
        capital_charge = (
            loan_volume
            * scenario.loan_rwa_weight
            * REFERENCE_TIER1_RATIO
            * scenario.capital_hurdle_rate
        )
    wholesale_funded = loan_volume - deposit_volume if loan_volume > deposit_volume else 0.0
    excess_deposits = deposit_volume - loan_volume if deposit_volume > loan_volume else 0.0
    required = loan_volume * scenario.required_deposit_funding_ratio
    shortfall = required - deposit_volume if required > deposit_volume else 0.0
    wholesale_cost = wholesale_funded * scenario.wholesale_funding_rate
    excess_income = excess_deposits * scenario.cash_rate
    penalty = shortfall * scenario.funding_shortfall_penalty_rate
    total = (
        loan_interest_income
        - credit_loss
        - capital_charge
        - deposit_interest_cost
        - wholesale_cost
        - penalty
        + excess_income
    )
    # ROE as documented: profit before the capital charge over required tier-1 capital, in
    # the documented operation order. ``total + capital_charge`` is algebraically equal but
    # not bit-equal, and an ROE floor set exactly at a candidate's ROE (a targeted boundary
    # case) must classify that candidate the same way the engine does.
    required_capital = loan_volume * scenario.loan_rwa_weight * REFERENCE_TIER1_RATIO
    profit_before_capital = (
        loan_interest_income
        - credit_loss
        - deposit_interest_cost
        - wholesale_cost
        - penalty
        + excess_income
    )
    roe = profit_before_capital / required_capital if required_capital > 0 else 0.0
    return ReferenceTerms(
        loan_interest_income=loan_interest_income,
        expected_credit_loss=credit_loss,
        capital_charge=capital_charge,
        deposit_interest_cost=deposit_interest_cost,
        wholesale_funding_cost=wholesale_cost,
        excess_deposit_income=excess_income,
        funding_shortfall=shortfall,
        funding_shortfall_penalty=penalty,
        total_objective=total,
        return_on_equity=roe,
    )


def reference_logistic(utility: np.ndarray) -> np.ndarray:
    """Logistic link with the documented +/-40 utility clip."""
    clipped = np.clip(utility, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def reference_loan_probability(customers, offered, competitor, own, gap, coef) -> np.ndarray:
    """docs/modelling_assumptions.md -> loan acceptance utility, restated by hand."""
    utility = (
        coef.intercept
        + own * (offered - coef.rate_anchor)
        + gap * (offered - competitor)
        + coef.risk_score_coef * customers["risk_score"].to_numpy()
        + coef.credit_score_coef * (customers["credit_score"].to_numpy() - 700.0) / 100.0
        + coef.lvr_coef * (customers["lvr"].to_numpy() - 0.75) / 0.10
    )
    return reference_logistic(utility)


def reference_deposit_probability(customers, offered, competitor, own, gap, coef) -> np.ndarray:
    """docs/modelling_assumptions.md -> deposit uptake utility, restated by hand."""
    utility = (
        coef.intercept
        + own * (offered - coef.rate_anchor)
        + gap * (offered - competitor)
        + coef.rate_sensitivity_coef * customers["rate_sensitivity"].to_numpy()
        + coef.relationship_coef * np.minimum(customers["relationship_years"].to_numpy(), 15.0)
        + coef.digital_coef * customers["digital_engagement"].to_numpy()
    )
    return reference_logistic(utility)


# --------------------------------------------------------- segmented tier candidates


@dataclass(frozen=True)
class TierSpec:
    """Plain-data tier candidates for the brute-force reference (no production types)."""

    rates: tuple[float, ...]
    volume: tuple[float, ...]
    interest: tuple[float, ...]  # loan interest income or deposit interest cost
    credit_loss: tuple[float, ...] = ()


def _tier_volumes(draw, n: int, falling: bool) -> tuple[float, ...]:
    """Either arbitrary non-negative volumes, or a rate response: loan volume falling and
    deposit volume rising with the rate, at comparable scales. The second shape is what
    creates live loan/deposit/funding trade-offs (the OPT-002 defect class)."""
    if draw(st.booleans()):
        return tuple(draw(st.lists(volumes, min_size=n, max_size=n)))
    base = draw(_floats(1e7, 2e9))
    step = draw(_floats(0.0, 0.6))
    factors = [(1.0 - step) ** i if falling else (1.0 + step) ** i for i in range(n)]
    return tuple(base * f for f in factors)


@st.composite
def loan_tier_specs(draw, min_rates: int = 1, max_rates: int = 3) -> TierSpec:
    start, _, step = draw(rate_grids(min_points=1, max_points=1, lo=0.03))
    n = draw(st.integers(min_rates, max_rates))
    tier_rates = tuple(round(start + i * step, 6) for i in range(n))
    tier_volumes = _tier_volumes(draw, n, falling=True)
    loss_rate = draw(_floats(0.0, 0.03))
    return TierSpec(
        rates=tier_rates,
        volume=tier_volumes,
        interest=tuple(r * v for r, v in zip(tier_rates, tier_volumes)),
        credit_loss=tuple(v * loss_rate for v in tier_volumes),
    )


@st.composite
def deposit_tier_specs(draw, min_rates: int = 1, max_rates: int = 3) -> TierSpec:
    start, _, step = draw(rate_grids(min_points=1, max_points=1, lo=0.01))
    n = draw(st.integers(min_rates, max_rates))
    tier_rates = tuple(round(start + i * step, 6) for i in range(n))
    tier_volumes = _tier_volumes(draw, n, falling=False)
    return TierSpec(
        rates=tier_rates,
        volume=tier_volumes,
        interest=tuple(r * v for r, v in zip(tier_rates, tier_volumes)),
    )


@dataclass(frozen=True)
class BruteForceWinner:
    loan_indices: tuple[int, ...]
    deposit_indices: tuple[int, ...]
    meets_floor: bool
    terms: ReferenceTerms


def _sum_in_tier_order(values: Iterable[float]) -> float:
    # Python 3.12 sum() compensates for rounding on floats. Keep documented
    # tier order and ordinary additions so an exact ROE floor stays exact.
    total = 0.0
    for value in values:
        total += value
    return total


def brute_force_segmented(
    loan_tiers: Sequence[TierSpec],
    deposit_tiers: Sequence[TierSpec],
    scenario: FundingScenario,
    min_return_on_equity: Optional[float] = None,
) -> list[BruteForceWinner]:
    """Every tier-rate combination, scored by hand, sorted best-first.

    Ranking is the documented constraint-first key: meets the ROE floor (when configured),
    then objective, then lower funding shortfall. Remaining ties are left to the caller.
    """
    scored: list[BruteForceWinner] = []
    for loan_choice in product(*(range(len(t.rates)) for t in loan_tiers)):
        for deposit_choice in product(*(range(len(t.rates)) for t in deposit_tiers)):
            loan_volume = _sum_in_tier_order(t.volume[i] for t, i in zip(loan_tiers, loan_choice))
            terms = reference_portfolio_terms(
                loan_volume=loan_volume,
                deposit_volume=_sum_in_tier_order(
                    t.volume[i] for t, i in zip(deposit_tiers, deposit_choice)
                ),
                loan_interest_income=_sum_in_tier_order(
                    t.interest[i] for t, i in zip(loan_tiers, loan_choice)
                ),
                credit_loss=_sum_in_tier_order(
                    t.credit_loss[i] for t, i in zip(loan_tiers, loan_choice)
                ),
                deposit_interest_cost=_sum_in_tier_order(
                    t.interest[i] for t, i in zip(deposit_tiers, deposit_choice)
                ),
                scenario=scenario,
            )
            meets = min_return_on_equity is None or terms.return_on_equity >= (
                min_return_on_equity
            )
            scored.append(BruteForceWinner(loan_choice, deposit_choice, meets, terms))
    scored.sort(
        key=lambda w: (w.meets_floor, w.terms.total_objective, -w.terms.funding_shortfall),
        reverse=True,
    )
    return scored
