from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.run_ledger import write_run_ledger
from balance_pricing_engine.run_ledger.checklist import (
    sign_off_all_items,
    sign_off_item,
    write_governance_checklist,
)
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


def test_create_run_directory_default_base_dir_is_data_outputs_runs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = create_run_directory("baseline")
    assert run_dir.parent == tmp_path / "data" / "outputs" / "runs"


def test_create_run_directory_avoids_same_timestamp_collision(tmp_path: Path) -> None:
    timestamp = datetime(2026, 5, 14, 1, 2, 3, 123456)

    first = create_run_directory("baseline", base_dir=tmp_path, timestamp=timestamp)
    second = create_run_directory("baseline", base_dir=tmp_path, timestamp=timestamp)

    assert first != second
    assert first.exists()
    assert second.exists()


@pytest.fixture(scope="module")
def config():
    return load_config("configs/base.yaml")


@pytest.fixture(scope="module")
def diagnostics(config):
    return run_baseline_diagnostics(config)


@pytest.fixture()
def run_dir_with_checklist(tmp_path):
    """A tmp run directory with governance_checklist.json + .md already written."""
    metadata = RunMetadata(
        run_id="test-run-001",
        scenario_name="test_scenario",
        timestamp="2026-05-23T00:00:00+00:00",
        git_commit=None,
        config_path="configs/base.yaml",
        config_hash="abc123",
        python_version="3.11.0",
        package_version="0.1.0",
        boundary_warning_flag=False,
        initial_boundary_warning_flag=False,
        expansion_rounds=0,
        policy_band_breached=False,
        competitor_rate_source=None,
    )
    # write_governance_checklist needs a config and diagnostics — use the simplest possible stubs
    # by calling _build_checklist_json directly instead.
    from balance_pricing_engine.run_ledger.checklist import _build_checklist_json
    import json
    json_content = _build_checklist_json(metadata)
    (tmp_path / "governance_checklist.json").write_text(json_content, encoding="utf-8")
    (tmp_path / "governance_checklist.md").write_text("# placeholder\n", encoding="utf-8")
    return tmp_path


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


def test_build_run_metadata_timestamp_is_utc_aware(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.timestamp.endswith("+00:00") or metadata.timestamp.endswith("Z")


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


def test_pricing_memo_sensitivity_formats_boundary_warning_as_yes_no(
    tmp_path: Path, config, diagnostics
) -> None:
    sensitivity = pd.DataFrame(
        {
            "scenario_name": ["baseline", "downside"],
            "best_loan_rate": [0.065, 0.067],
            "best_deposit_rate": [0.035, 0.036],
            "objective_value": [1_000_000.0, 900_000.0],
            "delta_objective_pct": [0.0, -0.1],
            "boundary_warning_flag": [True, False],
        }
    )
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)

    write_pricing_memo(tmp_path, metadata, config, diagnostics, sensitivity=sensitivity)

    content = (tmp_path / "pricing_memo.md").read_text()
    assert "| baseline | 6.50% | 3.50% | $1,000,000 | +0.0% | Yes |" in content
    assert "| downside | 6.70% | 3.60% | $900,000 | -10.0% | No |" in content
    assert "True" not in content
    assert "False" not in content


REQUIRED_CHECKLIST_ITEMS = [
    "Boundary result reviewed",
    "Sensitivity reviewed",
    "Competitor-rate freshness reviewed",
    "Funding assumptions reviewed",
    "Elasticity assumptions reviewed",
    "Conduct and fairness limitations noted",
    "Human approval required",
    "Synthetic-data limitation acknowledged",
]

EXPECTED_CHECKLIST_JSON_IDS = {
    "boundary-result-reviewed",
    "sensitivity-reviewed",
    "competitor-rate-freshness",
    "funding-assumptions-reviewed",
    "elasticity-assumptions-reviewed",
    "conduct-fairness-noted",
    "human-approval",
    "synthetic-data-acknowledged",
}


