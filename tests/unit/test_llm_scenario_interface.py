"""Tests for NLScenarioInterface - no real API calls."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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
