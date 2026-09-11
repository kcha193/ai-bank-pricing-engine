"""Unattended NZ market-rate collection, for a scheduler to invoke.

Wraps :class:`PricingIntelligenceService` with the things a cron/launchd job
needs and an interactive CLI session does not:

* a single-instance lock, so an overrunning collection cannot interleave with
  the next scheduled one and write two runs into the store at once
* structured JSON logging, reusing ``observability.logging``
* exit codes a scheduler can alert on (see EXIT_* below)
* a machine-readable summary written to disk for the next job in a pipeline
* alert delivery: detected rate moves are pushed to a webhook and/or Telegram
  when configured (see ``pricing_intelligence.alerts``). Unset by default,
  and a broken channel never fails the run.
* collector health tracking and escalation, and a persisted
  ``RunRecord`` -- both via ``pricing_intelligence.orchestration.
  run_operational_cycle``, the same function the interactive
  ``pricing_intelligence run`` CLI command uses, so scheduled and manual
  runs classify and alert identically.

Collection history cannot be backfilled -- a day not collected is gone -- so
this is written to fail loudly rather than exit quietly on a partial run.

    python3 scripts/collect_market_rates_workflow.py --render-javascript

See docs/pricing_intelligence.md for scheduling recipes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Optional

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.config import load_config
from balance_pricing_engine.observability.logging import configure_json_logging, log_event
from balance_pricing_engine.pricing_intelligence.config import (
    PricingIntelligenceConfig,
    resolve_database_path_from_env,
)
from balance_pricing_engine.pricing_intelligence.locking import (
    AlreadyRunningError,
    SingleInstanceLock as _SingleInstanceLock,
)
# Several of these (EXIT_OK, the three underscored helpers, exit_code_for)
# are not called directly in this module any more -- they are re-imported
# purely so this module's own namespace still carries them, for any test or
# tool written against the pre-relocation module (see orchestration.py's
# docstring for why they moved).
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,  # noqa: F401
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    _banks_with_zero_comparable_rates,  # noqa: F401
    _comparable_rate_count,  # noqa: F401
    _validation_only_failures,  # noqa: F401
    build_summary,
    default_lock_file_path,
    exit_code_for,  # noqa: F401
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import (
    ChronologicalOrderError,
    ObservationStore,
)

# Under the `balance_pricing_engine` hierarchy so configure_json_logging()'s
# handler actually applies; a bare "pricing_intelligence.*" name falls
# outside that hierarchy and silently drops INFO events while WARNING/ERROR
# fall through to logging's unformatted last-resort handler instead of JSON.
logger = logging.getLogger("balance_pricing_engine.pricing_intelligence.scheduled_collection")


def _clock_regression_failure(
    error: ChronologicalOrderError,
    product_type: ProductType,
    summary_path: Optional[Path],
) -> tuple[int, dict]:
    """Report a backwards clock as a total failure, with a written summary.

    Carries every key :func:`build_summary` produces so a consumer reading a
    summary file (the collector canary, launchd alerting) does not have to
    special-case this shape -- an empty run, one synthetic failure entry, and
    the guard's own message as the reason.
    """
    summary = {
        "product_type": product_type.value,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "banks_succeeded": [],
        # No bank was reached, so this cannot be derived from a report --
        # but alerting that keys off banks_failed still needs something to
        # fire on, and an empty list would read as "nothing went wrong".
        "banks_failed": ["*"],
        "rates_accepted": 0,
        "rates_rejected": 0,
        "rows_skipped": 0,
        "validation_errors": 0,
        "validation_warnings": 0,
        "new_states_stored": 0,
        "unchanged_states": 0,
        "changes_detected": 0,
        "suspicious_changes": 0,
        "changes": [],
        "alerts_sent": 0,
        "failures": [
            {
                "bank": None,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        ],
    }
    log_event(
        logger,
        "market collection aborted",
        level=logging.ERROR,
        exit_code=EXIT_TOTAL_FAILURE,
        reason=str(error),
        product_type=product_type.value,
    )
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return EXIT_TOTAL_FAILURE, summary


def run_collection(
    config: PricingIntelligenceConfig,
    render_javascript: bool,
    summary_path: Optional[Path] = None,
    product_type: ProductType | str = ProductType.HOME_LOAN,
    webhook_text_field: str = "text",
    pricing_config: Optional[object] = None,
) -> tuple[int, dict]:
    """Run one collection and return ``(exit_code, summary)``.

    ``product_type`` defaults to ``HOME_LOAN``, matching ``service.collect()``'s
    own default, so an existing scheduled deployment collects exactly what it
    always has. Collecting term deposits too needs a second scheduled
    invocation with ``--product term_deposit`` (and its own ``--lock-file``/
    ``--summary-file``, so it does not collide with the home-loan run) -- see
    ``docs/pricing_intelligence.md``.

    Detected rate moves are pushed to a webhook and/or Telegram when
    configured (see ``pricing_intelligence.alerts``); ``webhook_text_field``
    ("text" for Slack/Teams, "content" for Discord) only affects the webhook
    payload's message field.

    ``pricing_config`` (the full ``EngineConfig`` loaded from ``--config``,
    ``None`` when this ran on ``--database`` alone) enables auto-triggered
    pricing scenarios: every tracked/comparable term with a genuine rate move
    this run gets a scenario comparison built via
    ``pricing_scenario_trigger.run_triggered_scenarios`` -- imported here,
    not inside ``pricing_intelligence``, which is architecturally forbidden
    from depending on the optimiser (see
    ``tests/unit/test_pricing_intelligence_boundaries.py``). ``None`` (the
    default, matching a ``--database``-only invocation with no economic
    assumptions loaded) simply skips scenario-triggering.
    """
    product_type = ProductType(product_type)
    scenario_trigger_fn = None
    if pricing_config is not None:
        from balance_pricing_engine.pricing_intelligence.orchestration import ScenarioResult
        from balance_pricing_engine.pricing_scenario_trigger import (
            format_scenario_block,
            run_triggered_scenarios,
        )

        def scenario_trigger_fn(terms, snapshot):
            # Isolated per term: orchestration.run_operational_cycle() already
            # catches a failure from this whole callback so one can't abort
            # collection (see its own docstring), but that all-or-nothing
            # catch still discards every other term's already-computed
            # comparison if only one term raises -- e.g. a single term has no
            # external competitor peer left once our_bank is excluded, a
            # documented reachable case, not a bug in the others. A per-term
            # try/except here means the unaffected terms are still persisted
            # and attached to their own alerts.
            results = {}
            for term in terms:
                try:
                    comparison = run_triggered_scenarios(
                        pricing_config, term_months=term, snapshot=snapshot
                    )
                except Exception:  # noqa: BLE001 - decision-support only, one term must not cost the rest
                    logger.warning(
                        "scenario comparison failed for term=%s; continuing with the "
                        "remaining triggered terms",
                        term,
                        exc_info=True,
                    )
                    continue
                results[term] = ScenarioResult(
                    data=comparison.as_dicts(), alert_block=format_scenario_block(comparison)
                )
            return results

    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        render_javascript=render_javascript,
    ) as service:
        try:
            result = run_operational_cycle(
                service,
                product_type=product_type,
                webhook_text_field=webhook_text_field,
                scenario_trigger_fn=scenario_trigger_fn,
            )
        except ChronologicalOrderError as exc:
            # service.collect() stamps its own datetime.now, so this fires on
            # an ordinary scheduled run whenever the wall clock steps
            # backwards behind the last stored run. Caught here specifically
            # (not as a bare ValueError, which would swallow real bugs) so the
            # worker still writes a summary for the canary to upload and
            # returns a classified code instead of an unhandled traceback.
            return _clock_regression_failure(exc, product_type, summary_path)
        outcome = result.outcome

    summary = build_summary(outcome, product_type)
    summary["alerts_sent"] = result.alerts_sent
    code = result.code

    if code == EXIT_TOTAL_FAILURE:
        completion_level = logging.ERROR
    elif code == EXIT_PARTIAL_FAILURE:
        # In the launchd deployment this is the only structured health event,
        # so alerting that filters on warning/error severity must still see
        # an incomplete market view -- INFO would let it slip through
        # unnoticed even though exit_code_for() already treats it as a
        # failure.
        completion_level = logging.WARNING
    else:
        completion_level = logging.INFO

    log_event(
        logger,
        "market collection finished",
        level=completion_level,
        exit_code=code,
        **{
            key: summary[key]
            for key in (
                "banks_succeeded",
                "banks_failed",
                "rates_accepted",
                "new_states_stored",
                "changes_detected",
                "suspicious_changes",
                "alerts_sent",
            )
        },
    )
    for change in outcome.changes:
        log_event(
            logger,
            "market change detected",
            level=logging.WARNING if change.suspicious else logging.INFO,
            bank=change.bank,
            change_type=change.change_type.value,
            change_bps=change.change_bps,
            suspicious=change.suspicious,
        )

    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return code, summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to an EngineConfig YAML (e.g. configs/base.yaml) to load "
            "pricing_intelligence settings (selected banks, validation "
            "thresholds, collection delays, database path) from; --database "
            "still overrides just the database path"
        ),
    )
    parser.add_argument("--database", default=None, help="Observation store path")
    parser.add_argument(
        "--product",
        default=ProductType.HOME_LOAN.value,
        choices=[ProductType.HOME_LOAN.value, ProductType.TERM_DEPOSIT.value],
        help=(
            "Which market to collect (default: home_loan). Term deposits need "
            "a separate scheduled invocation with --product term_deposit and "
            "its own --lock-file/--summary-file, so it does not collide with "
            "the home-loan schedule -- see docs/pricing_intelligence.md."
        ),
    )
    parser.add_argument(
        "--render-javascript",
        action="store_true",
        help="Render pages in a headless browser. Required for live collection.",
    )
    parser.add_argument(
        "--lock-file",
        default=None,
        help=(
            "Single-instance lock path (default: derived from --product, "
            "matching the checked-in cron/launchd artifacts' per-product "
            "lock files -- see deploy/scheduling/ and "
            "docs/pricing_intelligence.md)"
        ),
    )
    parser.add_argument(
        "--summary-file", default=None, help="Write a JSON run summary here"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the text summary")
    parser.add_argument(
        "--webhook-text-field",
        default="text",
        help=(
            "JSON field the webhook payload's message goes in when "
            "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL is set. The default "
            "('text') is read directly by Slack and Teams webhooks; use "
            "'content' for Discord."
        ),
    )
    args = parser.parse_args(argv)

    configure_json_logging()

    config = resolve_database_path_from_env(PricingIntelligenceConfig())
    # Retained (not just its .pricing_intelligence block) so scheduled runs
    # can auto-trigger pricing scenarios -- see run_collection()'s
    # pricing_config docstring.
    engine_config = None
    if args.config:
        # configs/base.yaml's pricing_intelligence block would otherwise be
        # silently ignored -- scheduled production runs could then contact
        # excluded banks and validate/store observations under different
        # policy from interactive runs, per the same pattern already fixed
        # in the pricing_intelligence CLI.
        engine_config = load_config(args.config)
        if engine_config.pricing_intelligence is not None:
            config = engine_config.pricing_intelligence
    if args.database:
        config = config.model_copy(update={"database_path": args.database})

    lock_file = args.lock_file or default_lock_file_path(args.product, config.database_path)
    try:
        with _SingleInstanceLock(Path(lock_file)):
            code, summary = run_collection(
                config,
                render_javascript=args.render_javascript,
                summary_path=Path(args.summary_file) if args.summary_file else None,
                product_type=args.product,
                webhook_text_field=args.webhook_text_field,
                pricing_config=engine_config,
            )
    except AlreadyRunningError as exc:
        log_event(logger, "collection skipped", level=logging.WARNING, reason=str(exc))
        return EXIT_ALREADY_RUNNING

    if not args.quiet:
        print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
