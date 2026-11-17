"""Validation and reconciliation for competitor-rate runs."""

from __future__ import annotations

from datetime import datetime, timezone

from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    CompetitorRateRun,
    ProductType,
    ValidationIssue,
    ValidationSummary,
)
from balance_pricing_engine.competitor_rates.sources import OFFICIAL_SOURCES


MAX_NORMAL_RATE_PCT = 15.0
LARGE_MOVEMENT_PCT = 0.50


def validate_rate_run(
    run: CompetitorRateRun,
    previous_run: CompetitorRateRun | None = None,
    strict: bool = True,
) -> ValidationSummary:
    """Validate extracted records and source coverage."""
    issues: list[ValidationIssue] = []
    valid_records = 0
    for record in run.records:
        record_errors: list[str] = []
        if not record.bank:
            record_errors.append("bank is required")
        if not record.product_type:
            record_errors.append("product_type is required")
        if not record.source_url:
            record_errors.append("source_url is required")
        if record.captured_at_utc is None:
            record_errors.append("captured_at_utc is required")
        if not record.term_label_raw and not record.product_name:
            record_errors.append("term label or product name is required")
        if record.advertised_rate_pct < 0 or record.advertised_rate_pct > MAX_NORMAL_RATE_PCT:
            severity = "error" if strict else "warning"
            issues.append(
                ValidationIssue(
                    severity=severity,
                    message=(
                        f"Rate outside expected range: {record.advertised_rate_pct:.3f}%"
                    ),
                    bank=record.bank,
                    product_type=ProductType(record.product_type),
                )
            )
        for error in record_errors:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=error,
                    bank=record.bank or None,
                    product_type=ProductType(record.product_type) if record.product_type else None,
                )
            )
        if not record_errors:
            valid_records += 1

    _add_duplicate_issues(run, issues)
    _add_source_coverage_issues(run, issues, strict=strict)
    if previous_run is not None:
        _add_movement_issues(run, previous_run, issues)

    error_count = sum(issue.severity == "error" for issue in issues)
    warning_count = sum(issue.severity == "warning" for issue in issues)
    status = "failed" if error_count else "warning" if warning_count else "valid"
    return ValidationSummary(
        status=status,
        total_records=len(run.records),
        valid_records=valid_records,
        warning_count=warning_count,
        error_count=error_count,
        issues=issues,
    )


def validate_staleness(
    run: CompetitorRateRun,
    max_staleness_hours: int,
    strict: bool,
    now: datetime | None = None,
) -> list[ValidationIssue]:
    """Return staleness issues for a stored latest file."""
    now = now or datetime.now(timezone.utc)
    captured = run.captured_at_utc.astimezone(timezone.utc)
    age_hours = (now - captured).total_seconds() / 3600
    if age_hours <= max_staleness_hours:
        return []
    return [
        ValidationIssue(
            severity="error" if strict else "warning",
            message=(
                f"Competitor-rate file is stale: {age_hours:.1f} hours old "
                f"(max {max_staleness_hours})"
            ),
        )
    ]


def duplicate_identity(record: CompetitorRateRecord) -> str:
    """The economic identity two records must share to be duplicates of each other.

    ``observation_key()`` -- the identity the trusted pricing-intelligence validator uses: bank,
    product, rate category, term, payment frequency, product name, balance tier and
    eligibility condition. Two simultaneous balance tiers or payment-frequency options are
    therefore different products, not duplicates (DATA-006).

    A ``non_comparable`` row (fund returns, PIE effective-rate columns per tax band) lumps
    heterogeneous products into one category, so its own row text is part of its identity,
    as in ``pricing_intelligence.validation``. Two such rows from identical text with
    different rates are still a conflict.
    """
    key = record.observation_key()
    if record.rate_category == "non_comparable":
        return f"{key}|{record.raw_text_context}"
    return key


def _add_duplicate_issues(run: CompetitorRateRun, issues: list[ValidationIssue]) -> None:
    """Fail a run in which two records claim one economic identity.

    A conflict (different rates) is the trusted validator's conflicting-observation rule:
    which reading wins would depend on input order. An exact repeat is still an error here,
    as before Phase 2d: the legacy file has no per-record rejection, so a doubled row is
    refused rather than silently collapsed. The message says which case it is.
    """
    records_by_key: dict[str, list[CompetitorRateRecord]] = {}
    for record in run.records:
        records_by_key.setdefault(duplicate_identity(record), []).append(record)
    for key, records in records_by_key.items():
        if len(records) < 2:
            continue
        rates = sorted({record.advertised_rate_pct for record in records})
        first = records[0]
        if len(rates) > 1:
            message = (
                f"Conflicting competitor rate records for {key}: "
                + ", ".join(f"{rate:.3f}%" for rate in rates)
            )
        else:
            message = (
                f"Duplicate competitor rate records found for {key} "
                f"({len(records)} identical readings)"
            )
        issues.append(
            ValidationIssue(
                severity="error",
                message=message,
                bank=first.bank,
                product_type=ProductType(first.product_type),
            )
        )


def _add_source_coverage_issues(
    run: CompetitorRateRun,
    issues: list[ValidationIssue],
    strict: bool,
) -> None:
    attempted = {
        (attempt.bank, ProductType(attempt.product_type).value): attempt
        for attempt in run.source_attempts
    }
    for source in OFFICIAL_SOURCES:
        key = (source.bank, source.product_type.value)
        attempt = attempted.get(key)
        if attempt is None:
            issues.append(
                ValidationIssue(
                    severity="error" if strict else "warning",
                    message=f"Missing expected source for {source.bank} {source.product_type.value}",
                    bank=source.bank,
                    product_type=source.product_type,
                )
            )
            continue
        if not attempt.success or attempt.records_extracted == 0:
            issues.append(
                ValidationIssue(
                    severity="error" if strict else "warning",
                    message=(
                        f"Official source failed for {attempt.bank} "
                        f"{ProductType(attempt.product_type).value}: "
                        f"{attempt.error or 'no records extracted'}"
                    ),
                    bank=attempt.bank,
                    product_type=ProductType(attempt.product_type),
                )
            )


def _add_movement_issues(
    run: CompetitorRateRun,
    previous_run: CompetitorRateRun,
    issues: list[ValidationIssue],
) -> None:
    previous_by_key = {
        _movement_key(record): record.advertised_rate_pct for record in previous_run.records
    }
    for record in run.records:
        previous_rate = previous_by_key.get(_movement_key(record))
        if previous_rate is None:
            continue
        movement = record.advertised_rate_pct - previous_rate
        if abs(movement) > LARGE_MOVEMENT_PCT:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    message=(
                        f"Large day-over-day movement for {record.bank} "
                        f"{record.product_type} {record.term_label_raw}: "
                        f"{movement:+.2f} percentage points"
                    ),
                    bank=record.bank,
                    product_type=ProductType(record.product_type),
                )
            )


def _movement_key(record) -> tuple[str, str, str, str, str]:
    return (
        record.bank,
        str(record.product_type),
        record.product_name,
        record.rate_category,
        record.term_label_raw,
    )
