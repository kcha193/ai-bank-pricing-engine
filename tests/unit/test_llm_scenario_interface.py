"""Tests for NLScenarioInterface - no real API calls."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.scenario_interface import (
    ConfigPatch,
    NLScenarioInterface,
    apply_patches,
)


@pytest.fixture()
def base_config():
    return load_config(Path("configs/base.yaml"))


def test_apply_patches_top_level_field(base_config):
    patches = [ConfigPatch(path=["random_seed"], value=99)]
    patched = apply_patches(base_config, patches)
    assert patched.random_seed == 99


def test_apply_patches_nested_field(base_config):
    original_cash_rate = base_config.market.cash_rate
    new_rate = original_cash_rate - 0.0025  # -25 bps
    patches = [ConfigPatch(path=["market", "cash_rate"], value=new_rate)]
    patched = apply_patches(base_config, patches)
    assert patched.market.cash_rate == pytest.approx(new_rate)
    assert base_config.market.cash_rate == pytest.approx(original_cash_rate)  # immutable


def test_apply_patches_multiple_fields(base_config):
    patches = [
        ConfigPatch(path=["market", "cash_rate"], value=0.05),
        ConfigPatch(path=["market", "competitor_loan_rate"], value=0.07),
    ]
    patched = apply_patches(base_config, patches)
    assert patched.market.cash_rate == pytest.approx(0.05)
    assert patched.market.competitor_loan_rate == pytest.approx(0.07)


def test_apply_patches_raises_on_unknown_path(base_config):
    patches = [ConfigPatch(path=["nonexistent_field"], value=42)]
    with pytest.raises((AttributeError, ValueError)):
        apply_patches(base_config, patches)


def test_apply_patches_raises_on_invalid_value(base_config):
    # cash_rate must be a float, not a string
    patches = [ConfigPatch(path=["market", "cash_rate"], value="not-a-float")]
    with pytest.raises(Exception):
        apply_patches(base_config, patches)


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
    assert result.proposal.patches[0].path == ["market", "cash_rate"]
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

    patched = interface.apply(base_config, result, approve=True)

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

    with pytest.raises(ValueError, match="No valid proposal"):
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
