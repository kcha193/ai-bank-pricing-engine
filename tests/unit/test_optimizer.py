from balance_pricing_engine.config import load_config
from balance_pricing_engine.scenarios.runner import run_baseline_scenario


def test_baseline_optimizer_returns_valid_joint_rates():
    config = load_config("configs/base.yaml")

    result = run_baseline_scenario(config)

    assert config.products.loan_rate_grid.start <= result.loan_rate <= config.products.loan_rate_grid.stop
    assert (
        config.products.deposit_rate_grid.start
        <= result.deposit_rate
        <= config.products.deposit_rate_grid.stop
    )
    assert result.expected_loan_volume > 0
    assert result.expected_deposit_volume > 0
