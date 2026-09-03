"""Alert formatting and delivery for detected market changes.

Formatting is separated from delivery on purpose. This module turns a
:class:`RateChange` into a rendered alert; where that alert goes is a sink's
problem. :class:`ConsoleAlertSink` and :class:`WebhookAlertSink` are the
concrete sinks; the :class:`AlertSink` protocol is the extension point for
anything else (email, a paging system, the in-process event publisher).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable
from urllib.request import Request, urlopen

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot
from balance_pricing_engine.secrets.provider import EnvSecretsProvider, SecretsProvider

logger = logging.getLogger(__name__)

#: Env var read for the outgoing webhook URL, matching the ANTHROPIC_API_KEY /
#: PRICING_ENGINE_API_KEYS pattern: the secret lives in the environment, never
#: in a config file. Unset means "no webhook delivery configured".
ALERT_WEBHOOK_URL_SECRET_NAME = "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL"

#: Posts a JSON-encoded payload to a URL and returns nothing, raising on
#: failure. Injectable so tests never reach the network -- the same pattern
#: as the collector ``Fetcher`` type in ``collectors/base.py``.
WebhookPoster = Callable[[str, bytes], None]

_SEVERITY_MARKERS = {
    ChangeType.RATE_DECREASE: "🔴",
    ChangeType.RATE_INCREASE: "🔵",
    ChangeType.NEW_PRODUCT: "🟢",
    ChangeType.REMOVED_PRODUCT: "⚫",
    ChangeType.ELIGIBILITY_CHANGE: "🟡",
    ChangeType.PROMOTION_CHANGE: "🟡",
}

_TERM_NAMES = {
    6: "6 Month",
    12: "1 Year",
    18: "18 Month",
    24: "2 Year",
    36: "3 Year",
    48: "4 Year",
    60: "5 Year",
}


@dataclass(frozen=True)
class Alert:
    """A rendered, delivery-agnostic alert."""

    title: str
    body: str
    change: RateChange

    def render(self) -> str:
        return f"{self.title}\n\n{self.body}"


@runtime_checkable
class AlertSink(Protocol):
    """Where rendered alerts are delivered."""

    def send(self, alert: Alert) -> None: ...


class ConsoleAlertSink:
    """Prints alerts to stdout. The default sink for CLI use."""

    def send(self, alert: Alert) -> None:
        print(alert.render())
        print()


class CollectingAlertSink:
    """Collects alerts in memory. Used by tests and by the dashboard."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


def build_default_poster(timeout_seconds: float = 10.0) -> WebhookPoster:
    """Return a poster that sends a real HTTPS POST via the standard library.

    No third-party HTTP client dependency: urllib is enough for a single JSON
    POST, and this repository avoids adding one for a single call site.
    """

    def post(url: str, payload: bytes) -> None:
        request = Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(request, timeout=timeout_seconds):
            pass

    return post


