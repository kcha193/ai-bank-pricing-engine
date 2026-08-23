"""Collector tests run entirely against saved fixtures -- never a live bank site."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    COLLECTOR_CLASSES,
    ANZCollector,
    BNZCollector,
    CollectionStatus,
    build_collectors,
    collect_all_mortgage_rates,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.competitor_rates.normalise import normalise_term

OBSERVED_AT = datetime(2026, 8, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("label", "expected_months"),
    [
        ("6 months", 6),
        ("1 year", 12),
        ("18 months", 18),
        ("2 years", 24),
        ("3 years", 36),
        ("4 years", 48),
        ("5 years", 60),
    ],
)
def test_term_normalisation_covers_every_tracked_term(label, expected_months):
    assert normalise_term(label).months == expected_months


def test_all_five_nz_banks_have_a_registered_collector():
    assert {collector.bank for collector in COLLECTOR_CLASSES} == {
        "ANZ",
        "ASB",
        "BNZ",
        "Westpac",
        "Kiwibank",
    }


def test_every_collector_uses_an_official_bank_domain():
    for collector in COLLECTOR_CLASSES:
        assert collector.home_loan_url.startswith("https://")
        assert ".co.nz" in collector.home_loan_url


def test_collector_parses_rates_and_retains_provenance(fixture_fetcher):
    collector = ANZCollector(fetcher=fixture_fetcher())
    result = collector.collect_mortgage_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )

    assert result.status is CollectionStatus.SUCCESS
    assert result.rate_count > 0

    record = next(
        r for r in result.records if r.term_months_normalised == 12 and r.rate_category == "special"
    )
    assert record.advertised_rate_pct == 4.79
    assert record.bank == "ANZ"
    assert record.source_url == ANZCollector.home_loan_url
    assert record.captured_at_utc == OBSERVED_AT
    assert record.raw_text_context
    assert len(record.raw_html_sha256) == 64
    assert record.parser_version


def test_rate_parsing_strips_percentage_and_pa_suffix(fixture_fetcher):
    collector = ANZCollector(fetcher=fixture_fetcher())
    records = collector.collect_mortgage_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    ).records
    rates = {r.advertised_rate_pct for r in records}
    # "4.79% p.a." in the fixture must become the float 4.79, not 4.79% or 479.
    assert 4.79 in rates
    assert all(0 < rate < 15 for rate in rates)


def test_special_rates_infer_the_twenty_percent_equity_condition(fixture_fetcher):
    collector = ANZCollector(fetcher=fixture_fetcher())
    records = collector.collect_mortgage_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    ).records
    special = next(r for r in records if r.rate_category == "special")
    assert special.max_lvr == 0.80
    assert special.min_equity == 0.20


def test_bank_specific_product_naming_maps_to_canonical_category(fixture_fetcher):
    """BNZ brands its discounted rates 'Classic', not 'Special'."""
    collector = BNZCollector(fetcher=fixture_fetcher())
    records = collector.collect_mortgage_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    ).records
    twelve_month = [r for r in records if r.term_months_normalised == 12]
    categories = {r.rate_category for r in twelve_month}
    assert "special" in categories, "BNZ Classic must map to the canonical special category"


def test_fetch_failure_is_returned_not_raised(fixture_fetcher):
    collector = ANZCollector(fetcher=fixture_fetcher(failing_banks=["ANZ"]))
    result = collector.collect_mortgage_rates(persist_snapshot=False)

    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "ConnectionError"
    assert result.records == ()


def test_unrecognised_page_structure_is_a_parser_failure(fixture_fetcher):
    collector = ANZCollector(fetcher=fixture_fetcher(empty_banks=["ANZ"]))
    result = collector.collect_mortgage_rates(persist_snapshot=False)

    assert result.status is CollectionStatus.FAILED
    assert result.error_type == "ParserError"


def test_one_failing_bank_does_not_prevent_the_others(fixture_fetcher):
    collectors = build_collectors(fetcher=fixture_fetcher(failing_banks=["Westpac"]))
    report = collect_all_mortgage_rates(
        collectors, captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )

    assert {result.bank for result in report.failed} == {"Westpac"}
    assert {result.bank for result in report.succeeded} == {"ANZ", "ASB", "BNZ", "Kiwibank"}
    assert report.records, "surviving banks must still yield records"
    assert report.any_success


def test_a_collector_raising_unexpectedly_is_isolated(fixture_fetcher):
    class ExplodingCollector(ANZCollector):
        bank = "Exploding"

        def collect_mortgage_rates(self, *args, **kwargs):
            raise RuntimeError("collector is broken")

    collectors = [ExplodingCollector(fetcher=fixture_fetcher()), BNZCollector(
        fetcher=fixture_fetcher()
    )]
    report = collect_all_mortgage_rates(
        collectors, captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )

    assert len(report.failed) == 1
    assert report.failed[0].error_type == "RuntimeError"
    assert len(report.succeeded) == 1


def test_collection_report_renders_a_status_table(fixture_fetcher):
    collectors = build_collectors(fetcher=fixture_fetcher(failing_banks=["Westpac"]))
    report = collect_all_mortgage_rates(
        collectors, captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )
    rendered = report.render()

    assert "SUCCESS" in rendered
    assert "FAILED" in rendered
    assert "Westpac" in rendered
    assert "rates" in rendered


def test_all_five_collectors_parse_their_fixtures(fixture_fetcher):
    collectors = build_collectors(fetcher=fixture_fetcher())
    report = collect_all_mortgage_rates(
        collectors, captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )

    assert len(report.failed) == 0, report.render()
    for result in report.succeeded:
        assert result.rate_count > 0
        assert all(
            ProductType(r.product_type) is ProductType.HOME_LOAN for r in result.records
        )


def test_snapshot_persistence_writes_raw_html(fixture_fetcher, tmp_path):
    collector = ANZCollector(fetcher=fixture_fetcher(), raw_root=tmp_path)
    result = collector.collect_mortgage_rates(captured_at_utc=OBSERVED_AT)

    assert result.snapshot_path is not None
    saved = Path(result.snapshot_path)
    assert saved.parent == tmp_path / "2026-08-20"
    assert "Fixed Special" in saved.read_text()


def test_snapshot_persistence_does_not_collide_within_the_same_day(fixture_fetcher, tmp_path):
    """A second same-day collection must not overwrite the first run's raw evidence."""
    collector = ANZCollector(fetcher=fixture_fetcher(), raw_root=tmp_path)
    morning = OBSERVED_AT.replace(hour=8)
    afternoon = OBSERVED_AT.replace(hour=16)

    first = collector.collect_mortgage_rates(captured_at_utc=morning)
    second = collector.collect_mortgage_rates(captured_at_utc=afternoon)

    assert first.snapshot_path != second.snapshot_path
    assert Path(first.snapshot_path).exists()
    assert Path(second.snapshot_path).exists()