def test_write_governance_checklist_creates_file(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    assert (tmp_path / "governance_checklist.md").exists()


def test_governance_checklist_contains_all_required_checks(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    for item in REQUIRED_CHECKLIST_ITEMS:
        assert item in content, f"Missing checklist item: {item!r}"


def test_governance_checklist_uses_unchecked_boxes(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    assert "- [ ]" in content


def test_governance_checklist_includes_run_id(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    assert metadata.run_id in content


def test_governance_checklist_shows_boundary_status(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    expected_status = "REQUIRES REVIEW" if metadata.boundary_warning_flag else "CLEAR"
    assert expected_status in content


def test_write_governance_checklist_writes_json_file(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    assert (tmp_path / "governance_checklist.json").exists()


def test_governance_checklist_json_has_required_top_level_keys(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    data = json.loads((tmp_path / "governance_checklist.json").read_text())
    assert set(data.keys()) == {
        "run_id",
        "scenario_name",
        "timestamp",
        "config_hash",
        "generated_by",
        "items",
    }


def test_governance_checklist_json_items_have_correct_schema(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    data = json.loads((tmp_path / "governance_checklist.json").read_text())

    for item in data["items"]:
        assert set(item.keys()) == {
            "id",
            "section",
            "label",
            "status",
            "notes",
            "signed_by",
            "signed_at",
        }
        assert item["status"] == "pending"
        assert item["notes"] is None
        assert item["signed_by"] is None
        assert item["signed_at"] is None


def test_governance_checklist_json_has_all_expected_ids(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    data = json.loads((tmp_path / "governance_checklist.json").read_text())

    assert {item["id"] for item in data["items"]} == EXPECTED_CHECKLIST_JSON_IDS


def _checklist_json(run_dir) -> dict:
    return json.loads((run_dir / "governance_checklist.json").read_text())


def test_sign_off_item_updates_status_and_signer(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_item(run_dir, "human-approval", approver="alice")

    payload = _checklist_json(run_dir)
    item = next(i for i in payload["items"] if i["id"] == "human-approval")
    assert item["status"] == "approved"
    assert item["signed_by"] == "alice"
    assert item["signed_at"] is not None


def test_sign_off_item_records_notes(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_item(run_dir, "boundary-result-reviewed", approver="bob", notes="Reviewed, clear.")

    payload = _checklist_json(run_dir)
    item = next(i for i in payload["items"] if i["id"] == "boundary-result-reviewed")
    assert item["notes"] == "Reviewed, clear."


def test_sign_off_item_raises_on_unknown_id(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    with pytest.raises(ValueError, match="not found"):
        sign_off_item(run_dir, "nonexistent-item-id", approver="alice")


def test_sign_off_item_raises_if_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        sign_off_item(tmp_path, "human-approval", approver="alice")


def test_sign_off_item_regenerates_markdown(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_item(run_dir, "human-approval", approver="alice")

    md = (run_dir / "governance_checklist.md").read_text()
    assert "alice" in md
    assert "approved" in md.lower()


def test_sign_off_all_items_marks_all_approved(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_all_items(run_dir, approver="carol")

    payload = _checklist_json(run_dir)
    for item in payload["items"]:
        assert item["status"] == "approved"
        assert item["signed_by"] == "carol"


def test_sign_off_all_items_applies_shared_notes(run_dir_with_checklist):
    run_dir = run_dir_with_checklist

    sign_off_all_items(run_dir, approver="carol", notes="Batch approved after review.")

    payload = _checklist_json(run_dir)
    for item in payload["items"]:
        assert item["notes"] == "Batch approved after review."


def test_write_run_ledger_creates_directory(tmp_path: Path, config, diagnostics) -> None:
    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    assert run_dir.exists() and run_dir.is_dir()


def test_write_run_ledger_creates_all_expected_files(
    tmp_path: Path, config, diagnostics
) -> None:
    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    expected = [
        "metadata.json",
        "config_snapshot.yaml",
        "grid_results.csv",
        "top_results.csv",
        "counterfactuals.csv",
        "cohort_attribution.csv",
        "summary.json",
        "pricing_memo.md",
        "governance_checklist.md",
    ]
    for filename in expected:
        assert (run_dir / filename).exists(), f"Missing: {filename}"


def test_write_run_ledger_produces_checklist_json(
    tmp_path: Path, config, diagnostics
) -> None:
    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    assert (run_dir / "governance_checklist.json").exists()


def test_write_run_ledger_returns_path(tmp_path: Path, config, diagnostics) -> None:
    result = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    assert isinstance(result, Path)


def test_write_run_ledger_uses_single_timestamp_for_directory_and_metadata(
    tmp_path: Path, config, diagnostics, monkeypatch
) -> None:
    from datetime import timezone

    class FrozenDatetime:
        @classmethod
        def now(cls, tz=None) -> datetime:
            return datetime(2026, 5, 14, 1, 2, 3, 123456, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "balance_pricing_engine.run_ledger.writer.datetime",
        FrozenDatetime,
        raising=False,
    )

    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )

    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert run_dir.name.startswith("20260514_010203_123456_baseline")
    assert metadata["timestamp"] == "2026-05-14T01:02:03.123456+00:00"


def test_write_run_ledger_handles_diagnostics_without_cohort_context(
    tmp_path: Path, config, diagnostics
) -> None:
    sparse_diagnostics = replace(
        diagnostics,
        loan_customers=None,
        deposit_customers=None,
        market=None,
        loan_rate_elasticity=None,
        loan_competitor_gap_elasticity=None,
        deposit_rate_elasticity=None,
        deposit_competitor_gap_elasticity=None,
        response_coefficients=None,
    )

    run_dir = write_run_ledger(
        scenario_name="sparse",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=sparse_diagnostics,
        base_dir=tmp_path,
    )

    assert (run_dir / "summary.json").exists()
    assert not (run_dir / "cohort_attribution.csv").exists()
