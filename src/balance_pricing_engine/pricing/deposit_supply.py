"""Fixed NZD deposit opportunity and annualised funding for synthetic simulations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Annotated, Literal, Optional

import numpy as np
import pandas as pd
from pydantic import ConfigDict, Field, StringConstraints, model_validator

from balance_pricing_engine.config import EconomicConfig
from balance_pricing_engine.pricing.profit import FloatOrArray, non_negative_part

_Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
_Probability = Annotated[float, Field(ge=0, le=1)]
_NonNegative = Annotated[float, Field(ge=0)]


class DepositFlow(str, Enum):
    """Exclusive economic source of funded principal over the scenario horizon."""

    EXTERNAL_NEW_MONEY = "EXTERNAL_NEW_MONEY"
    RETAINED_MATURITY = "RETAINED_MATURITY"
    INTERNAL_MIGRATION = "INTERNAL_MIGRATION"
    EXISTING_BALANCE = "EXISTING_BALANCE"


class ConcentrationMode(str, Enum):
    """Treatment of an illustrative exposure limit for the priced term."""

    HARD_LIMIT = "HARD_LIMIT"
    SOFT_PENALTY = "SOFT_PENALTY"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class _SupplyConfig(EconomicConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DepositOpportunitySegment(_SupplyConfig):
    """One disjoint pool in the horizon, selected using raw individual balances.

    Balance intervals are [balance_min, balance_max), matching pricing-tier upper
    boundaries. Channel and relationship labels select synthetic response strata.
    Differing flow labels partition the declared NZD opportunity. The same
    numerical sample can represent different economic pools.
    """

    segment_id: _Identifier
    opportunity_nzd: _NonNegative
    term_months: int = Field(default=12, gt=0)
    channel: Literal["all", "digital", "assisted"] = "all"
    customer_segment: Literal["all", "short_relationship", "established"] = "all"
    balance_min: _NonNegative = 0.0
    balance_max: Optional[float] = Field(default=None, gt=0)
    flow: DepositFlow = DepositFlow.EXTERNAL_NEW_MONEY
    reachable_share: _Probability = 1.0
    eligible_participation: _Probability = 1.0
    conversion_multiplier: _NonNegative = 1.0
    funding_eligibility: _Probability = 1.0
    stock_maturity_fraction: Optional[_Probability] = None
    maturity_runoff_probability: Optional[_Probability] = None
    source_interest_rate: _Probability = 0.0
    acquisition_cost_rate: _Probability = 0.0

    @model_validator(mode="after")
    def validate_segment(self) -> DepositOpportunitySegment:
        if self.balance_max is not None and self.balance_max <= self.balance_min:
            raise ValueError("Segment balance_max must exceed balance_min.")
        if (
            self.flow != DepositFlow.RETAINED_MATURITY
            and self.maturity_runoff_probability is not None
        ):
            raise ValueError("A maturity runoff probability requires a retained maturity segment.")
        if self.flow == DepositFlow.EXTERNAL_NEW_MONEY and self.source_interest_rate != 0:
            raise ValueError("External new money has no source-product interest expense.")
        return self

    @property
    def addressable_flow(self) -> float:
        if self.stock_maturity_fraction is None:
            return self.opportunity_nzd
        return self.opportunity_nzd * self.stock_maturity_fraction

    @property
    def eligible_opportunity(self) -> float:
        return self.addressable_flow * self.reachable_share * self.eligible_participation

    @property
    def incremental_fraction(self) -> float:
        if self.flow == DepositFlow.EXTERNAL_NEW_MONEY:
            return 1.0
        return self.maturity_runoff_probability or 0.0

    @property
    def opening_pool(self) -> float:
        # An offer's narrower reach does not shrink the existing book or its maturity wall.
        return 0.0 if self.flow == DepositFlow.EXTERNAL_NEW_MONEY else self.addressable_flow

    @property
    def counterfactual_runoff(self) -> float:
        return self.opening_pool * (self.maturity_runoff_probability or 0.0)


class DepositConcentrationPolicy(_SupplyConfig):
    """Limit on incremental usable exposure in this profile's one priced term."""

    mode: ConcentrationMode
    limit_nzd: _NonNegative
    penalty_rate: _Probability = 0.0

    @model_validator(mode="after")
    def validate_penalty(self) -> DepositConcentrationPolicy:
        if (self.mode == ConcentrationMode.SOFT_PENALTY) != (self.penalty_rate > 0):
            raise ValueError("A positive penalty rate is required only for SOFT_PENALTY.")
        return self


