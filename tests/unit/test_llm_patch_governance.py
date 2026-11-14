"""GOV-007: an LLM can propose assumptions, never authorise itself."""

import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.scenario_interface import (
    ConfigPatch,
    NLScenarioInterface,
    apply_patches,
)
from balance_pricing_engine.optimization.response_fn import GovernanceError


def interface_for(tmp_path, patches):
    response = MagicMock()
    response.content = [MagicMock(text=json.dumps({"patches": patches}))]
    client = MagicMock()
    client.messages.create.return_value = response
    return NLScenarioInterface(client=client, audit_log_path=tmp_path / "audit.jsonl")


def entries(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


@pytest.mark.parametrize(
    "path,value",
    [
        (["response", "loan_rate_elasticity"], 170),
        (["response", "loan_rate_elasticity"], -100),
        (["competitor_rates", "enabled"], True),
        (["competitor_rates", "source_file"], "secret-path"),
        (["market"], {"cash_rate": 0.04}),
        (["model_registry", "approved"], True),
        (["simulation", "n_loan_customers"], 100000000),
        (["balance_sheet", "min_return_on_equity"], 0),
        (["market", "__dict__"], {}),
        (["market.cash_rate"], 0.04),
        (["market", ""], 0.04),
        (["market", 0], 0.04),
        ([], 0.04),
        (["market", "cash_rate"], {"token": "test-secret-never-log"}),
        (["market", "cash_rate"], "test-secret-never-log"),
        (["market", "cash_rate"], True),
        (["market", "cash_rate"], float("nan")),
        (["market", "cash_rate"], float("inf")),
    ],
)
def test_proposal_rejects_forbidden_paths_and_unsafe_values(tmp_path, path, value):
    interface = interface_for(tmp_path, [{"path": path, "value": value}])
    result = interface.propose(load_config("configs/base.yaml"), "synthetic instruction")
    assert result.proposal is None
    assert result.error
    log = entries(tmp_path)
    assert [row["event"] for row in log] == ["proposal_generated", "proposal_rejected"]
    assert "test-secret-never-log" not in (tmp_path / "audit.jsonl").read_text()


def test_boolean_alone_cannot_identify_an_approver():
    with pytest.raises(GovernanceError, match="approv"):
        apply_patches(
            load_config("configs/base.yaml"),
            [ConfigPatch(path=["random_seed"], value=99)],
            approved=True,
        )


def test_human_approval_and_application_have_separate_audit_events(tmp_path):
    config = load_config("configs/base.yaml")
    patches = [{"path": ["market", "cash_rate"], "value": 0.0475}]
    interface = interface_for(tmp_path, patches)
    result = interface.propose(config, "cut cash rate")
    approval = result.approve(approver_id="reviewer@example.test")
    updated = interface.apply(config, result, approve=True, approval=approval)
    assert updated.market.cash_rate == 0.0475
    assert config.market.cash_rate == 0.025
    log = entries(tmp_path)
    assert [row["event"] for row in log] == [
        "proposal_generated",
        "approval",
        "application_succeeded",
    ]
    assert log[-1]["patches"] == patches
    assert log[-1]["approver_id"] == "reviewer@example.test"
    assert log[-1]["proposal_hash"] == approval.proposal_hash
    assert len(log[-1]["config_hash"]) == 64
    assert datetime.fromisoformat(log[-1]["approved_at_utc"]).utcoffset().total_seconds() == 0


@pytest.mark.parametrize("approver", [None, "", "   "])
def test_missing_approver_rejected(tmp_path, approver):
    interface = interface_for(tmp_path, [{"path": ["random_seed"], "value": 7}])
    result = interface.propose(load_config("configs/base.yaml"), "change seed")
    with pytest.raises((GovernanceError, ValueError)):
        result.approve(approver_id=approver)
    assert not any(row["event"] == "approval" for row in entries(tmp_path))


def test_reviewed_proposal_cannot_be_replaced(tmp_path):
    config = load_config("configs/base.yaml")
    interface = interface_for(tmp_path, [{"path": ["random_seed"], "value": 7}])
    result = interface.propose(config, "change seed")
    approval = result.approve(approver_id="reviewer")
    other = interface_for(tmp_path, [{"path": ["random_seed"], "value": 9}]).propose(
        config, "change seed again"
    )
    replaced = replace(result, proposal=other.proposal)
    with pytest.raises(GovernanceError, match="proposal"):
        interface.apply(config, replaced, approve=True, approval=approval)
    assert not any(row["event"] == "application_succeeded" for row in entries(tmp_path))


def test_failed_batch_is_atomic_and_never_logs_success(tmp_path):
    config = load_config("configs/base.yaml")
    interface = interface_for(
        tmp_path,
        [
            {"path": ["market", "cash_rate"], "value": 0.04},
            {"path": ["balance_sheet", "required_deposit_funding_ratio"], "value": 0.8},
        ],
    )
    result = interface.propose(config, "change funding assumptions")
    approval = result.approve(approver_id="reviewer")
    # Change the base after review so the final EngineConfig fails cross-field validation.
    config.market.home_loan_reference_rate = 0.0001
    before = config.model_dump()
    with pytest.raises(ValueError):
        interface.apply(config, result, approve=True, approval=approval)
    assert config.model_dump() == before
    log = entries(tmp_path)
    assert log[-1]["event"] == "application_failed"
    assert log[-1]["error_category"] == "ValidationError"
    assert not any(row["event"] == "application_succeeded" for row in log)


def test_proposal_validates_entire_batch_before_review(tmp_path):
    config = load_config("configs/base.yaml")
    interface = interface_for(
        tmp_path,
        [
            {"path": ["market", "cash_rate"], "value": 0.04},
            {"path": ["balance_sheet", "required_deposit_funding_ratio"], "value": 2},
        ],
    )
    result = interface.propose(config, "invalid funding ratio")
    assert result.proposal is None
    assert config.market.cash_rate == 0.025
    assert entries(tmp_path)[-1]["event"] == "proposal_rejected"


def test_failed_application_keeps_approved_identity_and_content(tmp_path):
    config = load_config("configs/base.yaml")
    interface = interface_for(tmp_path, [{"path": ["random_seed"], "value": 7}])
    result = interface.propose(config, "change seed")
    approval = result.approve(approver_id="reviewer")
    other = interface_for(tmp_path, [{"path": ["random_seed"], "value": 9}]).propose(
        config, "other seed"
    )
    with pytest.raises(GovernanceError):
        interface.apply(config, other, approve=True, approval=approval)
    failed = entries(tmp_path)[-1]
    assert failed["event"] == "application_failed"
    assert failed["approver_id"] == "reviewer"
    assert failed["patches"] == [{"path": ["random_seed"], "value": 7}]


def test_interface_uses_its_own_audit_sink_for_detached_result(tmp_path):
    config = load_config("configs/base.yaml")
    interface = interface_for(tmp_path, [{"path": ["random_seed"], "value": 7}])
    result = interface.propose(config, "change seed")
    detached = replace(result, audit_log_path=None)
    approval = detached.approve(approver_id="reviewer")
    interface.apply(config, detached, approve=True, approval=approval)
    assert entries(tmp_path)[-1]["event"] == "application_succeeded"


@pytest.mark.parametrize("seed", [-1, 1.5])
def test_llm_seed_must_be_usable_by_the_scenario_rng(tmp_path, seed):
    interface = interface_for(tmp_path, [{"path": ["random_seed"], "value": seed}])
    result = interface.propose(load_config("configs/base.yaml"), "invalid seed")
    assert result.proposal is None
