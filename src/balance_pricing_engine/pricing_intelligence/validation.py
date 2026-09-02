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

#: Matches any percentage token (e.g. "4.01%") in raw_text_context. Used two
#: ways by _disambiguate_non_comparable_identities() and its helpers
#: _match_previous_non_comparable_precise()/_permissive():
#: _strip_all_percentage_tokens() removes every occurrence outright (immune
#: to an unrelated mutable figure, e.g. a fund's "since inception" return,
#: but loses a percentage that is itself the only thing telling two rows
#: apart, e.g. a PIR/tax-band label such as "PIR 28%: 4.01%");
#: _strip_advertised_rate_token() instead inspects each occurrence's
#: numeric value and removes only the one matching the record's own
#: advertised rate, leaving a structural qualifier like that PIR label
#: untouched.
_PERCENTAGE_TOKEN_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _strip_all_percentage_tokens(raw_text_context: str) -> str:
    """Remove every percentage token from ``raw_text_context``.

    The permissive fallback match in :func:`_match_previous_non_comparable`,
    used only when the precise (advertised-rate-only) match finds nothing:
    most non_comparable rows are already distinguished by non-numeric label
    text (a fund name, a product description), so a row can still be
    recognised as continuing its predecessor even when some *other*
    percentage in the same text mutated independently, such as a fund's
    "since inception" return alongside its (separately parsed) "Return"
    column.
    """
    return _PERCENTAGE_TOKEN_PATTERN.sub("", raw_text_context)


