"""The reference depositor for the default term-deposit benchmark (DATA-005).

A bank's term-deposit page typically advertises several rates for one term: balance tiers
($10,000-$499,999 and $500,000+), and sometimes a different rate for monthly interest. Taking
each bank's highest rate across all of them lets a rate the reference customer cannot get --
a $500k+ tier, or a payment option they did not choose -- set the market leader and median.

The default benchmark therefore describes one explicit **reference depositor**: a balance
(``balance_amount``) and a preferred interest-payment frequency. A record is a candidate for
that depositor only when its *published* terms admit them:

* **Balance.** ``min_balance_or_deposit <= balance_amount`` when a minimum is published, and
  ``balance_amount <= max_balance_or_deposit`` when a maximum is published (both inclusive).
  A published bound that excludes the reference amount excludes the record. A record with *no*
  published bound (``balance_basis="not_stated"``) is the bank's general advertised rate as far
  as the page shows; it is admitted only while ``admit_unstated_balance`` is true, and it is
  always disclosed as "eligibility not established", never as confirmed.
* **Payment frequency.** A record offering the preferred frequency -- alone or as one option of
  a combined value such as ``at_maturity+monthly`` -- is ``published``. One offering only an
  explicitly configured ``fallback_frequencies`` option is ``fallback`` (disclosed; empty by
  default, so a known incompatible frequency such as monthly-only is excluded rather than mixed
  with at-maturity rates). An ``unknown`` frequency is ``not_stated``: admitted only while
  ``admit_unstated_frequency`` is true, disclosed, and never labelled at-maturity. No
  annual-equivalent rate is computed: there is no approved conversion methodology.

Within one bank and term the representative is chosen deterministically: the payment-frequency
match first (preferred, then each fallback in order, then unstated), then the most competitive
rate among the balance-eligible candidates, then the benchmark's own category, then product name
and observation identity -- never HTML row order. An unstated balance tier is not ranked down
(a Phase 2b comparable special often states none); it is disclosed on the representative. Ineligible records are not deleted; the
snapshot lists them in ``reference_exclusions`` and the all-tier view still shows them.

Pure functions of stored record fields: no schema change, and historical records answer the
same way they would have at the time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, offers_frequency

#: Payment frequencies a reference depositor can choose (``unknown`` is not a choice).
ReferenceFrequency = Literal["at_maturity", "monthly", "quarterly", "annually"]

BalanceBasis = Literal["published", "not_stated"]
FrequencyBasis = Literal["published", "fallback", "not_stated"]

_FREQUENCY_LABELS = {
    "at_maturity": "at maturity",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "annually": "annually",
}


class DepositReferencePolicy(BaseModel):
    """Who the default term-deposit benchmark describes.

    Default: a $10,000 deposit with interest paid at maturity -- the ordinary retail tier every
    tracked bank's headline rate is quoted for, and the first edge of the peer-plausibility
    balance bands (``PeerPlausibilityThresholds.balance_tier_edges``). A policy proposal, not a
    business fact; change it explicitly, and the effective configuration hash changes with it.
    """

    model_config = ConfigDict(frozen=True)

    balance_amount: float = Field(default=10_000.0, gt=0, allow_inf_nan=False)
    interest_payment_frequency: ReferenceFrequency = "at_maturity"
    #: Ordered, explicitly approved substitutes when a bank does not offer the preferred
    #: frequency. Empty by default: a different frequency is a different product.
    fallback_frequencies: tuple[ReferenceFrequency, ...] = ()
    #: Admit (and disclose) a rate whose page publishes no balance tier.
    admit_unstated_balance: bool = True
    #: Admit (and disclose) a rate whose page publishes no payment frequency.
    admit_unstated_frequency: bool = True

    @model_validator(mode="after")
    def validate_fallbacks(self) -> "DepositReferencePolicy":
        if len(set(self.fallback_frequencies)) != len(self.fallback_frequencies):
            raise ValueError("fallback_frequencies must not repeat a frequency")
        if self.interest_payment_frequency in self.fallback_frequencies:
            raise ValueError("fallback_frequencies must not include the preferred frequency")
        return self

    def describe(self) -> str:
        """``"$10,000 deposit, interest at maturity"``."""
        amount = f"${self.balance_amount:,.0f}"
        frequency = _FREQUENCY_LABELS[self.interest_payment_frequency]
        return f"{amount} deposit, interest {frequency}"


@dataclass(frozen=True)
class ReferenceAssessment:
    """Whether one record can represent its bank for the reference depositor, and why."""

    eligible: bool
    #: Exclusion reason when not eligible: ``balance_below_minimum``, ``balance_above_maximum``,
    #: ``balance_not_stated``, ``frequency_incompatible`` or ``frequency_not_stated``.
    reason: Optional[str]
    balance_basis: Optional[BalanceBasis]
    frequency_basis: Optional[FrequencyBasis]
    #: 0 for the preferred frequency, then each fallback, then unstated. Lower is better.
    frequency_rank: int

    @property
    def established(self) -> bool:
        return self.balance_basis == "published" and self.frequency_basis == "published"


def _excluded(reason: str, balance_basis=None, frequency_basis=None) -> ReferenceAssessment:
    return ReferenceAssessment(False, reason, balance_basis, frequency_basis, -1)


def assess_reference_eligibility(
    record: CompetitorRateRecord, policy: DepositReferencePolicy
) -> ReferenceAssessment:
    """Assess ``record`` against the reference depositor (see the module docstring)."""
    amount = policy.balance_amount
    minimum, maximum = record.min_balance_or_deposit, record.max_balance_or_deposit
    if minimum is not None and amount < minimum:
        return _excluded("balance_below_minimum", "published")
    if maximum is not None and amount > maximum:
        return _excluded("balance_above_maximum", "published")
    balance_basis: BalanceBasis = (
        "not_stated" if minimum is None and maximum is None else "published"
    )
    if balance_basis == "not_stated" and not policy.admit_unstated_balance:
        return _excluded("balance_not_stated", balance_basis)

    frequency = record.interest_payment_frequency
    if offers_frequency(frequency, policy.interest_payment_frequency):
        return ReferenceAssessment(True, None, balance_basis, "published", 0)
    for index, fallback in enumerate(policy.fallback_frequencies, start=1):
        if offers_frequency(frequency, fallback):
            return ReferenceAssessment(True, None, balance_basis, "fallback", index)
    if frequency == "unknown":
        if not policy.admit_unstated_frequency:
            return _excluded("frequency_not_stated", balance_basis, "not_stated")
        rank = len(policy.fallback_frequencies) + 1
        return ReferenceAssessment(True, None, balance_basis, "not_stated", rank)
    return _excluded("frequency_incompatible", balance_basis)


@dataclass(frozen=True)
class ReferenceExclusion:
    """A record the reference depositor cannot get, kept visible for audit."""

    bank: str
    term_months: int
    rate_pct: float
    rate_category: str
    product_name: str
    reason: str
    min_balance: Optional[float]
    max_balance: Optional[float]
    interest_payment_frequency: str
    observation_key: str


def reference_exclusion(
    record: CompetitorRateRecord, assessment: ReferenceAssessment
) -> ReferenceExclusion:
    return ReferenceExclusion(
        bank=record.bank,
        term_months=record.term_months_normalised or 0,
        rate_pct=record.advertised_rate_pct,
        rate_category=record.rate_category,
        product_name=record.product_name,
        reason=assessment.reason or "",
        min_balance=record.min_balance_or_deposit,
        max_balance=record.max_balance_or_deposit,
        interest_payment_frequency=record.interest_payment_frequency,
        observation_key=record.observation_key(),
    )
