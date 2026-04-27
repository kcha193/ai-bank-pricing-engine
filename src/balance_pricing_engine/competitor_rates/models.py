"""Canonical models for auditable competitor-rate ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PARSER_VERSION = "competitor-rates-v1"


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

    @model_validator(mode="after")
    def validate_identity_and_range(self) -> "CompetitorRateRecord":
        if not self.term_label_raw and not self.product_name:
            raise ValueError("term_label_raw or product_name is required")
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