def _strip_advertised_rate_token(raw_text_context: str, advertised_rate_pct: float) -> str:
    """Remove only the record's own advertised rate from ``raw_text_context``.

    Matched by numeric value rather than a fixed decimal-precision format, so
    "4.01%" is found and removed whether the source page rendered it as
    "4.01%", "4.010%", or "4.0100%" -- any other percentage in the text (a
    PIR/tax-band qualifier, an unrelated fee) is left untouched.

    Only the *last* value-matching occurrence is removed, mirroring
    :func:`~.extractors._extract_rate`/:func:`~.extractors._extract_plausible_rate`,
    which pick ``advertised_rate_pct`` itself as the *last* plausible
    percentage in the row (``plausible[-1]``) -- not the first. A structural
    qualifier that coincidentally shares the rate's value (e.g. "Growth Fund
    | 4% allocation | 4% return", where "4% return" is the parsed rate) must
    survive normalisation, or once the two values diverge (the return moves
    to 4.1%, the allocation stays 4%) the qualifier would appear only on one
    side, changing the identity for a row whose only genuine change was its
    rate. Matching the extractor's own end of the row is a closer proxy for
    "the rate's own token" than "every same-valued percentage" -- or than the
    first occurrence, which is the *wrong* end here -- without needing
    cell-boundary information this already-flattened text doesn't carry.
    """
    last_match: "re.Match[str] | None" = None
    for match in _PERCENTAGE_TOKEN_PATTERN.finditer(raw_text_context):
        if abs(float(match.group(1)) - advertised_rate_pct) < 1e-6:
            last_match = match
    if last_match is None:
        return raw_text_context
    return raw_text_context[: last_match.start()] + raw_text_context[last_match.end() :]


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

    A deposit-side ``non_comparable`` record is given a stable per-row
    identity (see :func:`_disambiguate_non_comparable_identities`) before any
    of the above, so heterogeneous rows sharing the generic category never
    collide on ``observation_key()`` in the first place. Scoped to deposits
    only: a mortgage's non_comparable products (subsidised/green top-ups,
    closed products) are already individually named by the extractor and
    have never needed this, so leaving them untouched preserves every
    already-stored mortgage identity rather than migrating it.
    """
    thresholds = thresholds or ValidationThresholds()
    records = list(records)
    previous = list(previous or [])
    non_comparable_indices = [
        i
        for i, record in enumerate(records)
        if record.rate_category == "non_comparable"
        and ProductType(record.product_type) is ProductType.TERM_DEPOSIT
    ]
    if non_comparable_indices:
        disambiguated = _disambiguate_non_comparable_identities(
            [records[i] for i in non_comparable_indices], previous
        )
        for i, record in zip(non_comparable_indices, disambiguated):
            records[i] = record
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
        # _disambiguate_non_comparable_identities() above, so two genuinely
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


def _tagged(record: CompetitorRateRecord, discriminator_text: str) -> CompetitorRateRecord:
    tag = hashlib.sha256(discriminator_text.encode("utf-8")).hexdigest()[:8]
    return record.model_copy(update={"product_name": f"{record.product_name} [{tag}]"})


def _non_comparable_base_group_key(record: CompetitorRateRecord) -> tuple:
    """Identity fields a disambiguation tag never touches.

    Deliberately excludes ``product_name`` (the field the tag is appended
    to) and anything derived from ``raw_text_context``, so this key means
    the same thing for an untagged current record and an already-tagged
    ``previous`` record -- letting :func:`_disambiguate_non_comparable_identities`
    group both sides for matching without caring which run tagged either one.
    """
    return (
        record.bank,
        str(ProductType(record.product_type)),
        record.rate_category,
        record.term_months_normalised
        if record.term_months_normalised is not None
        else record.term_label_raw.lower(),
        record.interest_payment_frequency,
    )


def _match_previous_non_comparable_precise(
    record: CompetitorRateRecord, candidates: Sequence[CompetitorRateRecord]
) -> Optional[CompetitorRateRecord]:
    """Find the one ``previous`` record ``record`` precisely continues, if any.

    Only the record's own advertised rate is stripped, keeping a PIR/tax-band
    qualifier or any other non-identity figure intact -- see
    :func:`_disambiguate_non_comparable_identities` for why this must be
    resolved for the whole batch before :func:`_match_previous_non_comparable_permissive`
    is tried on any record. Returns a match only when it identifies exactly
    one candidate: an ambiguous tie means "not confidently this one," not
    "guess."
    """
    if not candidates:
        return None
    own_precise = _strip_advertised_rate_token(record.raw_text_context, record.advertised_rate_pct)
    precise_matches = [
        candidate
        for candidate in candidates
        if _strip_advertised_rate_token(candidate.raw_text_context, candidate.advertised_rate_pct)
        == own_precise
    ]
    return precise_matches[0] if len(precise_matches) == 1 else None


def _match_previous_non_comparable_permissive(
    record: CompetitorRateRecord, candidates: Sequence[CompetitorRateRecord]
) -> Optional[CompetitorRateRecord]:
    """Find the one ``previous`` record ``record`` permissively continues, if any.

    Every percentage is stripped, so a genuinely unrelated non-identity
    figure drifting between runs (e.g. a fund's "since inception" return)
    doesn't break continuity. Only tried, batch-wide, after
    :func:`_match_previous_non_comparable_precise` -- see
    :func:`_disambiguate_non_comparable_identities`. Returns a match only
    when it identifies exactly one candidate.
    """
    if not candidates:
        return None
    own_permissive = _strip_all_percentage_tokens(record.raw_text_context)
    permissive_matches = [
        candidate
        for candidate in candidates
        if _strip_all_percentage_tokens(candidate.raw_text_context) == own_permissive
    ]
    return permissive_matches[0] if len(permissive_matches) == 1 else None


def _disambiguate_non_comparable_identities(
    records: Sequence[CompetitorRateRecord],
    previous: Sequence[CompetitorRateRecord],
) -> list[CompetitorRateRecord]:
    """Give every ``non_comparable`` row a stable, content-derived identity.

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

    Appending a short hash of a discriminator derived from
    ``raw_text_context`` to ``product_name`` -- already part of
    :meth:`~.models.CompetitorRateRecord.eligibility_group_key`'s identity
    formula -- gives each row its own stable-across-runs identity without
    touching ``raw_text_context`` itself, which stays the untouched source
    evidence.

    A record's tag must not depend on which *sibling* rows happen to be in
    this same batch: an earlier version disambiguated by whether two rows
    collided once every percentage was stripped from their text, but that
    made a surviving row's identity flip the moment a colliding sibling
    stopped being collected (e.g. two rows told apart only by a PIR/tax-band
    percentage -- "PIR 28%: 4.01%" vs "PIR 33%: 4.19%" -- one of them
    disappearing let the survivor resolve to a different, sibling-free tag),
    reading a completely unchanged row as withdrawn and a new one appearing.

    Instead, each record first looks for the ``previous`` record it
    continues, scoped to :func:`_non_comparable_base_group_key` so only rows
    that could plausibly be the same position are compared, and inherits
    that record's exact ``product_name`` (tag included) unchanged -- stable
    across runs by construction, since it never depends on this batch's
    siblings at all, only on history. A record with no continuing
    predecessor (first time seen, or its old match was claimed by another
    record in this same batch) gets a fresh tag from its own text with only
    its own advertised rate stripped (see :func:`_strip_advertised_rate_token`):
    distinct fresh rows are already told apart by their own content (a fund
    name, a tax-band qualifier) without needing to know about each other,
    and any future run re-establishes continuity through this same match.

    Matching runs in two full batch-wide passes --
    :func:`_match_previous_non_comparable_precise` for every record before
    :func:`_match_previous_non_comparable_permissive` is tried on any of
    them -- rather than trying both tiers per record as the batch is walked.
    Interleaving them per record makes the outcome depend on iteration
    order: with two prior rows told apart only by a PIR/tax-band percentage
    ("PIR 28%: 4.01%" vs "PIR 33%: 4.19%"), a row whose own precise text has
    since changed permissively matches *both* once every percentage is
    stripped. Reached before its sibling's unchanged row has precisely
    claimed its own predecessor, that permissive lookup sees both
    candidates still available, calls the tie ambiguous, and mints a
    needless fresh identity -- reached after, only one candidate remains
    and the match succeeds. Resolving every precise match first removes
    that dependency: the unchanged row always claims its own predecessor
    before any permissive lookup runs, so the changed row's permissive
    match is judged against the batch's true leftover candidates no matter
    which record was visited first.
    """
    previous_by_base: dict[tuple, list[CompetitorRateRecord]] = {}
    for candidate in previous:
        if candidate.rate_category == "non_comparable" and ProductType(
            candidate.product_type
        ) is ProductType.TERM_DEPOSIT:
            previous_by_base.setdefault(_non_comparable_base_group_key(candidate), []).append(
                candidate
            )

    claimed_previous_ids: set[int] = set()

    def _available(record: CompetitorRateRecord) -> list[CompetitorRateRecord]:
        return [
            candidate
            for candidate in previous_by_base.get(_non_comparable_base_group_key(record), [])
            if id(candidate) not in claimed_previous_ids
        ]

    matches: dict[int, CompetitorRateRecord] = {}
    for index, record in enumerate(records):
        match = _match_previous_non_comparable_precise(record, _available(record))
        if match is not None:
            claimed_previous_ids.add(id(match))
            matches[index] = match

    for index, record in enumerate(records):
        if index in matches:
            continue
        match = _match_previous_non_comparable_permissive(record, _available(record))
        if match is not None:
            claimed_previous_ids.add(id(match))
            matches[index] = match

    tagged: list[CompetitorRateRecord] = []
    for index, record in enumerate(records):
        match = matches.get(index)
        if match is not None:
            tagged.append(record.model_copy(update={"product_name": match.product_name}))
        else:
            tagged.append(
                _tagged(
                    record,
                    _strip_advertised_rate_token(record.raw_text_context, record.advertised_rate_pct),
                )
            )
    return tagged


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