class DepositMarketProfile(_SupplyConfig):
    """An explicitly illustrative scenario with one priced product and term.

    Each segment declares its own incremental or opening-book capacity for a
    shared horizon. No segment amount is inferred from synthetic customer counts.
    """

    profile_id: _Identifier
    version: _Identifier
    opportunity_basis: Literal["FLOW", "STOCK"]
    horizon_months: int = Field(default=12, gt=0)
    product: Literal["term_deposit"] = "term_deposit"
    currency: Literal["NZD"] = "NZD"
    source_classification: Literal["SYNTHETIC / UNCALIBRATED"] = "SYNTHETIC / UNCALIBRATED"
    validation_status: Literal["ILLUSTRATIVE_ONLY"] = "ILLUSTRATIVE_ONLY"
    evidence_status: Literal["UNCALIBRATED"] = "UNCALIBRATED"
    review_notes: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    segments: tuple[DepositOpportunitySegment, ...] = Field(min_length=1)
    opening_funding_nzd: _NonNegative = 0.0
    opening_unallocated_interest_rate: _Probability = 0.0
    concentration: Optional[DepositConcentrationPolicy] = None

    @model_validator(mode="after")
    def validate_profile(self) -> DepositMarketProfile:
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("Deposit segment identifiers must be unique.")
        if len({segment.term_months for segment in self.segments}) != 1:
            raise ValueError("Deposit opportunity segments must have one comparable priced term.")
        is_stock = self.opportunity_basis == "STOCK"
        if any(
            (segment.stock_maturity_fraction is not None) != is_stock for segment in self.segments
        ):
            raise ValueError(
                "STOCK profiles require a horizon maturity fraction in every segment. "
                "FLOW profiles must omit it."
            )
        for left, right in combinations(self.segments, 2):
            if _segments_overlap(left, right):
                raise ValueError(
                    f"Deposit segments {left.segment_id} and {right.segment_id} overlap."
                )
        _finite_sum([segment.opportunity_nzd for segment in self.segments])
        _finite_sum(
            [
                self.opening_funding_nzd,
                *(
                    segment.eligible_opportunity
                    for segment in self.segments
                    if segment.flow == DepositFlow.EXTERNAL_NEW_MONEY
                ),
            ]
        )
        if self.allocated_opening_funding > self.opening_funding_nzd:
            raise ValueError("Opening funding must cover all existing and maturity source pools.")
        return self

    @property
    def term_months(self) -> int:
        return self.segments[0].term_months

    @property
    def allocated_opening_funding(self) -> float:
        return _finite_sum([segment.opening_pool for segment in self.segments])


def _segments_overlap(left: DepositOpportunitySegment, right: DepositOpportunitySegment) -> bool:
    return (
        left.flow == right.flow
        and _strata_overlap(left.channel, right.channel)
        and _strata_overlap(left.customer_segment, right.customer_segment)
        and (left.balance_max is None or right.balance_min < left.balance_max)
        and (right.balance_max is None or left.balance_min < right.balance_max)
    )


def _strata_overlap(left: str, right: str) -> bool:
    return left == right or "all" in (left, right)


def _finite_sum(values: Sequence[float]) -> float:
    try:
        total = math.fsum(values)
    except OverflowError as error:
        raise ValueError("Deposit supply totals must be finite.") from error
    if not math.isfinite(total):
        raise ValueError("Deposit supply totals must be finite.")
    return total


@dataclass(frozen=True)
class DepositFlowAttribution:
    """Annualised interest, acquisition expense and principal for one segment."""

    segment_id: str
    flow: DepositFlow
    gross_booked: float
    existing_funding_displaced: float
    net_incremental_funding: float
    eligible_incremental_funding: float
    gross_interest_cost: float
    counterfactual_source_interest_cost: float
    acquisition_cost: float
    unsupported_funding: float

    @property
    def incremental_interest_cost(self) -> float:
        return self.gross_interest_cost - self.counterfactual_source_interest_cost


