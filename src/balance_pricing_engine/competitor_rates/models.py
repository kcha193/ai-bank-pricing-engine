"""Canonical models for auditable competitor-rate ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Literal, Optional, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from balance_pricing_engine.competitor_rates.bank_identity import canonical_bank_or_none
from balance_pricing_engine.competitor_rates.normalise import published_lvr_values


PARSER_VERSION = "competitor-rates-v1"

# Historical reads use this when the original row text cannot be recovered.
HISTORICAL_EVIDENCE_UNAVAILABLE = (
    "(evidence unavailable: this state's stored evidence has since been "
    "refreshed by a later, materially-unchanged collection and no longer "
    "reflects this point in time)"
)
_RATE_PERCENTAGE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")


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

#: The canonical vocabulary a combined interest_payment_frequency value (see
#: CompetitorRateRecord.interest_payment_frequency) is built from -- every
#: "+"-separated token must be one of these, so a typo from strict source
#: JSON or a third-party collector ("montly") can't slip past the field's
#: str type and silently defeat _select_frequency()'s preference matching.
_CANONICAL_INTEREST_FREQUENCIES = frozenset(get_args(InterestPaymentFrequency))


def offers_frequency(interest_payment_frequency: str, frequency: str) -> bool:
    """Whether an as-observed frequency value explicitly offers ``frequency``.

    A combined value ("at_maturity+monthly") offers each of its options. ``"unknown"`` offers
    nothing: an unpublished frequency is never evidence of any particular one.
    """
    return frequency != "unknown" and frequency in interest_payment_frequency.split("+")


#: How a mortgage record's LVR/equity eligibility was established (DATA-013).
LvrEligibilityBasis = Literal["published", "inferred", "unknown"]


def category_agnostic_key(
    bank: str,
    product_type: "ProductType | str",
    term_months_normalised: Optional[int],
    term_label_raw: str,
    interest_payment_frequency: str,
    min_balance_or_deposit: Optional[float],
    max_balance_or_deposit: Optional[float],
) -> str:
    """Standalone form of :meth:`CompetitorRateRecord.category_agnostic_group_key`.

    Takes raw field values rather than a validated instance so a row that
    fails :class:`CompetitorRateRecord` construction entirely -- and so never
    becomes an instance to call the method on -- can still be matched against
    a stored predecessor for withdrawal suppression (see
    ``competitor_rates.extractors.parse_html_rates``).
    """
    return "|".join(
        [
            bank,
            ProductType(product_type).value,
            str(term_months_normalised)
            if term_months_normalised is not None
            else term_label_raw.lower(),
            interest_payment_frequency,
            _numeric_band(min_balance_or_deposit),
            _numeric_band(max_balance_or_deposit),
        ]
    )


#: Separates a skipped row's :func:`category_agnostic_key` from the rate category its own
#: cell or column heading names (never present in a bank, term or frequency).
_SKIPPED_CATEGORY_SEPARATOR = "#"


def skipped_row_key(group_key: str, rate_category: Optional[str]) -> str:
    """A ``skipped_row_keys`` entry: the group key, plus the category when the cell names one.

    The category lets a withdrawal check tell an unreadable standard rate from a special that
    shares its balance tier (see ``pricing_intelligence.service._skipped_row_predecessor_keys``).
    """
    if rate_category is None:
        return group_key
    return f"{group_key}{_SKIPPED_CATEGORY_SEPARATOR}{rate_category}"


def split_skipped_row_key(key: str) -> tuple[str, Optional[str]]:
    """``(group_key, rate_category or None)`` for a ``skipped_row_keys`` entry."""
    group_key, separator, rate_category = key.partition(_SKIPPED_CATEGORY_SEPARATOR)
    return group_key, (rate_category if separator else None)


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
    #: Not typed as InterestPaymentFrequency: normalise_frequency() returns a
    #: stable combined value (e.g. "at_maturity+monthly") for a row
    #: advertising a genuine multiple-option choice, since
    #: interest_payment_frequency is identity-bearing and collapsing every
    #: such row to one of the five canonical values would make two rows
    #: offering different option sets collide on observation_key(). Config
    #: policy (e.g. TermDepositStrategyConfig.interest_payment_frequency_preference)
    #: stays the closed InterestPaymentFrequency vocabulary -- only the
    #: as-observed value on a record needs to accommodate a combination.
    interest_payment_frequency: str = "unknown"
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

    @field_validator("interest_payment_frequency")
    @classmethod
    def validate_interest_payment_frequency(cls, value: str) -> str:
        tokens = value.split("+")
        if (
            len(tokens) != len(set(tokens))
            or not all(token in _CANONICAL_INTEREST_FREQUENCIES for token in tokens)
            # "unknown" means no frequency was published at all -- mutually
            # exclusive with a real one. normalise_frequency() only ever
            # returns it alone (never alongside a matched keyword), so
            # "unknown+monthly" is not a legitimate combination: it would
            # make _select_frequency() treat the row as offering monthly
            # payments it never actually advertised.
            or ("unknown" in tokens and len(tokens) > 1)
        ):
            raise ValueError(
                "interest_payment_frequency must be a '+'-joined combination of "
                f"{sorted(_CANONICAL_INTEREST_FREQUENCIES)} with no duplicates, and 'unknown' "
                f"only on its own, got {value!r}"
            )
        # Sorted to match normalise_frequency()'s own canonical ordering: this
        # field participates in observation_key(), so "monthly+at_maturity"
        # and "at_maturity+monthly" -- the same option set, differently
        # ordered by a strict source or a third-party collector -- must
        # collapse onto one identity rather than reading as a withdrawal and
        # a new product appearing.
        return "+".join(sorted(tokens))

    @field_validator("raw_html_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("raw_html_sha256 must be a SHA-256 hex digest")
        return value.lower()

    def lvr_eligibility_basis(self) -> LvrEligibilityBasis:
        """Where ``max_lvr``/``min_equity`` came from (DATA-013).

        Published bounds must match the row's own text. The 80%/20% default is inferred only
        when the available text and category support the bank's rule. Missing or conflicting
        evidence gives an unknown basis, even when a numeric bound is stored.
        """
        bounds = (self.max_lvr, self.min_equity)
        if self.product_type != ProductType.HOME_LOAN or bounds == (None, None):
            return "unknown"
        text = self.raw_text_context
        if not text.strip() or text == HISTORICAL_EVIDENCE_UNAVAILABLE:
            return "unknown"
        published = published_lvr_values(text)
        if published is not None:
            matches = all(
                stored is None or stored == stated for stored, stated in zip(bounds, published)
            )
            return "published" if matches else "unknown"
        if bounds == (0.8, 0.2) and self._matches_min_equity_rule(text.lower()):
            return "inferred"
        return "unknown"

    def _matches_min_equity_rule(self, lowered: str) -> bool:
        # The registry imports the record model through its collectors.
        from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK

        bank = canonical_bank_or_none(self.bank)
        if bank is None or not any(
            float(match.group(1)) == self.advertised_rate_pct
            for match in _RATE_PERCENTAGE.finditer(lowered)
        ):
            return False
        collector = COLLECTORS_BY_BANK[bank]
        if any(marker in lowered for marker in collector.min_equity_product_markers):
            return True
        return (
            collector.default_fixed_category_implies_min_equity
            and self.rate_category == collector.default_fixed_category
            and not any(
                marker in lowered
                for marker in (
                    *collector.special_product_markers,
                    *collector.standard_product_markers,
                    *collector.excluded_product_markers,
                )
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def eligibility_inferred(self) -> bool:
        """True when at least one LVR/equity constraint was inferred, not published.

        Not part of :meth:`observation_key` and never stored: a pure function of the record's
        own evidence (see :meth:`lvr_eligibility_basis`).
        """
        return self.lvr_eligibility_basis() == "inferred"

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

    def category_agnostic_group_key(self) -> str:
        """:meth:`eligibility_group_key`, further ignoring ``rate_category``
        *and* ``product_name``.

        ``rate_category`` is real identity everywhere else -- a bank
        typically advertises "standard" and "special" simultaneously, so
        change detection must not collapse them. But that also means a
        parser fault that flips a row's category *and* produces an
        implausible rate change has no predecessor under
        ``eligibility_group_key()`` either, so validation's suspicious-
        movement check would find no baseline to compare against and let
        the movement through unflagged. Used only as a last-resort,
        validation-only lookup (never for change detection's identity
        pairing) when the normal fallback finds nothing.

        ``product_name`` is dropped too: the generic extractor derives it by
        searching the row for the category text itself (see
        ``competitor_rates.extractors._product_name``), so a genuine category
        flip commonly changes ``product_name`` right along with it (e.g.
        "Fixed Standard" -> "Fixed Special") -- keeping it here would defeat
        the very fallback this key exists for. The remaining fields are
        deliberately coarse; ambiguity is handled by every caller's
        "exactly one unclaimed candidate" guard, not by this key.

        Delegates to the standalone :func:`category_agnostic_key` so a row
        that never became a validated instance (see
        ``competitor_rates.extractors.parse_html_rates``) can still compute
        the identical key from its raw field values.
        """
        return category_agnostic_key(
            self.bank,
            self.product_type,
            self.term_months_normalised,
            self.term_label_raw,
            self.interest_payment_frequency,
            self.min_balance_or_deposit,
            self.max_balance_or_deposit,
        )

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

    @field_validator("bank")
    @classmethod
    def canonicalise_bank(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _canonical_bank(value)


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

    @field_validator("bank")
    @classmethod
    def canonicalise_bank(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _canonical_bank(value)


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

    @field_validator("selected_banks")
    @classmethod
    def canonicalise_selected_banks(cls, value: list[str]) -> list[str]:
        from balance_pricing_engine.competitor_rates.bank_identity import canonicalise_bank_list

        return canonicalise_bank_list(value)


def _canonical_bank(value: str) -> str:
    """Canonical tracked-bank name (DATA-014); imported lazily to avoid an import cycle."""
    from balance_pricing_engine.competitor_rates.bank_identity import canonical_bank

    return canonical_bank(value)


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
    # --- Phase 2d provenance (additive; defaults keep older constructions valid) ---------
    #: The one record per bank each market statistic used (DATA-018).
    mortgage_bank_representatives: list[CompetitorRateRecord] = Field(default_factory=list)
    mortgage_banks: list[str] = Field(default_factory=list)
    term_deposit_banks: list[str] = Field(default_factory=list)
    #: File name only and a content hash: a decision record never carries a server path.
    source_file_name: Optional[str] = None
    source_sha256: Optional[str] = None
    strict: bool = True
    evaluated_at_utc: Optional[datetime] = None
    source_age_hours: Optional[float] = None
    max_staleness_hours: Optional[int] = None
    stale: bool = False
    source_validation_status: Optional[str] = None
    min_banks: Optional[int] = None
    #: Warnings raised while resolving (uncertain specials, untrusted records excluded),
    #: as opposed to the source file's own staleness and validation issues.
    resolution_warnings: list[str] = Field(default_factory=list)
    deposit_special_assessments: list[dict] = Field(default_factory=list)
    #: Trust evidence the legacy JSON source cannot carry (it is never invented).
    reduced_assurance: list[str] = Field(default_factory=list)

    def provenance(self) -> dict:
        """JSON-safe evidence for a decision record: what was used, how old it was, and why."""
        return {
            "source_file_name": self.source_file_name,
            "source_sha256": self.source_sha256,
            "captured_at_utc": self.captured_at_utc.isoformat(),
            "evaluated_at_utc": (
                self.evaluated_at_utc.isoformat() if self.evaluated_at_utc else None
            ),
            "source_age_hours": self.source_age_hours,
            "max_staleness_hours": self.max_staleness_hours,
            "stale": self.stale,
            "strict": self.strict,
            "source_validation_status": self.source_validation_status,
            "mortgage_strategy": self.mortgage_strategy,
            "term_deposit_strategy": self.term_deposit_strategy,
            "mortgage_reference_rate": self.mortgage_reference_rate,
            "term_deposit_reference_rate": self.term_deposit_reference_rate,
            "mortgage_banks": list(self.mortgage_banks),
            "term_deposit_banks": list(self.term_deposit_banks),
            "min_banks": self.min_banks,
            "resolution_warnings": list(self.resolution_warnings),
            "deposit_special_assessments": list(self.deposit_special_assessments),
            "reduced_assurance": list(self.reduced_assurance),
        }
