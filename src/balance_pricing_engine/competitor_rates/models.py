"""Canonical models for auditable competitor-rate ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PARSER_VERSION = "competitor-rates-v1"


def _numeric_band(value: Optional[float]) -> str:
    """Format an optional eligibility number for stable identity comparison.

    Fixed precision avoids a float round-trip (e.g. through storage) turning
    an unchanged band into a different key.
    """
    return "" if value is None else f"{value:.4f}"


class ProductType(str, Enum):
    HOME_LOAN = "home_loan"
    TERM_DEPOSIT = "term_deposit"


InterestPaymentFrequency = Literal[
    "at_maturity",
    "monthly",
    "quarterly",
    "annually",
    "unknown",
]
ValidationStatus = Literal["unvalidated", "valid", "warning", "failed"]


class CompetitorRateRecord(BaseModel):
    """One advertised competitor rate with source and parser audit metadata."""

    model_config = ConfigDict(use_enum_values=True)

    bank: str
    product_type: ProductType
    product_name: str
    rate_category: str
    term_label_raw: str
    term_months_normalised: Optional[int] = Field(default=None, ge=0)
    term_days_raw: Optional[int] = Field(default=None, ge=0)
    advertised_rate_pct: float
    interest_payment_frequency: InterestPaymentFrequency = "unknown"
    min_balance_or_deposit: Optional[float] = Field(default=None, ge=0)
    max_balance_or_deposit: Optional[float] = Field(default=None, ge=0)
    lvr_condition: Optional[str] = None
    special_conditions: Optional[str] = None
    # --- Optional mortgage promotion/eligibility detail -------------------------
    # All nullable: a bank page that does not publish a field leaves it None
    # rather than having a value invented for it. `rate_category` already
    # separates standard from special advertised lines (one record per
    # advertised rate), so there is deliberately no standard_rate/special_rate
    # pair on a single record.
    max_lvr: Optional[float] = Field(default=None, gt=0, le=1)
    min_equity: Optional[float] = Field(default=None, ge=0, lt=1)
    new_business_only: Optional[bool] = None
    cashback_text: Optional[str] = None
    promotion_text: Optional[str] = None
    package_requirement: Optional[str] = None
    source_url: str
    source_bank_page_title: Optional[str] = None
    captured_at_utc: datetime
    effective_date_text: Optional[str] = None
    raw_text_context: str
    raw_html_sha256: str
    parser_version: str = PARSER_VERSION
    validation_status: ValidationStatus = "unvalidated"
    validation_warnings: list[str] = Field(default_factory=list)

    @field_validator("bank", "product_name", "rate_category", "source_url", "parser_version")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("field must not be empty")
        return value.strip()

    @field_validator("term_label_raw", "raw_text_context")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("captured_at_utc")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("raw_html_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("raw_html_sha256 must be a SHA-256 hex digest")
        return value.lower()

    def observation_key(self) -> str:
        """Stable identity for this advertised rate line across collection runs.

        Change detection compares like with like: two observations share a key
        when they describe the same bank/product/term/condition combination, so
        a rate movement is detected rather than being read as one product
        disappearing and another appearing. Deliberately excludes the rate
        itself and every provenance field.

        Includes the eligibility condition (``lvr_condition``/``max_lvr``/
        ``min_equity``) as well as :meth:`eligibility_group_key`'s fields:
        a bank can simultaneously advertise the same named/category/term
        product at multiple LVR tiers, and those rows would otherwise collide
        onto one key. A bank re-describing (or tightening/loosening) the
        eligibility condition on what is otherwise the same, singly-advertised
        product changes this key too -- change detection pairs that case back
        to an :class:`.ChangeType` ``ELIGIBILITY_CHANGE`` via a secondary
        match on :meth:`eligibility_group_key` rather than by including the
        condition in the primary identity.
        """
        return "|".join(
            [
                self.eligibility_group_key(),
                self.lvr_condition or "",
                _numeric_band(self.max_lvr),
                _numeric_band(self.min_equity),
            ]
        )

    def eligibility_group_key(self) -> str:
        """Identity ignoring the eligibility condition.

        Used by change detection as a fallback match when
        :meth:`observation_key` finds no exact pairing: if exactly one
        previously-known key in the same group is otherwise unclaimed, the
        two are paired as an eligibility edit on the same product rather than
        read as one product disappearing and a new one appearing. Also the
        collision guard for simultaneous products: two products sharing
        bank/category/term/frequency can otherwise differ only by name or by
        a numeric balance tier, which would collide onto the same group.
        """
        parts = [
            self.bank,
            str(self.product_type),
            self.rate_category,
            str(self.term_months_normalised)
            if self.term_months_normalised is not None
            else self.term_label_raw.lower(),
            self.interest_payment_frequency,
            self.product_name.strip().lower(),
            _numeric_band(self.min_balance_or_deposit),
            _numeric_band(self.max_balance_or_deposit),
        ]
        return "|".join(parts)

    @model_validator(mode="after")
    def validate_identity_and_range(self) -> "CompetitorRateRecord":
        if not self.term_label_raw and not self.product_name:
            raise ValueError("term_label_raw or product_name is required")
        if (
            self.max_lvr is not None
            and self.min_equity is not None
            and abs((self.max_lvr + self.min_equity) - 1.0) > 1e-9
        ):
            raise ValueError("max_lvr and min_equity must sum to 1.0 when both are provided")
        if (
            self.max_balance_or_deposit is not None
            and self.min_balance_or_deposit is not None
            and self.max_balance_or_deposit < self.min_balance_or_deposit
        ):
            raise ValueError("max balance must be greater than or equal to min balance")
        return self


class SourceAttempt(BaseModel):
    """Fetch/parse outcome for one official bank product page."""

    bank: str
    product_type: ProductType
    source_url: str
    success: bool
    records_extracted: int = Field(default=0, ge=0)
    snapshot_path: Optional[str] = None
    raw_html_sha256: Optional[str] = None
    error: Optional[str] = None


class ValidationIssue(BaseModel):
    severity: Literal["warning", "error"]
    message: str
    bank: Optional[str] = None
    product_type: Optional[ProductType] = None


class ValidationSummary(BaseModel):
    status: Literal["valid", "warning", "failed"]
    total_records: int = Field(ge=0)
    valid_records: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    issues: list[ValidationIssue] = Field(default_factory=list)


class CompetitorRateRun(BaseModel):
    """Canonical stored output for one ingestion run."""

    run_id: str
    captured_at_utc: datetime
    parser_version: str = PARSER_VERSION
    source_urls_attempted: list[str]
    source_attempts: list[SourceAttempt]
    validation_summary: ValidationSummary
    counts_by_bank_product_type: dict[str, int]
    records: list[CompetitorRateRecord]


class MortgageStrategyConfig(BaseModel):
    strategy: Literal["bank_specific", "market_average", "market_min", "market_median"] = (
        "market_average"
    )
    bank: Optional[str] = None
    term_months: Optional[int] = 12
    term_label: Optional[str] = None
    include_specials: bool = True
    require_min_20_percent_equity: bool = False


class TermDepositStrategyConfig(BaseModel):
    strategy: Literal["bank_specific", "market_average", "market_max", "market_median"] = (
        "market_average"
    )
    bank: Optional[str] = None
    term_months: Optional[int] = 12
    product_type: Literal["term_deposit", "pie_term_deposit"] = "term_deposit"
    interest_payment_frequency_preference: list[InterestPaymentFrequency] = Field(
        default_factory=lambda: ["at_maturity", "annually", "quarterly", "monthly"]
    )


class CompetitorRatesConfig(BaseModel):
    enabled: bool = False
    source_file: str = "data/processed/competitor_rates/latest.json"
    selected_banks: list[str] = Field(
        default_factory=lambda: ["ANZ", "ASB", "Westpac", "BNZ", "Kiwibank"]
    )
    max_staleness_hours: int = Field(default=48, gt=0)
    strict: bool = True
    mortgage: MortgageStrategyConfig = Field(default_factory=MortgageStrategyConfig)
    term_deposit: TermDepositStrategyConfig = Field(default_factory=TermDepositStrategyConfig)


class ResolvedCompetitorRates(BaseModel):
    """Resolved decimal rates ready for the pricing engine."""

    mortgage_reference_rate: float
    term_deposit_reference_rate: float
    mortgage_strategy: str
    term_deposit_strategy: str
    mortgage_records: list[CompetitorRateRecord]
    term_deposit_records: list[CompetitorRateRecord]
    captured_at_utc: datetime
    validation_warnings: list[str] = Field(default_factory=list)
