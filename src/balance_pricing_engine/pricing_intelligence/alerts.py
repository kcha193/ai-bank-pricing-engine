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
import threading
import time
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError
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

#: Same pattern, for Telegram delivery. Both must be set for
#: :func:`build_telegram_sink_from_env` to return a sink -- a bot token with
#: no destination chat (or vice versa) is not a usable configuration.
TELEGRAM_BOT_TOKEN_SECRET_NAME = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_SECRET_NAME = "TELEGRAM_CHAT_ID"

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
    """A rendered, delivery-agnostic alert.

    ``change`` is ``None`` for an alert not tied to one specific detected
    change -- a collector health/recovery notice or an intelligence brief
    (see ``pricing_intelligence.orchestration``/``briefing``). Sinks and
    ``dispatch_alerts_safely`` degrade their change-specific behaviour
    (structured webhook fields, per-alert log context) gracefully when it is
    absent, rather than requiring a fabricated placeholder change.
    """

    title: str
    body: str
    change: Optional[RateChange] = None

    def render(self) -> str:
        return f"{self.title}\n\n{self.body}"


def plain_alert(title: str, body: str) -> Alert:
    """Build a change-less alert (collector health, recovery, briefs)."""
    return Alert(title=title, body=body, change=None)


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


#: Fallback pause (seconds) for a 429 response that carries no usable
#: ``Retry-After`` header. Slack's own webhook rate limiting always sends
#: one, but a generic webhook consumer might not.
DEFAULT_RETRY_AFTER_SECONDS = 1.0

#: How many times ``build_default_poster`` will wait out a 429 before giving
#: up and letting the caller's own failure handling (``dispatch_alerts_safely``)
#: take over.
DEFAULT_MAX_RATE_LIMIT_RETRIES = 3

#: Upper bound on how long a single retry will wait, regardless of what the
#: ``Retry-After`` header claims. The header value comes from whatever server
#: is on the other end of a URL read from an environment variable -- a
#: misconfigured or malicious endpoint returning an enormous value must not be
#: able to block a scheduled collection run's alert delivery indefinitely.
MAX_RETRY_AFTER_SECONDS = 30.0


def _parse_retry_after_seconds(header_value: Optional[str]) -> float:
    """Parse a ``Retry-After`` header value (seconds form only), capped.

    Webhook rate limiters (Slack included) send the delta-seconds form, not
    the HTTP-date form, so that is all this needs to handle. Anything else
    falls back to :data:`DEFAULT_RETRY_AFTER_SECONDS` rather than failing the
    delivery over an unparseable header. The result is always clamped to
    :data:`MAX_RETRY_AFTER_SECONDS`.
    """
    if header_value is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        parsed = float(header_value)
    except ValueError:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, parsed))


