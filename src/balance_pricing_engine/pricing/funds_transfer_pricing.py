"""Term-matched funds transfer pricing (FTP) for synthetic simulations (FIN-003, Phase 4d).

``FTP(tenor, as_of) = term-matched reference(tenor) + liquidity premium(tenor) + documented
adjustments(tenor)``. Every quote here is illustrative and synthetic. No observed NZ market
quote is stored, and nothing in this module is approved for production pricing.

Conventions (see ``docs/funds_transfer_pricing.md``):

- Rates are annualised simple rates (``annual_simple``, ACT/365F), the same basis as the
  engine's annual accrual. Quotes keep their declared unit and convert to fractions on
  lookup. Any other compounding convention is rejected.
- Interpolation is opt-in and linear in tenor months, separately on the reference rate, the
  liquidity premium and each named adjustment. It is disclosed on every quote. A tenor
  declared as *required* must be quoted exactly; extrapolation is never performed.
- External funding rates (residual wholesale deficit, surplus reinvestment) are the only
  rates that reach the consolidated objective. Loan and deposit FTP rates are internal
  attribution and cancel against the Treasury book on consolidation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import math
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints, model_validator

from balance_pricing_engine.config import EconomicConfig
from balance_pricing_engine.synthetic.market import MarketState

SYNTHETIC_FTP_LABEL = "SYNTHETIC / UNCALIBRATED"
SUPPORTED_CURRENCIES = ("NZD",)
SUPPORTED_COMPOUNDING = ("annual_simple",)
#: Candidate maturities for illustration, not production curve assumptions.
ILLUSTRATIVE_TENORS_MONTHS = (3, 6, 12, 24, 36, 60)

_Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_Tenor = Annotated[int, Field(gt=0, le=600)]


class RateUnit(str, Enum):
    """The unit every quote on a curve is expressed in. There is no default."""

    FRACTION = "fraction"
    PERCENT = "percent"
    BASIS_POINTS = "basis_points"


_UNIT_SCALE = {RateUnit.FRACTION: 1.0, RateUnit.PERCENT: 0.01, RateUnit.BASIS_POINTS: 0.0001}
# A quote above this magnitude cannot plausibly be in the declared unit (for example 3.2
# declared as a fraction is almost certainly a percentage), so the unit is ambiguous.
_UNIT_PLAUSIBLE_LIMIT = {
    RateUnit.FRACTION: 0.5,
    RateUnit.PERCENT: 50.0,
    RateUnit.BASIS_POINTS: 5_000.0,
}


class Interpolation(str, Enum):
    EXACT_ONLY = "exact_only"
    LINEAR_IN_TENOR = "linear_in_tenor"


class _FTPModel(EconomicConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FTPAdjustment(_FTPModel):
    """A named, documented addition to the funding leg at one tenor (curve units)."""

    name: _Identifier
    value: float
    rationale: _Text


class FTPCurvePoint(_FTPModel):
    tenor_months: _Tenor
    reference_rate: float
    liquidity_premium: float
    adjustments: tuple[FTPAdjustment, ...] = ()

    @model_validator(mode="after")
    def _validate_point(self) -> FTPCurvePoint:
        names = [adjustment.name for adjustment in self.adjustments]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate adjustment name at tenor {self.tenor_months} months.")
        if self.liquidity_premium < 0:
            raise ValueError(
                f"Liquidity premium at tenor {self.tenor_months} months must be non-negative."
            )
        return self


@dataclass(frozen=True)
class FTPQuote:
    """One looked-up tenor, converted to fractions, with its derivation disclosed."""

    tenor_months: int
    reference_rate: float
    liquidity_premium: float
    adjustments: tuple[tuple[str, float], ...]
    interpolated: bool
    bracket_months: tuple[int, int] | None

    @property
    def adjustment_total(self) -> float:
        return math.fsum(value for _, value in self.adjustments)

    @property
    def funding_spread(self) -> float:
        """Liquidity premium plus adjustments: the funding leg above the reference."""
        return self.liquidity_premium + self.adjustment_total

    @property
    def all_in_rate(self) -> float:
        return self.reference_rate + self.liquidity_premium + self.adjustment_total


class FTPCurve(_FTPModel):
    """A dated, versioned NZD term curve with explicit units and lookup policy."""

    curve_id: _Identifier
    version: _Identifier
    currency: str
    as_of: AwareDatetime
    source: _Text
    instrument: _Text
    source_classification: Literal["SYNTHETIC / UNCALIBRATED"] = SYNTHETIC_FTP_LABEL
    rate_unit: RateUnit
    compounding: str
    day_count: Literal["ACT/365F"] = "ACT/365F"
    interpolation: Interpolation = Interpolation.EXACT_ONLY
    extrapolation: Literal["forbid"] = "forbid"
    max_age_days: int = Field(gt=0)
    required_tenors_months: tuple[_Tenor, ...] = ()
    allow_negative_rates: bool = False
    points: tuple[FTPCurvePoint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_curve(self) -> FTPCurve:
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Currency {self.currency} is not supported; FTP curves must be "
                f"{', '.join(SUPPORTED_CURRENCIES)} to match the engine's balances."
            )
        if self.compounding not in SUPPORTED_COMPOUNDING:
            raise ValueError(
                f"Compounding convention {self.compounding!r} is not supported; use "
                f"{', '.join(SUPPORTED_COMPOUNDING)}."
            )
        tenors = [point.tenor_months for point in self.points]
        for previous, current in zip(tenors, tenors[1:]):
            if current == previous:
                raise ValueError(f"Duplicate FTP tenor {current} months.")
            if current < previous:
                raise ValueError("FTP curve tenors must be strictly increasing.")
        limit = _UNIT_PLAUSIBLE_LIMIT[self.rate_unit]
        for point in self.points:
            values = (
                point.reference_rate,
                point.liquidity_premium,
                *(adjustment.value for adjustment in point.adjustments),
            )
            if any(abs(value) > limit for value in values):
                raise ValueError(
                    f"A quote at {point.tenor_months} months exceeds {limit:g} "
                    f"{self.rate_unit.value}; the rate unit is ambiguous."
                )
            if point.reference_rate < 0 and not self.allow_negative_rates:
                raise ValueError(
                    f"Negative reference rate at {point.tenor_months} months requires "
                    "allow_negative_rates."
                )
        for tenor in self.required_tenors_months:
            if tenor not in tenors:
                raise ValueError(
                    f"Required FTP tenor {tenor} months is missing; neighbouring tenors are "
                    "never substituted."
                )
        return self

    @property
    def key(self) -> str:
        """Identity that keeps curves with different dates or versions distinct."""
        return f"{self.curve_id}@{self.version}#{self.as_of.isoformat()}"

    def check_valid_at(self, valuation_time: datetime) -> None:
        """Reject look-ahead quotes and quotes older than the validity window."""
        if valuation_time.tzinfo is None or valuation_time.utcoffset() is None:
            raise ValueError("FTP valuation time must be timezone-aware.")
        if self.as_of > valuation_time:
            raise ValueError(
                f"FTP curve {self.key} is dated after the valuation time {valuation_time}; "
                "look-ahead quotes are prohibited."
            )
        if valuation_time - self.as_of > timedelta(days=self.max_age_days):
            raise ValueError(
                f"FTP curve {self.key} is stale at {valuation_time}: older than "
                f"{self.max_age_days} days."
            )

    def quote(self, tenor_months: int) -> FTPQuote:
        """Return the exact quote, or a disclosed interpolation when the policy allows."""
        scale = _UNIT_SCALE[self.rate_unit]
        by_tenor = {point.tenor_months: point for point in self.points}
        if tenor_months in by_tenor:
            return _scaled_quote(by_tenor[tenor_months], scale)
        lowest, highest = self.points[0].tenor_months, self.points[-1].tenor_months
        if not lowest < tenor_months < highest:
            raise ValueError(
                f"FTP tenor {tenor_months} months is outside the curve ({lowest}-{highest} "
                "months); extrapolation is not permitted."
            )
        if self.interpolation is Interpolation.EXACT_ONLY:
            raise ValueError(
                f"FTP tenor {tenor_months} months is not quoted and the curve allows exact "
                "lookups only."
            )
        left = [point for point in self.points if point.tenor_months < tenor_months][-1]
        right = next(point for point in self.points if point.tenor_months > tenor_months)
        return _interpolated_quote(left, right, tenor_months, scale)


def _scaled_quote(point: FTPCurvePoint, scale: float) -> FTPQuote:
    return FTPQuote(
        tenor_months=point.tenor_months,
        reference_rate=point.reference_rate * scale,
        liquidity_premium=point.liquidity_premium * scale,
        adjustments=tuple((item.name, item.value * scale) for item in point.adjustments),
        interpolated=False,
        bracket_months=None,
    )


def _interpolated_quote(
    left: FTPCurvePoint, right: FTPCurvePoint, tenor: int, scale: float
) -> FTPQuote:
    if [item.name for item in left.adjustments] != [item.name for item in right.adjustments]:
        raise ValueError(
            f"Interpolating {tenor} months requires the same adjustments at "
            f"{left.tenor_months} and {right.tenor_months} months."
        )
    weight = (tenor - left.tenor_months) / (right.tenor_months - left.tenor_months)

    def between(low: float, high: float) -> float:
        return (low + weight * (high - low)) * scale

    return FTPQuote(
        tenor_months=tenor,
        reference_rate=between(left.reference_rate, right.reference_rate),
        liquidity_premium=between(left.liquidity_premium, right.liquidity_premium),
        adjustments=tuple(
            (low.name, between(low.value, high.value))
            for low, high in zip(left.adjustments, right.adjustments)
        ),
        interpolated=True,
        bracket_months=(left.tenor_months, right.tenor_months),
    )


class FTPLiquidityBucket(_FTPModel):
    """Share of the loan's funding requirement assigned to one liquidity tenor."""

    tenor_months: _Tenor
    weight: float = Field(gt=0, le=1)