def test_build_collectors_rejects_an_unknown_bank():
    with pytest.raises(KeyError, match="Rabobank"):
        build_collectors(banks=["Rabobank"])


def test_browser_fetcher_reports_a_clear_error_when_playwright_is_absent():
    """Live collection needs JS rendering; a missing extra must say so plainly."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("playwright is installed; the missing-dependency path cannot run")

    from balance_pricing_engine.competitor_rates.collectors import build_browser_fetcher
    from balance_pricing_engine.competitor_rates.collectors.base import FetchError
    from balance_pricing_engine.pricing_intelligence.config import CollectionConfig

    fetcher = build_browser_fetcher(CollectionConfig())
    with pytest.raises(FetchError, match="playwright is not installed"):
        fetcher("https://www.anz.co.nz/rates-fees-agreements/home-loans/")


def test_fetch_with_retries_succeeds_after_a_transient_failure(monkeypatch):
    from balance_pricing_engine.competitor_rates.collectors import base as collectors_base
    from balance_pricing_engine.pricing_intelligence.config import CollectionConfig

    monkeypatch.setattr(collectors_base.time, "sleep", lambda *_: None)
    attempts = []

    def attempt():
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("transient")
        return b"ok"

    config = CollectionConfig(max_retries=2, retry_backoff_seconds=0.01)
    result = collectors_base._fetch_with_retries(config, "https://example.test", attempt)

    assert result == b"ok"
    assert len(attempts) == 2


def test_fetch_with_retries_gives_up_after_max_retries(monkeypatch):
    from balance_pricing_engine.competitor_rates.collectors import base as collectors_base
    from balance_pricing_engine.competitor_rates.collectors.base import FetchError
    from balance_pricing_engine.pricing_intelligence.config import CollectionConfig

    monkeypatch.setattr(collectors_base.time, "sleep", lambda *_: None)
    attempts = []

    def attempt():
        attempts.append(1)
        raise ConnectionError("still down")

    config = CollectionConfig(max_retries=2, retry_backoff_seconds=0.01)
    with pytest.raises(FetchError, match="could not fetch"):
        collectors_base._fetch_with_retries(config, "https://example.test", attempt)

    assert len(attempts) == 3, "initial attempt plus two configured retries"


def test_browser_fetcher_applies_the_shared_retry_helper(monkeypatch):
    """A browser-rendering fetch must route through the same retry/backoff
    helper as the default fetcher, not fail on the first transient error."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed; nothing to route through retries")

    from balance_pricing_engine.competitor_rates.collectors import build_browser_fetcher
    from balance_pricing_engine.competitor_rates.collectors import base as collectors_base
    from balance_pricing_engine.pricing_intelligence.config import CollectionConfig

    seen_configs = []

    def fake_fetch_with_retries(config, url, attempt):
        seen_configs.append(config)
        return b"<html>ok</html>"

    monkeypatch.setattr(collectors_base, "_fetch_with_retries", fake_fetch_with_retries)

    config = CollectionConfig(max_retries=3, retry_backoff_seconds=1.5)
    html = build_browser_fetcher(config)("https://example.test")

    assert html == b"<html>ok</html>"
    assert seen_configs == [config]


def test_render_javascript_selects_the_browser_fetcher():
    collectors = build_collectors(banks=["ANZ"], render_javascript=True)

    assert collectors[0].render_javascript is True


def test_an_injected_fetcher_wins_over_render_javascript(fixture_fetcher):
    """Tests must never be routed to a browser by a config flag."""
    collectors = build_collectors(
        banks=["ANZ"], fetcher=fixture_fetcher(), render_javascript=True
    )
    result = collectors[0].collect_mortgage_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )

    assert result.status is CollectionStatus.SUCCESS