def build_default_poster(
    timeout_seconds: float = 10.0,
    max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> WebhookPoster:
    """Return a poster that sends a real HTTPS POST via the standard library.

    No third-party HTTP client dependency: urllib is enough for a single JSON
    POST, and this repository avoids adding one for a single call site.

    A 429 response is retried after honoring the ``Retry-After`` header
    (falling back to :data:`DEFAULT_RETRY_AFTER_SECONDS` when absent or
    unparseable), up to ``max_rate_limit_retries`` times. Without this, a
    burst of alerts for one repricing event -- several terms changing on the
    same bank in one collection -- hits Slack's per-webhook rate limit after
    the first message and silently loses the rest, since ``dispatch_alerts_safely``
    logs delivery failures rather than retrying them on a later run.
    """

    def post(url: str, payload: bytes) -> None:
        attempt = 0
        while True:
            request = Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            try:
                with urlopen(request, timeout=timeout_seconds):
                    return
            except HTTPError as exc:
                if exc.code != 429 or attempt >= max_rate_limit_retries:
                    raise
                sleep(_parse_retry_after_seconds(exc.headers.get("Retry-After")))
                attempt += 1

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
        # `is not None`, not `or`: an injected poster whose __bool__/__len__
        # makes it falsey (e.g. an empty recording adapter) must still be
        # used -- `or` would silently discard it and send a real network
        # request via build_default_poster() instead. Same pattern as
        # build_webhook_sink_from_env()'s secrets-provider check.
        self._poster = poster if poster is not None else build_default_poster()

    def send(self, alert: Alert) -> None:
        payload = {self.text_field: alert.render()}
        change = alert.change
        if change is not None:
            payload.update(
                {
                    "bank": change.bank,
                    "change_type": change.change_type.value,
                    "change_bps": change.change_bps,
                    "suspicious": change.suspicious,
                    "detected_at": change.detected_at.isoformat(),
                }
            )
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
    # `is None`, not `or`: a caller-injected provider whose __bool__/__len__
    # makes it falsey (e.g. one representing an empty secret store) must
    # still be used -- `or` would silently discard it in favour of
    # EnvSecretsProvider, potentially reading a real ambient webhook URL the
    # caller's provider was deliberately overriding. Same pattern as
    # llm._client.build_client.
    secrets = EnvSecretsProvider() if secrets is None else secrets
    webhook_url = secrets.get_secret(secret_name)
    if not webhook_url:
        return None
    return WebhookAlertSink(webhook_url, text_field=text_field)


class TelegramAlertSink:
    """Sends a rendered alert as a plain-text Telegram message.

    Uses the Bot API's ``sendMessage`` method directly (``POST
    https://api.telegram.org/bot<token>/sendMessage``) -- no third-party
    Telegram client dependency, same rationale as :class:`WebhookAlertSink`
    using bare ``urlopen``. No ``parse_mode`` is set: every alert body in this
    module is plain text with emoji markers, not Markdown/HTML, and sending it
    unescaped through a parse mode would risk a stray ``_``/``*``/``[`` in a
    bank or product name breaking Telegram's parser and losing the whole
    message.

    Reuses :func:`build_default_poster` for delivery, including its 429
    retry loop. Telegram's own rate-limit response carries its suggested
    delay in the JSON body (``parameters.retry_after``), not the
    ``Retry-After`` header ``_parse_retry_after_seconds`` reads -- so a 429
    here retries after :data:`DEFAULT_RETRY_AFTER_SECONDS` rather than
    Telegram's exact suggested delay. That is an accepted simplification:
    ``dispatch_alerts_safely``'s own deadline already bounds total wait time
    regardless of the per-retry delay chosen, and adding a second,
    Telegram-specific retry-delay parser for a non-safety-critical value
    is not worth the extra surface area.

    ``send()`` raises on failure, same contract as ``WebhookAlertSink.send`` --
    callers wanting a broken Telegram channel to never affect a collection
    run should go through :func:`dispatch_alerts_safely`.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        poster: Optional[WebhookPoster] = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        # `is not None`, not `or` -- same falsey-injected-poster reasoning as
        # WebhookAlertSink.__init__.
        self._poster = poster if poster is not None else build_default_poster()

    def send(self, alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": alert.render()}
        self._poster(url, json.dumps(payload).encode("utf-8"))


def build_telegram_sink_from_env(
    secrets: Optional[SecretsProvider] = None,
    bot_token_secret_name: str = TELEGRAM_BOT_TOKEN_SECRET_NAME,
    chat_id_secret_name: str = TELEGRAM_CHAT_ID_SECRET_NAME,
) -> Optional[TelegramAlertSink]:
    """Build a Telegram sink from the configured secrets, or None if either is unset.

    Same "no delivery channel is the default" shape as
    :func:`build_webhook_sink_from_env`. Both the token and the chat id must
    be present -- one without the other cannot deliver anything.
    """
    # `is None`, not `or` -- same falsey-injected-provider reasoning as
    # build_webhook_sink_from_env.
    secrets = EnvSecretsProvider() if secrets is None else secrets
    bot_token = secrets.get_secret(bot_token_secret_name)
    chat_id = secrets.get_secret(chat_id_secret_name)
    if not bot_token or not chat_id:
        return None
    return TelegramAlertSink(bot_token, chat_id)


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


#: Total wall-clock budget for one ``dispatch_alerts_safely`` call, across
#: every alert and its own retries. ``run_collection()`` calls this
#: synchronously while its single-instance lock is held, before writing the
#: run summary -- an unbounded run here would delay the next scheduled
#: collection and leave monitoring without a completion record for however
#: long a misbehaving webhook endpoint chose to stall.
DEFAULT_ALERT_DISPATCH_DEADLINE_SECONDS = 30.0


def _alert_log_label(alert: Alert) -> str:
    """Identify an alert in a log line, whether or not it carries a change."""
    if alert.change is None:
        return alert.title
    return f"{alert.change.bank} {alert.change.change_type.value}"


def dispatch_alerts_safely(
    alerts: Sequence[Alert],
    sink: AlertSink,
    *,
    deadline_seconds: float = DEFAULT_ALERT_DISPATCH_DEADLINE_SECONDS,
) -> int:
    """Deliver alerts through a sink, never letting a delivery failure escape.

    Mirrors ``events.publisher.publish_safely()``, including its bounded-wait
    mechanism: a broken or misconfigured alert channel (an unreachable
    webhook, an expired URL, or one that is rate-limiting and retrying) must
    never fail, or block past ``deadline_seconds``, the run it was reporting
    on. Each alert is attempted independently, so one failure does not cost
    the rest. Failures are logged, not silently dropped.

    Returns how many alerts were confirmed delivered. Callers that must know
    *which* alerts succeeded (not just the count) -- e.g. to record per-alert
    delivery state in a dedup ledger -- should use
    :func:`dispatch_alerts_safely_detailed` instead; this is a thin wrapper
    over it.
    """
    return len(dispatch_alerts_safely_detailed(alerts, sink, deadline_seconds=deadline_seconds))


def dispatch_alerts_safely_detailed(
    alerts: Sequence[Alert],
    sink: AlertSink,
    *,
    deadline_seconds: float = DEFAULT_ALERT_DISPATCH_DEADLINE_SECONDS,
) -> list[Alert]:
    """Deliver alerts through a sink, never letting a delivery failure escape.

    Same delivery behaviour as :func:`dispatch_alerts_safely` (see there for
    the full rationale), but returns the list of alerts individually
    confirmed delivered rather than just a count -- a caller that marks
    alerts as notified (so a retried run does not re-send them) must persist
    that only for sends actually confirmed, not for every alert attempted;
    persisting an attempt as delivered would suppress a genuinely failed send
    on every future retry.

    Each ``sink.send()`` call runs on its own daemon thread, joined with
    whatever time remains of ``deadline_seconds``. A per-alert deadline check
    alone would not be enough: ``WebhookAlertSink``'s own 429 retries can
    each wait as long as ``MAX_RETRY_AFTER_SECONDS``, so a single alert could
    otherwise blow the whole budget before control ever returned to this
    loop. If a send has not finished by the time its slice of the budget
    runs out, this moves on without waiting further -- the thread is left to
    finish (or not) in the background, the same trade-off ``publish_safely``
    makes for a hung publisher. If starting that thread itself raises (e.g.
    the process is at its OS thread limit), that is caught and logged the
    same as any other delivery failure -- it must not propagate out of this
    "never fails the run" helper either.
    """
    sent: list[Alert] = []
    deadline = time.monotonic() + deadline_seconds
    for alert in alerts:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "alert delivery skipped for %s: dispatch deadline of %.0fs exceeded",
                _alert_log_label(alert),
                deadline_seconds,
            )
            continue

        outcome: dict = {}

        def _send(alert: Alert = alert, outcome: dict = outcome) -> None:
            try:
                sink.send(alert)
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported on the calling thread
                outcome["error"] = exc

        worker = threading.Thread(target=_send, daemon=True)
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
            logger.warning(
                "alert delivery failed for %s: could not start delivery thread: %s",
                _alert_log_label(alert),
                exc,
            )
            continue
        worker.join(remaining)

        if worker.is_alive():
            logger.warning(
                "alert delivery timed out for %s: exceeded dispatch deadline of %.0fs",
                _alert_log_label(alert),
                deadline_seconds,
            )
        elif outcome.get("ok"):
            sent.append(alert)
        else:
            logger.warning(
                "alert delivery failed for %s: %s",
                _alert_log_label(alert),
                outcome.get("error"),
            )
    return sent
