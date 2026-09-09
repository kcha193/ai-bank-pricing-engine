"""run_triggered_scenarios(): the auto-triggered pricing-scenario comparison.

Lives outside pricing_intelligence/ (see the module's own docstring), so
these tests exercise it directly against a small/fast EngineConfig -- no
network, no live collection.
"""

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_scenario_trigger import (
    ScenarioComparison,
    format_scenario_block,
    run_triggered_scenarios,
)


def _small_config():
    config = load_config("configs/base.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 300, "n_deposit_customers": 300}
    )
    return config.model_copy(update={"simulation": simulation})


def _record(bank, rate_pct):
    from datetime import datetime, timezone

    return CompetitorRateRecord(
        bank=bank,
        product_type="home_loan",
        product_name="Fixed Special",
        rate_category="special",
        term_label_raw="12 months",
        term_months_normalised=12,
        advertised_rate_pct=rate_pct,
        source_url="https://example.com",
        captured_at_utc=datetime.now(timezone.utc),
        raw_text_context="x",
        raw_html_sha256="a" * 64,
    )


def _snapshot():
    records = [
        _record("ANZ", 4.79),
        _record("BNZ", 4.85),
        _record("Westpac", 4.85),
        _record("ASB", 4.89),
        _record("Kiwibank", 4.89),
    ]
    return build_market_snapshot(records)


def _deposit_record(bank, rate_pct):
    from datetime import datetime, timezone

    return CompetitorRateRecord(
        bank=bank,
        product_type="term_deposit",
        product_name="Term Deposit",
        rate_category="term_deposit",
        term_label_raw="12 months",
        term_months_normalised=12,
        advertised_rate_pct=rate_pct,
        source_url="https://example.com",
        captured_at_utc=datetime.now(timezone.utc),
        raw_text_context="x",
        raw_html_sha256="b" * 64,
    )


def _deposit_snapshot():
    records = [
        _deposit_record("ANZ", 4.20),
        _deposit_record("BNZ", 4.30),
        _deposit_record("Westpac", 4.30),
        _deposit_record("ASB", 4.35),
        _deposit_record("Kiwibank", 4.35),
    ]
    return build_market_snapshot(
        records, product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
    )


def test_run_triggered_scenarios_returns_the_documented_rows():
    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_snapshot(), max_boundary_expansions=0
    )

    assert isinstance(result, ScenarioComparison)
    assert result.term_months == 12
    labels = [row.label for row in result.rows]
    assert labels == ["No change", "Match median", "Match leader", "Leader -5bps", "Model optimum"]


def test_model_optimum_matches_the_match_median_scenario():
    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_snapshot(), max_boundary_expansions=0
    )

    match_median = next(row for row in result.rows if row.label == "Match median")
    assert result.model_optimum == match_median.__class__(
        label="Model optimum",
        strategy="MODEL_OPTIMUM",
        competitor_rate=match_median.competitor_rate,
        our_rate=match_median.our_rate,
        objective=match_median.objective,
    )


def test_match_leader_uses_the_lowest_advertised_rate():
    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_snapshot(), max_boundary_expansions=0
    )

    match_leader = next(row for row in result.rows if row.label == "Match leader")
    assert match_leader.competitor_rate == 0.0479  # ANZ, the leader


def test_rates_are_fractions_not_percentages():
    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_snapshot(), max_boundary_expansions=0
    )

    for row in result.rows:
        assert 0 < row.our_rate < 1, "our_rate must be a fraction (0.0549), not a percent (5.49)"


def test_as_dicts_round_trips_through_json():
    import json

    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_snapshot(), max_boundary_expansions=0
    )

    payload = json.dumps(result.as_dicts())
    restored = json.loads(payload)
    assert len(restored) == len(result.rows)
    assert restored[0]["label"] == "No change"


def test_format_scenario_block_is_compact_and_carries_the_disclaimer():
    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_snapshot(), max_boundary_expansions=0
    )

    block = format_scenario_block(result)

    assert "PRICING ENGINE" in block
    assert "Current rate:" in block
    assert "Match median:" in block
    assert "Match leader:" in block
    assert "Model optimum:" in block
    assert "Decision support only -- no automatic pricing action taken." in block
    # Deliberately terse: no full grid, no Leader -5bps row in the alert block.
    assert "Leader -5bps" not in block
    assert len(block.splitlines()) < 12


def test_a_term_deposit_snapshot_overlays_the_deposit_side_not_the_loan_side():
    """A TERM_DEPOSIT snapshot must never be passed as apply_market_strategy_to_config's
    loan-side `snapshot` argument -- that raises MarketStrategyError, since the loan
    side requires a HOME_LOAN market view for every non-MANUAL strategy. It must
    instead drive the deposit-side overlay (deposit_strategy/deposit_snapshot)."""
    result = run_triggered_scenarios(
        _small_config(), term_months=12, snapshot=_deposit_snapshot(), max_boundary_expansions=0
    )

    assert isinstance(result, ScenarioComparison)
    labels = [row.label for row in result.rows]
    assert labels == ["No change", "Match median", "Match leader", "Leader -5bps", "Model optimum"]

    match_leader = next(row for row in result.rows if row.label == "Match leader")
    # Deposits rank highest-first -- ASB/Kiwibank's 4.35% leads, not ANZ's 4.20%.
    assert match_leader.competitor_rate == 0.0435
    for row in result.rows:
        assert 0 < row.our_rate < 1, "our_rate must be a fraction, not a percent"

    no_change = next(row for row in result.rows if row.label == "No change")
    assert no_change.competitor_rate == _small_config().market.competitor_deposit_rate, (
        "the loan-side competitor rate must be untouched by a deposit scenario"
    )