class FTPFundingProfile(_FTPModel):
    """Synthetic tenor matching. Repricing, liquidity and wholesale tenors are separate.

    - ``loan_repricing_tenor_months``: the fixed-rate period. It selects the reference rate
      for the interest-rate component only.
    - ``loan_liquidity_buckets``: an illustrative funding profile of the remaining principal
      (amortisation, prepayment, refinancing). Liquidity premium and adjustments are the
      weighted sum over these buckets. The whole mortgage is never assumed to mature at the
      end of its fixed period.
    - ``deposit_contractual_tenor_months``: the term deposit is credited at its contractual
      maturity. Renewal beyond maturity is not credited without evidence.
    - ``wholesale_funding_tenor_months``: the marginal external funding tenor for the
      aggregate residual deficit.
    - Surplus funds are credited at the reference rate of the reinvestment tenor plus an
      explicit (possibly negative) spread. They never earn the bank's own funding premium.
    """

    profile_id: _Identifier
    version: _Identifier
    source_classification: Literal["SYNTHETIC / UNCALIBRATED"] = SYNTHETIC_FTP_LABEL
    loan_repricing_tenor_months: _Tenor
    loan_liquidity_buckets: tuple[FTPLiquidityBucket, ...] = Field(min_length=1)
    deposit_contractual_tenor_months: _Tenor
    deposit_renewal_basis: Literal["contractual_maturity_only"] = "contractual_maturity_only"
    wholesale_funding_tenor_months: _Tenor
    surplus_reinvestment_tenor_months: _Tenor
    surplus_reinvestment_spread: float = Field(default=0.0, ge=-0.05, le=0.05)
    allocation_policy: Literal["locked_at_origination"] = "locked_at_origination"
    review_notes: _Text

    @model_validator(mode="after")
    def _validate_buckets(self) -> FTPFundingProfile:
        tenors = [bucket.tenor_months for bucket in self.loan_liquidity_buckets]
        if len(set(tenors)) != len(tenors):
            raise ValueError("Loan liquidity bucket tenors must be unique.")
        if not math.isclose(
            math.fsum(bucket.weight for bucket in self.loan_liquidity_buckets),
            1.0,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("Loan liquidity bucket weights must sum to one.")
        return self

    @property
    def key(self) -> str:
        return f"{self.profile_id}@{self.version}"

    def rates(self, curve: FTPCurve) -> FTPRateSet:
        """Resolve every tenor in the profile on one curve."""
        repricing = curve.quote(self.loan_repricing_tenor_months)
        buckets = tuple(
            (bucket.weight, curve.quote(bucket.tenor_months))
            for bucket in self.loan_liquidity_buckets
        )
        deposit = curve.quote(self.deposit_contractual_tenor_months)
        wholesale = curve.quote(self.wholesale_funding_tenor_months)
        surplus = curve.quote(self.surplus_reinvestment_tenor_months)
        loan_liquidity = math.fsum(weight * quote.funding_spread for weight, quote in buckets)
        return FTPRateSet(
            curve_key=curve.key,
            profile_key=self.key,
            loan_reference_rate=repricing.reference_rate,
            loan_liquidity_rate=loan_liquidity,
            loan_ftp_rate=repricing.reference_rate + loan_liquidity,
            deposit_ftp_rate=deposit.all_in_rate,
            wholesale_funding_rate=wholesale.all_in_rate,
            surplus_reinvestment_rate=surplus.reference_rate + self.surplus_reinvestment_spread,
            quotes=tuple(
                sorted(
                    {
                        quote.tenor_months: quote
                        for quote in (
                            repricing,
                            deposit,
                            wholesale,
                            surplus,
                            *(quote for _, quote in buckets),
                        )
                    }.values(),
                    key=lambda quote: quote.tenor_months,
                )
            ),
        )


@dataclass(frozen=True)
class FTPRateSet:
    """Resolved internal FTP rates and the external marginal funding rates (fractions)."""

    curve_key: str
    profile_key: str
    loan_reference_rate: float
    loan_liquidity_rate: float
    loan_ftp_rate: float
    deposit_ftp_rate: float
    wholesale_funding_rate: float
    surplus_reinvestment_rate: float
    quotes: tuple[FTPQuote, ...]

    def __post_init__(self) -> None:
        for name in (
            "loan_reference_rate",
            "loan_liquidity_rate",
            "loan_ftp_rate",
            "deposit_ftp_rate",
            "wholesale_funding_rate",
            "surplus_reinvestment_rate",
        ):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"FTP {name} must be finite.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "curve_key": self.curve_key,
            "profile_key": self.profile_key,
            "loan_reference_rate": self.loan_reference_rate,
            "loan_liquidity_rate": self.loan_liquidity_rate,
            "loan_ftp_rate": self.loan_ftp_rate,
            "deposit_ftp_rate": self.deposit_ftp_rate,
            "wholesale_funding_rate": self.wholesale_funding_rate,
            "surplus_reinvestment_rate": self.surplus_reinvestment_rate,
            "quotes": [
                {
                    "tenor_months": quote.tenor_months,
                    "reference_rate": quote.reference_rate,
                    "liquidity_premium": quote.liquidity_premium,
                    "adjustments": [list(item) for item in quote.adjustments],
                    "interpolated": quote.interpolated,
                    "bracket_months": (
                        None if quote.bracket_months is None else list(quote.bracket_months)
                    ),
                }
                for quote in self.quotes
            ],
        }


