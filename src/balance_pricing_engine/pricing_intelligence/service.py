"""The pricing-intelligence facade.

One entry point for "what is happening in the NZ retail banking pricing
market?". Callers -- the CLI, the dashboard, scenario scripts -- use this rather
than wiring collectors, validation, storage, and change detection themselves.

It answers market questions only. It never prices anything: turning market
intelligence into a competitor assumption is
:mod:`balance_pricing_engine.pricing_intelligence.strategy`, and deciding a rate
remains the optimiser's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Callable, Optional, Sequence

from balance_pricing_engine.competitor_rates.collectors import (
    BankPricingCollector,
    CollectionReport,
    Fetcher,
    build_collectors,
    collect_all_rates,
)
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.changes import (
    ChangeType,
    RateChange,
    detect_changes,
    load_changes,
    resolve_pairing,
)
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.market_state import (
    MarketStateAssessment,
    classify_market_state,
)
from balance_pricing_engine.pricing_intelligence.positioning import (
    CompetitivePosition,
    MarketActivity,
    compute_competitive_position,
    compute_market_activity,
)
from balance_pricing_engine.pricing_intelligence.snapshot import (
    MarketSnapshot,
    build_market_snapshot,
)
from balance_pricing_engine.pricing_intelligence.store import (
    ChronologicalOrderError,
    ObservationStore,
    WriteResult,
)
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    ResolvedCompetitorAssumption,
    resolve_competitor_rate,
)
from balance_pricing_engine.pricing_intelligence.validation import (
    ObservationIssue,
    ValidationOutcome,
    validate_observations,
)

logger = logging.getLogger(__name__)


def _exclude_suspicious(
    records: Sequence[CompetitorRateRecord],
) -> list[CompetitorRateRecord]:
    """Drop records flagged suspicious pending human confirmation.

    A suspicious-but-accepted observation is still stored for audit and
    history, but must not feed a market-derived pricing view -- see
    :meth:`PricingIntelligenceService.collect`. Prefer :func:`_resolve_suspicious`
    where a fallback lookup is available: dropping a record outright makes
    the bank/term vanish from the view rather than falling back to what was
    last verified, which can itself move a market-derived assumption.
    """
    return [record for record in records if record.validation_status != "warning"]


def _resolve_suspicious(
    records: Sequence[CompetitorRateRecord],
    confirmed: Sequence[CompetitorRateRecord],
) -> list[CompetitorRateRecord]:
    """Replace a suspicious-tagged record with its last confirmed state.

    A parser fault such as 5.35% becoming 8.49% must not be able to move a
    MARKET_LEADER/MARKET_MEDIAN assumption simply by making the old,
    confirmed rate disappear from the view. Matched via
    :func:`~.changes.resolve_pairing` (exact key, falling back to an
    unambiguous eligibility-group match) rather than a plain key lookup: a
    record whose eligibility field moved alongside its suspicious rate
    changes its own key too, so an exact lookup into ``confirmed`` would miss
    the very predecessor it needs to fall back to and drop the record
    regardless. A key with no confirmed fallback (a suspicious value on its
    very first-ever observation) is dropped, matching :func:`_exclude_suspicious`.

    A further, validation-only-style fallback on
    :meth:`~.models.CompetitorRateRecord.category_agnostic_group_key` covers
    the case :func:`~.validation.validate_observations` already flags via the
    same key: a suspicious value that also flips ``rate_category`` (e.g. a
    ``special`` rate misclassified as ``standard``) changes both
    ``observation_key`` and ``eligibility_group_key`` too, so without this
    the bank's last confirmed rate would simply vanish from the snapshot
    instead of substituting back in. Applied only when exactly one unclaimed
    confirmed record shares the group -- an ambiguous match is never guessed
    at as a substitute.

    Deduplicated by ``observation_key()``: unlike an eligibility edit (which
    ``get_current_observations()`` always supersedes out of ``records``, so
    its fallback target is never independently present), a category flip's
    predecessor is a *different* eligibility group and is never superseded --
    it can legitimately already be sitting in ``records`` in its own right
    (still genuinely advertised, unaffected by this run's parser fault). Left
    undeduplicated, the same confirmed rate would appear twice: once on its
    own row, once again as the flagged record's substitute.
    """
    suspicious = [record for record in records if record.validation_status == "warning"]
    fallback_by_key = resolve_pairing(confirmed, suspicious) if suspicious else {}
    claimed_confirmed_keys = {c.observation_key() for c in fallback_by_key.values()}
    unclaimed_confirmed = [c for c in confirmed if c.observation_key() not in claimed_confirmed_keys]
    category_agnostic_candidates: dict[str, list[CompetitorRateRecord]] = {}
    for c in unclaimed_confirmed:
        category_agnostic_candidates.setdefault(c.category_agnostic_group_key(), []).append(c)

    resolved: list[CompetitorRateRecord] = []
    seen_keys: set = set()
    for record in records:
        if record.validation_status != "warning":
            if record.observation_key() not in seen_keys:
                seen_keys.add(record.observation_key())
                resolved.append(record)
            continue
        fallback = fallback_by_key.get(record.observation_key())
        if fallback is None:
            candidates = category_agnostic_candidates.get(record.category_agnostic_group_key(), [])
            if len(candidates) == 1:
                fallback = candidates[0]
        if fallback is not None and fallback.observation_key() not in seen_keys:
            seen_keys.add(fallback.observation_key())
            resolved.append(fallback)
    return resolved


def _resolve_suspicious_if_present(
    records: Sequence[CompetitorRateRecord],
    confirmed_source: Callable[[], Sequence[CompetitorRateRecord]],
) -> list[CompetitorRateRecord]:
    """Apply :func:`_resolve_suspicious`, deferring the confirmed-fallback
    lookup (a separate store query) until a suspicious record is actually
    present. Shared by the live and historical (as-of) snapshot paths.
    """
    if any(record.validation_status == "warning" for record in records):
        return _resolve_suspicious(records, confirmed_source())
    return list(records)


def _suspicious_predecessor_keys(
    baseline: Sequence[CompetitorRateRecord],
    accepted: Sequence[CompetitorRateRecord],
    suspicious_keys: set,
) -> set:
    """Baseline keys that must not be reported as withdrawn this run because
    they are the unmatched :meth:`~.models.CompetitorRateRecord.category_agnostic_group_key`
    predecessor of a suspicious record in ``accepted``.

    ``resolve_pairing()`` (used by :func:`~.changes.detect_changes` for
    withdrawal detection) cannot pair a record whose ``rate_category`` flipped
    alongside an implausible rate -- ``rate_category`` is real identity in
    ``eligibility_group_key()``, unlike an eligibility field, precisely so two
    simultaneously-advertised categories are never collapsed together. Left
    unhandled, that predecessor would be reported ``REMOVED_PRODUCT`` purely
    because this run's parser fault renamed its category, which permanently
    excludes it from :meth:`~.store.ObservationStore.get_last_confirmed_observations`
    -- the very fallback :func:`_resolve_suspicious` needs to substitute it
    back into the market view. Only suppressed when exactly one unclaimed
    baseline record shares the group, the same ambiguity guard
    :func:`~.validation.validate_observations` applies when it flags the
    record in the first place.

    Checked even when ``record`` already has its own exact-or-eligibility
    match this run: a category flip that stays suspicious for a second run
    (5.00% special -> 8.00% standard warning -> 7.00% standard warning) has
    an exact warning-tagged predecessor by the second run (same key both
    times), but that predecessor is itself unconfirmed -- the confirmed
    special key from before the flip is a *different*, still-orphaned
    baseline record that needs this same protection regardless.
    """
    pairs = resolve_pairing(baseline, accepted)
    claimed_baseline_keys = {p.observation_key() for p in pairs.values()}
    unclaimed: dict[str, list[CompetitorRateRecord]] = {}
    for p in baseline:
        if p.observation_key() not in claimed_baseline_keys:
            unclaimed.setdefault(p.category_agnostic_group_key(), []).append(p)

    suppressed: set = set()
    for record in accepted:
        if record.observation_key() not in suspicious_keys:
            continue
        candidates = unclaimed.get(record.category_agnostic_group_key(), [])
        if len(candidates) == 1:
            suppressed.add(candidates[0].observation_key())
    return suppressed


def _rejected_predecessor_keys(
    baseline: Sequence[CompetitorRateRecord],
    rejected: Sequence[CompetitorRateRecord],
) -> set:
    """Baseline keys that must not be reported as withdrawn this run because
    they are the unmatched :meth:`~.models.CompetitorRateRecord.category_agnostic_group_key`
    predecessor of a hard-rejected record.

    Mirrors :func:`_suspicious_predecessor_keys` for records that failed
    validation outright rather than being accepted-but-flagged: a rejected
    row whose ``rate_category`` also flipped alongside its implausible rate
    cannot be paired by :func:`~.changes.resolve_pairing` either (real
    identity there too), so without this its predecessor would be reported
    ``REMOVED_PRODUCT`` purely because a parser fault renamed its category --
    even though enough of the bank's other rows passed validation for it to
    stay in scope for withdrawal detection at all. Only suppressed when
    exactly one unclaimed baseline record shares the group.

    Checked even when ``record`` already has its own exact-or-eligibility
    match this run -- see :func:`_suspicious_predecessor_keys`'s docstring
    for why a repeated rejection across runs can give the record an exact
    (but itself still-orphaned) predecessor without that protecting the
    confirmed key from before the category flip.
    """
    pairs = resolve_pairing(baseline, rejected)
    claimed_baseline_keys = {p.observation_key() for p in pairs.values()}
    unclaimed: dict[str, list[CompetitorRateRecord]] = {}
    for p in baseline:
        if p.observation_key() not in claimed_baseline_keys:
            unclaimed.setdefault(p.category_agnostic_group_key(), []).append(p)

    suppressed: set = set()
    for record in rejected:
        candidates = unclaimed.get(record.category_agnostic_group_key(), [])
        if len(candidates) == 1:
            suppressed.add(candidates[0].observation_key())
    return suppressed


def _skipped_row_predecessor_keys(
    baseline: Sequence[CompetitorRateRecord],
    accepted: Sequence[CompetitorRateRecord],
    skipped_group_keys: Sequence[str],
) -> set:
    """Baseline keys that must not be reported as withdrawn this run because
    they are the unmatched :meth:`~.models.CompetitorRateRecord.category_agnostic_group_key`
    predecessor of a row that failed :class:`~.models.CompetitorRateRecord`
    construction outright during extraction.

    Mirrors :func:`_rejected_predecessor_keys` for a row that never became a
    ``CompetitorRateRecord`` at all -- so it never reached
    :func:`~.validation.validate_observations` to land in ``validation.rejected``
    -- see ``competitor_rates.extractors.parse_html_rates``'s ``skipped_row_keys``
    parameter. Without this, a row dropped by a parser fault this run would
    read as its predecessor having been withdrawn, even though enough of the
    bank's other rows passed validation for it to stay in scope for
    withdrawal detection at all.

    Unlike :func:`_rejected_predecessor_keys`/:func:`_suspicious_predecessor_keys`
    (which only suppress an unambiguous single match, since an ambiguous
    group there merely means "this specific record's predecessor can't be
    pinned down" -- there is no independent proof any of them actually
    failed), a name in ``skipped_group_keys`` is proof-positive that
    extraction failed for *some* row sharing that exact group this run. When
    more than one baseline record shares that same coarse group, we cannot
    tell *which* one the failure belongs to, but we know it belongs to one
    of them -- so every candidate in the group is suppressed, not just an
    unambiguous singleton, or a genuinely withdrawn sibling could be spared
    the ``REMOVED_PRODUCT`` event to which it merely happens to share a
    category-agnostic key with the row that actually failed.
    """
    if not skipped_group_keys:
        return set()
    pairs = resolve_pairing(baseline, accepted)
    claimed_baseline_keys = {p.observation_key() for p in pairs.values()}
    unclaimed: dict[str, list[CompetitorRateRecord]] = {}
    for p in baseline:
        if p.observation_key() not in claimed_baseline_keys:
            unclaimed.setdefault(p.category_agnostic_group_key(), []).append(p)

    suppressed: set = set()
    for group_key in skipped_group_keys:
        for candidate in unclaimed.get(group_key, []):
            suppressed.add(candidate.observation_key())
    return suppressed


def _last_confirmed_predecessors(
    last_confirmed: Sequence[CompetitorRateRecord],
    accepted: Sequence[CompetitorRateRecord],
) -> dict[str, CompetitorRateRecord]:
    """The best-matching last-confirmed predecessor for each accepted
    record, used by ``collect()``'s suspicious-confirmation rebase.

    Falls back from an exact-or-eligibility-group match (via
    :func:`~.changes.resolve_pairing`) to an unambiguous
    :meth:`~.models.CompetitorRateRecord.category_agnostic_group_key` match
    when the chain of suspicious readings also flipped ``rate_category``:
    neither ``observation_key()`` nor ``eligibility_group_key()`` can cross
    that boundary, since ``rate_category`` is real identity in both, so
    ``resolve_pairing()`` alone leaves such a record with no last-confirmed
    match at all -- and an unmatched record is treated as having no prior
    confirmed state to compare against, confirming an artificial movement
    from the latest (still unconfirmed) warning-chain hop instead.
    """
    pairs = dict(resolve_pairing(last_confirmed, accepted))
    claimed_keys = {p.observation_key() for p in pairs.values()}
    unclaimed: dict[str, list[CompetitorRateRecord]] = {}
    for p in last_confirmed:
        if p.observation_key() not in claimed_keys:
            unclaimed.setdefault(p.category_agnostic_group_key(), []).append(p)

    for record in accepted:
        key = record.observation_key()
        if key in pairs:
            continue
        candidates = unclaimed.get(record.category_agnostic_group_key(), [])
        if len(candidates) == 1:
            pairs[key] = candidates[0]
    return pairs


def _exclude_stale(
    records: Sequence[CompetitorRateRecord], now: datetime, max_age_hours: float
) -> list[CompetitorRateRecord]:
    """Drop a record whose last confirmation is older than the freshness limit.

    Complements :func:`_exclude_suspicious`. An unreachable bank's last-known
    rate stays in change detection's baseline indefinitely (AGENTS.md: an
    unreachable bank is unknown, not withdrawn), but that same "unknown"
    value must not keep feeding a live market-derived pricing view forever --
    a months-stale rate could otherwise silently drive MARKET_LEADER or
    MARKET_MEDIAN.
    """
    cutoff = now - timedelta(hours=max_age_hours)
    return [record for record in records if record.captured_at_utc >= cutoff]


def _resolve_rate_category(
    product_type: ProductType | str, rate_category: Optional[str]
) -> str:
    """Comparable category for the selected market when the caller didn't
    supply one explicitly.

    Mirrors ``pricing_intelligence.cli``'s own ``_default_category()``:
    mortgages compare on advertised specials (where front-book competition
    happens); deposits on the plain term-deposit tier, excluding PIE
    variants. ``None`` is the sentinel for "not supplied" -- every market-
    view method below defaults to it rather than to a literal ``"special"``,
    so a caller that only sets ``product_type=TERM_DEPOSIT`` still resolves
    against the deposit market instead of a mortgage-specific default that
    happens to match no deposit records at all. An explicitly requested
    category (even one that equals the other product's default) is honoured
    as-is.
    """
    if rate_category is not None:
        return rate_category
    if ProductType(product_type) is ProductType.TERM_DEPOSIT:
        return "term_deposit"
    return "special"


#: Distinguishes "rate_category not supplied" from an explicit
#: rate_category=None on get_market_snapshot()/get_previous_market_snapshot()
#: specifically. Those two build their result via build_market_snapshot(),
#: whose own contract already treats rate_category=None as "unfiltered,
#: every category" -- collapsing that onto the same None _resolve_rate_category()
#: treats as "not supplied, resolve the product's default" would silently
#: turn an explicit request for the unfiltered view into a filtered one for
#: any caller relying on that contract. The other market-view methods below
#: (get_market_state, get_competitive_position, resolve_competitor_assumption)
#: have no unfiltered mode of their own -- comparing across categories has no
#: coherent meaning for a market-state classification or a single resolved
#: rate -- so they keep the simpler None-is-not-supplied convention
#: _resolve_rate_category() documents.
_RATE_CATEGORY_OMITTED = object()


def _reliably_collected_banks(
    report: CollectionReport,
    accepted: Sequence[CompetitorRateRecord],
    baseline: Sequence[CompetitorRateRecord],
    min_coverage_ratio: float,
) -> tuple[str, ...]:
    """Banks whose collection this run plausibly covered their known range.

    A bank can report SUCCESS with just one parseable row after a page-layout
    regression silently drops the rest of its table. Passing that bank
    straight to withdrawal detection would then read every other, still
    genuinely-current product as withdrawn. A bank whose *matched* baseline
    keys this run falls short of its previously known key count (below
    ``min_coverage_ratio``) is excluded here, same as a bank that failed to
    fetch outright: unknown, not withdrawn.

    Coverage is measured by matched predecessor identity (via
    :func:`~.changes.resolve_pairing`), not a raw accepted-record count: a
    parser regression that replaces missing rows with the same number of
    plausible but wrongly-identified ones would otherwise satisfy the
    baseline count while matching none of the actual known keys, still
    reading every one of them as withdrawn.

    ``non_comparable`` identities are excluded from both sides of the ratio:
    they are audit-only (never a market statistic, see
    :mod:`~.validation`'s disambiguation), and a bank can accumulate more of
    them than genuinely comparable rows. Counting them here would let a page
    that stops publishing (or a parser that stops extracting) just the
    audit-only rows drag a bank below the coverage threshold even though
    every comparable term-deposit rate collected cleanly -- and because that
    then excludes the bank from withdrawal detection entirely, those stale
    identities never get pruned either, so every later run stays low-coverage
    too.
    """
    comparable_baseline = [record for record in baseline if record.rate_category != "non_comparable"]
    baseline_keys: dict[str, set[str]] = {}
    for record in comparable_baseline:
        baseline_keys.setdefault(record.bank, set()).add(record.observation_key())

    matched_keys: dict[str, set[str]] = {}
    for predecessor in resolve_pairing(comparable_baseline, accepted).values():
        matched_keys.setdefault(predecessor.bank, set()).add(predecessor.observation_key())

    reliable: list[str] = []
    for bank in (result.bank for result in report.succeeded):
        known = baseline_keys.get(bank, set())
        matched = matched_keys.get(bank, set())
        if not known or len(matched) >= len(known) * min_coverage_ratio:
            reliable.append(bank)
    return tuple(reliable)


def _low_coverage_category_predecessor_keys_to_suppress(
    accepted: Sequence[CompetitorRateRecord],
    baseline: Sequence[CompetitorRateRecord],
    min_coverage_ratio: float,
) -> set[str]:
    """Baseline keys to exempt from withdrawal detection for a
    ``(bank, rate_category)`` pair whose coverage this run fell below
    ``min_coverage_ratio``, even when the bank's *overall* comparable
    coverage (lumped across every non-``non_comparable`` category -- see
    :func:`_reliably_collected_banks`) still clears the threshold.

    A bank's aggregate ratio can stay healthy while one specific category
    silently collapses -- e.g. a term-deposit bank whose ordinary
    ``term_deposit`` rows a parser regression now misses entirely, while
    its ``pie_term_deposit`` rows keep collecting cleanly, can still clear
    the default 50% *combined* threshold and reach ``detect_changes()`` as
    a ``banks_collected`` bank. Without this, every one of that category's
    now-missing keys reads as a genuine ``REMOVED_PRODUCT`` at write time --
    silently removing the bank's last-known rates in that category from
    every future default market snapshot, before any post-hoc scheduled-
    health check (e.g. the workflow's zero-comparable-rate exit code) could
    even run to flag it.

    Computed per ``(bank, rate_category)``, covering every category
    including ``non_comparable`` (this subsumes the former
    non_comparable-only version of this check), the same way
    :func:`_reliably_collected_banks` computes its own lumped ratio -- just
    scoped one category at a time instead of combined -- so a category-scoped
    regression is caught regardless of which category it hits, while a
    single identity genuinely closing within an otherwise-healthy category
    still reports as withdrawn.

    Skips a ``(bank, category)`` pair with exactly one known key: a size-1
    category can only ever show 0-of-1 or 1-of-1 matched, so "0 of 1" gives
    the ratio check nothing to distinguish a genuine sole closure from a
    parser regression -- ``0 < 1 * min_coverage_ratio`` is true for *any*
    positive ratio, so every such disappearance would be suppressed
    unconditionally, permanently hiding that category's stale rate from
    ``get_current_observations()``/market snapshots instead of letting the
    normal, single-key withdrawal path report it. A size-2-or-more category
    still needs the ratio check: with only a *raw* missing-key count, a
    parser fault dropping every row in a large category would report each
    one as an individually "genuine" withdrawal instead of the coverage
    regression it actually is.
    """
    categories = {record.rate_category for record in baseline}
    suppressed: set[str] = set()
    for category in categories:
        category_baseline = [record for record in baseline if record.rate_category == category]
        baseline_keys: dict[str, set[str]] = {}
        for record in category_baseline:
            baseline_keys.setdefault(record.bank, set()).add(record.observation_key())

        matched_keys: dict[str, set[str]] = {}
        for predecessor in resolve_pairing(category_baseline, accepted).values():
            matched_keys.setdefault(predecessor.bank, set()).add(predecessor.observation_key())

        for bank, known in baseline_keys.items():
            if len(known) <= 1:
                continue
            matched = matched_keys.get(bank, set())
            if len(matched) < len(known) * min_coverage_ratio:
                suppressed.update(known - matched)
    return suppressed


@dataclass(frozen=True)
class CollectionOutcome:
    """Everything one collection run produced."""

    report: CollectionReport
    validation: ValidationOutcome
    write_result: Optional[WriteResult]
    changes: tuple[RateChange, ...]
    #: Banks that fetched successfully but matched too few of their previously
    #: known rate lines (below ``min_bank_coverage_ratio``) -- the same signal
    #: _reliably_collected_banks() computes to suppress false withdrawal
    #: events, surfaced here so a caller judging collection health (e.g. the
    #: scheduled-collection workflow) doesn't have to recompute it. A bank
    #: with no known baseline (first-ever collection) is never included, same
    #: as in the withdrawal-suppression use: an empty range isn't "missing".
    low_coverage_banks: tuple[str, ...] = ()

    @property
    def succeeded_banks(self) -> tuple[str, ...]:
        return tuple(result.bank for result in self.report.succeeded)

    @property
    def failed_banks(self) -> tuple[str, ...]:
        return tuple(result.bank for result in self.report.failed)

    @property
    def rate_count(self) -> int:
        return len(self.validation.accepted)

    def render(self) -> str:
        lines = [self.report.render(), ""]
        lines.append(f"Validation: {self.validation.summary()}")
        if self.write_result is not None:
            lines.append(
                f"Stored: {self.write_result.inserted} new advertised states, "
                f"{self.write_result.unchanged} unchanged"
            )
        lines.append(f"Changes detected: {len(self.changes)}")
        return "\n".join(lines)


class PricingIntelligenceService:
    """Collects, stores, and interprets public NZ bank pricing."""

    def __init__(
        self,
        config: Optional[PricingIntelligenceConfig] = None,
        store: Optional[ObservationStore] = None,
        collectors: Optional[Sequence[BankPricingCollector]] = None,
        fetcher: Optional[Fetcher] = None,
        raw_root: str | Path = "data/raw/competitor_rates",
        render_javascript: bool = False,
    ) -> None:
        self.config = config or PricingIntelligenceConfig()
        self.store = store or ObservationStore(self.config.database_path)
        self._collectors = collectors
        self._fetcher = fetcher
        self._raw_root = raw_root
        self.render_javascript = render_javascript

    # ------------------------------------------------------------- collection

    @property
    def collectors(self) -> list[BankPricingCollector]:
        if self._collectors is not None:
            return list(self._collectors)
        return build_collectors(
            banks=self.config.selected_banks,
            config=self.config.collection,
            fetcher=self._fetcher,
            raw_root=self._raw_root,
            render_javascript=self.render_javascript,
        )

    def collect(
        self,
        captured_at_utc: Optional[datetime] = None,
        persist: bool = True,
        persist_snapshot: bool = True,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> CollectionOutcome:
        """Run a full collection: fetch, validate, store, and detect changes.

        A failing bank is isolated into a FAILED entry in the report; the run
        continues and the remaining banks are still stored. Records failing
        validation are rejected before storage, so a parser fault cannot enter
        the history.

        ``product_type`` selects home loans (the default) or term deposits.
        Each product keeps its own baseline, change history, and bootstrap
        state, so collecting one is never read as the other being withdrawn.
        """
        product_type = ProductType(product_type)
        captured_at_utc = captured_at_utc or datetime.now(timezone.utc)

        # Checked before fetching, not just left to record_observations()'s
        # own guard at the end of this method: a backwards wall clock (an
        # NTP correction, a restored VM snapshot) is detected the same way
        # either place, but catching it here means a scheduled run fails
        # immediately instead of only after every bank has already been
        # fetched and validated -- work whose results would otherwise be
        # thrown away once the later guard raises. Only relevant when this
        # call will actually persist; a dry run never reaches that guard.
        if persist:
            latest = self.store.latest_collection_timestamp(product_type=product_type)
            if latest is not None and captured_at_utc < latest:
                raise ChronologicalOrderError(
                    "collections must be written in chronological order per product "
                    f"(this run {captured_at_utc}, latest stored {latest})"
                )
        report = collect_all_rates(
            self.collectors,
            product_type=product_type,
            captured_at_utc=captured_at_utc,
            persist_snapshot=persist_snapshot,
            inter_request_delay_seconds=self.config.collection.inter_request_delay_seconds,
        )

        # Captured before this run's own storage write, per bank: an empty
        # baseline does not by itself mean "first collection ever for this
        # bank" (every currently tracked product could simply have been
        # withdrawn), so bootstrap suppression below asks instead whether
        # this bank has ever had any observation stored -- independently of
        # whether some other bank already has a long history, so a bank that
        # failed every prior run and only now recovers is still treated as
        # bootstrapping, not a burst of genuine new products.
        previously_seen_banks = set(self.store.get_banks(product_type=product_type))

        # The last known state per key, not scoped to the latest completed
        # run: a bank that failed a prior run would otherwise fall out of the
        # baseline entirely, making its next (unchanged) collection look like
        # every advertised line is new.
        baseline = self.store.get_current_observations(product_type=product_type)
        # Repeatability is not independent verification. Always measure a new
        # reading against the last trusted state, never a prior parser spike.
        last_confirmed_before_run = self.store.get_last_confirmed_observations(
            product_type=product_type
        )
        validation = validate_observations(
            report.records, thresholds=self.config.validation,
            previous=_resolve_suspicious(baseline, last_confirmed_before_run),
        )
        trusted_pairs = _last_confirmed_predecessors(last_confirmed_before_run, validation.accepted)
        previous_pairs = resolve_pairing(baseline, validation.accepted)
        for record in validation.accepted:
            key = record.observation_key()
            predecessor = previous_pairs.get(key)
            trusted = trusted_pairs.get(key)
            if (
                predecessor is not None and predecessor.validation_status == "warning"
                and (trusted is None or trusted.observation_key() != key)
                and key not in validation.suspicious_keys
            ):
                validation.issues.append(ObservationIssue(
                    severity="warning", code="suspicious_movement", bank=record.bank,
                    observation_key=key,
                    message="unverified eligibility/category change remains quarantined",
                ))
        for issue in validation.issues:
            logger.warning("validation: %s", issue)

        if not validation.accepted:
            return CollectionOutcome(
                report=report, validation=validation, write_result=None, changes=()
            )

        # A suspicious-but-accepted record (a plausible yet unusually large
        # move) is stored for audit and history, but tagged so market-derived
        # pricing views can exclude it until a human confirms it against the
        # source page -- a parser fault must not feed an unverified rate
        # straight into the optimiser.
        suspicious_keys = validation.suspicious_keys
        accepted = [
            record.model_copy(update={"validation_status": "warning"})
            if record.observation_key() in suspicious_keys
            else record
            for record in validation.accepted
        ]

        # Observations and changes commit together so a failed event write
        # cannot advance the next run's baseline and lose a movement.
        with self.store.atomic():
            # A dry run (persist=False) skips writing, but still detects changes
            # against the stored baseline -- otherwise --no-persist could not be
            # used to preview what a real collection would do.
            write_result = (
                self.store.record_observations(
                    accepted, observed_at=captured_at_utc, product_type=product_type
                )
                if persist
                else None
            )
            # Resolve the trusted predecessor across eligibility/category keys
            # so quarantine cannot withdraw the last usable market observation.
            last_confirmed_pairs = _last_confirmed_predecessors(
                last_confirmed_before_run, accepted
            )
            # A clean recovery from a warning is distinct from a category
            # relabelling with no suspicious history. Only the former needs
            # the trusted predecessor substituted into its change baseline.
            plain_pairs = resolve_pairing(baseline, accepted)
            confirming_keys = {
                key
                for key, previous_record in plain_pairs.items()
                if previous_record.validation_status == "warning" and key not in suspicious_keys
            }
            # Computed once and reused below both for change detection's
            # withdrawal-suppression scope and for CollectionOutcome's own
            # low_coverage_banks, so a caller judging collection health does
            # not have to recompute the same coverage check independently.
            reliably_collected_banks = _reliably_collected_banks(
                report, accepted, baseline, self.config.validation.min_bank_coverage_ratio
            )
            # A clean reading after a parser spike is compared with the last
            # trusted rate; the spike must not become a fictitious reversal.
            recovery_predecessors = {
                plain_pairs[key].observation_key(): last_confirmed_pairs[key]
                for key in confirming_keys
                if key in last_confirmed_pairs
            }
            change_baseline = list({
                record.observation_key(): record
                for record in (
                    recovery_predecessors.get(record.observation_key(), record)
                    for record in baseline
                )
            }.values())
            changes = detect_changes(
                previous=change_baseline,
                current=accepted,
                detected_at=captured_at_utc,
                thresholds=self.config.validation,
                run_id=write_result.run_id if write_result is not None else None,
                # Scoped to banks that both responded and whose coverage this run
                # plausibly matches their known range, so neither an unreachable
                # bank nor a partial-parse regression is read as withdrawing its
                # whole range.
                banks_collected=reliably_collected_banks,
                # A record rejected by validation this run is unknown, not
                # withdrawn -- a single implausible line must not read as a
                # market event. Suppressed by the *previous* record's key, not
                # the rejected record's own key: a rejected line that also
                # changed a key-forming eligibility field (e.g. a parser fault
                # that garbles the rate and misreads max_lvr together) would
                # otherwise have a key that never matches its last confirmed
                # predecessor, so the predecessor's key is only found via the
                # same eligibility-group fallback used elsewhere, or -- when the
                # rejected line's rate_category flipped too -- the
                # category-agnostic fallback in _rejected_predecessor_keys(). A
                # suspicious (accepted-but-flagged) record's predecessor gets the
                # analogous treatment via _suspicious_predecessor_keys().
                # A previously quarantined predecessor also remains protected
                # during a clean recovery. Ordinary category changes still
                # emit their genuine new/removed pair.
                rejected_keys=(
                    {
                        previous_record.observation_key()
                        for previous_record in resolve_pairing(
                            baseline, validation.rejected
                        ).values()
                    }
                    | _rejected_predecessor_keys(baseline, validation.rejected)
                    | _suspicious_predecessor_keys(baseline, accepted, suspicious_keys)
                    # A row that failed CompetitorRateRecord construction
                    # outright this run (a parser fault, before the row could
                    # even reach validate_observations) is unknown, not
                    # withdrawn, same as a rejected-but-constructed record
                    # just above.
                    | _skipped_row_predecessor_keys(
                        baseline, accepted, tuple(report.skipped_row_keys)
                    )
                    | {
                        last_confirmed_pairs[key].observation_key()
                        for key in confirming_keys
                        if key in last_confirmed_pairs
                    }
                    # A bank counted as reliably_collected_banks above only
                    # because that check lumps every non-non_comparable
                    # category into one combined ratio -- one specific
                    # category (non_comparable, or a real but non-default
                    # category like pie_term_deposit) can still have
                    # regressed on its own while the lumped total stays
                    # healthy. Without this, every now-missing key in that
                    # category reads as a genuine withdrawal instead of the
                    # same "unknown, not withdrawn" treatment
                    # _reliably_collected_banks() gives a whole-bank
                    # coverage regression.
                    | _low_coverage_category_predecessor_keys_to_suppress(
                        accepted, baseline, self.config.validation.min_bank_coverage_ratio
                    )
                ),
            )
            # A bank's first-ever successful collection would otherwise report
            # every one of its rates as a new product, which is noise rather than
            # market intelligence. Suppressed per bank, not globally.
            bootstrapping_banks = {
                change.bank
                for change in changes
                if change.change_type is ChangeType.NEW_PRODUCT
                and change.bank not in previously_seen_banks
            }
            if bootstrapping_banks:
                logger.info(
                    "bootstrapping bank(s) with no prior observation; suppressing "
                    "new-product events for %s",
                    ", ".join(sorted(bootstrapping_banks)),
                )
                changes = [
                    change
                    for change in changes
                    if not (
                        change.change_type is ChangeType.NEW_PRODUCT
                        and change.bank in bootstrapping_banks
                    )
                ]
            if changes and persist:
                self.store.record_changes(changes)

        low_coverage_banks = tuple(
            sorted(
                {result.bank for result in report.succeeded} - set(reliably_collected_banks)
            )
        )
        return CollectionOutcome(
            report=report,
            validation=validation,
            write_result=write_result,
            changes=tuple(changes),
            low_coverage_banks=low_coverage_banks,
        )

    # ------------------------------------------------------------ market view

    def get_market_snapshot(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = _RATE_CATEGORY_OMITTED,
        as_of_run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MarketSnapshot:
        """Return the currently-advertised market across tracked terms.

        ``rate_category`` defaults to the product's comparable category (see
        :func:`_resolve_rate_category`) when omitted. Pass ``rate_category=None``
        explicitly for the unfiltered view across every category, matching
        :func:`~.snapshot.build_market_snapshot`'s own contract -- see
        :data:`_RATE_CATEGORY_OMITTED`.

        Uses the last known state per key (via ``get_current_observations()``),
        not a strict "last run" filter: a bank's rate that failed to fetch, or
        a single line another bank had rejected by validation, must not drop
        out of the market view just because it wasn't reconfirmed this exact
        run -- only a confirmed withdrawal does that. A key whose latest state
        is flagged suspicious falls back to its last confirmed (non-warning)
        state rather than disappearing outright, so a parser fault cannot
        move MARKET_LEADER/MARKET_MEDIAN simply by making the prior rate
        vanish from the view. That last-known state is not kept forever, though: one older than
        ``config.max_observation_age_hours`` is dropped, so a bank gone quiet
        for weeks cannot keep driving MARKET_LEADER/MARKET_MEDIAN on a stale
        rate. Only applies to the live view -- an explicit ``as_of_run_id``
        reconstruction is exempt, since staleness relative to "now" is not
        the question being asked.

        ``as_of_run_id`` reconstructs an older run's state via
        ``get_observations_as_of()``: ``last_seen_run_id`` (what
        ``get_latest_rates()`` filters on) is overwritten forward on every
        unchanged re-observation, so it cannot answer for a run that isn't
        the latest one.
        """
        if rate_category is _RATE_CATEGORY_OMITTED:
            rate_category = _resolve_rate_category(product_type, None)
        as_of: Optional[datetime] = None
        if as_of_run_id is not None:
            as_of = self.store.get_run_started_at(as_of_run_id)
            if as_of is None:
                records = []
            else:
                # before_run_id=as_of_run_id anchors the reconstruction to
                # this specific run's own (started_at, rowid) position, not
                # just its timestamp -- two runs for the same product can
                # share started_at, and without this a later same-timestamp
                # run's state would still win the per-key selection even when
                # this run_id explicitly names the earlier one. See
                # ObservationStore._as_of_cutoff()'s docstring.
                records = self.store.get_observations_as_of(
                    as_of=as_of, product_type=product_type, before_run_id=as_of_run_id
                )
                records = _resolve_suspicious_if_present(
                    records,
                    lambda: self.store.get_confirmed_observations_as_of(
                        as_of=as_of, product_type=product_type, before_run_id=as_of_run_id
                    ),
                )
        else:
            records = self.store.get_current_observations(product_type=product_type)
            records = _resolve_suspicious_if_present(
                records,
                lambda: self.store.get_last_confirmed_observations(product_type=product_type),
            )
            records = _exclude_stale(
                records,
                now=now or datetime.now(timezone.utc),
                max_age_hours=self.config.max_observation_age_hours,
            )
        return build_market_snapshot(
            _exclude_suspicious(records),
            product_type=product_type,
            rate_category=rate_category,
            terms_months=self.config.tracked_terms_months,
            banks=self.config.selected_banks,
            as_of=as_of,
            excluded_product_names=self.config.excluded_product_names,
        )

    def get_previous_market_snapshot(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = _RATE_CATEGORY_OMITTED,
    ) -> Optional[MarketSnapshot]:
        """Return the snapshot from the run before the latest, if there is one.

        ``rate_category`` defaults to the product's comparable category when
        omitted; pass ``rate_category=None`` explicitly for the unfiltered
        view across every category -- see :data:`_RATE_CATEGORY_OMITTED`.
        """
        if rate_category is _RATE_CATEGORY_OMITTED:
            rate_category = _resolve_rate_category(product_type, None)
        # Resolved together from one store lookup, not as two separate
        # get_previous_run_started_at()/get_previous_run_id() calls: another
        # same-product collection committing between two separate calls
        # would shift which run each one resolves "previous" relative to,
        # so the timestamp and run_id could end up naming two different
        # runs -- see ObservationStore.get_previous_run()'s docstring.
        previous_run = self.store.get_previous_run(product_type=product_type)
        if previous_run is None:
            return None
        as_of = previous_run.started_at
        # Anchored to the resolved run's own identity, not just its
        # timestamp: two runs for the same product can share observed_at, and
        # a timestamp-only reconstruction can't tell which of them to stop
        # at -- see ObservationStore._as_of_cutoff()'s docstring.
        before_run_id = previous_run.run_id
        records = self.store.get_observations_as_of(
            as_of=as_of, product_type=product_type, before_run_id=before_run_id
        )
        if not records:
            return None
        records = _resolve_suspicious_if_present(
            records,
            lambda: self.store.get_confirmed_observations_as_of(
                as_of=as_of, product_type=product_type, before_run_id=before_run_id
            ),
        )
        return build_market_snapshot(
            _exclude_suspicious(records),
            product_type=product_type,
            rate_category=rate_category,
            terms_months=self.config.tracked_terms_months,
            banks=self.config.selected_banks,
            as_of=as_of,
            excluded_product_names=self.config.excluded_product_names,
        )

    def get_changes(
        self,
        days: Optional[int] = 7,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        now: Optional[datetime] = None,
        scope_to_market: bool = False,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = None,
    ) -> list[RateChange]:
        """Return detected changes inside a lookback window, newest first.

        ``days=None`` returns every stored change with no lookback limit,
        matching :meth:`get_rate_history`'s own ``days=None`` convention.

        ``scope_to_market=True`` additionally restricts to
        ``selected_banks``/``tracked_terms_months`` -- the same scope every
        snapshot/history view already uses -- so a database holding
        observations from an earlier or broader configuration can't report
        activity for a bank or term the rest of the market view doesn't show.
        """
        rows = self.store.get_changes_since(
            days=days,
            bank=bank,
            term_months=term_months,
            product_type=product_type,
            rate_category=rate_category,
            now=now,
        )
        changes = load_changes(rows)
        if scope_to_market:
            changes = [
                change
                for change in changes
                if change.bank in self.config.selected_banks
                and change.term_months in self.config.tracked_terms_months
            ]
        return changes

    def get_rate_history(
        self,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = None,
        days: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[CompetitorRateRecord]:
        """Return the distinct advertised states for a bank/term, oldest first.

        Bounded by ``now`` (defaulting to the current time) on the upper edge
        too, matching :meth:`get_changes`: a historical ``now=`` must not
        expose a state first observed after that point.
        """
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(days=days) if days is not None else None
        return self.store.get_rate_history(
            bank=bank,
            term_months=term_months,
            product_type=product_type,
            rate_category=rate_category,
            since=since,
            until=now,
        )

    def get_rate_history_series(
        self,
        term_months: int,
        rate_category: Optional[str] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> list[tuple[date, dict[str, float]]]:
        """Each bank's representative advertised rate at ``term_months``, one
        entry per date something changed, oldest first.

        Reconstructed via :meth:`~.store.ObservationStore.get_observations_as_of`
        (already handles withdrawal and eligibility-supersession exclusion) run
        through :func:`~.snapshot.build_market_snapshot` (already picks the
        single most competitive rate per bank when several products are
        simultaneously advertised) at every date something changed --
        deliberately not derived from raw per-state rows in the dashboard/chart
        layer, which cannot see either of those without reimplementing them: a
        bank with several simultaneous special products or LVR tiers needs the
        same representative-rate selection the live snapshot uses, and a
        product withdrawn on one date must actually stop appearing, not just
        forward-fill its last rate through every later date some other bank's
        change happens to introduce.
        """
        rate_category = _resolve_rate_category(product_type, rate_category)
        history = self.store.get_rate_history(
            term_months=term_months, product_type=product_type, rate_category=rate_category
        )
        change_dates = {record.captured_at_utc.date() for record in history}
        for change in self.get_changes(
            days=None,
            term_months=term_months,
            product_type=product_type,
            rate_category=rate_category,
        ):
            if change.change_type is ChangeType.REMOVED_PRODUCT and (
                change.rate_category == rate_category
            ):
                change_dates.add(change.detected_at.date())

        series: list[tuple[date, dict[str, float]]] = []
        for day in sorted(change_dates):
            as_of = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
            records = self.store.get_observations_as_of(as_of=as_of, product_type=product_type)
            # Matches get_market_snapshot()'s as_of_run_id branch: a
            # warning-tagged (suspicious) state must fall back to its last
            # confirmed rate here too, not flow straight into the chart --
            # otherwise a parser fault visibly moves the history even though
            # the live market view correctly stays on the confirmed rate.
            records = _resolve_suspicious_if_present(
                records,
                lambda as_of=as_of: self.store.get_confirmed_observations_as_of(
                    as_of=as_of, product_type=product_type
                ),
            )
            snapshot = build_market_snapshot(
                _exclude_suspicious(records),
                product_type=product_type,
                rate_category=rate_category,
                terms_months=[term_months],
                banks=self.config.selected_banks,
                as_of=as_of,
                excluded_product_names=self.config.excluded_product_names,
            )
            market = snapshot.term(term_months)
            rates = {entry.bank: entry.rate_pct for entry in market.entries} if market else {}
            series.append((day, rates))
        return series

    def get_market_activity(
        self,
        days: Optional[int] = None,
        now: Optional[datetime] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = None,
    ) -> MarketActivity:
        """Summarise repricing activity over a lookback window.

        Scoped to ``selected_banks``/``tracked_terms_months``, matching every
        snapshot, the dashboard, and :meth:`get_market_state`: a database
        holding observations from an earlier or broader configuration must
        not alter this public market summary via an excluded bank or term.
        """
        rate_category = _resolve_rate_category(product_type, rate_category)
        days = days or self.config.market_state.lookback_days
        return compute_market_activity(
            self.get_changes(
                days=days,
                now=now,
                scope_to_market=True,
                product_type=product_type,
                rate_category=rate_category,
            ),
            lookback_days=days,
            now=now,
            directional_dominance_ratio=(
                self.config.market_state.directional_dominance_ratio
            ),
        )

    def get_market_state(
        self,
        now: Optional[datetime] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = None,
    ) -> MarketStateAssessment:
        """Classify how competitively active the market currently is.

        Scoped to ``selected_banks``/``tracked_terms_months``: a database
        holding observations from an earlier or broader configuration would
        otherwise let a change outside the currently-configured market push
        the classification, even though the snapshot it accompanies is
        already scoped to those same settings.
        """
        rate_category = _resolve_rate_category(product_type, rate_category)
        changes = self.get_changes(
            days=self.config.market_state.lookback_days,
            now=now,
            scope_to_market=True,
            product_type=product_type,
            rate_category=rate_category,
        )
        return classify_market_state(
            changes, thresholds=self.config.market_state, now=now
        )

    def get_competitive_position(
        self,
        our_rate: float,
        term_months: int = 12,
        rate_category: Optional[str] = None,
        now: Optional[datetime] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> CompetitivePosition:
        """Position a rate (in advertised percent) against the current market.

        ``rate_category`` defaults to the product-appropriate comparison tier
        (via :func:`_resolve_rate_category`) rather than a mortgage-specific
        literal, so a deposit position resolves against the deposit market
        instead of a snapshot with no matching rates. Resolved here, before
        calling :meth:`get_market_snapshot`, rather than forwarding ``None``
        straight through: that method's own ``rate_category=None`` means the
        *unfiltered* view across every category (see
        :data:`_RATE_CATEGORY_OMITTED`) -- a position against a single,
        comparable market has no coherent "unfiltered" reading, so this
        method's own ``None`` must resolve to a concrete category before it
        ever reaches that call.
        """
        rate_category = _resolve_rate_category(product_type, rate_category)
        snapshot = self.get_market_snapshot(
            product_type=product_type, rate_category=rate_category, now=now
        )
        term_market = snapshot.term(term_months)
        if term_market is None:
            raise ValueError(
                f"no advertised market data at {term_months} months; "
                f"available terms: {list(snapshot.term_months)}"
            )
        return compute_competitive_position(
            term_market, our_rate, our_bank=self.config.our_bank
        )

    # ------------------------------------------------------- pricing hand-off

    def resolve_competitor_assumption(
        self,
        strategy: CompetitorRateStrategy | str = CompetitorRateStrategy.MARKET_MEDIAN,
        term_months: int = 12,
        manual_rate: Optional[float] = None,
        offset_bps: float = 0.0,
        rate_category: Optional[str] = None,
        now: Optional[datetime] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> ResolvedCompetitorAssumption:
        """Resolve the competitor assumption the optimiser should run with.

        ``rate_category`` defaults to the product-appropriate comparison tier
        (via :func:`_resolve_rate_category`) rather than a mortgage-specific
        literal, so a deposit assumption resolves against the deposit market
        instead of a snapshot with no matching rates. Resolved here, before
        calling :meth:`get_market_snapshot`, rather than forwarding ``None``
        straight through: that method's own ``rate_category=None`` means the
        *unfiltered* view across every category (see
        :data:`_RATE_CATEGORY_OMITTED`), which would let a
        ``MARKET_MEDIAN``/``MARKET_AVERAGE``/etc. strategy silently blend a
        mortgage's ``special``/``standard`` tiers -- or a deposit's
        ``term_deposit``/PIE rows -- into one competitor assumption fed
        straight into the optimiser.
        """
        rate_category = _resolve_rate_category(product_type, rate_category)
        snapshot = self.get_market_snapshot(
            product_type=product_type, rate_category=rate_category, now=now
        )
        return resolve_competitor_rate(
            strategy=strategy,
            snapshot=snapshot,
            term_months=term_months,
            manual_rate=manual_rate,
            offset_bps=offset_bps,
            our_bank=self.config.our_bank,
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "PricingIntelligenceService":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
