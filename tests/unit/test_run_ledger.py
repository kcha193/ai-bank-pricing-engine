from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.run_ledger.config_snapshot import write_config_snapshot
from balance_pricing_engine.run_ledger.directory import create_run_directory
from balance_pricing_engine.run_ledger.memo import write_pricing_memo
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


def test_write_config_snapshot_creates_file(tmp_path: Path) -> None:
    write_config_snapshot("configs/base.yaml", tmp_path)
    assert (tmp_path / "config_snapshot.yaml").exists()


def test_write_config_snapshot_content_matches_original(tmp_path: Path) -> None:
    write_config_snapshot("configs/base.yaml", tmp_path)
    original = Path("configs/base.yaml").read_text(encoding="utf-8")
    snapshot = (tmp_path / "config_snapshot.yaml").read_text(encoding="utf-8")
    assert original == snapshot


def test_write_pricing_memo_creates_file(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    assert (tmp_path / "pricing_memo.md").exists()


def test_pricing_memo_contains_recommendation_section(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Recommendation" in content


def test_pricing_memo_contains_objective_decomposition(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Objective Decomposition" in content


def test_pricing_memo_contains_boundary_diagnostics(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Boundary Diagnostics" in content


def test_pricing_memo_contains_model_risk_notes(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Model-Risk Notes" in content


def test_pricing_memo_contains_committee_questions(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Questions for Pricing Committee" in content


def test_pricing_memo_contains_synthetic_data_limitation(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Synthetic Data Limitation" in content


def test_pricing_memo_contains_loan_rate(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    loan_rate_pct = f"{diagnostics.best_result.loan_rate:.2%}"
    assert loan_rate_pct in content


def test_pricing_memo_with_sensitivity_includes_sensitivity_section(
    tmp_path: Path, config, diagnostics
) -> None:
    from balance_pricing_engine.diagnostics import run_sensitivity_analysis

    sensitivity = run_sensitivity_analysis(config)
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics, sensitivity=sensitivity)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Sensitivity Summary" in content
    assert "baseline" in content  # scenario name appears in table
