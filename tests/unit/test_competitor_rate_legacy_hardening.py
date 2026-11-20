"""Phase 2d: legacy competitor-rate pipeline hardening (DATA-006, DATA-018, SEC-010).

Offline and deterministic: every record comes from a checked-in synthetic fixture or is built
inline. Nothing here fetches a bank page.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from balance_pricing_engine.application.contracts import PricingScenarioRequest
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.competitor_rates import cli
from balance_pricing_engine.competitor_rates.apply import (
    CompetitorRateResolutionError,
    _deposit_representative,
    _filter_term_deposit_records,
    _resolve_strategy,
    apply_competitor_rates_to_config,
    apply_competitor_rates_with_resolution,
    load_and_resolve_competitor_rates,
)
from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK
from balance_pricing_engine.competitor_rates.extractors import parse_html_rates
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    CompetitorRateRun,
    CompetitorRatesConfig,
    ProductType,
    SourceAttempt,
    ValidationSummary,
)
from balance_pricing_engine.competitor_rates.sources import OFFICIAL_SOURCES
from balance_pricing_engine.competitor_rates.store import (
    load_rate_run,
    neutralise_csv_cell,
    write_rate_run,
)
from balance_pricing_engine.competitor_rates.validate import validate_rate_run
from balance_pricing_engine.config import EngineConfig, load_config, revalidation_payload
from balance_pricing_engine.governance.gates import DecisionStatus

T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
FX = Path("tests/fixtures/pricing_intelligence")
TD_FX = FX / "term_deposits_2026-10-30"
SPECIALS_FX = FX / "term_deposit_specials"
LEGACY_FIXTURE = "tests/fixtures/competitor_rates/latest.json"
BANKS = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]

_TD_TABLE = """<html><body><table><tr><th>Term</th><th>$5,000+</th></tr>
<tr><td>6 Months</td><td>3.60% p.a.</td></tr>{rows}</table></body></html>"""


# ----------------------------------------------------------------------------- helpers


def _legacy_records(bank: str, product: ProductType, html: str) -> list[CompetitorRateRecord]:
    """What ``competitor_rates fetch`` stores: the generic parser, then bank categorisation."""
    records = parse_html_rates(bank, product, f"https://example.invalid/{bank.lower()}", html, T0)
    return cli._apply_bank_categorisation(records)


def _td(bank: str, html: str) -> list[CompetitorRateRecord]:
    return _legacy_records(bank, ProductType.TERM_DEPOSIT, html)


def _fixture_td(bank: str) -> list[CompetitorRateRecord]:
    return _td(bank, (TD_FX / f"{bank.lower()}_term_deposits.html").read_text())


def _fixture_hl(bank: str) -> list[CompetitorRateRecord]:
    html = (FX / f"{bank.lower()}_home_loans.html").read_text()
    return _legacy_records(bank, ProductType.HOME_LOAN, html)


def _ordinary_td(bank: str, rate_12m: float) -> list[CompetitorRateRecord]:
    row = f"<tr><td>12 Months</td><td>{rate_12m:.2f}% p.a.</td></tr>"
    return _td(bank, _TD_TABLE.format(rows=row))


def _attempts(success: bool = True) -> list[SourceAttempt]:
    return [
        SourceAttempt(
            bank=source.bank,
            product_type=source.product_type,
            source_url=source.url,
            success=success,
            records_extracted=1 if success else 0,
        )
        for source in OFFICIAL_SOURCES
    ]


def _run(records, *, captured_at=T0, status="valid", attempts=None) -> CompetitorRateRun:
    return CompetitorRateRun(
        run_id="phase-2d",
        captured_at_utc=captured_at,
        source_urls_attempted=[],
        source_attempts=_attempts() if attempts is None else attempts,
        validation_summary=ValidationSummary(
            status=status, total_records=len(records), valid_records=len(records),
            warning_count=0, error_count=1 if status == "failed" else 0,
        ),
        counts_by_bank_product_type={},
        records=list(records),
    )


def _write(tmp_path, records, **kwargs) -> str:
    path = tmp_path / "latest.json"
    path.write_text(_run(records, **kwargs).model_dump_json(), encoding="utf-8")
    return str(path)


def _config(source_file: str, **overrides) -> CompetitorRatesConfig:
    payload = {
        "enabled": True,
        "source_file": source_file,
        "selected_banks": BANKS,
        "max_staleness_hours": 48,
        "strict": True,
        "mortgage": {"strategy": "market_average", "term_months": 12},
        "term_deposit": {"strategy": "market_average", "term_months": 12},
    }
    payload.update(overrides)
    return CompetitorRatesConfig.model_validate(payload)


def _mortgage(make_record, bank, rate, **overrides):
    return make_record(bank=bank, rate_pct=rate, captured_at_utc=T0, **overrides)


def _deposit(make_record, bank, rate, **overrides):
    payload = {
        "product_type": "term_deposit",
        "product_name": "Term deposit",
        "rate_category": "term_deposit",
        "interest_payment_frequency": "at_maturity",
    }
    payload.update(overrides)
    return make_record(bank=bank, rate_pct=rate, captured_at_utc=T0, **payload)


def _issue_messages(summary) -> list[str]:
    return [issue.message for issue in summary.issues]


# ------------------------------------------------- DATA-006: legacy TD categorisation


@pytest.mark.parametrize("bank", BANKS)
def test_t01_legacy_td_categories_match_the_trusted_collector_path(bank):
    """The legacy fetch must categorise every deposit row exactly as the collector path does."""
    html = (TD_FX / f"{bank.lower()}_term_deposits.html").read_text()
    trusted = COLLECTORS_BY_BANK[bank](fetcher=lambda url: b"").parse_term_deposit_html(html, T0)

    legacy = _fixture_td(bank)

    assert [(r.raw_text_context, r.advertised_rate_pct, r.rate_category) for r in legacy] == [
        (r.raw_text_context, r.advertised_rate_pct, r.rate_category) for r in trusted
    ]


def test_t01_ordinary_bnz_rows_stay_ordinary_term_deposits():
    ordinary = [
        (r.term_months_normalised, r.advertised_rate_pct)
        for r in _fixture_td("BNZ")
        if r.rate_category == "term_deposit"
    ]

    assert (12, 4.05) in ordinary
    assert all(rate < 6.0 for _, rate in ordinary)


def test_t02_pie_return_rows_never_enter_the_ordinary_td_benchmark():
    html = (SPECIALS_FX / "l_pie_and_ordinary.html").read_text()
    records = _td("ASB", html) + _ordinary_td("BNZ", 4.1) + _ordinary_td("Kiwibank", 4.2)
    pie = [r for r in records if r.advertised_rate_pct == 4.70]

    selected = _filter_term_deposit_records(records, _config("unused.json"))

    assert pie and all(r.rate_category == "pie_term_deposit" for r in pie)
    assert {(r.bank, r.advertised_rate_pct) for r in selected} == {
        ("ASB", 4.0), ("BNZ", 4.1), ("Kiwibank", 4.2)
    }


def test_t03_fund_return_rows_are_non_comparable_and_never_benchmark_bnz():
    """Audit (b2): the legacy path benchmarked BNZ's 3-year deposit at a fund's 8.12% return."""
    records = _td("BNZ", (FX / "bnz_term_deposits.html").read_text())
    fund_rows = [r for r in records if "fund" in r.raw_text_context.lower()]

    assert len(fund_rows) == 3
    assert {r.rate_category for r in fund_rows} == {"non_comparable"}
    config = _config("unused.json", selected_banks=["BNZ"], strict=False,
                     term_deposit={"term_months": 36})
    assert _filter_term_deposit_records(records, config) == []


def test_t03_bnz_three_year_benchmark_is_its_ordinary_rate():
    config = _config("unused.json", selected_banks=["BNZ"], term_deposit={"term_months": 36})

    selected = _filter_term_deposit_records(_fixture_td("BNZ"), config)

    assert [(r.rate_category, r.advertised_rate_pct) for r in selected] == [("term_deposit", 4.6)]


def test_t03_a_stored_pre_fix_file_is_fenced_at_resolution(tmp_path):
    """A latest.json written by the old fetch still carries fund rows as term deposits."""
    html = (FX / "bnz_term_deposits.html").read_text()
    unfenced = parse_html_rates("BNZ", ProductType.TERM_DEPOSIT, "https://x.invalid", html, T0)
    assert any(r.rate_category == "term_deposit" and r.advertised_rate_pct == 8.12
               for r in unfenced)
    source = _write(tmp_path, unfenced + _hl_market(), attempts=[])
    config = _config(source, strict=False,
                     term_deposit={"strategy": "bank_specific", "bank": "BNZ", "term_months": 36})

    with pytest.raises(CompetitorRateResolutionError, match="No competitor rates matched"):
        load_and_resolve_competitor_rates(config, now=T0)


# ---------------------------------------------------- strict validation and identity


def test_t04_simultaneous_balance_tiers_are_not_duplicates():
    records = _td("ASB", (SPECIALS_FX / "g_multiple_balance_tiers.html").read_text())
    assert len({(r.min_balance_or_deposit, r.term_months_normalised) for r in records}) == 4

    summary = validate_rate_run(_run(records), strict=True)

    assert not [m for m in _issue_messages(summary) if "Duplicate" in m or "onflict" in m]


def test_t05_payment_frequency_options_are_not_duplicates():
    records = _td("ASB", (SPECIALS_FX / "jk_payment_frequencies.html").read_text())
    assert {r.interest_payment_frequency for r in records} == {"monthly", "at_maturity"}

    summary = validate_rate_run(_run(records), strict=True)

    assert not [m for m in _issue_messages(summary) if "Duplicate" in m or "onflict" in m]


def test_t06_genuinely_conflicting_observations_fail_strict_validation(make_record):
    first = _deposit(make_record, "ASB", 4.00)
    conflicting = _deposit(make_record, "ASB", 4.50, raw_text_context="ASB 12 months 4.50%")

    summary = validate_rate_run(_run([first, conflicting]), strict=True)

    assert summary.status == "failed"
    assert any("Conflicting" in m for m in _issue_messages(summary))


def test_t06_non_comparable_rows_from_the_same_text_with_different_rates_conflict(make_record):
    first = _deposit(make_record, "BNZ", 6.62, rate_category="non_comparable",
                     raw_text_context="3 years | n/a | Conservative Fund")
    second = first.model_copy(update={"advertised_rate_pct": 9.29})

    summary = validate_rate_run(_run([first, second]), strict=True)

    assert summary.status == "failed"


def test_t06_an_exact_repeat_still_fails_and_is_named_a_duplicate(make_record):
    record = _deposit(make_record, "ASB", 4.00)

    summary = validate_rate_run(_run([record, record]), strict=True)

    assert summary.status == "failed"
    assert any("2 identical readings" in m for m in _issue_messages(summary))


def test_t07_strict_validation_passes_complete_fixture_runs():
    records = [r for bank in BANKS for r in _fixture_hl(bank) + _fixture_td(bank)]

    summary = validate_rate_run(_run(records), strict=True)

    assert summary.status != "failed", [i.message for i in summary.issues if i.severity == "error"]


def test_t07_strict_validation_still_rejects_invalid_rates_and_failed_sources(make_record):
    records = [r for bank in BANKS for r in _fixture_td(bank)]
    invalid = records + [_deposit(make_record, "ASB", 16.0, product_name="Bad parse")]

    assert validate_rate_run(_run(invalid), strict=True).status == "failed"
    assert validate_rate_run(_run(records, attempts=_attempts(False)), strict=True).status == (
        "failed"
    )


def test_t07_mortgage_validation_of_the_legacy_fixture_is_unchanged():
    summary = validate_rate_run(load_rate_run(LEGACY_FIXTURE), strict=True)

    assert summary.status == "valid"
    assert summary.issues == []


# --------------------------------------------------------------- staleness: strict and not


def _hl_market() -> list[CompetitorRateRecord]:
    return [r for bank in BANKS for r in _fixture_hl(bank)]


def test_t08_strict_mode_rejects_a_stale_source(tmp_path):
    source = _write(tmp_path, _hl_market() + _all_ordinary_td())

    with pytest.raises(CompetitorRateResolutionError, match="stale"):
        load_and_resolve_competitor_rates(_config(source), now=T0 + timedelta(hours=49))


def _all_ordinary_td() -> list[CompetitorRateRecord]:
    return [r for bank, rate in zip(BANKS, (4.0, 4.1, 4.2, 4.3, 4.4))
            for r in _ordinary_td(bank, rate)]


def test_t09_non_strict_mode_exposes_age_threshold_and_status(tmp_path):
    source = _write(tmp_path, _hl_market() + _all_ordinary_td(), status="warning")

    resolved = load_and_resolve_competitor_rates(
        _config(source, strict=False), now=T0 + timedelta(hours=60)
    )

    assert resolved.stale is True
    assert resolved.strict is False
    assert resolved.source_age_hours == pytest.approx(60.0)
    assert resolved.max_staleness_hours == 48
    assert resolved.source_validation_status == "warning"
    assert any("stale" in w for w in resolved.validation_warnings)
    provenance = resolved.provenance()
    assert provenance["stale"] is True
    assert provenance["source_age_hours"] == pytest.approx(60.0)
    json.dumps(provenance)


def test_t09_fresh_source_is_not_labelled_stale(tmp_path):
    source = _write(tmp_path, _hl_market() + _all_ordinary_td())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0 + timedelta(hours=1))

    assert resolved.stale is False
    assert resolved.source_age_hours == pytest.approx(1.0)


def test_t09_overlay_returns_the_resolution_and_logs_non_strict_warnings(tmp_path, caplog):
    config = _engine_config(tmp_path, strict=False, max_staleness_hours=1)

    applied, resolution = apply_competitor_rates_with_resolution(config)
    with caplog.at_level(logging.WARNING, logger="balance_pricing_engine.competitor_rates.apply"):
        plain = apply_competitor_rates_to_config(config)

    assert resolution is not None and resolution.stale is True
    assert plain.market == applied.market
    assert any("stale" in record.getMessage() for record in caplog.records)
    # Numerical configuration carries no warning text.
    assert "stale" not in json.dumps(revalidation_payload(applied)["market"])


def test_t09_disabled_overlay_returns_the_same_config_and_no_resolution():
    config = _small_engine_config()

    applied, resolution = apply_competitor_rates_with_resolution(config)

    assert applied is config
    assert resolution is None


def _small_engine_config() -> EngineConfig:
    config = load_config("configs/base.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 300, "n_deposit_customers": 300}
    )
    return config.model_copy(update={"simulation": simulation})


def _engine_config(tmp_path, *, records=None, **competitor) -> EngineConfig:
    run = load_rate_run(LEGACY_FIXTURE)
    if records is not None:
        run = run.model_copy(update={"records": records})
    source = tmp_path / "latest.json"
    source.write_text(run.model_dump_json(), encoding="utf-8")
    raw = _small_engine_config().model_dump()
    raw["competitor_rates"] = {
        **raw["competitor_rates"],
        "enabled": True,
        "source_file": str(source),
        "max_staleness_hours": 100_000,
        "strict": True,
        **competitor,
    }
    return EngineConfig.model_validate(raw)


def _competitor_gate(decision):
    return next(g for g in decision.governance.gates if g.code == "COMPETITOR_RATE_FRESHNESS")


def test_t10_pricing_service_surfaces_a_non_strict_stale_source(tmp_path):
    config = _engine_config(tmp_path, strict=False, max_staleness_hours=1)

    decision = PricingService().run_scenario(PricingScenarioRequest(config=config))

    gate = _competitor_gate(decision)
    assert decision.status is DecisionStatus.REVIEW_REQUIRED
    assert gate.status.value == "WARN"
    assert gate.evidence["competitor_source"]["stale"] is True
    assert gate.evidence["competitor_source"]["max_staleness_hours"] == 1
    assert gate.evidence["competitor_source"]["source_age_hours"] > 1
    recorded = next(
        g for g in decision.decision_record.governance_gates
        if g["code"] == "COMPETITOR_RATE_FRESHNESS"
    )
    assert recorded["evidence"]["competitor_source"]["stale"] is True
    assert recorded["evidence"]["competitor_source"]["source_file_name"] == "latest.json"
    assert len(recorded["evidence"]["competitor_source"]["source_sha256"]) == 64
    assert str(tmp_path) not in json.dumps(recorded)


def test_t10_pricing_service_surfaces_an_uncertain_td_special(tmp_path):
    run = load_rate_run(LEGACY_FIXTURE)
    uncertain = _td("ASB", (SPECIALS_FX / "b_special_same_term.html").read_text())
    records = [r for r in run.records if not (r.bank == "ASB" and r.product_type == "term_deposit")]
    config = _engine_config(tmp_path, records=records + uncertain)

    decision = PricingService().run_scenario(PricingScenarioRequest(config=config))

    gate = _competitor_gate(decision)
    assert decision.status is DecisionStatus.REVIEW_REQUIRED
    assert any("uncertain" in w for w in gate.evidence["warnings"])


def test_t10_pricing_service_records_provenance_for_a_clean_source(tmp_path):
    config = _engine_config(tmp_path)

    decision = PricingService().run_scenario(PricingScenarioRequest(config=config))

    gate = _competitor_gate(decision)
    assert gate.status.value == "PASS"
    assert gate.evidence["competitor_source"]["stale"] is False
    assert gate.evidence["competitor_source"]["mortgage_banks"] == [
        "ANZ", "ASB", "BNZ", "Kiwibank"
    ]


# --------------------------------------------------------- DATA-018: equal bank weights


def test_t11_market_aggregates_use_one_representative_per_bank(make_record):
    records = [
        _mortgage(make_record, "ANZ", 4.79),
        _mortgage(make_record, "ANZ", 4.99, product_name="Fixed Special (package)"),
        _mortgage(make_record, "ASB", 4.89),
        _mortgage(make_record, "BNZ", 5.00),
    ]

    assert _resolve_strategy(records, "market_average", None) == pytest.approx(
        (4.79 + 4.89 + 5.00) / 3
    )


def test_t12_another_anz_row_does_not_increase_anz_weight(make_record):
    base = [
        _mortgage(make_record, "ANZ", 4.79),
        _mortgage(make_record, "ASB", 4.89),
        _mortgage(make_record, "BNZ", 4.95),
    ]
    extra = _mortgage(make_record, "ANZ", 4.99, product_name="Fixed Special (package)")

    before = _resolve_strategy(base, "market_average", None)
    after = _resolve_strategy(base + [extra], "market_average", None)

    assert after == pytest.approx(before)
    # The former row-weighted figure would have moved.
    assert (4.79 + 4.89 + 4.95 + 4.99) / 4 != pytest.approx(before)


def test_t12_audit_example_changes_only_through_anz_representative(make_record):
    """Audit (c): ANZ 4.79 and 4.69 plus ASB 4.89 -- ANZ is represented once, by 4.69."""
    records = [
        _mortgage(make_record, "ANZ", 4.79),
        _mortgage(make_record, "ASB", 4.89),
        _mortgage(make_record, "ANZ", 4.69, product_name="Fixed Special (package)"),
    ]

    assert _resolve_strategy(records, "market_average", None) == pytest.approx((4.69 + 4.89) / 2)
    assert _resolve_strategy(records, "market_median", None) == pytest.approx((4.69 + 4.89) / 2)


def test_t13_representative_selection_is_input_order_independent(make_record):
    records = [
        _mortgage(make_record, "ANZ", 4.79, product_name="Fixed Special"),
        _mortgage(make_record, "ANZ", 4.79, product_name="Fixed Special (package)",
                  lvr_condition="min_20_percent_equity"),
        _mortgage(make_record, "ANZ", 4.99, product_name="Fixed Standard"),
        _mortgage(make_record, "ASB", 4.89),
        _mortgage(make_record, "BNZ", 4.59),
    ]
    results = {
        strategy: {
            _resolve_strategy(list(order), strategy, "ANZ" if strategy == "bank_specific" else None)
            for order in itertools.permutations(records)
        }
        for strategy in ("market_average", "market_median", "market_min", "bank_specific")
    }

    assert all(len(values) == 1 for values in results.values()), results


def test_t13_representatives_are_reported_deterministically(tmp_path, make_record):
    records = [
        _mortgage(make_record, bank, rate, product_name=name, lvr_condition=lvr)
        for bank, rate, name, lvr in [
            ("ANZ", 4.79, "Fixed Special", "min_20_percent_equity"),
            ("ANZ", 4.79, "Fixed Special (package)", "min_20_percent_equity"),
            ("ASB", 4.89, "Fixed Special", "min_20_percent_equity"),
            ("BNZ", 4.59, "Fixed Special", "min_20_percent_equity"),
        ]
    ]
    keys = set()
    for order in itertools.permutations(records):
        source = _write(tmp_path, list(order) + _all_ordinary_td())
        resolved = load_and_resolve_competitor_rates(_config(source), now=T0)
        keys.add(tuple(r.observation_key() for r in resolved.mortgage_bank_representatives))

    assert len(keys) == 1


def test_t14_resolved_mortgage_average_matches_the_per_bank_policy(tmp_path, make_record):
    records = [
        _mortgage(make_record, "ANZ", 4.79, lvr_condition="min_20_percent_equity"),
        _mortgage(make_record, "ANZ", 5.29, product_name="Fixed Special 70-80 LVR",
                  lvr_condition="min_20_percent_equity"),
        _mortgage(make_record, "ANZ", 5.49, product_name="Fixed Special 80-90 LVR",
                  lvr_condition="min_20_percent_equity"),
        _mortgage(make_record, "ASB", 4.89, lvr_condition="min_20_percent_equity"),
        _mortgage(make_record, "BNZ", 4.95, lvr_condition="min_20_percent_equity"),
    ]
    source = _write(tmp_path, records + _all_ordinary_td())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0)

    assert resolved.mortgage_reference_rate == pytest.approx((4.79 + 4.89 + 4.95) / 3 / 100)
    assert [(r.bank, r.advertised_rate_pct) for r in resolved.mortgage_bank_representatives] == [
        ("ANZ", 4.79), ("ASB", 4.89), ("BNZ", 4.95)
    ]
    assert resolved.mortgage_banks == ["ANZ", "ASB", "BNZ"]


def test_t15_mortgage_median_uses_one_value_per_bank(make_record):
    records = [
        _mortgage(make_record, "ANZ", 4.50),
        _mortgage(make_record, "ANZ", 4.60, product_name="Fixed Special B"),
        _mortgage(make_record, "ANZ", 4.70, product_name="Fixed Special C"),
        _mortgage(make_record, "ASB", 5.00),
        _mortgage(make_record, "BNZ", 5.10),
    ]

    assert _resolve_strategy(records, "market_median", None) == pytest.approx(5.00)


def test_t16_mortgage_leader_is_the_lowest_eligible_rate(make_record):
    records = [
        _mortgage(make_record, "ANZ", 4.79),
        _mortgage(make_record, "ANZ", 4.49, product_name="Fixed Special B"),
        _mortgage(make_record, "ASB", 4.59),
    ]

    assert _resolve_strategy(records, "market_min", None) == pytest.approx(4.49)
    assert _resolve_strategy(records, "bank_specific", "ANZ") == pytest.approx(4.49)


def test_t17_td_leader_is_the_highest_eligible_rate(make_record):
    records = [
        _deposit(make_record, "ANZ", 4.00),
        _deposit(make_record, "ANZ", 4.30, product_name="Term deposit B"),
        _deposit(make_record, "ASB", 4.20),
    ]

    assert _resolve_strategy(records, "market_max", None) == pytest.approx(4.30)
    assert _resolve_strategy(records, "market_average", None) == pytest.approx((4.30 + 4.20) / 2)


def test_t17_mixed_product_records_are_refused(make_record):
    records = [_mortgage(make_record, "ANZ", 4.79), _deposit(make_record, "ASB", 4.20)]

    with pytest.raises(CompetitorRateResolutionError, match="one product"):
        _resolve_strategy(records, "market_average", None)


# ------------------------------------------------------------- Phase 2b specials policy


def _special_market(asb_rows: str) -> list[CompetitorRateRecord]:
    asb = _td("ASB", _TD_TABLE.format(rows=asb_rows))
    return asb + _ordinary_td("BNZ", 4.10) + _ordinary_td("Kiwibank", 4.20)


_ASB_ORDINARY_12M = "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>"


def test_t18_restricted_td_special_is_excluded_without_degrading(tmp_path):
    records = _td("ASB", (SPECIALS_FX / "p_restricted_special.html").read_text())
    records += _ordinary_td("BNZ", 4.10) + _ordinary_td("Kiwibank", 4.20)
    source = _write(tmp_path, records + _hl_market())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0)

    asb = next(r for r in resolved.term_deposit_records if r.bank == "ASB")
    assert asb.advertised_rate_pct == 4.00
    assessment = next(a for a in resolved.deposit_special_assessments if a["bank"] == "ASB")
    assert assessment["status"] == "not_comparable"
    assert not [w for w in resolved.resolution_warnings if "uncertain" in w]


def test_t19_comparable_td_special_represents_its_bank(tmp_path):
    special = (
        "<tr><td>12 Months Special</td><td>4.50% p.a. Available to all customers</td></tr>"
    )
    source = _write(tmp_path, _special_market(_ASB_ORDINARY_12M + special) + _hl_market())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0)

    asb = next(r for r in resolved.term_deposit_records if r.bank == "ASB")
    assert (asb.rate_category, asb.advertised_rate_pct) == ("special", 4.50)
    assert resolved.term_deposit_reference_rate == pytest.approx((4.50 + 4.10 + 4.20) / 3 / 100)


def test_t19_a_comparable_special_below_the_ordinary_rate_does_not_displace_it(tmp_path):
    special = (
        "<tr><td>12 Months Special</td><td>3.90% p.a. Available to all customers</td></tr>"
    )
    source = _write(tmp_path, _special_market(_ASB_ORDINARY_12M + special) + _hl_market())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0)

    asb = next(r for r in resolved.term_deposit_records if r.bank == "ASB")
    assert (asb.rate_category, asb.advertised_rate_pct) == ("term_deposit", 4.00)


def test_t19_uncertain_td_special_is_excluded_and_warned(tmp_path):
    special = "<tr><td>12 Months Special</td><td>4.50% p.a.</td></tr>"
    source = _write(tmp_path, _special_market(_ASB_ORDINARY_12M + special) + _hl_market())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0)

    asb = next(r for r in resolved.term_deposit_records if r.bank == "ASB")
    assert asb.advertised_rate_pct == 4.00
    assert any("uncertain" in w and "ASB" in w for w in resolved.resolution_warnings)
    assert any("uncertain" in w for w in resolved.validation_warnings)


def test_t19_specials_are_not_assessed_for_a_pie_benchmark(tmp_path):
    special = "<tr><td>12 Months Special</td><td>4.50% p.a.</td></tr>"
    pie = _td("ASB", (SPECIALS_FX / "l_pie_and_ordinary.html").read_text())
    records = _special_market(_ASB_ORDINARY_12M + special) + pie
    records += [r.model_copy(update={"rate_category": "pie_term_deposit"})
                for r in _ordinary_td("BNZ", 4.6) + _ordinary_td("Kiwibank", 4.65)]
    source = _write(tmp_path, records + _hl_market())

    resolved = load_and_resolve_competitor_rates(
        _config(source, term_deposit={"product_type": "pie_term_deposit"}), now=T0
    )

    assert {r.rate_category for r in resolved.term_deposit_records} == {"pie_term_deposit"}
    assert resolved.resolution_warnings == []


# ------------------------------------------------------ PR #75: specials-only selection


def _eligible_special(make_record, rate, *, name="Special A", frequency="monthly", **overrides):
    return _deposit(
        make_record, "ANZ", rate, product_name=name, rate_category="special",
        interest_payment_frequency=frequency,
        raw_text_context="12 months special. Available to all customers.",
        **overrides,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_pr75_t01_t02_highest_special_wins_independent_of_order(make_record, reverse):
    records = [_eligible_special(make_record, 4.0),
               _eligible_special(make_record, 5.0, name="Special B")]
    if reverse:
        records.reverse()

    selected = _filter_term_deposit_records(records, _config("unused.json"))

    assert len(selected) == 1
    assert selected[0].advertised_rate_pct == 5.0
    assert selected[0].rate_category == "special"


@pytest.mark.parametrize("preference, expected", [
    (["monthly", "at_maturity"], 4.0), (["at_maturity", "monthly"], 5.0),
])
def test_pr75_t03_special_frequency_priority_beats_rate(make_record, preference, expected):
    records = [_eligible_special(make_record, 4.0),
               _eligible_special(make_record, 5.0, name="Special B", frequency="at_maturity")]
    config = _config("unused.json", term_deposit={
        "interest_payment_frequency_preference": preference,
    })

    selected = _filter_term_deposit_records(records, config)

    assert selected[0].advertised_rate_pct == expected


@pytest.mark.parametrize("preference", [["monthly"], ["annually"]])
@pytest.mark.parametrize("reverse", [False, True])
def test_pr75_t04_equal_special_rates_use_observation_key(make_record, reverse, preference):
    records = [_eligible_special(make_record, 5.0),
               _eligible_special(make_record, 5.0, name="Special B")]
    expected = min(records, key=lambda record: record.observation_key())

    selected = _deposit_representative([], records[::-1] if reverse else records, preference)

    assert selected is expected


@pytest.mark.parametrize("frequency", ["monthly", "at_maturity+monthly", "quarterly"])
@pytest.mark.parametrize("reverse", [False, True])
def test_pr75_t05_ordinary_frequency_ties_keep_observation_key(make_record, frequency, reverse):
    records = [_deposit(make_record, "ANZ", rate, product_name=name,
                        interest_payment_frequency=frequency)
               for rate, name in [(4.0, "Deposit A"), (5.0, "Deposit B")]]
    expected = min(records, key=lambda record: record.observation_key())

    selected = _deposit_representative(records[::-1] if reverse else records, [], ["monthly"])

    assert selected is expected


@pytest.mark.parametrize("rate, frequency, replaces", [
    (3.9, "monthly", False), (4.0, "monthly", False), (5.0, "monthly", True),
    (5.0, "at_maturity", False), (5.0, "at_maturity+monthly", False),
])
def test_pr75_t06_special_replaces_only_higher_at_same_frequency(
    make_record, rate, frequency, replaces,
):
    ordinary = _deposit(make_record, "ANZ", 4.0, interest_payment_frequency="monthly")
    special = _eligible_special(make_record, rate, frequency=frequency)

    selected = _deposit_representative([ordinary], [special], ["monthly", "at_maturity"])

    assert selected is (special if replaces else ordinary)


@pytest.mark.parametrize("evidence", [
    "12 months special.", "12 months special. Available to all new customers.",
])
def test_pr75_t07_ineligible_special_cannot_be_reference(make_record, tmp_path, evidence):
    eligible = _eligible_special(make_record, 4.0)
    excluded = _eligible_special(make_record, 8.0, name="Special B").model_copy(
        update={"raw_text_context": evidence},
    )
    others = _ordinary_td("BNZ", 4.1) + _ordinary_td("Kiwibank", 4.2)
    source = _write(tmp_path, [eligible, excluded] + others + _hl_market())
    config = _config(source, term_deposit={"strategy": "bank_specific", "bank": "ANZ"})

    resolved = load_and_resolve_competitor_rates(config, now=T0)

    assert resolved.term_deposit_reference_rate == 0.04
    excluded_assessment = next(a for a in resolved.deposit_special_assessments
                               if a["rate_pct"] == 8.0)
    assert excluded_assessment["status"] in {"uncertain", "not_comparable"}


@pytest.mark.parametrize("status", ["warning", "failed"])
def test_pr75_t07_untrusted_special_cannot_supply_reference(make_record, tmp_path, status):
    records = [_eligible_special(make_record, 4.0),
               _eligible_special(make_record, 5.0, name="Special B", validation_status=status)]
    source = _write(tmp_path, records + _ordinary_td("BNZ", 4.1)
                    + _ordinary_td("Kiwibank", 4.2) + _hl_market())
    config = _config(source, term_deposit={"strategy": "bank_specific", "bank": "ANZ"})

    resolved = load_and_resolve_competitor_rates(config, now=T0)

    assert resolved.term_deposit_reference_rate == 0.04
    assert any("untrusted" in warning for warning in resolved.resolution_warnings)


@pytest.mark.parametrize("strategy, expected", [
    ("bank_specific", 0.05), ("market_max", 0.05),
    ("market_median", 0.042), ("market_average", (5.0 + 4.1 + 4.2) / 3 / 100),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_pr75_t08_selected_special_reaches_strategies(
    make_record, tmp_path, strategy, expected, reverse,
):
    records = [_eligible_special(make_record, 4.0),
               _eligible_special(make_record, 5.0, name="Special B")]
    records += _ordinary_td("BNZ", 4.1) + _ordinary_td("Kiwibank", 4.2) + _hl_market()
    source = _write(tmp_path, records[::-1] if reverse else records)
    config = _config(source, term_deposit={"strategy": strategy, "bank": "ANZ"})

    resolved = load_and_resolve_competitor_rates(config, now=T0)

    assert resolved.term_deposit_reference_rate == expected
    assert len(resolved.term_deposit_records) == 3
    assert next(r for r in resolved.term_deposit_records
                if r.bank == "ANZ").advertised_rate_pct == 5.0


@pytest.mark.parametrize("strategy", ["market_average", "market_max", "market_median"])
@pytest.mark.parametrize("strict", [False, True])
def test_pr75_t09_specials_do_not_inflate_bank_coverage(make_record, tmp_path, strategy, strict):
    records = [_eligible_special(make_record, 4.0),
               _eligible_special(make_record, 5.0, name="Special B")]
    source = _write(tmp_path, records + _ordinary_td("BNZ", 4.1) + _hl_market())
    config = _config(source, strict=strict, term_deposit={"strategy": strategy})

    with pytest.raises(CompetitorRateResolutionError, match="2 eligible banks; minimum required: 3"):
        load_and_resolve_competitor_rates(config, now=T0)


@pytest.mark.parametrize("frequencies, preference, expected_name", [
    (("monthly", "at_maturity+monthly"), ["monthly"], "Special A"),
    (("at_maturity+monthly", "quarterly+monthly"), ["monthly"], "Special B"),
    (("at_maturity+monthly", "quarterly"), ["monthly", "quarterly"], "Special A"),
    (("quarterly", "annually"), ["monthly"], "Special B"),
    (("quarterly", "quarterly"), ["monthly"], "Special B"),
    (("quarterly", "annually"), [], "Special B"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_pr75_t10_special_combined_frequency_and_fallback(
    make_record, frequencies, preference, expected_name, reverse,
):
    records = [_eligible_special(make_record, rate, name=name, frequency=frequency)
               for rate, name, frequency in zip(
                   [4.0, 5.0], ["Special A", "Special B"], frequencies,
               )]

    selected = _deposit_representative([], records[::-1] if reverse else records, preference)

    assert selected.product_name == expected_name


# ---------------------------------------------------- reduced assurance and coverage


def test_t20_quarantined_or_failed_observations_never_contribute(tmp_path, make_record):
    records = [
        _mortgage(make_record, "ANZ", 3.99, validation_status="warning"),
        _mortgage(make_record, "ANZ", 4.79),
        _mortgage(make_record, "ASB", 1.00, validation_status="failed"),
        _mortgage(make_record, "ASB", 4.89),
        _mortgage(make_record, "BNZ", 4.95),
    ]
    source = _write(tmp_path, records + _all_ordinary_td())

    resolved = load_and_resolve_competitor_rates(
        _config(source, mortgage={"strategy": "market_min"}), now=T0
    )

    assert resolved.mortgage_reference_rate == pytest.approx(0.0479)
    assert any("excluded" in w.lower() for w in resolved.resolution_warnings)


def test_t20_legacy_source_discloses_missing_trust_evidence(tmp_path):
    source = _write(tmp_path, _hl_market() + _all_ordinary_td())

    resolved = load_and_resolve_competitor_rates(_config(source), now=T0)

    disclosed = " ".join(resolved.reduced_assurance).lower()
    for missing in ("quarantine", "peer", "operational"):
        assert missing in disclosed


def test_t21_under_covered_market_aggregates_are_refused(tmp_path, make_record):
    records = [_mortgage(make_record, "ANZ", 4.79), _mortgage(make_record, "ASB", 4.89)]
    source = _write(tmp_path, records + _all_ordinary_td())

    for strict in (True, False):
        with pytest.raises(CompetitorRateResolutionError, match="Insufficient competitor"):
            load_and_resolve_competitor_rates(_config(source, strict=strict), now=T0)


def test_t21_extra_rows_from_one_bank_do_not_count_as_coverage(tmp_path, make_record):
    records = [
        _mortgage(make_record, "ANZ", 4.79),
        _mortgage(make_record, "ANZ", 4.89, product_name="Fixed Special B"),
        _mortgage(make_record, "ANZ", 4.99, product_name="Fixed Special C"),
        _mortgage(make_record, "ASB", 4.89),
    ]
    source = _write(tmp_path, records + _all_ordinary_td())

    with pytest.raises(CompetitorRateResolutionError, match="2 eligible banks"):
        load_and_resolve_competitor_rates(_config(source), now=T0)


def test_t21_bank_specific_and_explicit_min_banks_are_honoured(tmp_path, make_record):
    records = [_mortgage(make_record, "ANZ", 4.79), _mortgage(make_record, "ASB", 4.89)]
    source = _write(tmp_path, records + _all_ordinary_td())

    specific = load_and_resolve_competitor_rates(
        _config(source, mortgage={"strategy": "bank_specific", "bank": "ASB"}), now=T0
    )
    relaxed = load_and_resolve_competitor_rates(_config(source), now=T0, min_banks=2)

    assert specific.mortgage_reference_rate == pytest.approx(0.0489)
    assert relaxed.mortgage_reference_rate == pytest.approx((4.79 + 4.89) / 2 / 100)


def test_t21_overlay_uses_the_configured_pricing_intelligence_min_banks(tmp_path):
    config = _engine_config(tmp_path)
    raw = config.model_dump()
    raw["competitor_rates"]["selected_banks"] = ["ANZ", "ASB"]
    two_banks = EngineConfig.model_validate(raw)

    with pytest.raises(CompetitorRateResolutionError, match="minimum required: 3"):
        apply_competitor_rates_to_config(two_banks)

    raw["pricing_intelligence"] = {**(raw.get("pricing_intelligence") or {}), "min_banks": 2}
    relaxed = EngineConfig.model_validate(raw)
    assert apply_competitor_rates_to_config(relaxed).market.competitor_loan_rate == (
        pytest.approx((5.5 + 5.7) / 2 / 100)
    )


# ------------------------------------------------------------------ SEC-010: CSV export


_MALICIOUS = {
    "=HYPERLINK(\"https://example.invalid\",\"x\")": "'=HYPERLINK(\"https://example.invalid\",\"x\")",
    "+SUM(1,2)": "'+SUM(1,2)",
    "-2+3+cmd|' /C calc'!A0": "'-2+3+cmd|' /C calc'!A0",
    "@SUM(1,2)": "'@SUM(1,2)",
    "  =1+1": "'  =1+1",
    "\t=1+1": "'\t=1+1",
    "\r=1+1": "'\r=1+1",
    "\n=1+1": "'\n=1+1",
    "\t": "'\t",
    "＝1+1": "'＝1+1",
}


@pytest.mark.parametrize(("raw", "expected"), sorted(_MALICIOUS.items()))
def test_t22_t23_t24_formula_prefixes_are_neutralised(raw, expected):
    assert neutralise_csv_cell(raw) == expected
    assert neutralise_csv_cell(expected) == expected  # never double-escaped


@pytest.mark.parametrize("value", [
    "-0.84", "+4.50", "-12", "1 year | 4.05% | Rate", "ANZ", "", "'=already", 4.79, -0.84, None,
])
def test_t25_legitimate_values_and_numeric_negatives_are_preserved(value):
    assert neutralise_csv_cell(value) == value


def _malicious_run(make_record) -> CompetitorRateRun:
    records = [
        _deposit(
            make_record, "ASB", rate, product_name=name, raw_text_context=text,
            special_conditions=" @SUM(1,2)", effective_date_text="-0.84",
        )
        for rate, name, text in [
            (4.0, "=HYPERLINK(\"https://example.invalid\",\"x\")", "=1+1"),
            (-0.84, "+SUM(1,2)", "\t=cmd|' /C calc'!A0"),
        ]
    ]
    return _run(records).model_copy(update={"run_id": "=evil"})


def test_t22_to_t25_latest_csv_has_no_live_formula_cells(tmp_path, make_record):
    write_rate_run(_malicious_run(make_record), tmp_path)

    frame = pd.read_csv(tmp_path / "latest.csv", dtype=str, keep_default_na=False)

    for column in frame.columns:
        for cell in frame[column]:
            stripped = cell.lstrip(" \t\r\n")
            assert not (stripped[:1] in "=+-@＝＋－＠" and stripped
                        and cell[:1] != "'" and not _is_number(cell)), (column, cell)
    assert list(frame["advertised_rate_pct"]) == ["4.0", "-0.84"]
    assert list(frame["effective_date_text"]) == ["-0.84", "-0.84"]
    assert list(frame["product_name"])[1] == "'+SUM(1,2)"


def _is_number(cell: str) -> bool:
    try:
        float(cell)
    except ValueError:
        return False
    return True


def test_t26_json_artifacts_keep_original_source_values(tmp_path, make_record):
    run = _malicious_run(make_record)

    write_rate_run(run, tmp_path)

    latest = (tmp_path / "latest.json").read_text(encoding="utf-8")
    history = next((tmp_path / "history").glob("*.json")).read_text(encoding="utf-8")
    assert latest == history == run.model_dump_json(indent=2)
    assert load_rate_run(tmp_path / "latest.json") == run


def test_t27_csv_export_does_not_mutate_the_source_objects(tmp_path, make_record):
    run = _malicious_run(make_record)
    snapshot = copy.deepcopy(run.model_dump())

    write_rate_run(run, tmp_path)

    assert run.model_dump() == snapshot


# ----------------------------------------------------------- CLI and unchanged economics


def test_t28_cli_apply_discloses_stale_non_strict_data_in_output_and_yaml(tmp_path, capsys):
    config = _engine_config(tmp_path, strict=False, max_staleness_hours=1)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    code = cli.main(["apply", "--config", str(config_path), "--write",
                     "--output-dir", str(tmp_path / "generated")])

    output = capsys.readouterr().out
    assert code == 0
    assert "STALE" in output
    assert "non-strict" in output.lower()
    generated = next((tmp_path / "generated").glob("*.yaml"))
    text = generated.read_text(encoding="utf-8")
    assert text.startswith("# WARNING")
    assert load_config(generated).market == apply_competitor_rates_to_config(config).market


def test_t28_cli_apply_resolves_once_and_prints_contributing_banks(tmp_path, capsys):
    config = _engine_config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    assert cli.main(["apply", "--config", str(config_path)]) == 0

    output = capsys.readouterr().out
    assert "Mortgage banks contributing: ANZ, ASB, BNZ, Kiwibank" in output
    assert "STALE" not in output


def test_t29_one_row_per_bank_inputs_keep_their_former_rates(tmp_path):
    """The legacy fixture has one qualifying row per bank: nothing about its pricing moves."""
    config = _engine_config(tmp_path)

    applied = apply_competitor_rates_to_config(config)

    # Exactly the values master resolved for this fixture (``sum(rates) / len(rates)``).
    # Python 3.12 improved float summation; compare with master's arithmetic on this Python.
    expected_loan = sum([5.5, 5.7, 5.6, 5.8]) / 4 / 100
    expected_deposit = sum([4.1, 4.3, 4.4, 4.2, 4.5]) / 5 / 100
    assert applied.market.competitor_loan_rate == pytest.approx(expected_loan, abs=0)
    assert applied.market.competitor_deposit_rate == pytest.approx(expected_deposit, abs=0)
    assert applied.market.home_loan_reference_rate == applied.market.competitor_loan_rate
    untouched = {k: v for k, v in revalidation_payload(applied).items() if k != "market"}
    assert untouched == {k: v for k, v in revalidation_payload(config).items() if k != "market"}


def test_t21_a_non_positive_min_banks_is_refused(tmp_path):
    source = _write(tmp_path, _hl_market() + _all_ordinary_td())

    with pytest.raises(CompetitorRateResolutionError, match="at least 1"):
        load_and_resolve_competitor_rates(_config(source), now=T0, min_banks=0)
