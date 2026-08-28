"""Validation of parsed observations before they are treated as market data.

A parsed number is a claim, not a fact. Everything here exists to stop a parser
regression from quietly becoming a repricing signal: a bank restructuring its
rate table is far more likely than a 360 bps overnight move.

Records are separated into accepted and rejected rather than mutated in place,
so a caller always knows what was dropped and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Iterable, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import resolve_pairing
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds

#: Matches an advertised-rate-shaped percentage token (e.g. "4.01%") so it can
#: be stripped from raw_text_context before hashing for a non_comparable row's
#: identity -- see _disambiguate_non_comparable_identity(). Deliberately a
#: simple, generic pattern (unlike extractors.py's promotional/eligibility-
#: aware ones): this only needs to remove the mutable rate value, not decide
#: which percentage in the text is "the" rate.
_RATE_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%")


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

    A ``non_comparable`` record is given a stable per-row identity (see
    :func:`_disambiguate_non_comparable_identity`) before any of the above,
    so heterogeneous rows sharing the generic category never collide on
    ``observation_key()`` in the first place.
    """
    thresholds = thresholds or ValidationThresholds()
    records = [
        _disambiguate_non_comparable_identity(record)
        if record.rate_category == "non_comparable"
        else record
        for record in records
    ]
    previous = list(previous or [])
    pairs = resolve_pairing(previous, records)

    claimed_previous_keys = {p.observation_key() for p in pairs.values()}
    unclaimed_previous = [p for p in previous if p.observation_key() not in claimed_previous_keys]
    category_agnostic_candidates: dict[str, list[CompetitorRateRecord]] = {}
    for p in unclaimed_previous:
        category_agnostic_candidates.setdefault(p.category_agnostic_group_key(), []).append(p)

    conflicting_rates_by_key = _conflicting_rates_within_batch(records)

    outcome = ValidationOutcome()

    for record in records:
        key = record.observation_key()
        issues = _validate_record(record, thresholds, key)

        conflicting_rates = conflicting_rates_by_key.get(key)
        # The conflict guard protects market statistics from an ambiguous
        # identity silently resolving by insertion order. A non_comparable
        # record no longer needs an exemption here: it was disambiguated by
        # _disambiguate_non_comparable_identity() above, so two genuinely
        # different heterogeneous rows (fund returns, PIE effective-rate
        # columns) no longer share a key at all. A collision that still
        # reaches this point (identical raw_text_context, different rates)
        # means the same input somehow produced two different readings --
        # a real parser fault worth rejecting loudly, not one to exempt.
        if conflicting_rates is not None:
            issues.append(
                ObservationIssue(
                    severity="error",
                    code="conflicting_observation",
                    message=(
                        f"{len(conflicting_rates)} different rates "
                        f"({', '.join(f'{rate:.3f}%' for rate in sorted(conflicting_rates))}) "
                        f"were parsed for the same identity ({key}) in this collection; "
                        "rejecting all of them rather than letting insertion order into "
                        "storage silently pick a winner"
                    ),
                    bank=record.bank or "?",
                    observation_key=key,
                )
            )

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


def _disambiguate_non_comparable_identity(
    record: CompetitorRateRecord,
) -> CompetitorRateRecord:
    """Give a ``non_comparable`` row a stable, content-derived identity.

    ``rate_category="non_comparable"`` deliberately lumps heterogeneous rows
    (fund returns, PIE effective-rate columns) into one category with no
    identity-bearing distinction of their own, so several such rows can share
    :meth:`~.models.CompetitorRateRecord.observation_key` while genuinely
    representing different products at different rates. Exempting them from
    the conflicting-observation guard let every one of them reach storage
    unfiltered, but :meth:`~.store.ObservationStore.record_observations` then
    read each simultaneous row as a *successive state of one product*: a
    completely unchanged collection wrote every one of them again as a fresh
    inserted state rather than unchanged, corrupting the audit history and
    growing the database on every scheduled run -- 121 of 224 live deposit
    records hit this.

    Appending a short hash of ``raw_text_context``, with its percentage
    token(s) stripped, to ``product_name`` -- already part of
    :meth:`~.models.CompetitorRateRecord.eligibility_group_key`'s identity
    formula -- gives each row its own stable-across-runs identity without
    touching ``raw_text_context`` itself, which stays the untouched source
    evidence. The rate must be stripped before hashing: ``raw_text_context``
    contains the parsed percentage, so a genuine rate update on the same row
    would otherwise change the hash too, making change detection read it as
    that product being withdrawn and a new one appearing rather than a rate
    update on the same identity.
    """
    structural_text = _RATE_TOKEN_PATTERN.sub("", record.raw_text_context)
    tag = hashlib.sha256(structural_text.encode("utf-8")).hexdigest()[:8]
    return record.model_copy(update={"product_name": f"{record.product_name} [{tag}]"})


def _conflicting_rates_within_batch(
    records: Sequence[CompetitorRateRecord],
) -> dict[str, set[float]]:
    """Observation keys claimed by two or more records in this same batch
    with *different* advertised rates.

    A collector or parser bug can emit two conflicting readings for what
    should be one identity within a single collection -- e.g. a genuine
    4.79% alongside a misparsed 1.00% for the same bank/category/term. Each
    is validated only against its own predecessor via ``resolve_pairing``,
    never against each other, so without this check both would
    independently pass and reach storage, where insertion order -- not
    plausibility -- decides which becomes "current". On a bank's first-ever
    collection there is no predecessor at all, so the movement check in
    :func:`_validate_movement` cannot catch this either. Only keys with more
    than one *distinct* rate are returned -- an exact repeat of the same
    reading is not a conflict.
    """
    rates_by_key: dict[str, set[float]] = {}
    for record in records:
        rates_by_key.setdefault(record.observation_key(), set()).add(record.advertised_rate_pct)
    return {key: rates for key, rates in rates_by_key.items() if len(rates) > 1}


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
