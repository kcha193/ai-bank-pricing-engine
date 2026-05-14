from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.run_ledger.directory import create_run_directory
from balance_pricing_engine.run_ledger.metadata import (
    RunMetadata,
    build_run_metadata,
    write_run_metadata,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def test_create_run_directory_is_created(tmp_path: Path) -> None:
    run_dir = create_run_directory("baseline", base_dir=tmp_path)
    assert run_dir.exists()
    assert run_dir.is_dir()


def test_create_run_directory_name_contains_scenario(tmp_path: Path) -> None:
    run_dir = create_run_directory("my_scenario", base_dir=tmp_path)
    assert "my_scenario" in run_dir.name


def test_create_run_directory_name_has_timestamp_prefix(tmp_path: Path) -> None:
    run_dir = create_run_directory("baseline", base_dir=tmp_path)
    # Expected format: YYYYMMDD_HHMMSS_baseline
    date_part = run_dir.name.split("_")[0]
    assert len(date_part) == 8 and date_part.isdigit()


def test_create_run_directory_spaces_replaced(tmp_path: Path) -> None:
    run_dir = create_run_directory("my scenario", base_dir=tmp_path)
    assert " " not in run_dir.name


def test_create_run_directory_default_base_dir_is_outputs_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = create_run_directory("baseline")
    assert run_dir.parent == tmp_path / "outputs" / "runs"


@pytest.fixture(scope="module")
def config():
    return load_config("configs/base.yaml")


@pytest.fixture(scope="module")
def diagnostics(config):
    return run_baseline_diagnostics(config)


REQUIRED_METADATA_FIELDS = {
    "run_id",
    "scenario_name",
    "timestamp",
    "config_path",
    "config_hash",
    "python_version",
    "boundary_warning_flag",
    "initial_boundary_warning_flag",
    "expansion_rounds",
    "policy_band_breached",
}


def test_build_run_metadata_has_required_fields(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert isinstance(metadata, RunMetadata)
    d = asdict(metadata)
    assert REQUIRED_METADATA_FIELDS.issubset(d.keys())


def test_build_run_metadata_scenario_name(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.scenario_name == "baseline"


def test_build_run_metadata_config_path(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.config_path == "configs/base.yaml"


def test_build_run_metadata_config_hash_is_stable(diagnostics) -> None:
    m1 = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    m2 = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert m1.config_hash == m2.config_hash
    assert len(m1.config_hash) == 64  # sha256 hex digest


def test_build_run_metadata_python_version(diagnostics) -> None:
    import sys

    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.python_version.startswith(
        f"{sys.version_info.major}.{sys.version_info.minor}."
    )


def test_build_run_metadata_boundary_flags_match_diagnostics(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.boundary_warning_flag == diagnostics.boundary_diagnostics.has_warning
    assert (
        metadata.initial_boundary_warning_flag
        == diagnostics.initial_boundary_diagnostics.has_warning
    )
    assert metadata.expansion_rounds == diagnostics.expansion_rounds
    assert metadata.policy_band_breached == diagnostics.policy_band_breached


def test_write_run_metadata_creates_json(tmp_path: Path, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_run_metadata(metadata, tmp_path)
    out = tmp_path / "metadata.json"
    assert out.exists()
    data = json.loads(out.read_text())
    assert "run_id" in data
    assert "config_hash" in data


def test_write_run_metadata_json_is_valid(tmp_path: Path, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_run_metadata(metadata, tmp_path)
    data = json.loads((tmp_path / "metadata.json").read_text())
    assert data["scenario_name"] == "baseline"
    assert isinstance(data["boundary_warning_flag"], bool)
    assert isinstance(data["expansion_rounds"], int)
