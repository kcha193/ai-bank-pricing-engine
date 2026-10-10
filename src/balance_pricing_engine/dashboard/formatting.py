"""Pure presentation helpers. They format; they never round or recompute values."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from balance_pricing_engine.pricing_intelligence.health import FreshnessState, HealthLevel
from balance_pricing_engine.pricing_intelligence.snapshot import _term_label

NZ_TZ = ZoneInfo("Pacific/Auckland")
EM_DASH = "—"
# Characters left unquoted in a link target; everything else (space, parentheses, brackets,
# quotes, angle brackets, backslash) is percent-encoded so it cannot terminate the link.
_SAFE_URL_CHARS = ":/?&=#%+,;@~-._"


_MD_SPECIALS = re.compile(r"([\\`*_\[\]()#!|<>~$:])")


def md_escape(text: str) -> str:
    """Escape Markdown so scraped text renders as literal text.

    Bank pages are untrusted: a product name or date string must not be able to inject a
    link, an image (a tracking beacon), a ``:colour[...]`` directive or LaTeX.
    """
    return _MD_SPECIALS.sub(r"\\\1", text)


def safe_md_link(label: str, url: Optional[str]) -> str:
    """``[label](url)`` for http(s) URLs only, with characters that could end the link quoted.

    Anything else (``javascript:``, ``data:``, empty) is shown as escaped plain text.
    """
    if not url or not url.startswith(("http://", "https://")):
        return md_escape(url or EM_DASH)
    return f"[{md_escape(label)}]({quote(url, safe=_SAFE_URL_CHARS)})"


def fmt_rate_pct(value: Optional[float], places: int = 2) -> str:
    """Advertised-percent rate (``4.85`` -> ``4.85%``)."""
    return EM_DASH if value is None else f"{value:.{places}f}%"


def fmt_rate_fraction(value: Optional[float], places: int = 2) -> str:
    """Decimal-fraction rate (``0.0485`` -> ``4.85%``), as the optimiser reports."""
    return EM_DASH if value is None else f"{value * 100:.{places}f}%"


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
