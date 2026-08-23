"""Validation of parsed observations before they are treated as market data.

A parsed number is a claim, not a fact. Everything here exists to stop a parser
regression from quietly becoming a repricing signal: a bank restructuring its
rate table is far more likely than a 360 bps overnight move.

Records are separated into accepted and rejected rather than mutated in place,
so a caller always knows what was dropped and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import resolve_pairing
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds


@dataclass(frozen=True)
class ObservationIssue:
    """One validation finding against a single observation."""

    severity: str  # "warning" | "error"
    code: str
    message: str
    bank: str
    observation_key: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.bank} {self.code}: {self.message}"


@dataclass
class ValidationOutcome:
    """Accepted and rejected observations, with the reasons for each rejection."""

    accepted: list[CompetitorRateRecord] = field(default_factory=list)
    rejected: list[CompetitorRateRecord] = field(default_factory=list)
    issues: list[ObservationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ObservationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ObservationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def suspicious_keys(self) -> set:
        return {
            issue.observation_key
            for issue in self.issues
            if issue.code == "suspicious_movement"
        }

    def summary(self) -> str:
        return (
            f"{len(self.accepted)} accepted, {len(self.rejected)} rejected, "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings"
        )


def validate_observations(
    records: Iterable[CompetitorRateRecord],
    thresholds: Optional[ValidationThresholds] = None,
    previous: Optional[Sequence[CompetitorRateRecord]] = None,
) -> ValidationOutcome:
    """Validate parsed observations against configured plausibility rules.

    Rate-range and required-field failures are errors: the record is rejected
    and never reaches the store. A suspicious movement is a *warning*: the
    record is kept (a genuine large reprice must not be discarded) but flagged
    so downstream change events carry the flag and an operator can verify it
    against the source page.

    The movement check compares each record against its predecessor via
    :func:`~balance_pricing_engine.pricing_intelligence.changes.resolve_pairing`
    (an exact observation key, falling back to an unambiguous eligibility-group
    match), not a plain key lookup: a record whose eligibility condition and
    rate both moved in the same collection would otherwise change its exact
    key and skip the movement check entirely, letting an implausible rate
    (e.g. 5.00% misread as 1.50%) through unflagged and into a market-derived
    pricing assumption before ``detect_changes()`` ever sees it.

    A predecessor left unclaimed by ``resolve_pairing`` (no exact or
    eligibility-group match) gets one further, validation-only fallback on
    :meth:`~balance_pricing_engine.competitor_rates.models.CompetitorRateRecord.category_agnostic_group_key`:
    a parser fault that flips ``rate_category`` *and* misreads the rate has
    no predecessor under either of the stricter keys, so without this it
    would have no baseline to compare against and the movement would pass
    unflagged. Only applied when exactly one unclaimed predecessor shares the
    category-agnostic group -- an ambiguous match is not used as a baseline.
    """
    thresholds = thresholds or ValidationThresholds()
    records = list(records)
    previous = list(previous or [])
    pairs = resolve_pairing(previous, records)

    claimed_previous_keys = {p.observation_key() for p in pairs.values()}
    unclaimed_previous = [p for p in previous if p.observation_key() not in claimed_previous_keys]
    category_agnostic_candidates: dict[str, list[CompetitorRateRecord]] = {}
    for p in unclaimed_previous:
        category_agnostic_candidates.setdefault(p.category_agnostic_group_key(), []).append(p)

    outcome = ValidationOutcome()

    for record in records:
        key = record.observation_key()
        issues = _validate_record(record, thresholds, key)

        baseline = pairs.get(key)
        if baseline is None:
            candidates = category_agnostic_candidates.get(record.category_agnostic_group_key(), [])
            if len(candidates) == 1:
                baseline = candidates[0]

        movement_issue = _validate_movement(record, baseline, thresholds, key)
        if movement_issue is not None:
            issues.append(movement_issue)

        outcome.issues.extend(issues)
        if any(issue.severity == "error" for issue in issues):
            outcome.rejected.append(record)
        else:
            outcome.accepted.append(record)

    return outcome


def _validate_record(
    record: CompetitorRateRecord,
    thresholds: ValidationThresholds,
    key: str,
) -> list[ObservationIssue]:
    issues: list[ObservationIssue] = []

    def error(code: str, message: str) -> None:
        issues.append(
            ObservationIssue(
                severity="error",
                code=code,
                message=message,
                bank=record.bank or "?",
                observation_key=key,
            )
        )

    # Required fields: bank, term, rate, source, observation time.
    if not record.bank:
        error("missing_bank", "bank is required")
    if not record.source_url:
        error("missing_source", "source_url is required")
    if record.captured_at_utc is None:
        error("missing_observed_at", "captured_at_utc is required")
    if record.term_months_normalised is None and not record.term_label_raw:
        error("missing_term", "a normalised term or a raw term label is required")
    if record.term_months_normalised is not None and record.term_months_normalised <= 0:
        error(
            "invalid_term",
            f"term must be positive, got {record.term_months_normalised} months",
        )

    is_loan = ProductType(record.product_type) is ProductType.HOME_LOAN
    minimum = (
        thresholds.min_mortgage_rate_pct if is_loan else thresholds.min_deposit_rate_pct
    )
    maximum = (
        thresholds.max_mortgage_rate_pct if is_loan else thresholds.max_deposit_rate_pct
    )
    if not minimum <= record.advertised_rate_pct <= maximum:
        error(
            "rate_out_of_range",
            f"advertised rate {record.advertised_rate_pct:.3f}% is outside the "
            f"plausible {minimum:.2f}%-{maximum:.2f}% band",
        )

    return issues


def _validate_movement(
    record: CompetitorRateRecord,
    previous: Optional[CompetitorRateRecord],
    thresholds: ValidationThresholds,
    key: str,
) -> Optional[ObservationIssue]:
    if previous is None:
        return None
    movement = rate_change_bps(previous.advertised_rate_pct, record.advertised_rate_pct)
    if abs(movement) <= thresholds.suspicious_change_bps:
        return None
    return ObservationIssue(
        severity="warning",
        code="suspicious_movement",
        message=(
            f"{previous.advertised_rate_pct:.2f}% -> {record.advertised_rate_pct:.2f}% "
            f"({movement:+.0f} bps) exceeds the {thresholds.suspicious_change_bps:.0f} bps "
            "threshold; verify the source page for a parser fault or a "
            "special/standard mix-up before treating this as a genuine reprice"
        ),
        bank=record.bank,
        observation_key=key,
    )
