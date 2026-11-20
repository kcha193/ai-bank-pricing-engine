"""Escalation of repeated "already running" skips (exit code 3; DATA-008).

A scheduled run that finds its product's lock held exits 3 and does nothing. Once is
routine; a streak means a stuck collector, a hung browser or a stale process. This module
keeps a per-product streak in a small JSON sidecar next to the product's lock file
(``<lock>.skips.json``), deliberately *not* in the SQLite store: the hung run being skipped
may be the one holding the database.

* Each distinct invocation that is skipped advances the streak; a repeated invocation id
  (a scheduler retrying the same run) is not counted twice.
* At ``warning_after_consecutive_skips`` a structured warning is logged; at
  ``alert_after_consecutive_skips`` one operational alert is delivered per streak to each
  configured channel. A channel whose delivery failed is retried on the next skip; a
  channel that received it is never re-sent during the same streak.
* Acquiring the lock ends the streak. If an alert went out, the same channels get one
  recovery notice. The streak then resets.

Sidecar updates are read-modify-write under their own advisory lock and replaced
atomically. The sidecar holds counts, timestamps, invocation ids and channel *names* only
-- never webhook URLs or tokens.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import logging
from pathlib import Path
from typing import Iterator, Mapping, Optional

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    AlertSink,
    dispatch_alerts_safely,
    plain_alert,
)
from balance_pricing_engine.pricing_intelligence.config import SkipEscalationPolicy
from balance_pricing_engine.pricing_intelligence.run_artifacts import write_json_atomically

logger = logging.getLogger(__name__)

SKIP_NONE = "none"
SKIP_WARNING = "warning"
SKIP_ALERT = "alert"

#: Invocation ids remembered for de-duplication; older ones age out.
_MAX_REMEMBERED_INVOCATIONS = 50

_PRODUCT_LABELS = {
    ProductType.HOME_LOAN: "Home loan",
    ProductType.TERM_DEPOSIT: "Term deposit",
}


def skip_state_path(lock_path: str | Path) -> Path:
    """The product's skip sidecar, colocated with (and named after) its lock file."""
    lock_path = Path(lock_path)
    return lock_path.with_name(lock_path.name + ".skips.json")


@dataclass(frozen=True)
class SkipOutcome:
    level: str
    consecutive_skips: int
    counted: bool
    alerts_sent: int


@dataclass(frozen=True)
class RecoveryOutcome:
    recovered: bool
    previous_streak: int
    alerts_sent: int


def _empty_state(product_type: ProductType) -> dict:
    return {
        "product_type": product_type.value,
        "consecutive_skips": 0,
        "streak_started_at": None,
        "last_skip_at": None,
        "counted_invocations": [],
        "warning_logged": False,
        "alerted_channels": [],
        "last_alert_at": None,
        "last_recovered_at": None,
    }


@contextmanager
def _locked_state(path: Path, product_type: ProductType) -> Iterator[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            state = _empty_state(product_type)
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise ValueError("state is not a JSON object")
                    state.update(loaded)
                except (OSError, ValueError) as exc:
                    # A torn or hand-edited sidecar must not stop the skip being reported;
                    # the streak restarts from this skip, which under-counts (never over-).
                    logger.warning(
                        "skip-escalation state %s is unreadable (%s); restarting the count",
                        path,
                        exc,
                    )
            yield state
            write_json_atomically(path, state)
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def _skip_alert(product_type: ProductType, state: dict, lock_hint: str):
    label = _PRODUCT_LABELS[product_type]
    return plain_alert(
        title=f"{label} collection blocked",
        body=(
            f"{state['consecutive_skips']} consecutive scheduled {label.lower()} collections "
            f"were skipped because a previous run still holds the lock (since "
            f"{state['streak_started_at']}). A collector may be stuck. Lock: {lock_hint}. "
            "See 'Repeated exit code 3' in docs/pricing_intelligence.md."
        ),
    )


def _recovery_alert(product_type: ProductType, previous_streak: int):
    label = _PRODUCT_LABELS[product_type]
    return plain_alert(
        title=f"{label} collection recovered",
        body=(
            f"Scheduled {label.lower()} collection acquired its lock again after "
            f"{previous_streak} consecutive skipped run(s)."
        ),
    )


def record_skip(
    state_path: str | Path,
    *,
    product_type: ProductType | str,
    invocation_id: str,
    policy: SkipEscalationPolicy,
    sinks: Mapping[str, AlertSink],
    now: datetime,
) -> SkipOutcome:
    """Count one skipped invocation and escalate per ``policy``."""
    product_type = ProductType(product_type)
    path = Path(state_path)
    alerts_sent = 0
    with _locked_state(path, product_type) as state:
        counted = invocation_id not in state["counted_invocations"]
        if counted:
            if state["consecutive_skips"] == 0:
                state["streak_started_at"] = now.isoformat()
            state["consecutive_skips"] += 1
            state["last_skip_at"] = now.isoformat()
            state["counted_invocations"] = (state["counted_invocations"] + [invocation_id])[
                -_MAX_REMEMBERED_INVOCATIONS:
            ]
        streak = state["consecutive_skips"]
        if streak >= policy.alert_after_consecutive_skips:
            level = SKIP_ALERT
        elif streak >= policy.warning_after_consecutive_skips:
            level = SKIP_WARNING
        else:
            level = SKIP_NONE

        if level == SKIP_ALERT:
            pending = {
                name: sink for name, sink in sinks.items() if name not in state["alerted_channels"]
            }
            if pending:
                alert = _skip_alert(product_type, state, path.name.removesuffix(".skips.json"))
                for name, sink in pending.items():
                    if dispatch_alerts_safely([alert], sink):
                        state["alerted_channels"].append(name)
                        state["last_alert_at"] = now.isoformat()
                        alerts_sent += 1
        if level != SKIP_NONE:
            state["warning_logged"] = True

    if level != SKIP_NONE:
        logger.log(
            logging.ERROR if level == SKIP_ALERT else logging.WARNING,
            "collection skip escalation: product=%s level=%s consecutive_skips=%d "
            "alerts_sent=%d counted=%s",
            product_type.value,
            level,
            streak,
            alerts_sent,
            counted,
        )
    return SkipOutcome(
        level=level, consecutive_skips=streak, counted=counted, alerts_sent=alerts_sent
    )


def record_lock_acquired(
    state_path: str | Path,
    *,
    product_type: ProductType | str,
    sinks: Mapping[str, AlertSink],
    now: datetime,
) -> RecoveryOutcome:
    """End any skip streak: the blocked condition cleared once this run holds the lock."""
    product_type = ProductType(product_type)
    path = Path(state_path)
    if not path.exists():
        return RecoveryOutcome(recovered=False, previous_streak=0, alerts_sent=0)
    alerts_sent = 0
    with _locked_state(path, product_type) as state:
        previous = int(state["consecutive_skips"])
        alerted: list[str] = list(state["alerted_channels"])
        if previous:
            alert = _recovery_alert(product_type, previous)
            for name in alerted:
                sink: Optional[AlertSink] = sinks.get(name)
                if sink is not None:
                    alerts_sent += dispatch_alerts_safely([alert], sink)
            recovered_at = now.isoformat()
            state.clear()
            state.update(_empty_state(product_type))
            state["last_recovered_at"] = recovered_at
    if previous:
        logger.log(
            logging.WARNING if alerted else logging.INFO,
            "collection skip streak cleared: product=%s previous_streak=%d recovery_alerts=%d",
            product_type.value,
            previous,
            alerts_sent,
        )
    return RecoveryOutcome(recovered=bool(previous), previous_streak=previous, alerts_sent=alerts_sent)
