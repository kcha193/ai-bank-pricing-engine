from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.anomaly import AnomalyReport, ConfigAnomalyChecker


def _base_config():
    return load_config("configs/base.yaml")


def test_clean_config_has_no_anomalies():
    config = _base_config()
    checker = ConfigAnomalyChecker()
    reports = checker.check(config)
    assert reports == [], f"Expected no anomalies, got: {reports}"


def test_negative_wholesale_spread_flagged():
    config = _base_config().model_copy(
        update={
            "market": _base_config().market.model_copy(
                update={"wholesale_funding_spread": -0.005}
            )
        }
    )
    reports = checker_check(config)
    assert any(r.rule_id == "negative_wholesale_spread" for r in reports)
    assert all(r.severity in ("warning", "error") for r in reports)


def checker_check(config):
    return ConfigAnomalyChecker().check(config)


def test_penalty_dominated_by_funding_spread_flagged():
    config = _base_config().model_copy(
        update={
            "market": _base_config().market.model_copy(
                update={"wholesale_funding_spread": 0.05}
            ),
            "balance_sheet": _base_config().balance_sheet.model_copy(
                update={"funding_shortfall_penalty_rate": 0.001}
            ),
        }
    )
    reports = checker_check(config)
    assert any(r.rule_id == "penalty_dominated_by_funding_spread" for r in reports)


def test_small_customer_pool_flagged():
    config = _base_config().model_copy(
        update={
            "simulation": _base_config().simulation.model_copy(update={"n_loan_customers": 500})
        }
    )
    reports = checker_check(config)
    assert any(r.rule_id == "small_customer_pool" for r in reports)


def test_narrow_spread_band_flagged():
    from balance_pricing_engine.config import SpreadGridConfigModel

    config = _base_config().model_copy(
        update={
            "pricing_strategy": _base_config().pricing_strategy.model_copy(
                update={
                    "home_loan_spread_bps": SpreadGridConfigModel(min=-20, max=20, step=10),
                }
            )
        }
    )
    reports = checker_check(config)
    assert any(r.rule_id == "narrow_spread_band" for r in reports)


def test_anomaly_report_has_required_fields():
    config = _base_config().model_copy(
        update={
            "market": _base_config().market.model_copy(
                update={"wholesale_funding_spread": -0.005}
            )
        }
    )
    reports = checker_check(config)
    negative_report = next(r for r in reports if r.rule_id == "negative_wholesale_spread")
    assert isinstance(negative_report, AnomalyReport)
    assert negative_report.severity in ("warning", "error")
    assert negative_report.message
    assert negative_report.field_path
    assert negative_report.observed_value is not None


def test_competitor_rates_enabled_but_strict_false_flagged():
    config = _base_config().model_copy(
        update={
            "competitor_rates": _base_config().competitor_rates.model_copy(
                update={"enabled": True, "strict": False}
            )
        }
    )
    reports = checker_check(config)
    assert any(r.rule_id == "competitor_rates_non_strict" for r in reports)