@dataclass(frozen=True)
class FTPMarketState(MarketState):
    """A market whose external funding rates come from a validated FTP curve.

    Only ``wholesale_funding_rate`` (residual deficit) and ``surplus_reinvestment_rate``
    (investible surplus) differ from the canonical market. ``cash_rate`` stays the policy
    rate. The curve and profile keys travel with the market, so any diagnostics built on
    these rates carry the simulation-only governance warning.
    """

    ftp_wholesale_funding_rate: float = math.nan
    ftp_surplus_reinvestment_rate: float = math.nan
    ftp_curve_key: str = ""
    ftp_profile_key: str = ""

    def __post_init__(self) -> None:
        if not (
            math.isfinite(self.ftp_wholesale_funding_rate)
            and math.isfinite(self.ftp_surplus_reinvestment_rate)
        ):
            raise ValueError("FTP market funding rates must be finite.")
        if not (self.ftp_curve_key and self.ftp_profile_key):
            raise ValueError("FTP market requires curve and profile identity.")

    @property
    def wholesale_funding_rate(self) -> float:
        return self.ftp_wholesale_funding_rate

    @property
    def surplus_reinvestment_rate(self) -> float:
        return self.ftp_surplus_reinvestment_rate


def ftp_market(market: MarketState, rates: FTPRateSet) -> FTPMarketState:
    """Bind the external FTP funding rates to an otherwise unchanged market."""
    return FTPMarketState(
        cash_rate=market.cash_rate,
        wholesale_funding_spread=market.wholesale_funding_spread,
        competitor_loan_rate=market.competitor_loan_rate,
        competitor_deposit_rate=market.competitor_deposit_rate,
        home_loan_reference_rate=market.home_loan_reference_rate,
        term_deposit_reference_rate=market.term_deposit_reference_rate,
        ftp_wholesale_funding_rate=rates.wholesale_funding_rate,
        ftp_surplus_reinvestment_rate=rates.surplus_reinvestment_rate,
        ftp_curve_key=rates.curve_key,
        ftp_profile_key=rates.profile_key,
    )