@dataclass(frozen=True)
class DepositOffer:
    """Additive per-tier offer outputs, before applying portfolio funding controls."""

    gross: float
    incremental: float
    interest_cost: float
    acquisition_cost: float
    flows: tuple[DepositFlowAttribution, ...]
    _positions: np.ndarray = field(repr=False, compare=False)
    _supply: DepositSupply = field(repr=False, compare=False)


@dataclass(frozen=True)
class DepositPortfolio:
    """Active annualised deposit expenses and usable funding for candidate portfolios."""

    gross: FloatOrArray
    usable_funding: FloatOrArray
    deposit_cost: FloatOrArray
    acquisition_cost: FloatOrArray
    concentration_penalty: FloatOrArray
    concentration_excess: FloatOrArray
    constrained_funding: FloatOrArray


@dataclass(frozen=True)
class DepositSupplyEstimate:
    """Fixed-opportunity and reconciled funding evidence for one selected portfolio."""

    addressable_flow: float
    eligible_opportunity: float
    eligible_incremental_opportunity: float
    sample_count: int
    flows: tuple[DepositFlowAttribution, ...]
    gross_bookings: float
    existing_funding_displaced: float
    net_incremental_funding: float
    eligible_incremental_funding: float
    opening_funding: float
    counterfactual_runoff: float
    actual_runoff: float
    reference_funding: float
    active_funding: float
    usable_funding: float
    gross_interest_cost: float
    counterfactual_source_interest_cost: float
    incremental_interest_cost: float
    reference_interest_cost: float
    active_interest_cost: float
    acquisition_cost: float
    unsupported_funding: float
    concentration_excess: float
    constrained_funding: float
    concentration_penalty: float
    concentration_binding: bool
    maturity_mismatch: bool


def _sample_weights(
    segment: DepositOpportunitySegment, customers: pd.DataFrame, balances: np.ndarray
) -> np.ndarray:
    mask = (balances >= segment.balance_min) & (balances > 0)
    if segment.balance_max is not None:
        mask &= balances < segment.balance_max
    if segment.channel != "all":
        engagement = _sample_feature(customers, "digital_engagement", upper=1)
        mask &= (engagement >= 0.5) == (segment.channel == "digital")
    if segment.customer_segment != "all":
        relationship = _sample_feature(customers, "relationship_years")
        mask &= (relationship >= 5) == (segment.customer_segment == "established")

    weights = np.zeros(len(customers), dtype=float)
    opportunity = segment.eligible_opportunity
    if opportunity == 0:
        return weights
    selected = balances[mask]
    if not len(selected):
        raise ValueError(f"Segment {segment.segment_id} has no positive sample balance.")
    # Scaling first protects the normalising sum when represented balances are large.
    relative_balance = selected / selected.max()
    weights[mask] = (relative_balance / np.sum(relative_balance)) * opportunity
    return weights


def _sample_feature(customers: pd.DataFrame, name: str, *, upper: float = math.inf) -> np.ndarray:
    if name not in customers:
        raise ValueError(f"Deposit sample must contain {name}.")
    values = customers[name].to_numpy(dtype=float)
    if not np.all(np.isfinite(values) & (values >= 0) & (values <= upper)):
        raise ValueError(
            f"Deposit sample {name} must be finite and non-negative (maximum {upper})."
        )
    return values


def _flow_attribution(
    segment: DepositOpportunitySegment, gross: float, rate: float
) -> DepositFlowAttribution:
    incremental = gross * segment.incremental_fraction
    displaced = gross - incremental
    eligible = incremental * segment.funding_eligibility
    unsupported = incremental - eligible
    if (
        segment.flow == DepositFlow.RETAINED_MATURITY
        and segment.maturity_runoff_probability is None
    ):
        unsupported = gross
    return DepositFlowAttribution(
        segment_id=segment.segment_id,
        flow=segment.flow,
        gross_booked=gross,
        existing_funding_displaced=displaced,
        net_incremental_funding=incremental,
        eligible_incremental_funding=eligible,
        gross_interest_cost=gross * rate,
        counterfactual_source_interest_cost=displaced * segment.source_interest_rate,
        acquisition_cost=gross * segment.acquisition_cost_rate,
        unsupported_funding=unsupported,
    )


