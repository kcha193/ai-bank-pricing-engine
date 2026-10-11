from datetime import datetime, timezone

import pytest

from balance_pricing_engine.dashboard.formatting import (
    fmt_age_hours,
    fmt_bps,
    fmt_freshness,
    fmt_nz_datetime,
    fmt_rate_fraction,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.pricing_intelligence.health import FreshnessState


def test_rates_keep_units_explicit():
    assert fmt_rate_pct(4.85) == "4.85%"
    assert fmt_rate_fraction(0.0485) == "4.85%"
    assert fmt_rate_pct(None) == "—" and fmt_rate_fraction(None) == "—"


@pytest.mark.parametrize(
    "value,text",
    [(-10.0, "-10 bps"), (4.0, "+4 bps"), (0.0, "0 bps"), (2.5, "+2.5 bps"), (None, "—")],
)
def test_bps(value, text):
    assert fmt_bps(value) == text


def test_nz_datetime_converts_from_utc_with_dst():
    # 2026-10-10 20:03 UTC is 09:03 NZDT on 11 Oct (DST began 27 Sep 2026)
    assert (
        fmt_nz_datetime(datetime(2026, 10, 10, 20, 3, tzinfo=timezone.utc)) == "11 Oct 2026 09:03"
    )
    # Winter: UTC+12
    assert fmt_nz_datetime(datetime(2026, 6, 30, 20, 3, tzinfo=timezone.utc)) == "01 Jul 2026 08:03"


def test_nz_datetime_treats_naive_values_as_utc_and_handles_none():
    assert fmt_nz_datetime(datetime(2026, 6, 30, 20, 3)) == "01 Jul 2026 08:03"
    assert fmt_nz_datetime(None) == "—"


@pytest.mark.parametrize(
    "url",
    [
        "https://bank.example/rates?a=1&b=2",
        "http://bank.example/",
        "HTTPS://BANK.EXAMPLE/Rates",
    ],
)
def test_http_url_or_none_accepts_plain_http_urls_unchanged(url):
    from balance_pricing_engine.dashboard.formatting import http_url_or_none

    assert http_url_or_none(url) == url


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://bank.example/rates",
        "//bank.example/rates",
        "https:///no-host",
        "https://bank.example/a b",
        "https://bank.example/\nInjected: header",
        "https://bank.example\t@evil.example/",
        "https://trusted.example@evil.example/",
    ],
)
def test_http_url_or_none_rejects_anything_that_is_not_a_plain_http_url(url):
    from balance_pricing_engine.dashboard.formatting import http_url_or_none

    assert http_url_or_none(url) is None


def test_age_terms_and_freshness_labels():
    assert fmt_age_hours(41.2) == "41 h" and fmt_age_hours(None) == "—"
    assert [fmt_term(m) for m in (6, 12, 18, 24, 60)] == ["6M", "1Y", "18M", "2Y", "5Y"]
    assert fmt_freshness(FreshnessState.STALE) == "⚠ STALE"
    assert fmt_freshness(FreshnessState.FRESH) == "FRESH"


@pytest.mark.parametrize(
    "value,signed,text",
    [
        (-4_508_279.0, False, "-$4.5m"),
        (400_000.0, True, "+$0.4m"),
        (400_000.0, False, "$0.4m"),
        (-1_700_000.0, True, "-$1.7m"),
        (50_000.0, True, "+$50,000"),
        (0.0, True, "$0"),
        (2_400_000_000.0, False, "$2.4bn"),
        (None, False, "—"),
    ],
)
def test_money_in_millions(value, signed, text):
    from balance_pricing_engine.dashboard.formatting import fmt_money_m

    assert fmt_money_m(value, signed=signed) == text
