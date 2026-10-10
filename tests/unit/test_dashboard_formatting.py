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


def test_md_escape_neutralises_links_images_and_directives():
    from balance_pricing_engine.dashboard.formatting import md_escape

    text = md_escape("![x](http://evil.example/p.png) :red[boom] $x$ <b>hi</b> `code`")
    assert "![" not in text and "](" not in text
    assert ":red[" not in text and "<b>" not in text and "`code`" not in text
    assert md_escape("plain text 4.89") == "plain text 4.89"  # ordinary prose is untouched


def test_safe_md_link_only_links_http_urls_and_cannot_break_out():
    from balance_pricing_engine.dashboard.formatting import safe_md_link

    ok = safe_md_link("official page", "https://bank.example/rates?a=1&b=2")
    assert ok == "[official page](https://bank.example/rates?a=1&b=2)"
    hostile = safe_md_link("official page", "https://e.example/a b)(![x](http://evil/p.png)")
    assert " " not in hostile.split("](", 1)[1] and hostile.count("](") == 1
    for url in ("javascript:alert(1)", "data:text/html,<script>", "", None):
        assert "](" not in safe_md_link("official page", url)


def test_age_terms_and_freshness_labels():
    assert fmt_age_hours(41.2) == "41 h" and fmt_age_hours(None) == "—"
    assert [fmt_term(m) for m in (6, 12, 18, 24, 60)] == ["6M", "1Y", "18M", "2Y", "5Y"]
    assert fmt_freshness(FreshnessState.STALE) == "⚠ STALE"
    assert fmt_freshness(FreshnessState.FRESH) == "FRESH"