class DepositSupply:
    """Bind the validated opportunity to the full sample before any tier selection."""

    def __init__(self, profile: DepositMarketProfile, customers: pd.DataFrame) -> None:
        self._profile = DepositMarketProfile.model_validate(profile)
        if not customers.index.is_unique:
            raise ValueError("Deposit sample row indices must be unique.")
        self._index = customers.index.copy(deep=True)
        balances = _sample_feature(customers, "deposit_amount")
        self._weights = tuple(
            _sample_weights(segment, customers, balances) for segment in self._profile.segments
        )
        self._eligible_opportunity = _finite_sum(
            [segment.eligible_opportunity for segment in self._profile.segments]
        )
        self._eligible_incremental_opportunity = _finite_sum(
            [
                segment.eligible_opportunity
                * segment.incremental_fraction
                * segment.funding_eligibility
                for segment in self._profile.segments
            ]
        )
        self._counterfactual_runoff = _finite_sum(
            [segment.counterfactual_runoff for segment in self._profile.segments]
        )
        self._reference_funding = self._profile.opening_funding_nzd - self._counterfactual_runoff
        unallocated = self._profile.opening_funding_nzd - self._profile.allocated_opening_funding
        self._reference_interest = _finite_sum(
            [
                unallocated * self._profile.opening_unallocated_interest_rate,
                *(
                    (segment.opening_pool - segment.counterfactual_runoff)
                    * segment.source_interest_rate
                    for segment in self._profile.segments
                ),
            ]
        )

    @property
    def profile(self) -> DepositMarketProfile:
        """The validated profile this supply was built from (bound to diagnostics)."""
        return self._profile

    @property
    def eligible_opportunity(self) -> float:
        return self._eligible_opportunity

    @property
    def weights(self) -> np.ndarray:
        """Economic opportunity per numerical row, summed over disjoint pools."""
        weights = np.sum(self._weights, axis=0)
        weights.setflags(write=False)
        return weights

    def offer(
        self, customers: pd.DataFrame, probabilities: np.ndarray, rate: float
    ) -> DepositOffer:
        """Evaluate rate-dependent conversion on a subset of the original sample."""
        if not customers.index.is_unique:
            raise ValueError("Deposit offer row indices must be unique.")
        positions = self._index.get_indexer(customers.index)
        if np.any(positions < 0):
            raise ValueError("Deposit offer rows must be from the original sample.")
        probabilities = np.asarray(probabilities, dtype=float)
        if probabilities.shape != (len(positions),) or not np.all(
            np.isfinite(probabilities) & (probabilities >= 0) & (probabilities <= 1)
        ):
            raise ValueError(
                "Deposit uptake probabilities must align with the offer and lie in [0, 1]."
            )
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("Deposit offer rate must be finite and non-negative.")

        flows = []
        for segment, weights in zip(self._profile.segments, self._weights):
            conversion = np.minimum(probabilities * segment.conversion_multiplier, 1.0)
            gross = min(
                float(np.sum(weights[positions] * conversion)), segment.eligible_opportunity
            )
            flows.append(_flow_attribution(segment, gross, rate))
        positions.setflags(write=False)
        return DepositOffer(
            gross=_finite_sum([flow.gross_booked for flow in flows]),
            incremental=_finite_sum([flow.eligible_incremental_funding for flow in flows]),
            interest_cost=_finite_sum([flow.incremental_interest_cost for flow in flows]),
            acquisition_cost=_finite_sum([flow.acquisition_cost for flow in flows]),
            flows=tuple(flows),
            _positions=positions,
            _supply=self,
        )

    def portfolio(
        self,
        gross: FloatOrArray,
        incremental: FloatOrArray,
        interest_cost: FloatOrArray,
        acquisition_cost: FloatOrArray,
    ) -> DepositPortfolio:
        """Apply any cap once, after all tiers have been pooled.

        The loan side is one horizon of new business with no opening asset book,
        so the opening deposit position and its interest are not offset against
        it. Only recognised incremental funding reaches the wholesale, surplus and
        shortfall terms, and only incremental interest reaches the objective. The
        signed incremental interest can be negative when an offer reprices
        displaced source balances downward.
        """
        # Summing separately rounded tier candidates can produce a one-ULP overrun.
        gross = np.minimum(gross, self.eligible_opportunity)
        incremental = np.minimum(incremental, self._eligible_incremental_opportunity)
        if not np.all(np.isfinite(interest_cost)):
            raise ValueError("Incremental deposit interest expense must be finite.")
        policy = self._profile.concentration
        excess = non_negative_part(incremental - policy.limit_nzd) if policy else 0.0
        hard_limit = policy is not None and policy.mode == ConcentrationMode.HARD_LIMIT
        constrained = excess if hard_limit else 0.0
        recognised = np.minimum(incremental, policy.limit_nzd) if hard_limit else incremental
        penalty = excess * policy.penalty_rate if policy else 0.0
        return DepositPortfolio(
            gross=gross,
            usable_funding=recognised,
            deposit_cost=interest_cost,
            acquisition_cost=acquisition_cost,
            concentration_penalty=penalty,
            concentration_excess=excess,
            constrained_funding=constrained,
        )

    def estimate(self, offers: Sequence[DepositOffer]) -> DepositSupplyEstimate:
        """Compile selected offer evidence, checking that sample tiers do not overlap."""
        if not offers or any(offer._supply is not self for offer in offers):
            raise ValueError("An estimate requires offers from this deposit supply.")
        positions = np.concatenate([offer._positions for offer in offers])
        if len(np.unique(positions)) != len(positions):
            raise ValueError("Deposit offers must not book the same sample position twice.")

        flows = tuple(flow for offer in offers for flow in offer.flows)
        gross = _finite_sum([offer.gross for offer in offers])
        incremental = _finite_sum([offer.incremental for offer in offers])
        interest = _finite_sum([offer.interest_cost for offer in offers])
        acquisition = _finite_sum([offer.acquisition_cost for offer in offers])
        portfolio = self.portfolio(gross, incremental, interest, acquisition)
        net_incremental = min(
            float(portfolio.gross), _finite_sum([flow.net_incremental_funding for flow in flows])
        )
        avoided_runoff = _finite_sum(
            [
                flow.net_incremental_funding
                for flow in flows
                if flow.flow == DepositFlow.RETAINED_MATURITY
            ]
        )
        return DepositSupplyEstimate(
            addressable_flow=_finite_sum(
                [segment.addressable_flow for segment in self._profile.segments]
            ),
            eligible_opportunity=self.eligible_opportunity,
            eligible_incremental_opportunity=self._eligible_incremental_opportunity,
            sample_count=len(self._index),
            flows=flows,
            gross_bookings=float(portfolio.gross),
            existing_funding_displaced=float(portfolio.gross) - net_incremental,
            net_incremental_funding=net_incremental,
            eligible_incremental_funding=min(incremental, self._eligible_incremental_opportunity),
            opening_funding=self._profile.opening_funding_nzd,
            counterfactual_runoff=self._counterfactual_runoff,
            actual_runoff=max(0.0, self._counterfactual_runoff - avoided_runoff),
            reference_funding=self._reference_funding,
            active_funding=self._reference_funding + net_incremental,
            usable_funding=float(portfolio.usable_funding),
            gross_interest_cost=_finite_sum([flow.gross_interest_cost for flow in flows]),
            counterfactual_source_interest_cost=_finite_sum(
                [flow.counterfactual_source_interest_cost for flow in flows]
            ),
            incremental_interest_cost=interest,
            reference_interest_cost=self._reference_interest,
            # Bank-level disclosure: the opening book's interest plus the offer's change.
            active_interest_cost=float(non_negative_part(self._reference_interest + interest)),
            acquisition_cost=acquisition,
            unsupported_funding=_finite_sum([flow.unsupported_funding for flow in flows]),
            concentration_excess=float(portfolio.concentration_excess),
            constrained_funding=float(portfolio.constrained_funding),
            concentration_penalty=float(portfolio.concentration_penalty),
            concentration_binding=bool(portfolio.concentration_excess),
            maturity_mismatch=self._profile.term_months < self._profile.horizon_months,
        )
