"""Tests for NLScenarioInterface - no real API calls."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.scenario_interface import (
    ConfigPatch,
    NLScenarioInterface,
    NLScenarioResult,
    PatchProposal,
    PatchApproval,
    apply_patches,
)
from balance_pricing_engine.optimization.response_fn import GovernanceError


@pytest.fixture()
def base_config():
    return load_config(Path("configs/base.yaml"))


def _approval(patches):
    proposal = PatchProposal(patches=patches)
    return PatchApproval(
        approver_id="test-reviewer",
        approved_at_utc=datetime.now(timezone.utc),
        proposal_hash=proposal.proposal_hash,
        patches=proposal.patches,
    )


def test_apply_patches_top_level_field(base_config):
    patches = [ConfigPatch(path=["random_seed"], value=99)]
    patched = apply_patches(base_config, patches, approved=True, approval=_approval(patches))
    assert patched.random_seed == 99


def test_apply_patches_nested_field(base_config):
    original_cash_rate = base_config.market.cash_rate
    new_rate = original_cash_rate - 0.0025  # -25 bps
    patches = [ConfigPatch(path=["market", "cash_rate"], value=new_rate)]
    patched = apply_patches(base_config, patches, approved=True, approval=_approval(patches))
    assert patched.market.cash_rate == pytest.approx(new_rate)
    assert base_config.market.cash_rate == pytest.approx(original_cash_rate)  # immutable


def test_apply_patches_multiple_fields(base_config):
    patches = [
        ConfigPatch(path=["market", "cash_rate"], value=0.05),
        ConfigPatch(path=["market", "competitor_loan_rate"], value=0.07),
    ]
    patched = apply_patches(base_config, patches, approved=True, approval=_approval(patches))
    assert patched.market.cash_rate == pytest.approx(0.05)
    assert patched.market.competitor_loan_rate == pytest.approx(0.07)


@pytest.mark.parametrize("path", [
    ["nonexistent_field"], ["model_dump"], ["market", "model_dump"],
])
def test_apply_patches_rejects_unknown_or_model_method_paths(path):
    with pytest.raises(ValidationError, match="allowed scenario"):
        ConfigPatch(path=path, value=42)


def test_apply_patches_raises_on_invalid_value():
    # cash_rate must be a float, not a string
    with pytest.raises(ValidationError, match="finite JSON number"):
        ConfigPatch(path=["market", "cash_rate"], value="not-a-float")


def _make_mock_client(patches_json: str):
    """Build a mock Anthropic client that returns a fixed JSON response."""
    message = MagicMock()
    message.content = [MagicMock(text=patches_json)]
    client = MagicMock()
    client.messages.create.return_value = message
    return client


def test_propose_returns_proposal_with_monkeypatched_client(base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]
    })
    client = _make_mock_client(patch_json)
    interface = NLScenarioInterface(client=client)

    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    assert result.proposal is not None
    assert result.used_llm is True
    assert len(result.proposal.patches) == 1
    assert result.proposal.patches[0].path == ("market", "cash_rate")
    assert result.proposal.patches[0].value == pytest.approx(0.0475)


def test_propose_falls_back_gracefully_when_llm_disabled(base_config):
    interface = NLScenarioInterface(use_llm=False)

    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    assert result.proposal is None
    assert result.used_llm is False
    assert result.error is not None


def test_propose_falls_back_on_malformed_json(base_config):
    client = _make_mock_client("not valid json at all")
    interface = NLScenarioInterface(client=client)

    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    assert result.proposal is None
    assert result.used_llm is False


def test_apply_with_approval_patches_config(base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]
    })
    client = _make_mock_client(patch_json)
    interface = NLScenarioInterface(client=client)
    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    patched = interface.apply(
        base_config, result, approve=True, approval=result.approve(approver_id="test-reviewer"),
    )

    assert patched.market.cash_rate == pytest.approx(0.0475)
    assert base_config.market.cash_rate != pytest.approx(0.0475)  # original unchanged


def test_apply_without_approval_raises(base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["market", "cash_rate"], "value": 0.0475}]
    })
    client = _make_mock_client(patch_json)
    interface = NLScenarioInterface(client=client)
    result = interface.propose(base_config, "cut the cash rate by 25 bps")

    with pytest.raises(ValueError, match="approval"):
        interface.apply(base_config, result, approve=False)


def test_apply_with_no_proposal_raises(base_config):
    interface = NLScenarioInterface(use_llm=False)
    result = interface.propose(base_config, "do something")

    with pytest.raises(GovernanceError, match="approval"):
        interface.apply(base_config, result, approve=True)


def test_propose_writes_audit_log(tmp_path, base_config):
    patch_json = json.dumps({
        "patches": [{"path": ["random_seed"], "value": 99}]
    })
    client = _make_mock_client(patch_json)
    audit_log = tmp_path / "nl_scenario_audit.jsonl"
    interface = NLScenarioInterface(client=client, audit_log_path=audit_log)

    interface.propose(base_config, "change seed to 99")

    assert audit_log.exists()
    entry = json.loads(audit_log.read_text().strip())
    assert entry["used_llm"] is True
    assert entry["n_patches"] == 1


# --- Governance gate tests ---


def test_apply_patches_raises_governance_error_without_approval(base_config):
    patches = [ConfigPatch(path=["random_seed"], value=99)]
    with pytest.raises(GovernanceError):
        apply_patches(base_config, patches)


def test_apply_patches_succeeds_with_identified_approval(base_config):
    patches = [ConfigPatch(path=["random_seed"], value=99)]
    result = apply_patches(base_config, patches, approved=True, approval=_approval(patches))
    assert result.random_seed == 99


def test_nl_scenario_result_apply_to_raises_without_approval(base_config):
    proposal = PatchProposal(patches=[ConfigPatch(path=["random_seed"], value=77)])
    result = NLScenarioResult(
        proposal=proposal,
        diff_yaml="# diff",
        used_llm=False,
        model_used="disabled",
        prompt_hash="abc123",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(GovernanceError):
        result.apply_to(base_config)


def test_nl_scenario_result_apply_to_succeeds_with_approval(base_config):
    proposal = PatchProposal(patches=[ConfigPatch(path=["random_seed"], value=77)])
    result = NLScenarioResult(
        proposal=proposal,
        diff_yaml="# diff",
        used_llm=False,
        model_used="disabled",
        prompt_hash="abc123",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    patched = result.apply_to(
        base_config, approved=True, approval=result.approve(approver_id="test-reviewer"),
    )
    assert patched.random_seed == 77


def test_nl_scenario_result_apply_to_raises_when_no_proposal(base_config):
    result_no_proposal = NLScenarioResult(
        proposal=None,
        diff_yaml="# LLM error: disabled",
        used_llm=False,
        model_used="disabled",
        prompt_hash="abc123",
        timestamp_utc="2026-01-01T00:00:00+00:00",
        error="LLM disabled",
    )
    with pytest.raises(ValueError):
        result_no_proposal.apply_to(base_config)