# -- Illustrative synthetic inputs -------------------------------------------------------

# Pre-declared construction, not fitted to any outcome: reference = 2.50% policy-rate proxy
# plus an upward term slope; premium = an increasing illustrative term funding premium. The
# 12-month all-in rate equals the legacy scalar wholesale rate (2.50% + 1.20%), so the term
# structure, not the level, drives the comparison. These are not observed NZ quotes.
_ILLUSTRATIVE_SLOPE = {3: 0.0010, 6: 0.0015, 12: 0.0025, 24: 0.0040, 36: 0.0055, 60: 0.0080}
_ILLUSTRATIVE_PREMIUM = {3: 0.0060, 6: 0.0075, 12: 0.0095, 24: 0.0120, 36: 0.0140, 60: 0.0170}
_ILLUSTRATIVE_POLICY_RATE = 0.025


def illustrative_curve(
    as_of: datetime,
    *,
    curve_id: str = "illustrative-term-ftp",
    version: str = "1.0.0",
    slope_scale: float = 1.0,
    premium_scale: float = 1.0,
    max_age_days: int = 7,
) -> FTPCurve:
    """Synthetic NZD curve at the candidate maturities. Scales are illustrative shocks."""
    return FTPCurve(
        curve_id=curve_id,
        version=version,
        currency="NZD",
        as_of=as_of,
        source="Synthetic illustrative construction (no observed market quotes)",
        instrument="Illustrative NZD term reference proxy plus term funding premium",
        rate_unit=RateUnit.FRACTION,
        compounding="annual_simple",
        max_age_days=max_age_days,
        required_tenors_months=ILLUSTRATIVE_TENORS_MONTHS,
        points=tuple(
            FTPCurvePoint(
                tenor_months=tenor,
                reference_rate=_ILLUSTRATIVE_POLICY_RATE + slope_scale * _ILLUSTRATIVE_SLOPE[tenor],
                liquidity_premium=premium_scale * _ILLUSTRATIVE_PREMIUM[tenor],
            )
            for tenor in ILLUSTRATIVE_TENORS_MONTHS
        ),
    )


