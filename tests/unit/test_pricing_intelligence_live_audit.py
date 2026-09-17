"""Minimal reproductions of official-page structures observed during the audit."""

from datetime import datetime, timezone

import pytest

from balance_pricing_engine.competitor_rates.collectors import BNZCollector, KiwibankCollector
from balance_pricing_engine.competitor_rates.normalise import (
    normalise_lvr_condition,
    normalise_lvr_values,
    normalise_term,
)
from balance_pricing_engine.pricing_intelligence.validation import validate_observations


@pytest.mark.parametrize("text", ["less than 20% equity", "under 20% equity", "LVR above 80%"])
def test_low_equity_is_not_inverted_into_a_minimum_equity_requirement(text):
    assert normalise_lvr_condition(text) == "low_equity_margin_applies"
    assert normalise_lvr_values(text) == (None, None)


def test_kiwibank_standard_column_keeps_its_low_equity_condition():
    html = """<table><tr><th>Term</th><th>Special fixed rate: at least 20% equity</th>
    <th>Standard fixed rate: less than 20% equity</th></tr>
    <tr><td>1 year fixed</td><td>4.95%</td><td>5.85%</td></tr></table>"""
    result = KiwibankCollector(fetcher=lambda url: html.encode()).collect_mortgage_rates(
        persist_snapshot=False
    )
    special, standard = sorted(result.records, key=lambda r: r.advertised_rate_pct)
    assert (special.max_lvr, special.min_equity) == (0.8, 0.2)
    assert standard.lvr_condition == "low_equity_margin_applies"
    assert (standard.max_lvr, standard.min_equity) == (None, None)


def test_bnz_sole_standard_header_folds_to_special_and_keeps_floating_distinct():
    """BNZ renamed its headline fixed column from "Classic" to "Standard" and
    moved its separate discretionary discount behind login, so this is now
    the *only* fixed tier on the page -- confirmed live 2026-09-17 via the
    column's own "More information" tooltip, which still requires at least
    20% equity, unchanged from what "Classic" used to require. An earlier
    version of this test asserted the opposite (rate_category "standard",
    min_equity None) on the mistaken assumption that the rename also meant
    the equity condition no longer applied; it did not verify against the
    tooltip actually attached to the "Standard" header and caused every
    scheduled BNZ collection to be excluded from the market comparison
    (zero rows in the "special" category -- see
    orchestration._zero_comparable_rate_failures). See BNZCollector's
    class docstring for how parse_mortgage_html() detects "no dedicated
    second tier on this page" and folds the row back to the same
    default_fixed_category path an unmarked row would take.
    """
    html = """<table><tr><th>Term</th><th>Standard Fixed or floating</th>
    <th>TotalMoney Offset</th><th>Rapid Repay Revolving</th><th>Mortgage One Revolving</th></tr>
    <tr><td>Floating</td><td>6.09%</td><td>6.19%</td><td>6.19%</td><td>6.19%</td></tr>
    <tr><td>1 year</td><td>4.99%</td><td>n/a</td><td>n/a</td><td>n/a</td></tr></table>"""
    result = BNZCollector(fetcher=lambda url: html.encode()).collect_mortgage_rates(
        captured_at_utc=datetime.now(timezone.utc), persist_snapshot=False
    )
    fixed = [r for r in result.records if r.term_months_normalised == 12]
    floating = [r for r in result.records if r.term_months_normalised is None]
    assert fixed[0].rate_category == "special"
    assert (fixed[0].max_lvr, fixed[0].min_equity) == (0.80, 0.20)
    assert fixed[0].lvr_condition == "min_20_percent_equity"
    assert len(floating) == 4
    assert len({r.observation_key() for r in floating}) == 4
    assert len(validate_observations(result.records).accepted) == 5


@pytest.mark.parametrize("label", ["-1 year", "1.5 years", "0 months", "0 days", "nonsense"])
def test_invalid_terms_do_not_become_plausible_positive_terms(label):
    assert normalise_term(label).months is None


@pytest.mark.parametrize("label", ["Flexible", "Revolving"])
def test_nonfixed_product_names_are_not_silently_dropped(label):
    html = f"<table><tr><th>Loan type</th><th>Rate</th></tr><tr><td>{label}</td><td>6.05%</td></tr></table>"
    result = KiwibankCollector(fetcher=lambda url: html.encode()).collect_mortgage_rates(
        persist_snapshot=False
    )
    assert len(result.records) == 1
    assert result.records[0].term_months_normalised is None
    assert result.records[0].rate_category == "floating"


def test_snapshot_write_failure_is_isolated_to_one_collector(
    fixture_fetcher, monkeypatch, tmp_path
):
    from balance_pricing_engine.competitor_rates.collectors import (
        ANZCollector,
        CollectionStatus,
        collect_all_rates,
    )

    broken = ANZCollector(fetcher=fixture_fetcher())

    def fail_write(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(broken, "_write_snapshot", fail_write)
    report = collect_all_rates(
        [broken, KiwibankCollector(fetcher=fixture_fetcher(), raw_root=tmp_path)],
        inter_request_delay_seconds=0,
    )
    assert report.results[0].status == CollectionStatus.FAILED
    assert report.results[1].status == CollectionStatus.SUCCESS


def test_westpac_rate_first_layout_does_not_put_the_rate_in_product_identity():
    from balance_pricing_engine.competitor_rates.collectors import WestpacCollector
    from balance_pricing_engine.pricing_intelligence.changes import detect_changes

    def parse(rate):
        html = f"<table><tr><th>Rate</th><th>Term</th></tr><tr><td>{rate}% p.a.</td><td>1 year</td></tr></table>"
        return (
            WestpacCollector(fetcher=lambda url: html.encode())
            .collect_mortgage_rates(persist_snapshot=False)
            .records
        )

    before, after = parse(5.59), parse(5.49)
    changes = detect_changes(before, after)
    assert len(changes) == 1
    assert changes[0].change_bps == -10.0


def test_legacy_westpac_rate_named_identity_cannot_retain_a_cheaper_old_rate(tmp_path, make_record):
    from datetime import timedelta
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    path = tmp_path / "legacy.db"
    now = datetime.now(timezone.utc)
    old = make_record(
        bank="Westpac",
        rate_category="standard",
        rate_pct=5.59,
        product_name="5.59% p.a.",
        term_label_raw="1 year",
        observed_at=now,
    )
    with ObservationStore(path) as store:
        store.record_observations([old], observed_at=now)
    with ObservationStore(path) as store:
        current = old.model_copy(update={"product_name": "1 year", "advertised_rate_pct": 5.69})
        store.record_observations([current], observed_at=now + timedelta(hours=1))
        assert len(store.get_current_observations()) == 1
        assert store.get_current_observations()[0].advertised_rate_pct == 5.69
