"""Canonical effective-config hashing (GOV-004).

``effective_config_hash`` identifies the validated ``EngineConfig`` that drove
pricing. It must not depend on YAML layout, key order, file name or temp paths.
"""

import hashlib
import json
from typing import Any

import yaml

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.config_identity import (
    CONFIG_HASH_BASIS,
    canonical_config_bytes,
    effective_config_hash,
    file_sha256,
    file_sha256_if_present,
)

BASE_YAML = "configs/base.yaml"


def _reverse_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


def _override(config: EngineConfig, **response_updates: float) -> EngineConfig:
    response = config.response.model_copy(update=response_updates)
    return EngineConfig.model_validate(config.model_copy(update={"response": response}).model_dump())


def test_hash_is_sha256_of_canonical_json_of_model_dump():
    """T01: same effective config -> same SHA-256, defined as documented."""
    config = load_config(BASE_YAML)
    expected_payload = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

    assert canonical_config_bytes(config) == expected_payload
    assert effective_config_hash(config) == hashlib.sha256(expected_payload).hexdigest()
    assert effective_config_hash(config) == effective_config_hash(load_config(BASE_YAML))
    assert len(effective_config_hash(config)) == 64


def test_yaml_whitespace_and_comments_do_not_change_hash(tmp_path):
    """T02"""
    raw = yaml.safe_load(open(BASE_YAML, encoding="utf-8"))
    compact = tmp_path / "compact.yaml"
    compact.write_text(yaml.safe_dump(raw, indent=2), encoding="utf-8")
    spaced = tmp_path / "spaced.yaml"
    spaced.write_text(
        "# leading comment\n\n" + yaml.safe_dump(raw, indent=6) + "\n\n# trailing\n",
        encoding="utf-8",
    )
    assert compact.read_bytes() != spaced.read_bytes()
    assert effective_config_hash(load_config(compact)) == effective_config_hash(
        load_config(spaced)
    )


def test_yaml_key_order_does_not_change_hash(tmp_path):
    """T03"""
    raw = yaml.safe_load(open(BASE_YAML, encoding="utf-8"))
    forward = tmp_path / "forward.yaml"
    forward.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    reverse = tmp_path / "reverse.yaml"
    reverse.write_text(yaml.safe_dump(_reverse_keys(raw), sort_keys=False), encoding="utf-8")
    assert forward.read_bytes() != reverse.read_bytes()
    assert effective_config_hash(load_config(forward)) == effective_config_hash(
        load_config(reverse)
    )


def test_file_name_does_not_change_hash(tmp_path):
    """Identical content under different names -> identical effective hash."""
    text = open(BASE_YAML, encoding="utf-8").read()
    first, second = tmp_path / "a.yaml", tmp_path / "nested_b.yml"
    first.write_text(text, encoding="utf-8")
    second.write_text(text, encoding="utf-8")
    assert effective_config_hash(load_config(first)) == effective_config_hash(load_config(second))


def test_loan_elasticity_override_changes_hash():
    """T04"""
    base = load_config(BASE_YAML)
    assert base.response.loan_rate_elasticity != -60.0
    assert effective_config_hash(_override(base, loan_rate_elasticity=-60.0)) != (
        effective_config_hash(base)
    )


def test_deposit_elasticity_override_changes_hash():
    """T05"""
    base = load_config(BASE_YAML)
    assert effective_config_hash(_override(base, deposit_rate_elasticity=60.0)) != (
        effective_config_hash(base)
    )


def test_equivalent_overrides_share_hash():
    """Two routes to the same effective config -> same hash."""
    base = load_config(BASE_YAML)
    via_copy = _override(base, loan_rate_elasticity=-60.0)
    via_yaml_roundtrip = EngineConfig.model_validate(
        yaml.safe_load(yaml.safe_dump(via_copy.model_dump(mode="json")))
    )
    assert effective_config_hash(via_copy) == effective_config_hash(via_yaml_roundtrip)


def test_file_sha256_hashes_raw_bytes_and_is_independent_of_effective_hash(tmp_path):
    """T06/T07: source-file hash covers bytes; effective hash covers economics."""
    source = tmp_path / "cfg.yaml"
    raw = open(BASE_YAML, encoding="utf-8").read()
    source.write_text(raw, encoding="utf-8")
    reformatted = tmp_path / "cfg_reformatted.yaml"
    reformatted.write_text("# note\n" + raw, encoding="utf-8")

    assert file_sha256(source) == hashlib.sha256(source.read_bytes()).hexdigest()
    assert file_sha256(source) == file_sha256(source)
    assert file_sha256(source) != file_sha256(reformatted)
    assert effective_config_hash(load_config(source)) == effective_config_hash(
        load_config(reformatted)
    )
    assert file_sha256(source) != effective_config_hash(load_config(source))


def test_hash_basis_label_is_stable():
    assert CONFIG_HASH_BASIS == "effective_config_canonical_json_sha256"


def test_file_sha256_if_present_is_none_for_missing_or_unsupplied_source(tmp_path):
    """Provenance is unavailable (None) rather than an error or a substitute hash."""
    present = tmp_path / "cfg.yaml"
    present.write_text("a: 1\n", encoding="utf-8")
    assert file_sha256_if_present(present) == file_sha256(present)
    assert file_sha256_if_present(tmp_path / "missing.yaml") is None
    assert file_sha256_if_present(tmp_path) is None  # a directory is not a source file
    assert file_sha256_if_present(None) is None