def scalar_equivalent_curve(
    *,
    cash_rate: float,
    wholesale_funding_spread: float,
    as_of: datetime,
    max_age_days: int = 7,
) -> FTPCurve:
    """A flat curve that reproduces the legacy scalar funding rates exactly.

    Every tenor quotes ``cash_rate`` as the reference. A non-negative legacy spread is the
    liquidity premium. A negative spread is a signed adjustment with a zero premium.
    The wholesale rate is ``cash_rate + spread`` and surplus earns ``cash_rate``
    (with a zero reinvestment spread), whatever supported tenors a profile selects.
    """
    liquidity_premium = wholesale_funding_spread
    adjustments = ()
    if wholesale_funding_spread < 0:
        liquidity_premium = 0.0
        adjustments = (
            FTPAdjustment(
                name="legacy_negative_wholesale_spread",
                value=wholesale_funding_spread,
                rationale="Signed adjustment from the configured scalar wholesale funding spread",
            ),
        )

    return FTPCurve(
        curve_id="legacy-scalar-equivalent",
        version="1.0.0",
        currency="NZD",
        as_of=as_of,
        source="Engine configuration: cash_rate and wholesale_funding_spread",
        instrument="Flat policy-rate proxy plus flat wholesale spread (legacy scalar)",
        rate_unit=RateUnit.FRACTION,
        compounding="annual_simple",
        max_age_days=max_age_days,
        required_tenors_months=ILLUSTRATIVE_TENORS_MONTHS,
        allow_negative_rates=cash_rate < 0,
        points=tuple(
            FTPCurvePoint(
                tenor_months=tenor,
                reference_rate=cash_rate,
                liquidity_premium=liquidity_premium,
                adjustments=adjustments,
            )
            for tenor in ILLUSTRATIVE_TENORS_MONTHS
        ),
    )


