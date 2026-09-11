"""Operational orchestration for pricing-intelligence collection runs.

This is the "one implementation, several entry points" seam: the interactive
``pricing_intelligence run`` CLI command and the unattended
``scripts/collect_market_rates_workflow.py`` scheduler both call
:func:`run_operational_cycle`, so collection health classification, alert
dispatch, and run-record persistence cannot drift between the two.

``build_summary``/``exit_code_for`` (and their private bank-scoped-failure
helpers) originated in the scheduler script and are relocated here verbatim:
they are core domain logic -- "how do I classify a collection run's health" --
not scheduler-specific concerns, and the interactive CLI command needs the
exact same classification the scheduler already relies on. The script
re-imports these names so nothing that already depended on them breaks.

Pricing-scenario auto-triggering is deliberately *not* imported
here: this package is architecturally forbidden from depending on the
optimiser at all (see ``tests/unit/test_pricing_intelligence_boundaries.py``
-- "nothing in the intelligence layer may price"). ``run_operational_cycle``
instead accepts an optional ``scenario_trigger_fn`` the caller supplies --
only a caller *outside* this package (the scheduler script) can actually
build one, since only it may import a scenario-computation module that
touches the optimiser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Callable, Mapping, NamedTuple, Optional
import uuid

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    Alert,
    AlertSink,
    ConsoleAlertSink,
    build_telegram_sink_from_env,
    build_webhook_sink_from_env,
    dispatch_alerts_safely,
    dispatch_alerts_safely_detailed,
    format_alerts,
    plain_alert,
)
from balance_pricing_engine.pricing_intelligence.changes import RateChange, load_changes
from balance_pricing_engine.pricing_intelligence.config import (
    HealthThresholds,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import (
    CollectionOutcome,
    PricingIntelligenceService,
    _resolve_rate_category,
)
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, RunRecord


class ScenarioResult(NamedTuple):
    """What a ``ScenarioTriggerFn`` returns for one triggered term.

    Both fields are pre-computed by the caller, not by this module -- ``data``
    for :meth:`~.store.ObservationStore.write_scenario_run` (JSON-serialisable),
    ``alert_block`` for :func:`_enrich_with_scenarios` to append verbatim.
    This package must not know how to compute or render either (see the
    module docstring), only how to persist/attach them.
    """

    data: list[dict]
    alert_block: str


#: A caller-supplied hook: given the tracked/comparable terms this run's
#: changes triggered and the current market snapshot, return a
#: :class:`ScenarioResult` per term. Injected rather than imported because
#: this package must not depend on how a scenario comparison is actually
#: computed -- see the module docstring.
ScenarioTriggerFn = Callable[[list[int], MarketSnapshot], Mapping[int, ScenarioResult]]

logger = logging.getLogger(__name__)

#: Every bank collected and stored.
EXIT_OK = 0
#: No bank could be collected -- the run produced nothing.
EXIT_TOTAL_FAILURE = 1
#: Some banks collected, at least one failed. The market view is incomplete, so
#: a scheduler should surface this rather than treat it as success.
EXIT_PARTIAL_FAILURE = 2
#: Another collection is already running; this invocation did nothing.
EXIT_ALREADY_RUNNING = 3

_STATUS_BY_CODE = {
    EXIT_OK: "SUCCESS",
    EXIT_PARTIAL_FAILURE: "PARTIAL_SUCCESS",
    EXIT_TOTAL_FAILURE: "FAILED",
}


def default_lock_file_path(product_type: ProductType | str) -> str:
    """Default single-instance lock path for one product's operational cycle.

    Derived from ``product_type`` -- not one shared literal duplicated in
    the interactive CLI's ``run`` command and the scheduled workflow script
    -- so the two agree by construction on which lock file protects which
    product+database. Matches the naming the checked-in cron/launchd
    artifacts already use (``collection-home-loan.lock`` /
    ``collection-term-deposit.lock`` -- see ``deploy/scheduling/``): before
    this, an ordinary interactive ``pricing-intelligence run`` defaulted to
    an unqualified ``collection.lock`` that no deployed schedule actually
    uses, so a manual run and the scheduled job for the same product could
    acquire different locks and run concurrently against the same database.
    """
    slug = ProductType(product_type).value.replace("_", "-")
    return f"data/processed/pricing_intelligence/collection-{slug}.lock"


# ---------------------------------------------------------------------------
# Run classification (relocated from scripts/collect_market_rates_workflow.py)
# ---------------------------------------------------------------------------


def _banks_with_zero_accepted_rates(outcome: CollectionOutcome) -> set[str]:
    accepted_banks = {record.bank for record in outcome.validation.accepted}
    return {bank for bank in outcome.succeeded_banks if bank not in accepted_banks}


def _comparable_rate_count(
    outcome: CollectionOutcome, product_type: ProductType | str = ProductType.HOME_LOAN
) -> int:
    """Accepted rows in the product's default scheduled comparison category.

    Not "every accepted row that isn't ``non_comparable``": a term-deposit
    collection can validation-accept a real, non-``non_comparable`` category
    such as ``pie_term_deposit`` while missing every ordinary
    ``term_deposit`` row entirely (e.g. a parser regression that only
    matches the PIE table) -- those PIE rows are genuine data, but the
    service's default deposit snapshot filters to ``rate_category=
    "term_deposit"`` specifically (see :func:`_resolve_rate_category`), so
    they can never enter it. The same applies to home loans: a bank
    contributing only its ``standard`` tier while missing ``special``
    leaves the default comparison view empty for that bank even though
    ``rate_count`` is positive. Resolved via the same
    :func:`_resolve_rate_category` the market snapshot itself uses, so this
    cannot drift from what "usable" actually means for a given product.
    """
    target_category = _resolve_rate_category(product_type, None)
    return sum(
        1 for record in outcome.validation.accepted if record.rate_category == target_category
    )


def _banks_with_zero_comparable_rates(
    outcome: CollectionOutcome, product_type: ProductType | str = ProductType.HOME_LOAN
) -> set[str]:
    """Banks that fetched and had rows accepted, but none in the product's
    default scheduled comparison category (see :func:`_comparable_rate_count`).

    One step further down the pipeline than :func:`_banks_with_zero_accepted_rates`:
    a bank whose only accepted rows fall outside the default comparison
    category contributed nothing the scheduled market view can use, the
    same as a bank with zero accepted rows at all -- just not yet caught,
    since validation genuinely accepted the rows. Banks with zero accepted
    rows at all are excluded here; :func:`_validation_only_failures`
    already reports those.
    """
    zero_accepted = _banks_with_zero_accepted_rates(outcome)
    target_category = _resolve_rate_category(product_type, None)
    comparable_banks = {
        record.bank
        for record in outcome.validation.accepted
        if record.rate_category == target_category
    }
    return {
        bank
        for bank in outcome.succeeded_banks
        if bank not in zero_accepted and bank not in comparable_banks
    }


def _validation_only_failures(outcome: CollectionOutcome) -> list[dict]:
    """Banks that fetched successfully but contributed zero accepted rows.

    A bank can fetch and parse cleanly (so it never reaches report.failed)
    while every one of its rows still gets rejected by validation -- e.g. a
    bank-scoped parser regression. Without this, such a bank stays listed as
    succeeded everywhere a scheduler or summary consumer looks, even though
    nothing usable was actually stored for it.
    """
    failures = []
    for bank in sorted(_banks_with_zero_accepted_rates(outcome)):
        codes = sorted(
            {
                issue.code
                for issue in outcome.validation.issues
                if issue.bank == bank and issue.severity == "error"
            }
        )
        failures.append(
            {
                "bank": bank,
                "error_type": "validation_rejected",
                "error": (
                    "every row was rejected by validation ("
                    f"{', '.join(codes) if codes else 'no accepted rows'})"
                ),
            }
        )
    return failures


def _zero_comparable_rate_failures(
    outcome: CollectionOutcome, product_type: ProductType | str = ProductType.HOME_LOAN
) -> list[dict]:
    """Banks with no accepted row in the product's default comparison category.

    See :func:`_banks_with_zero_comparable_rates` -- a bank in this state
    reads as succeeded everywhere else (it fetched, and validation genuinely
    accepted its rows), but contributed nothing a market snapshot or pricing
    assumption can use.
    """
    target_category = _resolve_rate_category(product_type, None)
    return [
        {
            "bank": bank,
            "error_type": "no_comparable_rates",
            "error": f"no accepted row in the default comparison category ({target_category})",
        }
        for bank in sorted(_banks_with_zero_comparable_rates(outcome, product_type))
    ]


def _low_coverage_failures(outcome: CollectionOutcome) -> list[dict]:
    """Banks that matched too few of their previously known rate lines.

    A page-layout regression can leave one row parseable while silently
    dropping the rest of a bank's table: the fetch succeeds, at least one row
    is accepted, and nothing is rejected by validation either (the missing
    rows were never parsed at all, so there is nothing to reject) -- so
    neither report.failed nor validation.rejected catches it. The service
    already computes this via its withdrawal-suppression coverage check
    (CollectionOutcome.low_coverage_banks); this surfaces the same signal
    here. Banks with zero accepted rows are reported by
    _validation_only_failures() instead, so they are excluded to avoid a
    duplicate entry.
    """
    zero_accepted = _banks_with_zero_accepted_rates(outcome)
    return [
        {
            "bank": bank,
            "error_type": "low_coverage",
            "error": (
                "matched fewer of this bank's known rate lines than the "
                "configured coverage threshold"
            ),
        }
        for bank in outcome.low_coverage_banks
        if bank not in zero_accepted
    ]


def _partial_rejection_failures(outcome: CollectionOutcome, already_flagged: set[str]) -> list[dict]:
    """Banks with at least one rejected row, not already flagged more specifically.

    exit_code_for() already treats any rejected observation as a partial
    failure (a term-specific parser regression can corrupt a single row
    while the bank's other rows still collect cleanly), but a bank in that
    state has some accepted rows and adequate coverage, so neither
    _validation_only_failures() nor _low_coverage_failures() catches it --
    the summary would report rates_rejected > 0 while listing the affected
    bank as succeeded with no bank-specific entry to point at.
    """
    rejected_banks = {record.bank for record in outcome.validation.rejected}
    failures = []
    for bank in sorted(rejected_banks - already_flagged):
        codes = sorted(
            {
                issue.code
                for issue in outcome.validation.issues
                if issue.bank == bank and issue.severity == "error"
            }
        )
        rejected_count = sum(1 for record in outcome.validation.rejected if record.bank == bank)
        failures.append(
            {
                "bank": bank,
                "error_type": "validation_rejected",
                "error": (
                    f"{rejected_count} row(s) rejected by validation ("
                    f"{', '.join(codes) if codes else 'unknown reason'})"
                ),
            }
        )
    return failures


def _hard_skipped_row_failures(outcome: CollectionOutcome, already_flagged: set[str]) -> list[dict]:
    """Banks with at least one row that failed record construction outright,
    not already flagged more specifically.

    A row that fails ``CompetitorRateRecord`` construction (see
    ``competitor_rates.extractors.parse_html_rates``'s ``skipped_row_keys``)
    never reaches ``validate_observations()`` at all, so it shows up in
    neither ``report.failed`` nor ``validation.rejected`` -- previously
    ``exit_code_for()`` could return success while a market-derived snapshot
    and pricing assumption were built from an incomplete bank range. On a
    bootstrap run there is not even a stored predecessor for the skipped row
    to fall back on, so this is the only place the gap is visible at all.
    """
    failures = []
    for result in outcome.report.results:
        if result.bank in already_flagged or not result.skipped_row_keys:
            continue
        failures.append(
            {
                "bank": result.bank,
                "error_type": "row_skipped",
                "error": (
                    f"{len(result.skipped_row_keys)} row(s) failed to parse into a "
                    "record and were skipped"
                ),
            }
        )
    return failures


def _bank_scoped_failures(
    outcome: CollectionOutcome, product_type: ProductType | str = ProductType.HOME_LOAN
) -> list[dict]:
    """Every bank-scoped failure not already visible in report.failed.

    Checked in order of specificity so a bank is reported once, under its
    most informative category: zero accepted rows, then zero comparable
    rows, then coverage below threshold, then any remaining bank with at
    least one rejected row, then any remaining bank with at least one
    hard-skipped row.
    """
    zero_accepted_failures = _validation_only_failures(outcome)
    zero_comparable_failures = _zero_comparable_rate_failures(outcome, product_type)
    low_coverage_failures = _low_coverage_failures(outcome)
    already_flagged = (
        {failure["bank"] for failure in zero_accepted_failures}
        | {failure["bank"] for failure in zero_comparable_failures}
        | {failure["bank"] for failure in low_coverage_failures}
    )
    partial_rejection_failures = _partial_rejection_failures(outcome, already_flagged)
    already_flagged |= {failure["bank"] for failure in partial_rejection_failures}
    hard_skipped_row_failures = _hard_skipped_row_failures(outcome, already_flagged)
    return (
        zero_accepted_failures
        + zero_comparable_failures
        + low_coverage_failures
        + partial_rejection_failures
        + hard_skipped_row_failures
    )


def build_summary(
    outcome: CollectionOutcome,
    product_type: ProductType | str = ProductType.HOME_LOAN,
) -> dict:
    """Machine-readable summary of one collection run.

    ``product_type`` is embedded so a summary file can be told apart from
    the other product's when both are scheduled -- see ``--product`` and
    ``run_collection()``. Defaults to ``HOME_LOAN`` to match this module's
    other single-product defaults; every existing caller collecting the
    default product keeps working unchanged.
    """
    bank_scoped_failures = _bank_scoped_failures(outcome, product_type)
    bank_scoped_failed_banks = {failure["bank"] for failure in bank_scoped_failures}
    return {
        "product_type": ProductType(product_type).value,
        "collected_at_utc": outcome.report.captured_at_utc.isoformat(),
        "banks_succeeded": [
            bank for bank in outcome.succeeded_banks if bank not in bank_scoped_failed_banks
        ],
        "banks_failed": list(outcome.failed_banks) + sorted(bank_scoped_failed_banks),
        "rates_accepted": outcome.rate_count,
        "rates_rejected": len(outcome.validation.rejected),
        "rows_skipped": len(outcome.report.skipped_row_keys),
        "validation_errors": len(outcome.validation.errors),
        "validation_warnings": len(outcome.validation.warnings),
        "new_states_stored": (
            outcome.write_result.inserted if outcome.write_result else 0
        ),
        "unchanged_states": (
            outcome.write_result.unchanged if outcome.write_result else 0
        ),
        "changes_detected": len(outcome.changes),
        "suspicious_changes": sum(1 for change in outcome.changes if change.suspicious),
        "changes": [change.describe() for change in outcome.changes],
        "failures": [
            {"bank": result.bank, "error_type": result.error_type, "error": result.error}
            for result in outcome.report.failed
        ]
        + bank_scoped_failures,
    }


def exit_code_for(
    outcome: CollectionOutcome, product_type: ProductType | str = ProductType.HOME_LOAN
) -> int:
    # A run where every bank failed to fetch is a failure, and so is one where
    # pages parsed but validation rejected every record -- either way nothing
    # usable was stored, and a scheduler must not treat that as success. Zero
    # rows in the product's default *comparison category* is the same failure
    # one step further down the pipeline: a run whose only accepted rows fall
    # outside that category (non_comparable audit-only rows, or a real but
    # off-default category like pie_term_deposit) passes rate_count > 0 while
    # the market snapshot it can actually build is empty -- see
    # _comparable_rate_count()'s docstring.
    if (
        not outcome.report.any_success
        or outcome.rate_count == 0
        or _comparable_rate_count(outcome, product_type) == 0
    ):
        return EXIT_TOTAL_FAILURE
    # Any rejected observation -- not just a bank where every row was
    # rejected -- means the collection is incomplete: a term-specific parser
    # regression can silently drop one product from an otherwise-healthy
    # bank. A low-coverage bank catches the case validation can't: rows that
    # were never parsed at all (not rejected -- simply missing) because a
    # page-layout regression silently dropped most of a bank's table. A bank
    # whose accepted rows fall entirely outside the default comparison
    # category is the same gap as a zero-accepted bank, just caught one step
    # later in the pipeline (see _banks_with_zero_comparable_rates()) --
    # most relevant on a bootstrap run, where there is no prior baseline for
    # low_coverage_banks to compare against at all. A hard-skipped row
    # (failed CompetitorRateRecord construction outright, before
    # validate_observations() ever saw it) is the same kind of gap as a
    # rejected observation, just one step earlier in the pipeline, and must
    # not let a run with an incomplete bank range report success. Warnings
    # (e.g. a suspicious but plausible movement) stay non-fatal.
    if (
        outcome.report.failed
        or outcome.validation.rejected
        or outcome.low_coverage_banks
        or outcome.report.skipped_row_keys
        or _banks_with_zero_comparable_rates(outcome, product_type)
    ):
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


# ---------------------------------------------------------------------------
# Collector health escalation
# ---------------------------------------------------------------------------

#: Escalation outcomes classify_health_escalation() can return.
HEALTH_NONE = "none"
HEALTH_WARNING = "warning"
HEALTH_ALERT = "alert"
HEALTH_RECOVERY = "recovery"


def _alert_delivered_during_current_streak(previous: CollectorHealth) -> bool:
    """True when ``previous.last_alert_sent_at`` falls inside the failure
    streak that just ended, not a stale value from an earlier one.

    ``update_collector_health()`` never clears ``last_alert_sent_at`` -- it
    only ever moves forward, whenever :meth:`record_collector_health_alert`
    confirms a delivery (see that method's docstring) -- so on its own it
    cannot distinguish "delivered during this streak" from "delivered once,
    long ago, during some earlier streak that already recovered". Comparing
    it against ``last_success_at`` (the boundary the current streak began
    right after) resolves that: no success recorded yet means the streak
    covers this bank's entire history, so any recorded alert counts.
    """
    if previous.last_alert_sent_at is None:
        return False
    if previous.last_success_at is None:
        return True
    return previous.last_alert_sent_at >= previous.last_success_at


def classify_health_escalation(
    thresholds: HealthThresholds, previous: Optional[CollectorHealth], current: CollectorHealth
) -> str:
    """Decide what, if anything, a collector's new health state warrants.

    Policy (configurable via ``thresholds``): a 1st failure logs only; the
    ``warning_after_failures``'th is a structured warning log (no Telegram
    send); the ``alert_after_failures``'th and beyond sends a Telegram health
    alert; a success that follows a streak which had already reached
    ``alert_after_failures`` *and* whose escalation alert was actually
    confirmed delivered (see :func:`_alert_delivered_during_current_streak`)
    is a recovery, alerted the same way. A success that recovers from a
    shorter streak, or from one long enough but whose alert never reached
    any channel (no sinks configured, or every delivery failed), is
    ``"none"`` -- nothing was ever sent to recover *from*, so a recovery
    notice would reach a channel that never saw the original incident.
    """
    if current.consecutive_failures == 0:
        previous_streak = previous.consecutive_failures if previous is not None else 0
        if previous_streak >= thresholds.alert_after_failures and previous is not None:
            if _alert_delivered_during_current_streak(previous):
                return HEALTH_RECOVERY
        return HEALTH_NONE
    if current.consecutive_failures >= thresholds.alert_after_failures:
        return HEALTH_ALERT
    if current.consecutive_failures >= thresholds.warning_after_failures:
        return HEALTH_WARNING
    return HEALTH_NONE


def _health_alert(bank: str, health: CollectorHealth) -> Alert:
    title = "⚠️ PRICING INTELLIGENCE COLLECTOR ISSUE"
    last_success = (
        health.last_success_at.strftime("%d %b %Y %H:%M") if health.last_success_at else "never"
    )
    body = "\n".join(
        [
            f"{bank} collector failed {health.consecutive_failures} consecutive runs.",
            "",
            f"Last successful collection: {last_success}",
            f"Error: {health.last_error or 'unknown'}",
        ]
    )
    return plain_alert(title, body)


def _recovery_alert(bank: str, health: CollectorHealth) -> Alert:
    title = f"✅ {bank} collector recovered"
    body = "Rates collected successfully after a prior failure streak."
    return plain_alert(title, body)


# ---------------------------------------------------------------------------
# Alert dispatch with per-channel dedup
# ---------------------------------------------------------------------------


def _dispatch_rendered_alerts(
    store, alerts: list[Alert], sinks: Mapping[str, AlertSink]
) -> int:
    """Dispatch already-rendered rate-move alerts once per configured
    channel, skipping any change already delivered on that channel (see
    ``store.notified_alerts``). Rendered (not raw changes) so scenario
    enrichment (see ``_enrich_with_scenarios``) happens once, before dedup
    filtering, rather than once per channel.

    Only alerts individually confirmed delivered are recorded as notified --
    a send that failed or timed out must remain eligible for retry on the
    next run, not be marked as if it succeeded (see
    :func:`~.alerts.dispatch_alerts_safely_detailed`)."""
    total_sent = 0
    for channel, sink in sinks.items():
        unnotified = [alert for alert in alerts if not store.was_notified(alert.change, channel)]
        if not unnotified:
            continue
        delivered = dispatch_alerts_safely_detailed(unnotified, sink)
        total_sent += len(delivered)
        for alert in delivered:
            store.record_notification(alert.change, channel)
    return total_sent


#: How far back to look for a stored rate move that was never delivered on
#: every currently configured alert channel -- most often because the
#: process exited after service.collect() committed the change but before
#: this module reached alert dispatch on that same run: the *next* scheduled
#: invocation's own collect() then sees the already-persisted rate as
#: unchanged, so the change would otherwise never be detected -- and
#: therefore never alerted -- again. Bounded so a change nobody has ever
#: configured a channel for (or one from a long-abandoned collection) does
#: not resurface forever.
UNDELIVERED_CHANGE_REPLAY_HOURS = 48.0


def _undelivered_change_replay_candidates(
    store,
    product_type: ProductType,
    sinks: Mapping[str, AlertSink],
    already_included: set[tuple],
    now: datetime,
) -> list[RateChange]:
    """Stored rate moves from a recent prior run that still have not reached
    every currently configured channel.

    ``was_notified()`` is checked per channel, the same dedup
    :func:`_dispatch_rendered_alerts` already applies to a live batch, so a
    change already delivered everywhere it needed to be is never re-sent --
    this only replays a change still missing on at least one channel.

    Includes suspicious changes: the live path (``rate_moves`` above) already
    delivers a suspicious move, rendered with its explicit "FLAGGED
    SUSPICIOUS -- verify against the source page" warning (see
    :func:`~.alerts.format_alert`) rather than dropping it -- excluding
    suspicious changes here would silently lose exactly the alert a parser
    fault most needs a human to see, on top of losing it to a channel outage.
    """
    since = now - timedelta(hours=UNDELIVERED_CHANGE_REPLAY_HOURS)
    rows = store.get_changes_since(since=since, now=now, product_type=product_type)
    candidates = []
    for change in load_changes(rows):
        if not change.is_rate_move:
            continue
        key = (change.observation_key, change.change_type, change.detected_at, change.run_id)
        if key in already_included:
            continue
        if all(store.was_notified(change, channel) for channel in sinks):
            continue
        candidates.append(change)
    return candidates


def _scenario_trigger_candidates(
    changes: list[RateChange], product_type: ProductType, config: PricingIntelligenceConfig
) -> list[int]:
    """Tracked, comparable-category terms with a genuine (non-suspicious)
    rate move this run -- passed to ``scenario_trigger_fn`` (see
    :data:`ScenarioTriggerFn`).

    Any bank's move counts, not just ``our_bank`` -- this triggers on a
    *competitor* reprice, which is exactly what a market-wide collection
    detects regardless of which bank moved.
    """
    target_category = _resolve_rate_category(product_type, None)
    terms = {
        change.term_months
        for change in changes
        if change.is_rate_move
        and not change.suspicious
        and change.rate_category == target_category
        and change.term_months in config.tracked_terms_months
    }
    return sorted(terms)


def _enrich_with_scenarios(
    alerts: list[Alert], scenarios_by_term: Mapping[int, "ScenarioResult"]
) -> list[Alert]:
    """Append each triggered term's pre-formatted alert block to
    the alert whose change landed on it.

    Takes the already-rendered ``ScenarioResult.alert_block`` rather than
    formatting the comparison itself -- this module must not know how to
    render one (see the module docstring), only that
    ``scenario_trigger_fn``'s caller already did.
    """
    from dataclasses import replace

    enriched = []
    for alert in alerts:
        comparison = (
            scenarios_by_term.get(alert.change.term_months) if alert.change is not None else None
        )
        if comparison is None:
            enriched.append(alert)
            continue
        enriched.append(replace(alert, body=alert.body + "\n\n" + comparison.alert_block))
    return enriched


def _dispatch_health_alerts(
    store,
    product_type: ProductType,
    banks: list[str],
    thresholds: HealthThresholds,
    sinks: Mapping[str, AlertSink],
    at: datetime,
    run_id: Optional[str] = None,
) -> int:
    """Update every attempted bank's health streak and deliver an
    escalation/recovery alert where the policy calls for one.

    Escalation/recovery alerts are batched into one ``dispatch_alerts_safely``
    call per sink, not one call per bank: a per-bank call would give each
    bank its own fresh ``DEFAULT_ALERT_DISPATCH_DEADLINE_SECONDS`` budget, so
    several banks escalating against one hung sink could serialise into
    banks * deadline seconds of blocking while this run's single-instance
    lock is still held -- one shared deadline per sink bounds that to a
    single deadline regardless of how many banks escalated.

    ``run_id``, when given, is threaded through to
    :meth:`~.store.ObservationStore.update_collector_health` so a retried
    ``run_operational_cycle()`` invocation under the same ``run_id`` does not
    advance a bank's failure streak a second time for what is logically one
    collection attempt -- see that method's own docstring.

    Returns how many health alerts were confirmed delivered, summed across
    every configured sink -- the same per-sink counting convention
    :func:`_dispatch_rendered_alerts` uses for its own return value -- so a
    caller folding this into a run's total ``alerts_sent`` is not missing a
    health notice that reached a channel purely because no rate move
    happened this run.
    """
    previous_by_bank = {
        health.bank: health for health in store.get_collector_health(product_type)
    }
    escalations: list[tuple[str, str, Alert]] = []
    for bank, success, error in banks:
        current = store.update_collector_health(
            bank, product_type, success=success, error=error, at=at, run_id=run_id
        )
        outcome = classify_health_escalation(thresholds, previous_by_bank.get(bank), current)
        if outcome == HEALTH_NONE:
            continue
        if outcome == HEALTH_WARNING:
            logger.warning(
                "collector health: %s has failed %d consecutive runs",
                bank,
                current.consecutive_failures,
            )
            continue
        alert = (
            _recovery_alert(bank, current)
            if outcome == HEALTH_RECOVERY
            else _health_alert(bank, current)
        )
        escalations.append((bank, outcome, alert))

    if not escalations:
        return 0

    # Recorded only for a bank whose escalation was individually confirmed
    # delivered to at least one sink -- the same "confirmed, not attempted"
    # standard _dispatch_rendered_alerts already applies to rate-move
    # alerts (see dispatch_alerts_safely_detailed). An unconditional record
    # here would mark an alert as sent even when every sink failed, timed
    # out, or none was configured at all, leaving the persisted
    # last_alert_sent_at audit inaccurate.
    alerts = [alert for _, _, alert in escalations]
    alert_to_bank = {alert: bank for bank, _, alert in escalations}
    delivered_banks: set[str] = set()
    total_sent = 0
    for sink in sinks.values():
        delivered = dispatch_alerts_safely_detailed(alerts, sink)
        total_sent += len(delivered)
        for alert in delivered:
            delivered_banks.add(alert_to_bank[alert])
    for bank, outcome, _ in escalations:
        # Only an ALERT delivery counts as "incident delivered" here -- see
        # classify_health_escalation()'s _alert_delivered_during_current_streak
        # helper, which reads this same field to decide whether a later
        # recovery is warranted. Recording a RECOVERY send here too would let
        # that recovery's own timestamp (always >= its own last_success_at,
        # since both are stamped from the same successful run) satisfy that
        # comparison for a *later* streak that never actually got its own
        # incident alert delivered -- a stale recovery send masquerading as
        # this streak's incident delivery.
        if bank in delivered_banks and outcome == HEALTH_ALERT:
            store.record_collector_health_alert(bank, product_type, at=at)
    return total_sent


# ---------------------------------------------------------------------------
# The operational cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationalRunResult:
    """Everything one ``run_operational_cycle()`` call produced."""

    outcome: CollectionOutcome
    run_record: Optional[RunRecord]
    alerts_sent: int
    code: int

    @property
    def status(self) -> str:
        return _STATUS_BY_CODE[self.code]


def run_operational_cycle(
    service: PricingIntelligenceService,
    *,
    product_type: ProductType | str = ProductType.HOME_LOAN,
    captured_at_utc: Optional[datetime] = None,
    run_id: Optional[str] = None,
    dry_run: bool = False,
    alert_sinks: Optional[Mapping[str, AlertSink]] = None,
    webhook_text_field: str = "text",
    scenario_trigger_fn: Optional[ScenarioTriggerFn] = None,
) -> OperationalRunResult:
    """Run one full collect -> validate -> persist -> detect -> alert cycle.

    ``alert_sinks`` maps a channel name (``"webhook"``, ``"telegram"``, ...)
    to the :class:`~.alerts.AlertSink` to deliver on; omit to build the
    default set from the environment (webhook + Telegram, whichever are
    configured -- see :mod:`.alerts`). Pass an explicit ``{}`` to disable
    alert delivery entirely. ``webhook_text_field`` is only used when
    building that default webhook sink (ignored when ``alert_sinks`` is
    supplied directly) -- ``"text"`` (the default) is read by Slack/Teams,
    ``"content"`` by Discord.

    ``scenario_trigger_fn`` enables auto-triggered pricing scenarios: when
    supplied, it is called once with every tracked/comparable term that had a
    genuine (non-suspicious) rate move this run, plus the current market
    snapshot, and must return a comparison object per term. Each is persisted
    (:meth:`~.store.ObservationStore.write_scenario_run`,
    skipped on a dry run) and appended to the corresponding rate-move
    alert(s) for that term. Omitted (the default) simply skips scenario-
    triggering. See :data:`ScenarioTriggerFn` and the module docstring for
    why this is injected rather than imported.

    A ``scenario_trigger_fn`` failure (e.g. no external competitor peer
    remains once ``our_bank`` is excluded from the only bank that moved this
    run -- a documented, reachable case, not a bug in the callback) is caught
    and logged, not propagated: this decision-support enrichment must never
    cost the collection its alerts, collector-health update, run record, or
    summary, all of which are otherwise still valid off a successful
    ``service.collect()``.

    ``dry_run`` collects and classifies as normal but does not persist
    observations or raw HTML snapshots (``service.collect(persist=False,
    persist_snapshot=False)``), does not write a :class:`~.store.RunRecord`
    or update collector health, and does not deliver real alerts --
    ``alert_sinks`` is ignored and every alert is routed to a
    :class:`~.alerts.ConsoleAlertSink` instead, so a dry run is safe to run
    against a real Telegram/webhook configuration and leaves no artifacts
    behind from what should be a non-mutating preview.

    Raises :class:`~.store.ChronologicalOrderError` unchanged (a backwards
    wall clock) -- callers wanting a classified failure for that case should
    catch it themselves, the same way ``scripts/collect_market_rates_workflow.py``
    already does.
    """
    product_type = ProductType(product_type)
    started_at = datetime.now(timezone.utc)
    operational_run_id = run_id or uuid.uuid4().hex

    outcome = service.collect(
        captured_at_utc=captured_at_utc,
        persist=not dry_run,
        persist_snapshot=not dry_run,
        product_type=product_type,
        run_id=operational_run_id,
    )

    code = exit_code_for(outcome, product_type)
    summary = build_summary(outcome, product_type)
    banks_attempted = sorted({result.bank for result in outcome.report.results})

    if dry_run:
        sinks: Mapping[str, AlertSink] = {"console": ConsoleAlertSink()}
    elif alert_sinks is not None:
        sinks = alert_sinks
    else:
        built: dict[str, AlertSink] = {}
        webhook = build_webhook_sink_from_env(text_field=webhook_text_field)
        if webhook is not None:
            built["webhook"] = webhook
        telegram = build_telegram_sink_from_env()
        if telegram is not None:
            built["telegram"] = telegram
        sinks = built

    rate_moves = [change for change in outcome.changes if change.is_rate_move]
    replayed_moves: list[RateChange] = []
    if not dry_run and sinks:
        # Re-offer any recent, still-undelivered stored change for delivery
        # this run -- see UNDELIVERED_CHANGE_REPLAY_HOURS. A dry run must not
        # participate: it never writes to the notified_alerts ledger, so its
        # ad-hoc "console" channel would never look delivered and every
        # candidate would resurface on every dry-run preview indefinitely.
        #
        # Anchored on captured_at_utc (the same business-time clock
        # detected_at is stamped from -- see service.collect()), not
        # started_at's wall clock: a caller backfilling or replaying a past
        # captured_at_utc must get a lookback window relative to that
        # simulated "now", not the real time this process happens to run at.
        replay_now = captured_at_utc or started_at
        if replay_now.tzinfo is None:
            replay_now = replay_now.replace(tzinfo=timezone.utc)
        already_included = {
            (change.observation_key, change.change_type, change.detected_at, change.run_id)
            for change in rate_moves
        }
        # Kept separate from rate_moves, not merged: a replayed change is
        # rendered without this run's market snapshots (see below) -- it
        # must not be rendered as if this run's fresh snapshot described the
        # old change's original detection.
        replayed_moves = _undelivered_change_replay_candidates(
            service.store, product_type, sinks, already_included, replay_now
        )

    scenarios_by_term: dict[int, ScenarioResult] = {}
    triggered_terms = (
        _scenario_trigger_candidates(outcome.changes, product_type, service.config)
        if scenario_trigger_fn is not None
        else []
    )
    if triggered_terms:
        scenario_snapshot = service.get_market_snapshot(product_type=product_type)
        try:
            scenarios_by_term = dict(scenario_trigger_fn(triggered_terms, scenario_snapshot))
        except Exception:  # noqa: BLE001 - decision-support only, must not abort the cycle
            logger.warning(
                "scenario_trigger_fn failed for run %s (terms=%s); continuing the "
                "operational cycle without scenario enrichment this run",
                operational_run_id,
                triggered_terms,
                exc_info=True,
            )
            scenarios_by_term = {}
        if not dry_run:
            for term, comparison in scenarios_by_term.items():
                service.store.write_scenario_run(
                    operational_run_id,
                    product_type,
                    term,
                    comparison.data,
                    generated_at=started_at,
                )

    alerts_sent = 0
    if sinks and (rate_moves or replayed_moves):
        snapshot = service.get_market_snapshot(product_type=product_type)
        previous_snapshot = service.get_previous_market_snapshot(product_type=product_type)
        rendered = format_alerts(rate_moves, snapshot, previous_snapshot)
        if scenarios_by_term:
            # Enriched before the replay batch is appended below: a
            # scenario comparison was computed from *this run's* current
            # snapshot (see scenario_snapshot above), so it must only ever
            # land on a live alert -- attaching it to a replayed alert from
            # an old change would attribute today's model comparison to
            # yesterday's event, the same current-run-context leak the
            # snapshot omission below exists to avoid.
            rendered = _enrich_with_scenarios(rendered, scenarios_by_term)
        if replayed_moves:
            # No snapshot context for a replayed change: this run's fresh
            # snapshot/previous_snapshot describe the newest run and its
            # immediate predecessor, not the two market states surrounding
            # the old change being replayed. Passing them here would let a
            # change missed yesterday and replayed after today's unchanged
            # collection report its rank as unchanged, while intervening
            # moves get attributed to the stale event instead --
            # format_alert() already omits rank/market context cleanly when
            # both snapshots are None.
            rendered = rendered + format_alerts(replayed_moves, None, None)
        if dry_run:
            # No dedup ledger writes on a dry run -- record_notification()
            # would otherwise let a later real run believe these changes
            # were already delivered.
            for sink in sinks.values():
                alerts_sent += dispatch_alerts_safely(rendered, sink)
        else:
            alerts_sent = _dispatch_rendered_alerts(service.store, rendered, sinks)

    error_summary = None
    if code != EXIT_OK:
        error_summary = "; ".join(
            f"{failure['bank']}: {failure['error']}" for failure in summary["failures"]
        ) or None

    run_record: Optional[RunRecord] = None
    if not dry_run:
        # Always classified, sinks or not: classify_health_escalation()'s
        # warning-level escalation (a structured log line, not a delivery) is
        # unconditional policy -- it must fire at warning_after_failures even
        # with no webhook/Telegram configured, the documented default. Only
        # the alert-level outcome's `for sink in sinks.values(): ...` loop
        # depends on sinks being non-empty, and naturally no-ops when it is.
        # Folded into alerts_sent (not discarded): a collector reaching the
        # alert threshold with no rate movement this run still delivers a
        # real notice, and the run's reported alert count must not read zero
        # when a sink actually received one.
        alerts_sent += _dispatch_health_alerts(
            service.store,
            product_type,
            [
                (
                    bank,
                    bank in summary["banks_succeeded"],
                    next(
                        (
                            failure["error"]
                            for failure in summary["failures"]
                            if failure["bank"] == bank
                        ),
                        None,
                    ),
                )
                for bank in banks_attempted
            ],
            service.config.health,
            sinks,
            started_at,
            run_id=operational_run_id,
        )

        market_state_value = None
        try:
            market_state_value = service.get_market_state(product_type=product_type).state.value
        except Exception:  # noqa: BLE001 - market state is best-effort context on the run record
            logger.warning("could not classify market state for run %s", operational_run_id)

        run_record = RunRecord(
            run_id=operational_run_id,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            status=_STATUS_BY_CODE[code],
            product_type=product_type,
            banks_attempted=tuple(banks_attempted),
            banks_successful=tuple(summary["banks_succeeded"]),
            banks_failed=tuple(summary["banks_failed"]),
            rates_observed=summary["rates_accepted"],
            rates_changed=summary["changes_detected"],
            validation_failures=summary["rates_rejected"],
            suspicious_observations=summary["suspicious_changes"],
            market_state=market_state_value,
            error_summary=error_summary,
            collection_run_id=outcome.write_result.run_id if outcome.write_result else None,
        )
        service.store.write_run_record(run_record)

    return OperationalRunResult(
        outcome=outcome, run_record=run_record, alerts_sent=alerts_sent, code=code
    )
