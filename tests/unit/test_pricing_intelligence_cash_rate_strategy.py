"""Cash-rate strategy: the seam between RBNZ OCR intelligence and the optimiser.

MANUAL is asserted as strictly as RBNZ_OCR: every pre-existing scenario
depends on it returning the config completely unchanged.
"""

from datetime import datetime, timezone

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.rbnz import OcrObservation, OcrSeries
from balance_pricing_engine.pricing_intelligence.strategy import (
    CashRateStrategy,
    MarketStrategyError,
    apply_cash_rate_strategy_to_config,
    resolve_cash_rate,
)

CAPTURED_AT = datetime(2026, 9, 3, tzinfo=timezone.utc)


@pytest.fixture
def ocr_series():
    return OcrSeries(
        observations=(
            OcrObservation("Aug-2026", 2.50, 2.98),
            OcrObservation("Jul-2026", 2.50, 2.85),
            OcrObservation("Jun-2026", 2.25, 2.68),
        ),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=CAPTURED_AT,
    )


@pytest.fixture
def config():
    return load_config("configs/base.yaml")


# --------------------------------------------------------------- resolution


def test_manual_strategy_uses_the_supplied_rate(ocr_series):
    resolved = resolve_cash_rate(CashRateStrategy.MANUAL, manual_rate=0.0425)

    assert resolved.cash_rate == 0.0425
    assert resolved.as_of_period is None
    assert "not consulted" in resolved.rationale


def test_manual_strategy_needs_a_rate():
    with pytest.raises(MarketStrategyError, match="requires manual_rate"):
        resolve_cash_rate(CashRateStrategy.MANUAL)


def test_rbnz_ocr_resolves_to_the_current_observation(ocr_series):
    resolved = resolve_cash_rate(CashRateStrategy.RBNZ_OCR, ocr_series=ocr_series)

    assert resolved.rate_pct == 2.50
    assert resolved.cash_rate == 0.025
    assert resolved.as_of_period == "Aug-2026"
    assert "Aug-2026" in resolved.rationale


def test_rbnz_ocr_requires_a_series():
    with pytest.raises(MarketStrategyError, match="requires an OCR series"):
        resolve_cash_rate(CashRateStrategy.RBNZ_OCR)


def test_rbnz_ocr_rationale_flags_the_end_of_month_lag(ocr_series):
    """The dashboard series is a month-end snapshot, so a mid-month OCR
    review is not guaranteed to be reflected yet -- the rationale must make
    that limitation visible rather than implying the rate is live."""
    resolved = resolve_cash_rate(CashRateStrategy.RBNZ_OCR, ocr_series=ocr_series)

    assert "End of Month" in resolved.rationale
    assert "may not yet reflect" in resolved.rationale


def test_rbnz_ocr_rejects_a_series_that_has_not_been_refreshed_in_time():
    """An observation dramatically older than one OCR review cycle means the
    fetch didn't actually pick up a current series (e.g. a stalled page) --
    that must fail loud, not silently price off a rate that could already be
    multiple OCR reviews stale."""
    stale_series = OcrSeries(
        observations=(OcrObservation("Jan-2026", 5.50, 5.80),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=CAPTURED_AT,  # 2026-09-03: ~215 days after Jan-2026 closed
    )

    with pytest.raises(MarketStrategyError, match="looks stale"):
        resolve_cash_rate(CashRateStrategy.RBNZ_OCR, ocr_series=stale_series)


def test_rbnz_ocr_accepts_an_observation_within_the_normal_publish_lag():
    """A freshly-closed month, captured within the ordinary end-of-month
    publish delay, must not be rejected as stale."""
    just_published = OcrSeries(
        observations=(OcrObservation("Aug-2026", 2.50, 2.98),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=datetime(2026, 9, 10, tzinfo=timezone.utc),  # 10 days after Aug closed
    )

    resolved = resolve_cash_rate(CashRateStrategy.RBNZ_OCR, ocr_series=just_published)

    assert resolved.rate_pct == 2.50


def test_strategy_accepts_a_plain_string(ocr_series):
    assert resolve_cash_rate("RBNZ_OCR", ocr_series=ocr_series).rate_pct == 2.50


# ------------------------------------------------------------ config overlay


def test_manual_leaves_the_config_completely_untouched(config):
    result = apply_cash_rate_strategy_to_config(config, CashRateStrategy.MANUAL)

    assert result is config, "the legacy path must not even copy the config"
    assert result.market.cash_rate == config.market.cash_rate


def test_manual_is_the_default_strategy(config):
    assert apply_cash_rate_strategy_to_config(config) is config


def test_rbnz_ocr_overlays_cash_rate(config):
    # A distinct, guaranteed-different-from-config OCR value, rather than the
    # shared ocr_series fixture: base.yaml's cash_rate is expected to track
    # the real OCR over time (see docs/modelling_assumptions.md), so a fixed
    # fixture value could coincidentally match it and let a broken overlay
    # (one that silently returns config.market.cash_rate unchanged) pass this
    # test for the wrong reason.
    original_cash_rate = config.market.cash_rate
    distinct_ocr_pct = round(original_cash_rate * 100 + 1.11, 4)
    series = OcrSeries(
        observations=(OcrObservation("Aug-2026", distinct_ocr_pct, None),),
        source_url="https://www.rbnz.govt.nz/statistics/key-statistics/x",
        captured_at_utc=CAPTURED_AT,
    )

    result = apply_cash_rate_strategy_to_config(
        config, CashRateStrategy.RBNZ_OCR, ocr_series=series
    )

    assert result.market.cash_rate == round(distinct_ocr_pct / 100, 8)
    assert result.market.cash_rate != original_cash_rate
    assert result is not config
    assert config.market.cash_rate == original_cash_rate, "the original config is unchanged"


def test_wholesale_funding_spread_is_never_touched(config, ocr_series):
    """A still-unverified assumption must not be silently guessed at here."""
    result = apply_cash_rate_strategy_to_config(
        config, CashRateStrategy.RBNZ_OCR, ocr_series=ocr_series
    )

    assert result.market.wholesale_funding_spread == config.market.wholesale_funding_spread


def test_the_overlaid_config_is_revalidated(config, ocr_series):
    result = apply_cash_rate_strategy_to_config(
        config, CashRateStrategy.RBNZ_OCR, ocr_series=ocr_series
    )

    assert type(config).model_validate(result.model_dump())


def test_an_overlaid_config_still_drives_the_optimiser(config, ocr_series):
    """The overlay must produce a config the existing engine can actually run."""
    from balance_pricing_engine.rate_grids import build_configured_rate_grids

    result = apply_cash_rate_strategy_to_config(
        config, CashRateStrategy.RBNZ_OCR, ocr_series=ocr_series
    )
    loan_grid, deposit_grid = build_configured_rate_grids(result)

    assert len(loan_grid) > 0
    assert len(deposit_grid) > 0