def illustrative_funding_profile(**update: Any) -> FTPFundingProfile:
    """12-month fixed mortgage and 12-month term deposit with a longer loan funding profile."""
    data: dict[str, Any] = {
        "profile_id": "illustrative-12m-fixed",
        "version": "1.0.0",
        "loan_repricing_tenor_months": 12,
        "loan_liquidity_buckets": (
            FTPLiquidityBucket(tenor_months=12, weight=0.25),
            FTPLiquidityBucket(tenor_months=36, weight=0.35),
            FTPLiquidityBucket(tenor_months=60, weight=0.40),
        ),
        "deposit_contractual_tenor_months": 12,
        "wholesale_funding_tenor_months": 12,
        "surplus_reinvestment_tenor_months": 3,
        "surplus_reinvestment_spread": 0.0,
        "review_notes": (
            "Invented funding profile for engineering tests. The liquidity buckets are not a "
            "validated behavioural life. Treasury/ALM review pending."
        ),
    }
    data.update(update)
    return FTPFundingProfile(**data)


# -- Internal transfer pricing ledger ------------------------------------------------------

# Allocations must cancel within accumulated floating-point error, never a material amount.
LEDGER_REL_TOL = 1e-9
LEDGER_ABS_TOL = 1e-6


def amounts_reconcile(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=LEDGER_REL_TOL, abs_tol=LEDGER_ABS_TOL)


