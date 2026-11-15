"""``pricing-intelligence review ...``: the operator side of the quarantine workflow.

    review list   [--status PENDING|ACCEPTED|APPROVED|REJECTED|VOIDED|SUPERSEDED|ALL]
    review show   --event-id N
    review approve --event-id N --reason TEXT --evidence REF [--dry-run]
    review reject  --event-id N --reason TEXT [--evidence REF] [--dry-run]
    review void    --event-id N --reason TEXT [--evidence REF] [--dry-run]

``list``/``show`` and ``--dry-run`` only read. The three decisions write to the append-only
ledger and require authorisation (:mod:`.review_auth`); the authenticated identity -- not a
typed name -- is what gets recorded. See :mod:`.quarantine` for what each decision means and
docs/pricing_intelligence.md for the operator procedure.

Everything printed that originates from a bank page is sanitised: stored source text is
untrusted and must not be able to drive the operator's terminal.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Callable, Optional

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence import review_auth
from balance_pricing_engine.pricing_intelligence.quarantine import (
    ReviewAction,
    ReviewCase,
    ReviewError,
    ReviewEvent,
    ReviewState,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService

_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_MAX_EVIDENCE_PRINTED = 400
ALL_STATUSES = "ALL"


def _safe(text: object, limit: Optional[int] = None) -> str:
    cleaned = _UNSAFE.sub("?", str(text)).replace("\n", " ")
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


def add_review_parser(subparsers: argparse._SubParsersAction, positive_int: Callable) -> None:
    review = subparsers.add_parser(
        "review",
        help=(
            "Review quarantined readings: list, show, approve, reject, void. "
            "Decisions need PRICING_INTELLIGENCE_REVIEW_CREDENTIALS / _TOKEN"
        ),
    )
    commands = review.add_subparsers(dest="review_command", required=True)

    listing = commands.add_parser("list", help="List readings by review status")
    listing.add_argument(
        "--status",
        default=ReviewState.PENDING.value,
        choices=[*(state.value for state in ReviewState), ALL_STATUSES],
        help="PENDING (default) are awaiting a decision; ACCEPTED are voidable accepted moves",
    )

    show = commands.add_parser("show", help="Show one event with its evidence and history")
    show.add_argument("--event-id", type=positive_int, required=True)

    descriptions = {
        "approve": "Confirm a pending reading against independent evidence (becomes trusted)",
        "reject": "Refuse a pending reading as incorrect (never becomes trusted)",
        "void": "Withdraw a previously trusted reading (removed from current market data)",
    }
    for name, text in descriptions.items():
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--event-id", type=positive_int, required=True)
        sub.add_argument("--reason", required=True, help="Why (mandatory, recorded verbatim)")
        sub.add_argument(
            "--evidence",
            default=None,
            help="Evidence URL or immutable reference (mandatory for approve)",
        )
        sub.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and show the outcome; write nothing and do not need authorisation",
        )


def _money(rate: Optional[float]) -> str:
    return "-" if rate is None else f"{rate:.2f}"


def _term(case: ReviewCase) -> str:
    return "-" if case.term_months is None else str(case.term_months)


def _bps(case: ReviewCase) -> str:
    return "-" if case.change_bps is None else f"{case.change_bps:+.0f}"


def _age(hours: float) -> str:
    return f"{hours / 24:.1f}d" if hours >= 48 else f"{hours:.0f}h"


def _print_table(cases: list[ReviewCase]) -> None:
    header = (
        f"{'EVENT':<7}{'BANK':<10}{'PRODUCT':<14}{'TERM':<6}{'OLD':>7}{'NEW':>7}{'BPS':>6}  "
        f"{'AGE':<7}{'RUNS':<5}{'STATUS':<11}RUN"
    )
    print(header)
    for case in cases:
        print(
            f"{case.event_id:<7}{_safe(case.bank):<10}{case.product_type:<14}{_term(case):<6}"
            f"{_money(case.previous_rate_pct):>7}{_money(case.proposed_rate_pct):>7}"
            f"{_bps(case):>6}  {_age(case.age_hours):<7}{case.runs_pending:<5}"
            f"{case.status.value:<11}{_safe(case.run_id)}"
        )


def _list(service: PricingIntelligenceService, args: argparse.Namespace, product: ProductType) -> int:
    statuses = None if args.status == ALL_STATUSES else [ReviewState(args.status)]
    if args.status == ALL_STATUSES:
        statuses = list(ReviewState)
    cases = service.list_review_cases(product, statuses)
    if not cases:
        print(f"No quarantined readings with status {args.status} for {product.value}.")
        return 0
    print(f"Quarantine review queue -- {product.value} ({args.status})")
    _print_table(cases)
    if args.status == ReviewState.PENDING.value:
        print()
        print("Next: review show --event-id N, then approve / reject (see docs/pricing_intelligence.md).")
    return 0


def _history_lines(events: list[ReviewEvent]) -> list[str]:
    if not events:
        return ["History: none -- no review decisions recorded"]
    lines = ["History:"]
    for event in events:
        lines.append(
            f"  #{event.review_seq} {event.reviewed_at:%Y-%m-%d %H:%M} UTC  {event.action.value:<8}"
            f"{event.previous_state.value} -> {event.new_state.value}  by {_safe(event.reviewer)}"
        )
        lines.append(f"      reason:   {_safe(event.reason)}")
        if event.evidence_ref:
            lines.append(f"      evidence: {_safe(event.evidence_ref)}")
    return lines


def _show(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    case = service.get_review_case(args.event_id)
    print(f"Event {case.event_id}  --  {case.status.value}")
    print(f"  Bank / product:  {_safe(case.bank)} / {case.product_type} ({case.rate_category})")
    print(f"  Term:            {_term(case)} months")
    print(
        f"  Rate:            {_money(case.previous_rate_pct)}% -> {_money(case.proposed_rate_pct)}% "
        f"({_bps(case)} bps)"
    )
    print(f"  Detected:        {case.detected_at:%Y-%m-%d %H:%M} UTC in collection run {_safe(case.run_id)}")
    if case.status is ReviewState.PENDING:
        print(
            f"  Quarantined:     {case.runs_pending} run(s), {_age(case.age_hours)} -- excluded from "
            "trusted market data until decided"
        )
    print(f"  Source:          {_safe(case.source_url)}")
    print(f"  Source text:     {_safe(case.evidence_text, _MAX_EVIDENCE_PRINTED)}")
    for signal in case.signals or ("none recorded",):
        print(f"  Signal:          {_safe(signal, _MAX_EVIDENCE_PRINTED)}")
    for line in _history_lines(service.get_review_history(case.event_id)):
        print(line)
    return 0


def _decide(
    service: PricingIntelligenceService, args: argparse.Namespace, action: ReviewAction
) -> int:
    reviewer = "dry-run"
    if not args.dry_run:
        try:
            reviewer = review_auth.authenticate(os.environ, prompt=True).identity
        except review_auth.ReviewAuthError as exc:
            print(f"ERROR: {exc}")
            return 1
    try:
        outcome = service.review(
            args.event_id,
            action,
            reviewer=reviewer,
            reason=args.reason,
            evidence_ref=args.evidence,
            dry_run=args.dry_run,
        )
    except ReviewError as exc:
        print(f"ERROR: {_safe(exc)}")
        return 1
    case = outcome.case
    target = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "VOID": "VOIDED"}[action.value]
    where = f"{_safe(case.bank)} {_term(case)}m {_money(case.previous_rate_pct)}% -> {_money(case.proposed_rate_pct)}%"
    if outcome.dry_run:
        print(f"DRY RUN: event {case.event_id} ({where}) would become {target}; nothing written.")
    elif outcome.created:
        print(f"Event {case.event_id} ({where}) is now {target} -- recorded as {outcome.event.review_id} by {reviewer}.")
        if action is ReviewAction.APPROVE:
            print("Approval confirms the reading against evidence; it is not a view on the rate.")
    else:
        event = outcome.event
        print(
            f"Event {case.event_id} was already {target} (decision {event.review_id} by "
            f"{_safe(event.reviewer)}); no change made."
        )
    return 0


def run_review(
    service: PricingIntelligenceService, args: argparse.Namespace, product: ProductType
) -> int:
    command = args.review_command
    try:
        if command == "list":
            return _list(service, args, product)
        if command == "show":
            return _show(service, args)
        return _decide(service, args, ReviewAction[command.upper()])
    except ReviewError as exc:
        print(f"ERROR: {_safe(exc)}")
        return 1
