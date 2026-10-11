"""Pure presentation helpers. They format; they never round or recompute values."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from balance_pricing_engine.pricing_intelligence.health import FreshnessState, HealthLevel
from balance_pricing_engine.pricing_intelligence.snapshot import _term_label

NZ_TZ = ZoneInfo("Pacific/Auckland")
EM_DASH = "—"


def http_url_or_none(url: Optional[str]) -> Optional[str]:
    """``url`` unchanged if it is a plain http(s) URL, else ``None``.

    Used for the one link the dashboard offers (the official source page), which is handed to
    ``st.link_button`` as a parameter -- never interpolated into Markdown. Rejects other
    schemes, scheme-relative and host-less URLs, whitespace/control characters, and userinfo
    (``https://trusted.example@evil.example/`` reads as one host and goes to another).
    """
    if not url or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return None
    return url


def fmt_rate_pct(value: Optional[float], places: int = 2) -> str:
    """Advertised-percent rate (``4.85`` -> ``4.85%``)."""
    return EM_DASH if value is None else f"{value:.{places}f}%"


def fmt_rate_fraction(value: Optional[float], places: int = 2) -> str:
    """Decimal-fraction rate (``0.0485`` -> ``4.85%``), as the optimiser reports."""
    return EM_DASH if value is None else f"{value * 100:.{places}f}%"


def fmt_money_m(value: Optional[float], *, signed: bool = False) -> str:
    """Dollars at a readable scale (``-4_508_279`` -> ``-$4.5m``); small values stay in dollars."""
    if value is None:
        return EM_DASH
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    magnitude = abs(value)
    if magnitude >= 1e9:
        body = f"${magnitude / 1e9:.1f}bn"
    elif magnitude >= 100_000:
        body = f"${magnitude / 1e6:.1f}m"
    else:
        body = f"${magnitude:,.0f}"
    return f"{sign}{body}"


def fmt_bps(value: Optional[float]) -> str:
    if value is None:
        return EM_DASH
    if value == 0:
        return "0 bps"
    text = f"{value:+.0f}" if float(value).is_integer() else f"{value:+.1f}"
    return f"{text} bps"


def fmt_nz_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return EM_DASH
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(NZ_TZ).strftime("%d %b %Y %H:%M")


def fmt_age_hours(hours: Optional[float]) -> str:
    return EM_DASH if hours is None else f"{hours:.0f} h"


def fmt_term(months: Optional[int]) -> str:
    return "Floating" if months is None else _term_label(months)


def fmt_freshness(state: FreshnessState) -> str:
    return "⚠ STALE" if state is FreshnessState.STALE else state.value


def fmt_health(level: HealthLevel) -> str:
    return "⚠ DEGRADED" if level is HealthLevel.DEGRADED else level.value