@dataclass(frozen=True)
class FTPAllocationLedger:
    """Loan, deposit and Treasury attribution for one funding position (NZD, annual).

    Sign convention: every line is the signed contribution to its own unit's P&L (income
    positive, expense negative in the contribution formulas). The loan business pays the
    loan FTP charge to Treasury; Treasury pays the deposit FTP credit to the deposit
    business. Only Treasury faces the external market: it pays the marginal wholesale rate
    on the residual deficit and earns the reinvestment rate on investible surplus, once,
    on the aggregate position. Steering penalties and the capital charge are not
    allocated here; they sit in the ROE bridge below the economic contribution.
    """

    loan_volume: float
    usable_deposit_funding: float
    loan_ftp_rate: float
    deposit_ftp_rate: float
    wholesale_funding_rate: float
    surplus_reinvestment_rate: float
    loan_interest_income: float
    expected_credit_loss: float
    deposit_interest_cost: float
    deposit_acquisition_cost: float

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not math.isfinite(value):
                raise ValueError(f"FTP ledger {name} must be finite.")
        if self.loan_volume < 0 or self.usable_deposit_funding < 0:
            raise ValueError("FTP ledger volumes must be non-negative.")

    @property
    def wholesale_deficit(self) -> float:
        return max(self.loan_volume - self.usable_deposit_funding, 0.0)

    @property
    def surplus_funds(self) -> float:
        return max(self.usable_deposit_funding - self.loan_volume, 0.0)

    @property
    def loan_ftp_charge(self) -> float:
        return self.loan_volume * self.loan_ftp_rate

    @property
    def deposit_ftp_credit(self) -> float:
        return self.usable_deposit_funding * self.deposit_ftp_rate

    @property
    def external_wholesale_funding_cost(self) -> float:
        return self.wholesale_deficit * self.wholesale_funding_rate

    @property
    def surplus_investment_income(self) -> float:
        return self.surplus_funds * self.surplus_reinvestment_rate

    @property
    def loan_business_contribution(self) -> float:
        return self.loan_interest_income - self.expected_credit_loss - self.loan_ftp_charge

    @property
    def deposit_business_contribution(self) -> float:
        return self.deposit_ftp_credit - self.deposit_interest_cost - self.deposit_acquisition_cost

    @property
    def treasury_ftp_receipts(self) -> float:
        return self.loan_ftp_charge

    @property
    def treasury_ftp_payments(self) -> float:
        return self.deposit_ftp_credit

    @property
    def treasury_contribution(self) -> float:
        return (
            self.treasury_ftp_receipts
            - self.treasury_ftp_payments
            - self.external_wholesale_funding_cost
            + self.surplus_investment_income
        )

    @property
    def internal_allocations(self) -> tuple[tuple[str, str, float], ...]:
        """Each internal transfer once per side: (unit, line, signed amount)."""
        return (
            ("loan_business", "loan_ftp_charge", -self.loan_ftp_charge),
            ("treasury", "loan_ftp_receipt", self.treasury_ftp_receipts),
            ("deposit_business", "deposit_ftp_credit", self.deposit_ftp_credit),
            ("treasury", "deposit_ftp_payment", -self.treasury_ftp_payments),
        )

    @property
    def internal_transfer_net(self) -> float:
        return math.fsum(amount for _, _, amount in self.internal_allocations)

    @property
    def consolidated_contribution(self) -> float:
        """Loan business + deposit business + Treasury."""
        return math.fsum(
            (
                self.loan_business_contribution,
                self.deposit_business_contribution,
                self.treasury_contribution,
            )
        )

    @property
    def external_economic_contribution(self) -> float:
        """Independent reference from external components only (no FTP rate appears)."""
        return math.fsum(
            (
                self.loan_interest_income,
                -self.expected_credit_loss,
                -self.deposit_interest_cost,
                -self.deposit_acquisition_cost,
                -self.external_wholesale_funding_cost,
                self.surplus_investment_income,
            )
        )

    @property
    def reconciliation_difference(self) -> float:
        return self.consolidated_contribution - self.external_economic_contribution

    def to_dict(self) -> dict[str, Any]:
        names = (
            "loan_volume",
            "usable_deposit_funding",
            "wholesale_deficit",
            "surplus_funds",
            "loan_ftp_rate",
            "deposit_ftp_rate",
            "wholesale_funding_rate",
            "surplus_reinvestment_rate",
            "loan_interest_income",
            "expected_credit_loss",
            "loan_ftp_charge",
            "loan_business_contribution",
            "deposit_ftp_credit",
            "deposit_interest_cost",
            "deposit_acquisition_cost",
            "deposit_business_contribution",
            "treasury_ftp_receipts",
            "treasury_ftp_payments",
            "external_wholesale_funding_cost",
            "surplus_investment_income",
            "treasury_contribution",
            "internal_transfer_net",
            "consolidated_contribution",
            "external_economic_contribution",
            "reconciliation_difference",
        )
        return {name: getattr(self, name) for name in names}


def ledger_from_result(result: Any, rates: FTPRateSet) -> FTPAllocationLedger:
    """Attribute one optimised (or fixed-offer) result and check it against the search.

    The search priced the residual deficit and surplus at ``rates``' external rates. This
    recomputes both from volumes and rates and fails if they disagree, so the ledger is an
    independent reconciliation, not a copy of the objective's own numbers.
    """
    usable = float(getattr(result, "usable_deposit_funding", result.expected_deposit_volume))
    ledger = FTPAllocationLedger(
        loan_volume=float(result.expected_loan_volume),
        usable_deposit_funding=usable,
        loan_ftp_rate=rates.loan_ftp_rate,
        deposit_ftp_rate=rates.deposit_ftp_rate,
        wholesale_funding_rate=rates.wholesale_funding_rate,
        surplus_reinvestment_rate=rates.surplus_reinvestment_rate,
        loan_interest_income=float(result.loan_interest_income),
        expected_credit_loss=float(result.expected_credit_loss),
        deposit_interest_cost=float(result.deposit_interest_cost),
        deposit_acquisition_cost=float(getattr(result, "deposit_acquisition_cost", 0.0)),
    )
    for name, expected, actual in (
        (
            "wholesale funding cost",
            ledger.external_wholesale_funding_cost,
            result.wholesale_funding_cost,
        ),
        ("surplus income", ledger.surplus_investment_income, result.excess_deposit_income),
    ):
        if not amounts_reconcile(float(actual), expected):
            raise ValueError(
                f"Optimised {name} {actual!r} does not reconcile to the FTP external rate "
                f"({expected!r})."
            )
    return ledger