class WebhookAlertSink:
    """POSTs a rendered alert as JSON to a generic webhook URL.

    The payload's message field defaults to ``"text"``, which Slack incoming
    webhooks and the legacy Microsoft Teams Office 365 connector both render
    directly with no further configuration -- set ``text_field="content"`` for
    a Discord webhook instead. Structured fields (``bank``, ``change_type``,
    ``change_bps``, ``suspicious``, ``detected_at``) ride alongside the message
    for any consumer that parses JSON rather than just displaying it; chat
    webhooks ignore fields they don't recognise.

    ``send()`` raises on failure rather than swallowing it -- callers that want
    a broken webhook to never affect anything else (a scheduled collection run,
    in particular) should go through :func:`dispatch_alerts_safely`, which is
    to alert delivery what ``events.publisher.publish_safely()`` is to domain
    events.
    """

    def __init__(
        self,
        webhook_url: str,
        text_field: str = "text",
        poster: Optional[WebhookPoster] = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.text_field = text_field
        self._poster = poster or build_default_poster()

    def send(self, alert: Alert) -> None:
        change = alert.change
        payload = {
            self.text_field: alert.render(),
            "bank": change.bank,
            "change_type": change.change_type.value,
            "change_bps": change.change_bps,
            "suspicious": change.suspicious,
            "detected_at": change.detected_at.isoformat(),
        }
        self._poster(self.webhook_url, json.dumps(payload).encode("utf-8"))


def build_webhook_sink_from_env(
    secrets: Optional[SecretsProvider] = None,
    text_field: str = "text",
    secret_name: str = ALERT_WEBHOOK_URL_SECRET_NAME,
) -> Optional[WebhookAlertSink]:
    """Build a webhook sink from the configured secret, or None if unset.

    The env-var-gated "None means not configured" shape matches
    ``llm._client.is_llm_available`` and ``api.auth.require_api_key``: no
    delivery channel is the default, and nothing breaks when one is absent.
    """
    secrets = secrets or EnvSecretsProvider()
    webhook_url = secrets.get_secret(secret_name)
    if not webhook_url:
        return None
    return WebhookAlertSink(webhook_url, text_field=text_field)


def term_name(term_months: Optional[int]) -> str:
    if term_months is None:
        return "Floating"
    return _TERM_NAMES.get(term_months, f"{term_months} Month")


def format_alert(
    change: RateChange,
    snapshot: Optional[MarketSnapshot] = None,
    previous_snapshot: Optional[MarketSnapshot] = None,
) -> Alert:
    """Render one change as an alert, enriched with market context when available.

    Rank movement is only shown when both snapshots are supplied, because a rank
    change cannot be inferred from a rate change alone -- the rest of the market
    may have moved too.
    """
    marker = _SEVERITY_MARKERS.get(change.change_type, "⚪")
    label = term_name(change.term_months)
    category = change.rate_category.replace("_", " ").title()

    market_label = (
        "TERM DEPOSIT"
        if change.product_type is ProductType.TERM_DEPOSIT
        else "MORTGAGE"
    )
    title = f"{marker} NZ {market_label} RATE CHANGE"
    lines = [f"{change.bank} — {label} Fixed {category}", ""]

    if change.is_rate_move:
        lines.append(f"{change.previous_rate_pct:.2f}% → {change.current_rate_pct:.2f}%")
        lines.append(f"Movement: {change.change_bps:+.0f} bps")
    elif change.change_type is ChangeType.NEW_PRODUCT:
        lines.append(f"New product advertised at {change.current_rate_pct:.2f}%")
    elif change.change_type is ChangeType.REMOVED_PRODUCT:
        lines.append(f"Withdrawn (last advertised {change.previous_rate_pct:.2f}%)")
    else:
        lines.append(change.change_type.value.replace("_", " ").title())
        if change.detail:
            lines.append(change.detail)

    if change.suspicious:
        lines.extend(["", "⚠️  FLAGGED SUSPICIOUS — verify against the source page", ""])

    rank_line = _rank_movement(change, snapshot, previous_snapshot)
    if rank_line:
        lines.extend(["", "Market rank:", rank_line])

    market_lines = _market_context(change, snapshot)
    if market_lines:
        lines.append("")
        lines.extend(market_lines)

    lines.append("")
    lines.append(f"Detected: {change.detected_at.strftime('%d %b %Y')}")
    if change.source_url:
        lines.append(f"Source: {change.source_url}")

    return Alert(title=title, body="\n".join(lines), change=change)


def _rank_movement(
    change: RateChange,
    snapshot: Optional[MarketSnapshot],
    previous_snapshot: Optional[MarketSnapshot],
) -> Optional[str]:
    if snapshot is None or previous_snapshot is None or change.term_months is None:
        return None
    # A snapshot built for a different product must not be borrowed from
    # either: "special" exists as a category on both the mortgage and deposit
    # markets, so a deposit change paired with a same-category mortgage
    # snapshot would otherwise pass the rate_category guard below and render
    # the wrong market's rank.
    if ProductType(snapshot.product_type) is not change.product_type:
        return None
    if ProductType(previous_snapshot.product_type) is not change.product_type:
        return None
    # A snapshot built for a different rate_category (e.g. the CLI's default
    # "special" snapshot passed alongside a "standard" change) ranks a
    # different market entirely; the rank shown must not be borrowed from it.
    if snapshot.rate_category != change.rate_category:
        return None
    if previous_snapshot.rate_category != change.rate_category:
        return None
    current_market = snapshot.term(change.term_months)
    previous_market = previous_snapshot.term(change.term_months)
    if current_market is None or previous_market is None:
        return None
    current_rank = current_market.rank_for(change.bank)
    previous_rank = previous_market.rank_for(change.bank)
    if current_rank is None or previous_rank is None:
        return None
    if current_rank == previous_rank:
        return f"#{current_rank} (unchanged)"
    return f"#{previous_rank} → #{current_rank}"


def _market_context(
    change: RateChange, snapshot: Optional[MarketSnapshot]
) -> list[str]:
    if snapshot is None or change.term_months is None or change.current_rate_pct is None:
        return []
    # Same guards as _rank_movement(): a snapshot built for a different
    # product or a different rate_category compares this change against the
    # wrong market's median.
    if ProductType(snapshot.product_type) is not change.product_type:
        return []
    if snapshot.rate_category != change.rate_category:
        return []
    market = snapshot.term(change.term_months)
    if market is None:
        return []
    difference = rate_change_bps(market.median_rate, change.current_rate_pct)
    return [
        f"Market median: {market.median_rate:.2f}%",
        f"Difference vs median: {difference:+.0f} bps",
    ]


def format_alerts(
    changes: Sequence[RateChange],
    snapshot: Optional[MarketSnapshot] = None,
    previous_snapshot: Optional[MarketSnapshot] = None,
) -> list[Alert]:
    """Render every change as an alert, most material movement first."""
    ordered = sorted(
        changes,
        key=lambda change: (-abs(change.change_bps or 0), change.bank),
    )
    return [format_alert(change, snapshot, previous_snapshot) for change in ordered]


def dispatch_alerts(alerts: Sequence[Alert], sink: AlertSink) -> int:
    """Deliver alerts through a sink, returning how many were sent."""
    for alert in alerts:
        sink.send(alert)
    return len(alerts)


def dispatch_alerts_safely(alerts: Sequence[Alert], sink: AlertSink) -> int:
    """Deliver alerts through a sink, never letting a delivery failure escape.

    Mirrors ``events.publisher.publish_safely()``: a broken or misconfigured
    alert channel (an unreachable webhook, an expired URL) must never fail or
    interrupt the run it was reporting on. Each alert is attempted
    independently, so one failure does not cost the rest. Failures are logged,
    not silently dropped.
    """
    sent = 0
    for alert in alerts:
        try:
            sink.send(alert)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - delivery must never propagate
            logger.warning(
                "alert delivery failed for %s %s: %s",
                alert.change.bank,
                alert.change.change_type.value,
                exc,
            )
    return sent
